/**
 * fastk chunk 引用：把 assistant 消息文本中的规范化引用标记
 * `[[chunk:<db>/<chunk_id>]]` 渲染为可点击徽章；点击后经平台代理取回该
 * chunk 的完整内容（含图片等富媒体）并以模态展示。
 *
 * 中文显示优化集中在查看器样式（chatStyles.chunkViewerBody）：中文字体栈、
 * 1.75 行高、两端对齐、strict 禁则（避免句号/逗号悬挂行首）。
 */

import { useEffect, useState, type ReactNode } from "react";
import { api, type FastkChunk } from "../api";
import { styles } from "./chatStyles";

/** 附图加载状态（每张独立，一张失败不影响其他）。 */
type ImgState = "idle" | "loading" | "ok" | "error";

/** 引用标记：`[[chunk:fastdb/fb62184133c0c818]]`。库名与 chunk_id 均为受限字符集。 */
const CHUNK_REF_RE = /\[\[chunk:([A-Za-z0-9_-]{1,64})\/([A-Za-z0-9_-]{1,64})\]\]/g;

type Segment = { kind: "text"; text: string } | { kind: "ref"; db: string; chunkId: string };

/** 把含引用标记的文本切分为纯文本段与引用段（供行内渲染）。 */
export function parseChunkRefs(text: string): Segment[] {
  const segments: Segment[] = [];
  let last = 0;
  for (const m of text.matchAll(CHUNK_REF_RE)) {
    const i = m.index ?? 0;
    if (i > last) segments.push({ kind: "text", text: text.slice(last, i) });
    segments.push({ kind: "ref", db: m[1], chunkId: m[2] });
    last = i + m[0].length;
  }
  if (last < text.length) segments.push({ kind: "text", text: text.slice(last) });
  return segments;
}

/** 单个 chunk 引用徽章（行内）。 */
function ChunkBadge({ db, chunkId, onOpen }: { db: string; chunkId: string; onOpen: () => void }) {
  return (
    <button
      type="button"
      style={styles.chunkBadge}
      title={`查看知识库引用 · ${db}/${chunkId}`}
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      <span aria-hidden>📚</span>
      <span>引用</span>
    </button>
  );
}

/** 单张附图的三态渲染（原位或末尾占位，互不阻断）。 */
function ChunkImage({
  url,
  state,
  alt,
}: {
  url: string | null;
  state: ImgState;
  alt: string;
}) {
  if (state === "loading")
    return <div style={styles.chunkImagePlaceholder}>正在加载附图…</div>;
  if (state === "error")
    return <div style={styles.chunkImageError}>附图加载失败，请关闭后重试</div>;
  if (state === "ok" && url)
    return <img src={url} alt={alt || "知识库附图"} style={styles.chunkImage} />;
  return null;
}

/** 正文图片占位符（整行）：`[[fastk-img:<n>]]`，n 为 chunk.images 的
 * chunk 内索引——导入侧把 `![alt](…)` 原位替换为该 token，正文与图片
 * 一一对应，杜绝正文文本误匹配。 */
const IMG_TOKEN_LINE_RE = /^\[\[fastk-img:(\d+)\]\]$/;
/** 行内占位符检测（非整行）。无 g 标志，避免 .test 的 lastIndex 陷阱。 */
const IMG_TOKEN_RE = /\[\[fastk-img:(\d+)\]\]/;
/** 行内拆分正则：捕获组保留占位符本身。 */
const IMG_TOKEN_SPLIT_RE = /(\[\[fastk-img:\d+\]\])/g;

/**
 * 正文与附图组合渲染。导入侧已把 `![alt](…)` 原位替换为显式占位符
 * `[[fastk-img:<n>]]`（n = chunk.images 索引），按占位符精确原位插图：
 * 独立成行的占位符整体替换，行内占位符拆段替换；越界/重复的占位符按
 * 普通文本保留，不吞内容。旧数据没有占位符，退回 "alt 独立行"（trim 后
 * 完全相等）匹配；两者都未命中的图按原顺序追加到正文末尾。chunkText 为
 * pre-wrap，按行累积时保留换行符即可还原排版。
 */
function renderTextWithImages(
  text: string,
  images: { url: string; alt: string }[],
  urls: (string | null)[],
  states: ImgState[]
): ReactNode[] {
  const nodes: ReactNode[] = [];
  const consumed = new Set<number>();
  let buffer = "";
  const flush = (key: string) => {
    if (buffer) {
      nodes.push(<span key={key}>{buffer}</span>);
      buffer = "";
    }
  };
  const emitImage = (n: number, key: string): boolean => {
    if (n < 0 || n >= images.length || consumed.has(n)) return false;
    flush(`t-${key}`);
    nodes.push(
      <ChunkImage
        key={`img-${key}-${n}`}
        url={urls[n] ?? null}
        state={states[n] ?? "idle"}
        alt={images[n].alt}
      />
    );
    consumed.add(n);
    return true;
  };

  const lines = text.split("\n");
  for (let li = 0; li < lines.length; li++) {
    const trimmed = lines[li].trim();
    const whole = trimmed.match(IMG_TOKEN_LINE_RE);
    if (whole && emitImage(+whole[1], `l${li}`)) {
      // 整行占位符：原位插图，命中行本身不再输出；保留换行。
      if (li < lines.length - 1) buffer += "\n";
      continue;
    }
    if (IMG_TOKEN_RE.test(trimmed)) {
      // 行内占位符：拆为 文本/图/文本 段，图后正文继续累积。
      const parts = lines[li].split(IMG_TOKEN_SPLIT_RE);
      for (let pi = 0; pi < parts.length; pi++) {
        const m = parts[pi].match(IMG_TOKEN_LINE_RE);
        if (m && emitImage(+m[1], `l${li}p${pi}`)) continue;
        buffer += parts[pi];
      }
      if (li < lines.length - 1) buffer += "\n";
      continue;
    }
    let hit = -1;
    if (trimmed) {
      for (let i = 0; i < images.length; i++) {
        if (
          !consumed.has(i) &&
          images[i].alt.trim() &&
          images[i].alt.trim() === trimmed
        ) {
          hit = i;
          break;
        }
      }
    }
    if (hit >= 0) {
      // 旧数据兜底：alt 独立行命中，先冲刷此前累积的正文再插图；命中行
      // 本身是图片的替代文本，不再重复输出。
      flush(`t${li}`);
      nodes.push(
        <ChunkImage
          key={`img-${hit}`}
          url={urls[hit] ?? null}
          state={states[hit] ?? "idle"}
          alt={images[hit].alt}
        />
      );
      consumed.add(hit);
      // 保留命中行后的换行，维持后续文本的相对位置。
      if (li < lines.length - 1) buffer += "\n";
    } else {
      buffer += lines[li] + (li < lines.length - 1 ? "\n" : "");
    }
  }
  flush("t-end");
  // 未能原位匹配的图，按原顺序追加到正文末尾。
  for (let i = 0; i < images.length; i++) {
    if (!consumed.has(i)) {
      nodes.push(
        <ChunkImage
          key={`img-${i}`}
          url={urls[i] ?? null}
          state={states[i] ?? "idle"}
          alt={images[i].alt}
        />
      );
    }
  }
  return nodes;
}

/** 引用内容查看器：模态展示 chunk 元数据、正文与附图（多图原位插入）。 */
function ChunkViewer({ db, chunkId, onClose }: { db: string; chunkId: string; onClose: () => void }) {
  const [chunk, setChunk] = useState<FastkChunk | null>(null);
  const [error, setError] = useState("");
  // 多图状态：与 chunk.images 对齐，逐张独立加载（<img> 无法带鉴权头）。
  const [imgUrls, setImgUrls] = useState<(string | null)[]>([]);
  const [imgStates, setImgStates] = useState<ImgState[]>([]);

  // 取 chunk 内容；随后对每张附图并行取 Blob（一张失败不影响其他）。
  useEffect(() => {
    let alive = true;
    const objectUrls: string[] = [];
    setChunk(null);
    setError("");
    setImgUrls([]);
    setImgStates([]);
    api
      .getFastkChunk(db, chunkId)
      .then(async (c) => {
        if (!alive) return;
        setChunk(c);
        const imgs = c.images ?? [];
        if (!imgs.length) return;
        setImgStates(imgs.map(() => "loading"));
        await Promise.all(
          imgs.map(async (_, i) => {
            try {
              const blob = await api.fetchFastkChunkImage(db, chunkId, i);
              const url = URL.createObjectURL(blob);
              objectUrls.push(url);
              if (alive) {
                setImgUrls((prev) => {
                  const n = [...prev];
                  n[i] = url;
                  return n;
                });
                setImgStates((prev) => {
                  const n = [...prev];
                  n[i] = "ok";
                  return n;
                });
              } else URL.revokeObjectURL(url);
            } catch {
              // 图片失败不阻断正文展示，但给出可见占位
              if (alive)
                setImgStates((prev) => {
                  const n = [...prev];
                  n[i] = "error";
                  return n;
                });
            }
          })
        );
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "加载失败");
      });
    return () => {
      alive = false;
      for (const u of objectUrls) URL.revokeObjectURL(u);
    };
  }, [db, chunkId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div style={styles.modal} onClick={onClose} role="dialog" aria-modal>
      <div style={styles.modalContent} onClick={(e) => e.stopPropagation()}>
        <div style={styles.modalHeader}>
          <span>📚 知识库引用</span>
          <button type="button" style={styles.modalClose} onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        <div style={styles.chunkViewerBody}>
          {error && !chunk && <div style={styles.chunkError}>{error}</div>}
          {!chunk && !error && <div style={styles.chunkLoading}>正在加载引用内容…</div>}
          {chunk && (
            <>
              <div style={styles.chunkMeta}>
                <div style={styles.chunkMetaRow}>
                  <span style={styles.chunkMetaLabel}>来源文件：</span>
                  <span style={styles.chunkMetaValue}>{chunk.path || "（无）"}</span>
                </div>
                <div style={styles.chunkMetaRow}>
                  <span style={styles.chunkMetaLabel}>所在章节：</span>
                  <span style={styles.chunkMetaValue}>{chunk.section || "（无）"}</span>
                </div>
                <div style={styles.chunkMetaRow}>
                  <span style={styles.chunkMetaLabel}>知识库：</span>
                  <span style={styles.chunkMetaValue}>
                    {db} · 第 {chunk.chunk_index + 1} 块
                  </span>
                </div>
              </div>
              <div style={styles.chunkText}>
                {renderTextWithImages(chunk.text, chunk.images ?? [], imgUrls, imgStates)}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * assistant 文本块渲染：解析 `[[chunk:...]]` 标记为行内徽章。同时管理
 * "当前打开的引用查看器"（同一时刻最多一个模态）。
 */
export function TextWithChunkRefs({ text, cursor }: { text: string; cursor?: boolean }) {
  const [openRef, setOpenRef] = useState<{ db: string; chunkId: string } | null>(null);
  const segments = parseChunkRefs(text);
  const hasRefs = segments.some((s) => s.kind === "ref");
  if (!hasRefs) {
    return (
      <div style={styles.msgText}>
        {text}
        {cursor && <span style={styles.cursor}>▊</span>}
      </div>
    );
  }
  return (
    <div style={styles.msgText}>
      {segments.map((seg, i) =>
        seg.kind === "text" ? (
          <span key={i}>{seg.text}</span>
        ) : (
          <ChunkBadge
            key={i}
            db={seg.db}
            chunkId={seg.chunkId}
            onOpen={() => setOpenRef({ db: seg.db, chunkId: seg.chunkId })}
          />
        )
      )}
      {cursor && <span style={styles.cursor}>▊</span>}
      {openRef && (
        <ChunkViewer
          db={openRef.db}
          chunkId={openRef.chunkId}
          onClose={() => setOpenRef(null)}
        />
      )}
    </div>
  );
}
