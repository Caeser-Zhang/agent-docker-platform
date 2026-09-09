/**
 * PPTX 模板库前端：画廊缩略图、composer 里的模板/风格选择器、管理员入库页。
 *
 * 存储模型决定了这里每条数据流的形状：模板字节躺在 named volume（agent-pptx-lib）
 * 上，后端 rw、所有用户容器 ro 挂载同一路径，所以 N 个用户只占 1 份空间（O(1)）。
 * 因此前端从不把模板下载进工作区，选择器只做两件事：
 *   1. 展示画廊 —— 缩略图走带鉴权的 blob 拉取（<img> 加不了 Authorization 头）；
 *   2. 把「容器内路径 + 版式/占位文案约束 + 调色板/配方 id」拼成 prompt 前缀，
 *      agent 在自己的容器里按该路径直接读取。
 *
 * 缩略图是两段式的：后端没有 LibreOffice / CJK 字体栈，渲不出 pptx，所以入库后
 * 先用「模板实际用色」色块占位（has_thumb=false）；管理员打开模板库页面时，浏览器
 * 会用 pptx-wasm 无头渲染首页 → PNG → 自动 PUT 回卷里（也可手动点「生成缩略图」
 * 重渲单份），之后所有用户看到的画廊都是真图。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type LibraryIngestResult,
  type LibraryPalette,
  type LibraryRecipe,
  type LibrarySeedResult,
  type LibraryStats,
  type LibraryStyles,
  type LibraryTemplateCard,
  type LibraryTemplateDetail,
} from "../api";
import { styles as cs } from "./chatStyles";
import { adminStyles as adm, adminCss } from "./adminStyles";

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------

export function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${i === 0 || v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

// 与 backend/app/services/pptx_library.py 的 MAX_UPLOAD_BYTES 保持一致。
// 选文件时就先否决超限的，避免用户等完整上传跑完才收到 413。
const MAX_IMPORT_BYTES = 25 * 1024 * 1024;

/** 展示名：中文优先，其次英文，最后退回 id（入库时 name 是可选的）。 */
export function templateLabel(card: LibraryTemplateCard): string {
  return card.name_zh || card.name || card.id;
}

// ---------------------------------------------------------------------------
// 无头缩略图渲染（pptx-wasm 框架无关 API，不需要挂载任何 React 组件）
// ---------------------------------------------------------------------------

/**
 * 把一份 pptx 的第 index 页渲成 PNG Blob。
 *
 * 用 `Presentation.open` + 离屏 canvas 而不是隐藏一个 `<PresentationViewer/>`
 * 再抓它的 canvas：后者要占 DOM、要等 Suspense，而且拿不到「资源到齐」的信号。
 * 首帧常常缺图（图片是异步解码的），所以 render 返回 complete=false 时，
 * 等 onAssetsReady 再渲一帧；4s 兜底防止个别资源永远不 ready 把按钮卡死。
 */
export async function renderSlideThumb(
  src: ArrayBuffer | Blob,
  width = 960,
  index = 0
): Promise<Blob> {
  const { Presentation } = await import("pptx-wasm");
  const pres = await Presentation.open(src);
  try {
    const info = pres.info;
    const height = Math.max(1, Math.round((info.height / info.width) * width));
    const canvas = document.createElement("canvas");
    const opts = { width, height, dpr: 1, fit: "contain" as const };
    let { complete } = await pres.render(index, canvas, opts);
    if (!complete) {
      await new Promise<void>((resolve) => {
        const off = pres.onAssetsReady(() => {
          off();
          resolve();
        });
        setTimeout(() => {
          off();
          resolve();
        }, 4000);
      });
      await pres.render(index, canvas, opts);
    }
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, "image/png")
    );
    if (!blob) throw new Error("canvas 导出 PNG 失败");
    return blob;
  } finally {
    pres.destroy();
  }
}

// ---------------------------------------------------------------------------
// 画廊缩略图（两段式）
// ---------------------------------------------------------------------------

/**
 * has_thumb=true → 拉 PNG blob 转 objectURL；否则用 palette_hint（入库时统计的
 * 实际用色 top5）拼色块占位，并注明「暂无预览」，管理员据此知道该去生成缩略图。
 */
export function LibraryThumb({
  card,
  variant = "chat",
  badge,
}: {
  card: LibraryTemplateCard;
  variant?: "chat" | "admin";
  badge?: string;
}) {
  const st = variant === "admin" ? adm : cs;
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!card.has_thumb) {
      setUrl(null);
      return;
    }
    let alive = true;
    let objUrl: string | null = null;
    // 管理页要能看到 sample 素材的预览，而用户侧路由对它们一律 404。
    const fetchThumb = variant === "admin" ? api.adminFetchLibraryThumb : api.fetchLibraryThumb;
    fetchThumb(card.id)
      .then((blob) => {
        if (!alive) return;
        // 后端 404 被 api 层吞成 null（例如刚被删掉），退回色块占位即可。
        if (!blob) return;
        objUrl = URL.createObjectURL(blob);
        setUrl(objUrl);
      })
      .catch(() => {
        /* 拉不到图不影响挑选，静默退化为色块 */
      });
    return () => {
      alive = false;
      if (objUrl) URL.revokeObjectURL(objUrl);
    };
  }, [card.id, card.has_thumb, variant]);

  const swatches = card.palette_hint?.length ? card.palette_hint.slice(0, 5) : ["#94a3b8"];
  const badgeStyle = variant === "admin" ? adm.libThumbBadge : cs.libBadge;

  return (
    <div style={st.libThumbBox}>
      {url ? (
        <img style={st.libThumbImg} src={url} alt={`${templateLabel(card)} 首页预览`} />
      ) : (
        <>
          <div style={st.libThumbSwatches}>
            {swatches.map((c, i) => (
              <span key={`${c}-${i}`} style={{ ...st.libSwatch, background: c }} />
            ))}
          </div>
          <div style={st.libThumbNote}>
            {card.has_thumb ? "预览加载中…" : "暂无预览 · 显示模板实际用色"}
          </div>
        </>
      )}
      {badge && <span style={badgeStyle}>{badge}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 数据加载（平台侧 API，不依赖用户容器是否在跑）
// ---------------------------------------------------------------------------

export interface LibraryCatalog {
  templates: LibraryTemplateCard[];
  styles: LibraryStyles | null;
  palettes: LibraryPalette[];
  recipes: LibraryRecipe[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}

/**
 * 一次取回模板列表 + 预定义风格。两者互相独立，用 allSettled：
 * 风格文件缺失时模板选择器仍然可用，反之亦然。
 */
export function useLibraryCatalog(): LibraryCatalog {
  const [templates, setTemplates] = useState<LibraryTemplateCard[]>([]);
  const [styles, setStyles] = useState<LibraryStyles | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    setLoading(true);
    Promise.allSettled([api.listLibraryTemplates(), api.getLibraryStyles()]).then(
      ([t, s]) => {
        const errs: string[] = [];
        if (t.status === "fulfilled") setTemplates(t.value.templates ?? []);
        else errs.push(`模板列表：${t.reason?.message ?? t.reason}`);
        if (s.status === "fulfilled") setStyles(s.value);
        else errs.push(`风格定义：${s.reason?.message ?? s.reason}`);
        // 只有一半失败时不打断使用，但要在菜单里说清楚。
        setError(errs.length === 2 ? errs.join("；") : errs[0] ?? null);
        setLoading(false);
      }
    );
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return {
    templates,
    styles,
    palettes: styles?.palettes ?? [],
    recipes: styles?.recipes ?? [],
    loading,
    error,
    reload,
  };
}

// ---------------------------------------------------------------------------
// prompt 注入
// ---------------------------------------------------------------------------

export interface LibrarySelection {
  template: LibraryTemplateCard | null;
  paletteId: string | null;
  recipeId: string | null;
}

export const emptyLibrarySelection: LibrarySelection = {
  template: null,
  paletteId: null,
  recipeId: null,
};

/**
 * 把选择结果拼成 prompt 前缀（与 skill 的「请使用 skill: …」同一范式：opencode
 * 没有模板字段，约束只能写进文本）。
 *
 * 重点是那条路径与「不要复制进工作区」：agent 拿到的是自己容器里的只读挂载点，
 * 复制一份就等于把 O(1) 共享存储退化成 O(N) 冗余 —— 这正是这个方案要避免的。
 */
export function buildLibraryPrompt(
  sel: LibrarySelection,
  lib: LibraryStyles | null
): string | null {
  const lines: string[] = [];
  const t = sel.template;
  if (t) {
    lines.push(`套用 PPT 模板「${templateLabel(t)}」`);
    lines.push(
      `模板文件在你容器内的只读路径：${t.path}。它是共享卷挂载，禁止复制、移动或改写该文件；` +
        `要产出成品请先把内容按模板结构生成到 /workspace 下的新 .pptx`
    );
    lines.push(
      `模板现状：${t.slides} 页 / ${t.aspect} 画幅 / ${t.layouts} 个版式` +
        (t.content_layout ? `；新增内容页沿用版式「${t.content_layout}」` : "")
    );
    if (t.must_replace.length) {
      lines.push(`必须替换掉的占位文案：${t.must_replace.join("；")}`);
    }
    const fonts = t.fonts?.used?.filter(Boolean) ?? [];
    if (fonts.length) lines.push(`沿用模板字体：${fonts.slice(0, 4).join("、")}`);
    if (!t.has_chart_part) lines.push("模板不含图表部件，需要图表时自行绘制原生形状，勿插入外链图片");
  }
  const palette = lib?.palettes.find((p) => p.id === sel.paletteId);
  if (palette) {
    lines.push(
      `配色用预定义调色板「${palette.name_zh}」(${palette.id})：${palette.colors.join(" ")}。` +
        `${palette.style}${palette.dark_mode_required ? "（必须深色底）" : ""}。${palette.tips}`
    );
  }
  const recipe = lib?.recipes.find((r) => r.id === sel.recipeId);
  if (recipe) {
    lines.push(
      `风格配方「${recipe.name_zh}」(${recipe.id})：${recipe.character}。` +
        `圆角 / 间距 / 组件规格按容器内 ${lib?.dir ?? "/library/pptx/styles"} 下的 JSON 取值`
    );
  }
  if (!lines.length) return null;
  return `PPTX 制作约束：\n${lines.map((l) => `- ${l}`).join("\n")}`;
}

// ---------------------------------------------------------------------------
// composer 选择器（模板 / 风格）
// ---------------------------------------------------------------------------

/** 模板画廊弹层：单选，再点一次取消。 */
export function TemplatePickerMenu({
  catalog,
  selectedId,
  onPick,
  onClose,
}: {
  catalog: LibraryCatalog;
  selectedId: string | null;
  onPick: (card: LibraryTemplateCard | null) => void;
  onClose: () => void;
}) {
  const { templates, loading, error, reload } = catalog;
  return (
    <div style={cs.libMenu}>
      <div style={cs.libMenuHead}>
        <span style={cs.libMenuTitle}>选择 PPT 模板</span>
        <span style={cs.libMenuHint}>
          模板存在共享卷上、以只读方式挂载进你的容器；选中后只把容器内路径写进指令，字节不会复制进工作区。
        </span>
      </div>
      {error && (
        <div style={cs.libMenuError}>
          {error}{" "}
          <button
            style={{ ...cs.chipRemove, color: "inherit", textDecoration: "underline" }}
            onClick={reload}
          >
            重试
          </button>
        </div>
      )}
      {loading && <div style={cs.libMenuEmpty}>加载中…</div>}
      {!loading && templates.length === 0 && !error && (
        <div style={cs.libMenuEmpty}>模板库为空 —— 请管理员在「模板库」页导入</div>
      )}
      {templates.length > 0 && (
        <div style={cs.libGrid}>
          {templates.map((card) => {
            const active = card.id === selectedId;
            return (
              <button
                key={card.id}
                style={{ ...cs.libCard, ...(active ? cs.libCardActive : {}) }}
                title={`容器内路径：${card.path}`}
                onClick={() => onPick(active ? null : card)}
              >
                <LibraryThumb card={card} badge={`${card.slides} 页`} />
                <div style={cs.libCardBody}>
                  <div style={cs.libCardName}>{templateLabel(card)}</div>
                  {card.description && <div style={cs.libCardDesc}>{card.description}</div>}
                  <div style={cs.libCardMeta}>
                    <span>{card.aspect}</span>
                    <span>{fmtBytes(card.size_bytes)}</span>
                    {card.tags.slice(0, 2).map((tag) => (
                      <span key={tag} style={cs.libTag}>
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      )}
      <button style={cs.libMenuClose} onClick={onClose}>
        {selectedId ? "完成" : "不使用模板"}
      </button>
    </div>
  );
}

/** 风格弹层：调色板与配方各一组，都可独立选中/取消。 */
export function StylePickerMenu({
  catalog,
  paletteId,
  recipeId,
  onPickPalette,
  onPickRecipe,
  onClose,
}: {
  catalog: LibraryCatalog;
  paletteId: string | null;
  recipeId: string | null;
  onPickPalette: (id: string | null) => void;
  onPickRecipe: (id: string | null) => void;
  onClose: () => void;
}) {
  const { palettes, recipes, loading, error, reload } = catalog;
  return (
    <div style={cs.libMenu}>
      <div style={cs.libMenuHead}>
        <span style={cs.libMenuTitle}>选择预定义风格</span>
        <span style={cs.libMenuHint}>
          调色板与配方与容器内 {catalog.styles?.dir ?? "/library/pptx/styles"} 的 JSON 同源；选中后把 id 写进指令，agent 本地读取取值。
        </span>
      </div>
      {error && (
        <div style={cs.libMenuError}>
          {error}{" "}
          <button
            style={{ ...cs.chipRemove, color: "inherit", textDecoration: "underline" }}
            onClick={reload}
          >
            重试
          </button>
        </div>
      )}
      {loading && <div style={cs.libMenuEmpty}>加载中…</div>}
      {!loading && palettes.length === 0 && recipes.length === 0 && !error && (
        <div style={cs.libMenuEmpty}>没有可用的风格定义</div>
      )}

      {palettes.length > 0 && (
        <>
          <div style={cs.libSectionTitle}>调色板 · {palettes.length}</div>
          <div style={cs.libGrid}>
            {palettes.map((p) => {
              const active = p.id === paletteId;
              return (
                <button
                  key={p.id}
                  style={{ ...cs.libMenuItem, ...(active ? cs.libMenuItemActive : {}) }}
                  title={`${p.use_cases.join("、")}${p.tips ? `\n${p.tips}` : ""}`}
                  onClick={() => onPickPalette(active ? null : p.id)}
                >
                  <span style={cs.libMenuName}>
                    {p.name_zh}
                    {p.dark_mode_required ? " · 深色" : ""}
                  </span>
                  <span style={cs.libPaletteColors}>
                    {p.colors.map((c, i) => (
                      <span key={`${c}-${i}`} style={{ ...cs.libPaletteDot, background: c }} />
                    ))}
                  </span>
                  <span style={cs.libMenuSub}>
                    {p.style} · {p.id}
                  </span>
                </button>
              );
            })}
          </div>
        </>
      )}

      {recipes.length > 0 && (
        <>
          <div style={cs.libSectionTitle}>风格配方 · {recipes.length}</div>
          <div style={cs.libGrid}>
            {recipes.map((r: LibraryRecipe) => {
              const active = r.id === recipeId;
              return (
                <button
                  key={r.id}
                  style={{ ...cs.libMenuItem, ...(active ? cs.libMenuItemActive : {}) }}
                  title={r.best_for}
                  onClick={() => onPickRecipe(active ? null : r.id)}
                >
                  <span style={cs.libMenuName}>{r.name_zh}</span>
                  <span style={cs.libMenuSub}>{r.character}</span>
                  <span style={cs.libMenuSub}>适用：{r.best_for}</span>
                </button>
              );
            })}
          </div>
        </>
      )}

      <button style={cs.libMenuClose} onClick={onClose}>
        {paletteId || recipeId ? "完成" : "不指定风格"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 管理员页
// ---------------------------------------------------------------------------

const SOURCES = [
  { value: "upload", label: "upload · 运营上传" },
  { value: "seed", label: "seed · 仓库种子" },
  { value: "sample", label: "sample · 开发期样例（普通用户不可见）" },
];

interface FormState {
  file: File | null;
  name: string;
  nameZh: string;
  description: string;
  tags: string;
  palette: string;
  recipe: string;
  license: string;
  source: string;
  enabled: boolean;
  optimizeImages: boolean;
  dropPromo: boolean;
  mustReplace: string;
}

const emptyForm: FormState = {
  file: null,
  name: "",
  nameZh: "",
  description: "",
  tags: "",
  palette: "",
  recipe: "",
  license: "internal",
  source: "upload",
  enabled: true,
  optimizeImages: true,
  dropPromo: true,
  mustReplace: "",
};

/** 逗号 / 换行都当分隔符，容忍粘贴带来的空格。 */
function splitList(raw: string): string[] {
  return raw
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);
}

export function LibraryAdminPage({
  username,
  onLogout,
  onExit,
}: {
  username: string;
  onLogout: () => void;
  onExit: () => void;
}) {
  const [stats, setStats] = useState<LibraryStats | null>(null);
  const [templates, setTemplates] = useState<LibraryTemplateCard[]>([]);
  const [catalog, setCatalog] = useState<LibraryStyles | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "enabled" | "disabled">("all");

  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [dragOver, setDragOver] = useState(false);
  const [result, setResult] = useState<LibraryIngestResult | null>(null);
  const [seedResult, setSeedResult] = useState<LibrarySeedResult | null>(null);
  const [detail, setDetail] = useState<LibraryTemplateDetail | null>(null);
  const [thumbPreview, setThumbPreview] = useState<{ id: string; url: string } | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const toastTimer = useRef<number | null>(null);

  // 自动补图状态：进度条文案 + 本次会话已试过的模板 id。
  const [autoThumb, setAutoThumb] = useState<{ done: number; total: number; name: string } | null>(
    null
  );
  const autoThumbTried = useRef<Set<string>>(new Set());
  const autoThumbRunning = useRef(false);
  const autoThumbCancel = useRef(false);

  const flash = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3200);
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    const [st, ts, lib] = await Promise.allSettled([
      api.adminLibraryStats(),
      api.adminListLibraryTemplates(),
      api.getLibraryStyles(),
    ]);
    const errs: string[] = [];
    if (st.status === "fulfilled") setStats(st.value);
    else errs.push(`统计：${st.reason?.message ?? st.reason}`);
    if (ts.status === "fulfilled") setTemplates(ts.value.templates ?? []);
    else errs.push(`列表：${ts.reason?.message ?? ts.reason}`);
    if (lib.status === "fulfilled") setCatalog(lib.value);
    setError(errs.length ? errs.join("；") : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // objectURL 的释放统一交给这个 effect：thumbPreview 换值或卸载时回收上一张。
  useEffect(
    () => () => {
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
      if (thumbPreview) URL.revokeObjectURL(thumbPreview.url);
    },
    [thumbPreview]
  );

  /**
   * 自动补图：后端没有 LibreOffice / CJK 字体栈，渲不出 pptx，所以首页预览只能
   * 在浏览器这边出。管理页一打开就把还没有缩略图的模板依次渲一遍回传，写进共享
   * 卷之后所有用户看到的都是真图，不必管理员逐份点按钮。
   *
   * - 串行：渲染是 wasm + canvas 的重活，同时开多个 Presentation 会压垮标签页。
   * - tried 集合按会话记忆：失败的也不无限重试，留给手动「生成缩略图」兜底。
   * - 只在卸载时取消：循环内部会 setTemplates，若把取消挂在 templates 依赖上，
   *   每渲完一张都会把自己打断。
   */
  useEffect(() => {
    if (autoThumbRunning.current) return;
    const pending = templates.filter(
      (t) => !t.has_thumb && !autoThumbTried.current.has(t.id)
    );
    if (!pending.length) return;

    autoThumbRunning.current = true;
    const failed: string[] = [];

    void (async () => {
      for (let i = 0; i < pending.length; i++) {
        const card = pending[i];
        autoThumbTried.current.add(card.id);
        if (autoThumbCancel.current) break;
        setAutoThumb({ done: i, total: pending.length, name: templateLabel(card) });
        try {
          const bytes = await api.adminFetchLibraryFile(card.id);
          const blob = await renderSlideThumb(bytes);
          await api.adminUploadLibraryThumb(card.id, blob);
          setTemplates((prev) =>
            prev.map((t) => (t.id === card.id ? { ...t, has_thumb: true } : t))
          );
        } catch {
          failed.push(templateLabel(card));
        }
      }
      setAutoThumb(null);
      autoThumbRunning.current = false;
      if (autoThumbCancel.current) return;
      // 重新拉一次让 stats.with_thumb 与列表保持一致。
      await loadAll();
      if (failed.length) {
        flash(
          `${failed.length} 份模板自动出图失败：${failed.join("、")}` +
            `（可手动点「生成缩略图」重试）`
        );
      } else {
        flash(`已自动补全 ${pending.length} 份模板的首页预览`);
      }
    })();
  }, [templates, flash, loadAll]);

  // 卸载即停：组件没了就别再往卷里写。
  useEffect(() => () => {
    autoThumbCancel.current = true;
  }, []);

  const setField = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const acceptFile = (file: File | undefined | null) => {
    if (!file) return;
    if (!/\.pptx$/i.test(file.name)) {
      flash("只接受 .pptx（老式 .ppt 请先另存为 .pptx）");
      return;
    }
    if (file.size > MAX_IMPORT_BYTES) {
      flash(
        `文件 ${fmtBytes(file.size)} 超过 ${MAX_IMPORT_BYTES / 1024 / 1024}MB 入库上限，` +
          `请先压缩媒体或拆分模板`
      );
      return;
    }
    setForm((f) => ({
      ...f,
      file,
      // 文件名常常就是模板名，先填上让管理员少打字。
      nameZh: f.nameZh || file.name.replace(/\.pptx$/i, ""),
    }));
    setResult(null);
  };

  const handleIngest = async () => {
    if (!form.file) {
      flash("请先选择 .pptx 文件");
      return;
    }
    setBusy("ingest");
    try {
      const res = await api.adminIngestLibraryTemplate({
        file: form.file,
        name: form.name.trim() || undefined,
        nameZh: form.nameZh.trim() || undefined,
        description: form.description.trim() || undefined,
        tags: splitList(form.tags),
        palette: form.palette || undefined,
        recipe: form.recipe || undefined,
        license: form.license.trim() || undefined,
        source: form.source,
        enabled: form.enabled,
        optimizeImages: form.optimizeImages,
        dropPromo: form.dropPromo,
        mustReplace: splitList(form.mustReplace),
      });
      setResult(res);
      setForm(emptyForm);
      flash(
        res.created
          ? `已入库 ${res.template.id}（${fmtBytes(res.report.output_bytes)}，省 ${res.report.saved_percent}%）`
          : `相同内容已在库中（${res.template.id}），本次为 no-op`
      );
      await loadAll();
    } catch (e: any) {
      flash(`入库失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  const handleToggle = async (card: LibraryTemplateCard) => {
    setBusy(`toggle:${card.id}`);
    try {
      await api.adminUpdateLibraryTemplate(card.id, { enabled: !card.enabled });
      setTemplates((prev) =>
        prev.map((t) => (t.id === card.id ? { ...t, enabled: !t.enabled } : t))
      );
      flash(`${templateLabel(card)} 已${card.enabled ? "下架" : "上架"}`);
    } catch (e: any) {
      flash(`操作失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async (card: LibraryTemplateCard) => {
    if (!window.confirm(`删除模板「${templateLabel(card)}」？卷内的文件与缩略图一并移除。`)) return;
    setBusy(`del:${card.id}`);
    try {
      await api.adminDeleteLibraryTemplate(card.id);
      setTemplates((prev) => prev.filter((t) => t.id !== card.id));
      if (detail?.id === card.id) setDetail(null);
      flash("已删除");
      await loadAll();
    } catch (e: any) {
      flash(`删除失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  /** 取卷内规范化后的字节 → 浏览器渲染首页 → 回传 PNG。 */
  const handleThumb = async (card: LibraryTemplateCard) => {
    setBusy(`thumb:${card.id}`);
    try {
      const bytes = await api.adminFetchLibraryFile(card.id);
      const blob = await renderSlideThumb(bytes);
      setThumbPreview({ id: card.id, url: URL.createObjectURL(blob) });
      await api.adminUploadLibraryThumb(card.id, blob);
      setTemplates((prev) =>
        prev.map((t) => (t.id === card.id ? { ...t, has_thumb: true } : t))
      );
      flash(`${templateLabel(card)} 缩略图已生成（${fmtBytes(blob.size)}）`);
      await loadAll();
    } catch (e: any) {
      flash(`生成缩略图失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  const handleSeed = async () => {
    setBusy("seed");
    try {
      const res = await api.adminSeedLibrary(false);
      setSeedResult(res);
      flash(`种子入库：扫描 ${res.scanned} · 新增 ${res.ingested} · 已存在 ${res.skipped_existing}`);
      await loadAll();
    } catch (e: any) {
      flash(`种子入库失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  const openDetail = async (card: LibraryTemplateCard) => {
    setBusy(`detail:${card.id}`);
    try {
      setDetail(await api.adminGetLibraryTemplate(card.id));
    } catch (e: any) {
      flash(`读取详情失败：${e.message}`);
    } finally {
      setBusy(null);
    }
  };

  const visible = templates.filter((t) =>
    filter === "all" ? true : filter === "enabled" ? t.enabled : !t.enabled
  );
  const palettes = catalog?.palettes ?? [];
  const recipes = catalog?.recipes ?? [];
  const report = result?.report;

  return (
    <div style={adm.container}>
      <style>{adminCss}</style>

      {/* --- Header ------------------------------------------------------- */}
      <div style={adm.header}>
        <div style={adm.headerLeft}>
          <span style={adm.headerIcon}>
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="3" y="4" width="18" height="13" rx="2" />
              <path d="M8 21h8M12 17v4" />
            </svg>
          </span>
          <div>
            <div style={adm.headerTitle}>PPTX 模板库</div>
            <div style={adm.headerSubtitle}>
              共享卷单副本 · 用户容器只读挂载 · 管理员专属
            </div>
          </div>
        </div>
        <div style={adm.headerRight}>
          <span style={adm.whoami}>
            {username} · <span style={{ color: "var(--amber)" }}>admin</span>
          </span>
          <button className="adm-btn adm-btn-primary" style={adm.btnPrimary} onClick={onExit}>
            返回聊天
          </button>
          <button className="adm-btn" style={adm.btn} onClick={onLogout}>
            退出
          </button>
        </div>
      </div>

      {/* --- Body --------------------------------------------------------- */}
      <div style={adm.body} className="adm-scroll">
        {error && <div style={adm.errorBanner}>加载失败：{error}</div>}

        {stats && (
          <div style={adm.cardsRow}>
            <div className="adm-card" style={adm.card}>
              <div style={adm.cardLabel}>模板</div>
              <div style={adm.cardValue}>{stats.templates}</div>
              <div style={adm.cardSub}>已上架 {stats.enabled} · 有缩略图 {stats.with_thumb}</div>
            </div>
            <div className="adm-card" style={adm.card}>
              <div style={adm.cardLabel}>物理副本</div>
              <div style={adm.cardValueGreen}>{stats.copies}</div>
              <div style={adm.cardSub}>所有容器共享同一份只读挂载，不随用户数增长</div>
            </div>
            <div className="adm-card" style={adm.card}>
              <div style={adm.cardLabel}>卷内占用</div>
              <div style={adm.cardValue}>{fmtBytes(stats.total_bytes)}</div>
              <div style={adm.cardSub}>规范化省下 {fmtBytes(stats.saved_bytes)}</div>
            </div>
            <div className="adm-card" style={adm.card}>
              <div style={adm.cardLabel}>来源分布</div>
              <div style={{ ...adm.cardValue, fontSize: "16px" }}>
                {Object.entries(stats.by_source)
                  .map(([k, v]) => `${k} ${v}`)
                  .join(" · ") || "—"}
              </div>
              <div style={adm.cardSub} title={stats.root}>
                {stats.root}
              </div>
            </div>
          </div>
        )}

        {/* Toolbar */}
        <div style={adm.toolbar}>
          <button
            className="adm-btn adm-btn-primary"
            style={adm.btnPrimary}
            onClick={() => setFormOpen((v) => !v)}
          >
            {formOpen ? "收起导入表单" : "导入模板"}
          </button>
          <button
            className="adm-btn"
            style={adm.btn}
            onClick={handleSeed}
            disabled={busy !== null}
            title="重跑仓库种子目录的增量入库（按源文件 sha256 去重，可反复调用）"
          >
            {busy === "seed" ? "入库中…" : "重跑种子入库"}
          </button>
          <button
            className="adm-btn"
            style={adm.btn}
            onClick={loadAll}
            disabled={loading || busy !== null}
          >
            刷新
          </button>
          <select
            className="adm-select"
            style={adm.select}
            value={filter}
            onChange={(e) => setFilter(e.target.value as typeof filter)}
          >
            <option value="all">全部（{templates.length}）</option>
            <option value="enabled">已上架</option>
            <option value="disabled">已下架</option>
          </select>
          <span style={adm.toolbarHint}>
            缩略图由浏览器渲染：后端无 pptx 渲染栈，生成一次即存进卷里供所有人复用
          </span>
        </div>

        {/* --- 导入表单 --------------------------------------------------- */}
        {formOpen && (
          <div className="adm-card" style={{ ...adm.card, padding: "14px 16px" }}>
            <div
              className={`adm-drop${dragOver ? " adm-drop-over" : ""}`}
              style={adm.libDrop}
              onClick={() => fileRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragOver(false);
                acceptFile(e.dataTransfer.files?.[0]);
              }}
            >
              {form.file ? (
                <>
                  <strong>{form.file.name}</strong> · {fmtBytes(form.file.size)}
                  <br />
                  <span style={adm.libHint}>点击或拖入可替换</span>
                </>
              ) : (
                <>
                  点击选择或拖入 .pptx
                  <br />
                  <span style={adm.libHint}>
                    入库会跑规范化流水线：剥离推广页 → 清孤儿 media → 图片激进瘦身 → 品牌/厂商清洗 →
                    残留文本扫描 → 结构校验 → 原子落盘（上限 25MB）
                  </span>
                </>
              )}
              <input
                ref={fileRef}
                type="file"
                accept=".pptx"
                hidden
                onChange={(e) => {
                  acceptFile(e.target.files?.[0]);
                  e.target.value = "";
                }}
              />
            </div>

            <div style={{ ...adm.libFormGrid, marginTop: "12px" }}>
              <label style={adm.libField}>
                <span style={adm.libLabel}>中文名</span>
                <input
                  className="adm-input"
                  style={adm.input}
                  value={form.nameZh}
                  onChange={(e) => setField("nameZh", e.target.value)}
                  placeholder="蓝色简约风通用分析与方案"
                />
              </label>
              <label style={adm.libField}>
                <span style={adm.libLabel}>英文名 / slug</span>
                <input
                  className="adm-input"
                  style={adm.input}
                  value={form.name}
                  onChange={(e) => setField("name", e.target.value)}
                  placeholder="blue-minimal-analysis"
                />
              </label>
              <label style={adm.libField}>
                <span style={adm.libLabel}>标签（逗号分隔）</span>
                <input
                  className="adm-input"
                  style={adm.input}
                  value={form.tags}
                  onChange={(e) => setField("tags", e.target.value)}
                  placeholder="方案汇报, 通用, 16:9"
                />
              </label>
              <label style={adm.libField}>
                <span style={adm.libLabel}>绑定调色板</span>
                <select
                  className="adm-select"
                  style={{ ...adm.select, width: "100%" }}
                  value={form.palette}
                  onChange={(e) => setField("palette", e.target.value)}
                >
                  <option value="">不绑定</option>
                  {palettes.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name_zh} · {p.id}
                    </option>
                  ))}
                </select>
              </label>
              <label style={adm.libField}>
                <span style={adm.libLabel}>绑定风格配方</span>
                <select
                  className="adm-select"
                  style={{ ...adm.select, width: "100%" }}
                  value={form.recipe}
                  onChange={(e) => setField("recipe", e.target.value)}
                >
                  <option value="">不绑定</option>
                  {recipes.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name_zh} · {r.id}
                    </option>
                  ))}
                </select>
              </label>
              <label style={adm.libField}>
                <span style={adm.libLabel}>来源</span>
                <select
                  className="adm-select"
                  style={{ ...adm.select, width: "100%" }}
                  value={form.source}
                  onChange={(e) => setField("source", e.target.value)}
                >
                  {SOURCES.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </label>
              <label style={adm.libField}>
                <span style={adm.libLabel}>授权</span>
                <input
                  className="adm-input"
                  style={adm.input}
                  value={form.license}
                  onChange={(e) => setField("license", e.target.value)}
                  placeholder="internal"
                />
              </label>
              <label style={{ ...adm.libField, gridColumn: "1 / -1" }}>
                <span style={adm.libLabel}>描述</span>
                <textarea
                  className="adm-input adm-textarea"
                  style={adm.textarea}
                  value={form.description}
                  onChange={(e) => setField("description", e.target.value)}
                  placeholder="适用场景、页型构成、注意事项 —— 会显示在用户的选择器里"
                />
              </label>
              <label style={{ ...adm.libField, gridColumn: "1 / -1" }}>
                <span style={adm.libLabel}>必须替换的占位文案（每行一条）</span>
                <textarea
                  className="adm-input adm-textarea"
                  style={adm.textarea}
                  value={form.mustReplace}
                  onChange={(e) => setField("mustReplace", e.target.value)}
                  placeholder={"XX公司\n2024年度"}
                />
                <span style={adm.libHint}>
                  会随模板卡片一起注入 prompt，agent 套用时必须逐条替换
                </span>
              </label>
            </div>

            <div style={{ display: "flex", gap: "16px", flexWrap: "wrap", marginTop: "10px" }}>
              <label style={adm.checkboxLabel}>
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => setField("enabled", e.target.checked)}
                />
                立即上架
              </label>
              <label style={adm.checkboxLabel}>
                <input
                  type="checkbox"
                  checked={form.optimizeImages}
                  onChange={(e) => setField("optimizeImages", e.target.checked)}
                />
                图片激进瘦身
              </label>
              <label style={adm.checkboxLabel}>
                <input
                  type="checkbox"
                  checked={form.dropPromo}
                  onChange={(e) => setField("dropPromo", e.target.checked)}
                />
                剥离推广/说明页
              </label>
              <button
                className="adm-btn adm-btn-primary"
                style={{ ...adm.btnPrimary, marginLeft: "auto" }}
                onClick={handleIngest}
                disabled={busy !== null || !form.file}
              >
                {busy === "ingest" ? "规范化入库中…" : "入库"}
              </button>
            </div>
          </div>
        )}

        {/* --- 规范化报告 ------------------------------------------------- */}
        {report && result && (
          <div style={{ marginTop: "12px" }}>
            <div style={adm.cardLabel}>
              规范化报告 · {result.template.id}
              {result.created ? "" : "（内容已存在，未新增副本）"}
            </div>
            <div style={adm.libReportBox}>
              <div>
                体积 {fmtBytes(report.input_bytes)} → {fmtBytes(report.output_bytes)}（省{" "}
                {report.saved_percent}%） · 页数 {report.slides_in} → {report.slides_out}
              </div>
              {report.dropped_slides.length > 0 && (
                <div>
                  剥离页：
                  {report.dropped_slides.map((d) => `#${d.index + 1}(${d.matched_strong.join("/") || d.matched_weak.join("/") || "弱匹配"})`).join("、")}
                </div>
              )}
              {report.images_slimmed.length > 0 && (
                <div>
                  瘦身图片 {report.images_slimmed.length} 张，共省{" "}
                  {fmtBytes(report.images_slimmed.reduce((a, b) => a + b.saved_bytes, 0))}
                  <div style={adm.libListRow}>
                    {report.images_slimmed.map((im) => (
                      <span key={im.new_part}>
                        {im.new_part} {im.action} {fmtBytes(im.before_bytes)}→
                        {fmtBytes(im.after_bytes)}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {report.orphan_media_removed.length > 0 && (
                <div>清孤儿 media {report.orphan_media_removed.length} 个</div>
              )}
              {(report.vendor_tags_removed.length > 0 || report.branding_scrubbed.length > 0) && (
                <div>
                  厂商痕迹清除 {report.vendor_tags_removed.length} 处 · 品牌清洗{" "}
                  {report.branding_scrubbed.length} 处
                  <div style={adm.libListRow}>
                    {[...report.vendor_tags_removed, ...report.branding_scrubbed]
                      .slice(0, 12)
                      .map((x, i) => (
                        <span key={`${x}-${i}`}>{x}</span>
                      ))}
                  </div>
                </div>
              )}
              {report.residual_texts.length > 0 && (
                <div style={{ color: "var(--amber)" }}>
                  残留可疑文案 {report.residual_texts.length} 条（建议补进「必须替换」清单）：
                  <div style={adm.libListRow}>
                    {report.residual_texts.slice(0, 15).map((r, i) => (
                      <span key={`${r.part}-${i}`}>
                        [{r.pattern}] {r.text.slice(0, 40)}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {report.warnings.length > 0 && (
                <div style={{ color: "var(--red)" }}>
                  校验告警：
                  <div style={adm.libListRow}>
                    {report.warnings.map((w, i) => (
                      <span key={`${w}-${i}`}>{w}</span>
                    ))}
                  </div>
                </div>
              )}
              {!report.warnings.length && !report.residual_texts.length && (
                <div style={{ color: "var(--green)" }}>结构校验通过，无残留可疑文案</div>
              )}
            </div>
          </div>
        )}

        {seedResult && (
          <div style={{ marginTop: "12px" }}>
            <div style={adm.cardLabel}>种子入库结果</div>
            <div style={adm.libReportBox}>
              扫描 {seedResult.scanned} · 新增 {seedResult.ingested} · 已存在{" "}
              {seedResult.skipped_existing} · 跳过样例 {seedResult.samples_skipped}
              {seedResult.failed.length > 0 && (
                <div style={{ color: "var(--red)" }}>
                  失败：
                  <div style={adm.libListRow}>
                    {seedResult.failed.map((f) => (
                      <span key={f.file}>
                        {f.file}: {f.error}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {thumbPreview && (
          <div style={{ marginTop: "12px" }}>
            <div style={adm.cardLabel}>
              刚生成的首页预览 · {thumbPreview.id}
              <button
                className="adm-btn"
                style={{ ...adm.btnSmall, marginLeft: "10px" }}
                onClick={() => setThumbPreview(null)}
              >
                关闭
              </button>
            </div>
            <div style={adm.libPreview}>
              <img
                src={thumbPreview.url}
                alt="首页预览"
                style={{ width: "100%", height: "100%", objectFit: "contain" }}
              />
            </div>
          </div>
        )}

        {/* --- 模板卡片 --------------------------------------------------- */}
        {loading && <div style={adm.empty}>加载中…</div>}
        {!loading && visible.length === 0 && (
          <div style={adm.empty}>
            {templates.length === 0
              ? "模板库为空 —— 用上方「导入模板」上传，或「重跑种子入库」拉取仓库种子"
              : "当前筛选条件下没有模板"}
          </div>
        )}
        <div style={adm.libGrid}>
          {visible.map((card) => (
            <div key={card.id} className="adm-lib-card" style={adm.libCard}>
              <LibraryThumb
                card={card}
                variant="admin"
                badge={`${card.source ?? "?"} · ${card.slides} 页`}
              />
              <div style={adm.libCardBody}>
                <div style={adm.libCardName}>{templateLabel(card)}</div>
                <div style={adm.libCardId}>{card.id}</div>
                {card.description && <div style={adm.libCardDesc}>{card.description}</div>}
                <div style={adm.libCardMeta}>
                  <span style={card.enabled ? adm.badgeGreen : adm.badgeGray}>
                    {card.enabled ? "已上架" : "已下架"}
                  </span>
                  <span>{card.aspect}</span>
                  <span>{fmtBytes(card.size_bytes)}</span>
                  <span>{card.layouts} 版式</span>
                  {card.palette && <span>色板 {card.palette}</span>}
                  {card.recipe && <span>配方 {card.recipe}</span>}
                </div>
                <div style={adm.libCardMeta}>
                  <span style={adm.mono}>{card.path}</span>
                </div>
                <div style={adm.libCardActions}>
                  <button
                    className="adm-btn"
                    style={adm.btnSmall}
                    onClick={() => openDetail(card)}
                    disabled={busy !== null}
                  >
                    详情
                  </button>
                  <button
                    className="adm-btn"
                    style={adm.btnSmall}
                    onClick={() => handleToggle(card)}
                    disabled={busy !== null}
                  >
                    {busy === `toggle:${card.id}` ? "…" : card.enabled ? "下架" : "上架"}
                  </button>
                  <button
                    className="adm-btn"
                    style={adm.btnSmall}
                    onClick={() => handleThumb(card)}
                    disabled={busy !== null}
                    title="浏览器端用 pptx-wasm 渲染首页并回传"
                  >
                    {busy === `thumb:${card.id}`
                      ? "渲染中…"
                      : card.has_thumb
                        ? "重生成缩略图"
                        : "生成缩略图"}
                  </button>
                  <button
                    className="adm-btn adm-btn-danger"
                    style={adm.btnSmallDanger}
                    onClick={() => handleDelete(card)}
                    disabled={busy !== null}
                  >
                    删除
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* --- 详情弹层 ----------------------------------------------------- */}
      {detail && (
        <div style={adm.overlay} onClick={() => setDetail(null)}>
          <div style={adm.modal} onClick={(e) => e.stopPropagation()}>
            <div style={adm.modalHeader}>
              <div style={adm.modalTitle}>
                {templateLabel(detail)} · <span style={adm.mono}>{detail.id}</span>
              </div>
              <button className="adm-btn" style={adm.btnSmall} onClick={() => setDetail(null)}>
                关闭
              </button>
            </div>
            <div style={adm.modalBody} className="adm-scroll">
              <div style={adm.libReportBox}>
                <div>
                  源 sha256 <span style={adm.mono}>{detail.source_sha256}</span>
                </div>
                <div>
                  规范化：{detail.normalize.slides_in} → {detail.normalize.slides_out} 页 ·{" "}
                  {fmtBytes(detail.normalize.input_bytes)} →{" "}
                  {fmtBytes(detail.normalize.output_bytes)}（省{" "}
                  {fmtBytes(detail.normalize.saved_bytes)}）· 剥离{" "}
                  {detail.normalize.dropped_slides} 页 · 清孤儿 media{" "}
                  {detail.normalize.orphan_media_removed} · 瘦身图片{" "}
                  {detail.normalize.images_slimmed} 张
                </div>
                <div>
                  画幅 {detail.aspect}
                  {detail.slide_size_inches
                    ? ` (${detail.slide_size_inches[0]}×${detail.slide_size_inches[1]} in)`
                    : ""}{" "}
                  · media {detail.media_count} 个 · 动画页 {detail.animated_slides} ·{" "}
                  {detail.has_chart_part ? "含图表部件" : "无图表部件"}
                </div>
                {detail.content_layout && (
                  <div>
                    内容页版式：{detail.content_layout}
                    {detail.content_layout_note ? ` · ${detail.content_layout_note}` : ""}
                  </div>
                )}
                {detail.embedded_fonts.length > 0 && (
                  <div>嵌入字体：{detail.embedded_fonts.join("、")}</div>
                )}
                {detail.fonts && (
                  <div>
                    主题字体：
                    {Object.entries(detail.fonts.theme)
                      .filter(([, v]) => v)
                      .map(([k, v]) => `${k}=${v}`)
                      .join("、") || "—"}
                    {detail.fonts.used.length ? ` · 实际使用 ${detail.fonts.used.join("、")}` : ""}
                  </div>
                )}
                {detail.must_replace.length > 0 && (
                  <div>必须替换：{detail.must_replace.join("；")}</div>
                )}
                {detail.license && <div>授权：{detail.license}</div>}
                {detail.warnings.length > 0 && (
                  <div style={{ color: "var(--red)" }}>告警：{detail.warnings.join("；")}</div>
                )}
                {detail.residual_texts.length > 0 && (
                  <div style={{ color: "var(--amber)" }}>
                    残留可疑文案 {detail.residual_texts.length} 条：
                    <div style={adm.libListRow}>
                      {detail.residual_texts.map((r, i) => (
                        <span key={`${r.part}-${i}`}>
                          [{r.pattern}] {r.text.slice(0, 60)}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              <div style={{ ...adm.cardLabel, marginTop: "12px" }}>
                页型分布（{detail.page_types.length} 页）
              </div>
              <div style={adm.libReportBox}>
                {Object.entries(detail.page_type_summary ?? {})
                  .map(([k, v]) => `${k}×${v}`)
                  .join(" · ") || "—"}
                <div style={{ ...adm.libListRow, marginTop: "6px" }}>
                  {detail.page_types.map((p) => (
                    <span key={`${p.part}-${p.index}`}>
                      #{p.index + 1} {p.type}
                      {p.layout ? `/${p.layout}` : ""} {p.title.slice(0, 18) || "(无标题)"}{" "}
                      {p.text_chars}字 {p.images}图
                    </span>
                  ))}
                </div>
              </div>
            </div>
            <div style={adm.modalFooter}>
              <span style={adm.libHint}>
                容器内只读路径 {detail.path} —— 用户选中后注入 prompt，字节不进工作区
              </span>
              <button
                className="adm-btn"
                style={adm.btnSmall}
                onClick={() => handleThumb(detail)}
                disabled={busy !== null}
              >
                {busy === `thumb:${detail.id}` ? "渲染中…" : "生成缩略图"}
              </button>
              <button
                className="adm-btn"
                style={adm.btnSmall}
                onClick={() => handleToggle(detail)}
                disabled={busy !== null}
              >
                {detail.enabled ? "下架" : "上架"}
              </button>
            </div>
          </div>
        </div>
      )}

      {autoThumb && (
        <div style={adm.libReportBox}>
          正在浏览器端渲染首页预览 {autoThumb.done + 1}/{autoThumb.total}：{autoThumb.name}…
          （渲完自动写回共享卷，之后用户侧画廊即为真图）
        </div>
      )}

      {toast && (
        <div className="adm-toast" style={adm.toast}>
          {toast}
        </div>
      )}
    </div>
  );
}
