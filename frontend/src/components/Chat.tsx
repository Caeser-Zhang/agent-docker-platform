import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { PresentationViewerHandle } from "pptx-wasm/react";
import { App as AntdApp, Button, Input, Modal, Radio, Tag, Tooltip } from "antd";
import {
  AppstoreAddOutlined,
  CloseOutlined,
  CodeOutlined,
  DeleteOutlined,
  DeploymentUnitOutlined,
  DownOutlined,
  EditOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  InboxOutlined,
  LoadingOutlined,
  MessageOutlined,
  PaperClipOutlined,
  PictureOutlined,
  PlusOutlined,
  ProfileOutlined,
  ReloadOutlined,
  RightOutlined,
  SendOutlined,
  StopOutlined,
  ThunderboltFilled,
} from "@ant-design/icons";
import {
  api,
  type AgentRuntime,
  type AgentStatus,
  type FeedbackReasonCode,
  type FeedbackSubmit,
  type KbDomainEntry,
  type LibraryTemplateCard,
  type ModelRef,
  type OcAgent,
  type OcCommand,
  type OcPermissionReply,
  type OcPermissionRequest,
  type OcQuestionRequest,
  type OcSession,
  type ProjectInfo,
  type ProvidersResponse,
} from "../api";
import { parseTodos, reduceEvent, toTurns, type Block, type FileDiff, type TodoItem, type Turn } from "../oc/messages";
import { buildTurnContext } from "../oc/feedback";
import { styles } from "./chatStyles";
import { ConfigPanel } from "./ConfigPanel";
import { FeedbackBar } from "./FeedbackBar";
import { FeedbackModal } from "./FeedbackModal";
import { OpinionFeedbackModal } from "./OpinionFeedbackModal";
import { TextWithChunkRefs } from "./ChunkRef";
import { ThemeToggle } from "../theme";
import {
  buildLibraryPrompt,
  templateLabel,
  useLibraryCatalog,
  StylePickerMenu,
  TemplatePickerMenu,
} from "./PptxLibrary";

// 知识领域卡片的局部样式（chatStyles 未覆盖领域分组维度，这里补三个小样式，
// 颜色跟随 chatStyles 的暗色主题变量风格）。
const kbDomainStyles = {
  cardTitle: { display: "flex", alignItems: "center", gap: "6px", fontSize: "12px", fontWeight: 600, color: "#e6edf3" },
  typeBadge: (isPublic: boolean): CSSProperties => ({
    fontSize: "10px",
    fontWeight: 500,
    lineHeight: "16px",
    padding: "0 6px",
    borderRadius: "8px",
    border: `1px solid ${isPublic ? "rgba(63,185,80,0.4)" : "rgba(210,153,34,0.4)"}`,
    color: isPublic ? "#3fb950" : "#d29922",
    background: isPublic ? "rgba(63,185,80,0.1)" : "rgba(210,153,34,0.1)",
  }),
  dbRow: { marginTop: "6px", paddingLeft: "8px", borderLeft: "2px solid rgba(255,255,255,0.08)" },
};

// ---------------------------------------------------------------------------
// 快捷技能（一键启用）
//
// opencode 的 prompt_async 没有 skill / 模板字段，约束只能写进文本前缀
// （见 PptxLibrary.buildLibraryPrompt 的同款注释）。所以「技能参数」的提交方式
// 是拼前缀，界面则通过下面的解析器把前缀还原成标签，参数收进 Tooltip。
// ---------------------------------------------------------------------------

/** 流程图方言 → 实际启用的 builtin skill 名。 */
export type FlowDialect = "mermaid" | "plantuml";
const FLOW_SKILL: Record<FlowDialect, string> = {
  mermaid: "pretty-mermaid",
  plantuml: "plantuml",
};
const PPT_SKILL = "pptx-generator";
/** 图标按钮「已选中」态的强调色（primary 变量，双主题自适应）。 */
const accentIcon = "var(--primary)";

/** skill 名 → 界面展示标签。多个 skill 可映射到同一个标签。 */
const SKILL_TAG_LABEL: Record<string, string> = {
  [PPT_SKILL]: "PPT生成",
  "pretty-mermaid": "流程图生成",
  plantuml: "流程图生成",
};

export interface SkillTag {
  label: string;
  /** 隐藏在标签内部的提交数据，仅 Tooltip 展示。 */
  detail: string;
}

const CONSTRAINT_HEADS = ["PPTX 制作约束：", "流程图生成约束：", "PPTX 制作约束", "流程图生成约束"];

/**
 * 把 prompt 文本拆成「技能标签 + 用户真实输入」。
 *
 * 识别两类我们自己生成的块：
 *   1. `请使用 skill: a, b`
 *   2. 以 `PPTX 制作约束：` / `流程图生成约束：` 开头、直到空行结束的 `- 条目` 列表
 * 命中即从展示文本里剥离，块内条目转为标签的 detail。
 */
export function splitSkillMarks(text: string): { tags: SkillTag[]; rest: string } {
  if (!text) return { tags: [], rest: "" };
  const lines = text.split("\n");
  const tags: SkillTag[] = [];
  const kept: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    const skillHit = /^请使用 skill[:：]\s*(.+)$/.exec(line);
    if (skillHit) {
      for (const name of skillHit[1].split(/[,，]/).map((s) => s.trim()).filter(Boolean)) {
        const label = SKILL_TAG_LABEL[name] || name;
        if (!tags.some((t) => t.label === label)) tags.push({ label, detail: name });
      }
      continue;
    }
    if (CONSTRAINT_HEADS.includes(line)) {
      const detail: string[] = [];
      let j = i + 1;
      for (; j < lines.length && lines[j].trim() !== ""; j++) {
        const item = lines[j].trim();
        // 块内条目是「模板名 / 配色 / 图表类型」等参数，收进标签的 Tooltip。
        if (item.startsWith("- ")) detail.push(item.slice(2).trim());
      }
      i = j - 1;
      if (!detail.length) continue;
      const label = line.startsWith("PPTX") ? "PPT生成" : "流程图生成";
      const existing = tags.find((t) => t.label === label);
      if (existing) existing.detail = `${existing.detail}\n${detail.join("\n")}`;
      else tags.push({ label, detail: detail.join("\n") });
      continue;
    }
    kept.push(lines[i]);
  }
  if (tags.length === 0) return { tags, rest: text };
  let rest = kept.join("\n").replace(/^\n+/, "");
  // 前缀与正文之间以空行分隔；剥离后正文尾部不应再留下技能块造成的空白。
  rest = rest.replace(/\n{3,}/g, "\n\n").trim();
  return { tags, rest: rest || text };
}

/** 流程图技能的 prompt 约束块（与 PPTX 制作约束同一范式）。 */
function buildFlowPrompt(dialect: FlowDialect): string {
  if (dialect === "mermaid") {
    return [
      "流程图生成约束：",
      "- 图表类型：Mermaid。请使用 pretty-mermaid skill 生成，产出美化后的 Mermaid 源码",
      "- 落盘为 /workspace 下的 .mmd 文件，同时把源码贴在回答里便于预览",
      "- 语法保持标准 Mermaid，不要引入 HTML 或外部图片引用",
    ].join("\n");
  }
  return [
    "流程图生成约束：",
    "- 图表类型：PlantUML。请使用 plantuml skill 生成",
    "- 落盘为 /workspace 下的 .puml 源文件，同时把源码贴在回答里便于预览",
    "- 平台内不渲染图片，只交付 .puml 源码，不要尝试调用本地渲染器",
  ].join("\n");
}

/** "provider/model" <-> ModelRef, the format opencode uses in config.model. */
function parseModel(value: string | null | undefined): ModelRef | undefined {
  if (!value) return undefined;
  const i = value.indexOf("/");
  if (i <= 0) return undefined;
  return { providerID: value.slice(0, i), id: value.slice(i + 1) };
}
const modelKey = (m?: ModelRef) => (m ? `${m.providerID}/${m.id}` : "");

/** All "@path" references in the text (start of line or after whitespace). */
const collectAtTokens = (text: string): string[] => {
  const out = new Set<string>();
  for (const m of text.matchAll(/(?:^|\s)@([^\s@]+)/g)) out.add(m[1]);
  return [...out];
};

/**
 * Best-effort mime for a workspace file referenced via @mention. opencode only
 * handles image/* attachments natively; everything else must be sent as
 * text/plain so it goes through the Read tool instead of a media part that
 * OpenAI-Chat providers reject. (SVG stays text/plain — it is textual.)
 */
const IMAGE_MIME_BY_EXT: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
};
const mimeForPath = (p: string): string => {
  const ext = p.includes(".") ? p.split(".").pop()!.toLowerCase() : "";
  return IMAGE_MIME_BY_EXT[ext] ?? "text/plain";
};

// ---------------------------------------------------------------------------
//  present_file 交付预览信令
//
//  Agent 侧 present_file 工具（容器内置插件）执行成功后，其 tool result 的
//  output 是一段 JSON payload，随 message.part.updated 经 SSE 到达这里。我们
//  不新建事件通道、也不重复造渲染器 —— 直接把 payload.path 交给已有的文件
//  预览管线（openPreview → FilesPanel），它已覆盖 html/markdown/图片/文本，
//  且 pptx 走 pptx-wasm 高保真渲染（PptxSlidePreview）。这一层只负责：
//  去重（同 path 原位刷新）、关闭记忆（dismissed 后不再自动弹）、自动弹出
//  开关（autoPresent）、多标签切换。
// ---------------------------------------------------------------------------

/** 交付预览的一个标签（id 即 workspace 相对 path，天然去重）。 */
type PresentTab = {
  path: string;
  title: string;
  kind: string;
  note: string | null;
  /** 同 path 重复展示时自增，用作渲染器 key 强制刷新。 */
  version: number;
};

/** present_file tool result output 解析后的载荷（只取前端需要的字段）。 */
type PresentPayload = {
  path: string;
  title: string;
  kind: string;
  note: string | null;
  focus: boolean;
  mode: string;
};

/** 标签上限，超出按 LRU（数组头部最旧）淘汰。 */
const PRESENT_TAB_MAX = 8;
const AUTO_PRESENT_KEY = "ui.autoPresent";
const DISMISSED_PRESENT_KEY = "ui.dismissedPresent";

/**
 * 安全解析 present_file 的 output JSON。只认 ok===true 且带 path 的成功信令；
 * 失败返回（ok:false / 限流 / 非法 JSON）一律 null，不打扰用户。
 */
function parsePresentOutput(output: unknown): PresentPayload | null {
  if (typeof output !== "string" || !output) return null;
  let p: any;
  try {
    p = JSON.parse(output);
  } catch {
    return null;
  }
  if (!p || p.ok !== true || typeof p.path !== "string" || !p.path) return null;
  const base = p.path.split("/").pop() || p.path;
  return {
    path: p.path,
    title: typeof p.title === "string" && p.title.trim() ? p.title : base,
    kind: typeof p.kind === "string" ? p.kind : "unknown",
    note: typeof p.note === "string" ? p.note : null,
    focus: p.focus !== false,
    mode: typeof p.mode === "string" ? p.mode : "auto",
  };
}

/**
 * 从历史 turns 里回收所有成功的 present_file 信令（会话恢复用）。按出现顺序
 * 去重，保留最后一次出现的 title/kind/note。不自动弹面板，仅重建标签列表。
 */
function extractPresentFromTurns(turns: Turn[]): PresentTab[] {
  const byPath = new Map<string, PresentTab>();
  for (const t of turns) {
    for (const b of t.blocks) {
      if (b.kind !== "tool" || b.name !== "present_file" || b.status !== "completed") continue;
      const p = parsePresentOutput(b.output);
      if (!p) continue;
      byPath.set(p.path, { path: p.path, title: p.title, kind: p.kind, note: p.note, version: 1 });
    }
  }
  return [...byPath.values()].slice(-PRESENT_TAB_MAX);
}


/**
 * Slash menu entry kinds: real opencode commands, the client-side "/agents"
 * pseudo command, and the agent entries it expands into.
 */
type SlashOption =
  | { kind: "command"; name: string; description?: string; source?: string }
  | { kind: "agentsCmd"; name: "agents"; description: string }
  | { kind: "agent"; name: string; description?: string };

/**
 * @-menu entries: workspace files plus — when the orchestrator is selected —
 * the subagents it manages (sent as prompt.agents mentions).
 */
type AtOption =
  | { kind: "file"; path: string }
  | { kind: "agent"; name: string; description?: string };

// ---------------------------------------------------------------------------
//  可拖拽侧栏（会话侧栏 / 工作区文件面板共用）
// ---------------------------------------------------------------------------

/** 侧栏宽度范围与默认值（px）。 */
const SIDEBAR_WIDTH = { min: 200, max: 520, def: 300, key: "ui.sidebarW" };
const FILES_WIDTH = { min: 240, max: 680, def: 320, key: "ui.filesW" };

const clampWidth = (w: number, r: { min: number; max: number }) =>
  Math.min(r.max, Math.max(r.min, Math.round(w)));

/** 从 localStorage 恢复宽度，缺失 / 非法时回退默认。 */
const loadWidth = (r: { min: number; max: number; def: number; key: string }) => {
  const n = Number(localStorage.getItem(r.key));
  return clampWidth(Number.isFinite(n) && n > 0 ? n : r.def, r);
};

/**
 * 侧栏边缘拖拽调宽手柄：flex 行里的一条 6px 竖线。Pointer Events +
 * setPointerCapture 让指针越出手柄（甚至越过 iframe）也持续跟手；拖动期间
 * 全局禁用文本选择，避免把旁边内容选中。双击手柄恢复默认宽度。
 */
function PanelResizer({
  onDelta, onReset, title,
}: {
  /** 本次指针水平位移（向右为正）；父组件决定加到还是减去宽度。 */
  onDelta: (dx: number) => void;
  onReset: () => void;
  title?: string;
}) {
  const lastXRef = useRef(0);
  // draggingRef 是逻辑开关（pointerdown 与首次 pointermove 之间 React 状态
  // 可能尚未提交，用 ref 才不会漏掉第一段位移）；dragging 只管高亮样式。
  const draggingRef = useRef(false);
  const [dragging, setDragging] = useState(false);

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    lastXRef.current = e.clientX;
    draggingRef.current = true;
    setDragging(true);
    // 合成 PointerEvent（自动化测试等）没有活动指针 id，setPointerCapture
    // 会抛 NotFoundError——捕获后拖拽逻辑继续可用（真实指针不受影响）。
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* 无活动指针 */ }
    document.body.style.userSelect = "none"; // 拖动期间禁选文本
  };
  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return;
    onDelta(e.clientX - lastXRef.current);
    lastXRef.current = e.clientX;
  };
  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    setDragging(false);
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* 已释放 */ }
    document.body.style.userSelect = "";
  };

  return (
    <div
      className={`panel-resizer${dragging ? " dragging" : ""}`}
      role="separator"
      aria-orientation="vertical"
      title={title}
      style={styles.panelDragHandle}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={onReset}
    />
  );
}

export function Chat({
  username,
  role,
  onOpenAdmin,
  onOpenLibrary,
  onOpenKbAccess,
  onOpenWishes,
  onLogout,
}: {
  username: string;
  role?: string;
  onOpenAdmin?: () => void;
  onOpenLibrary?: () => void;
  onOpenKbAccess?: () => void;
  /** 心愿墙（所有登录用户可访问，非管理员页面） */
  onOpenWishes?: () => void;
  onLogout: () => void;
}) {
  // antd App 上下文：confirm 模态继承 ConfigProvider 的明暗主题与主色。
  const { modal: modalApi } = AntdApp.useApp();
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null);
  const [runtime, setRuntime] = useState<AgentRuntime | null>(null);
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [model, setModel] = useState<ModelRef | undefined>(undefined);

  const [sessions, setSessions] = useState<OcSession[]>([]);
  const [currentSession, setCurrentSession] = useState<OcSession | null>(null);

  // 项目空间：workspace 子目录 = 会话工作目录。会话归属靠
  // session.location.directory 目录推导（平台只存项目清单，无映射表），
  // 因此分组在渲染时派生，SSE / 会话刷新逻辑不用感知项目。
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [expandedProjects, setExpandedProjects] = useState<Record<string, boolean>>(() => {
    try {
      return JSON.parse(window.localStorage.getItem("oc.expandedProjects") ?? "{}");
    } catch {
      return {};
    }
  });
  useEffect(() => {
    window.localStorage.setItem("oc.expandedProjects", JSON.stringify(expandedProjects));
  }, [expandedProjects]);
  // 新建项目弹窗：名称 + 模式（新建目录 / 绑定 workspace 已有目录）
  const [showProjectModal, setShowProjectModal] = useState(false);
  const [projName, setProjName] = useState("");
  const [projMode, setProjMode] = useState<"create" | "bind">("create");
  const [projDirs, setProjDirs] = useState<string[]>([]);
  const [projDir, setProjDir] = useState("");
  const [projBusy, setProjBusy] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [promptAgent, setPromptAgent] = useState<string | undefined>(undefined);
  const [promptModel, setPromptModel] = useState<ModelRef | undefined>(undefined);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState("");
  const [logs, setLogs] = useState("");
  const [busy, setBusy] = useState<string>("");
  // True while a background container start is in flight (page warmup or the
  // start button). POST /agent/start returns immediately; this drives the
  // status polling + phase UI until the phase settles on running/failed.
  const [starting, setStarting] = useState(false);
  // P1-4: SSE connection health — the browser's EventSource reconnects on its
  // own; while it is down the banner warns that replies may lag.
  const [sseDown, setSseDown] = useState(false);
  const [showConfig, setShowConfig] = useState(false);
  // 意见反馈弹窗（F2/F3）：两态状态机在组件内部，这里只控开关
  const [showOpinion, setShowOpinion] = useState(false);

  // P1-1: revert/unrevert of the last round's file changes, the agent's live
  // task list (todo.updated SSE events), and a second busy label for actions
  // that must not run concurrently (fork / summarize).
  const [revertedId, setRevertedId] = useState<string | null>(null);
  const [revertBusy, setRevertBusy] = useState(false);
  const [todos, setTodos] = useState<TodoItem[]>([]);
  const [busyLabel, setBusyLabel] = useState("");

  // 任务一：点赞/点踩反馈。messageId → verdict（锁定态）；busyId 为提交中的
  // 消息（乐观锁定）；downTargetId 为正在填写点踩原因的 assistant 消息。
  const [feedbackMap, setFeedbackMap] = useState<Record<string, "up" | "down">>({});
  const [feedbackBusyId, setFeedbackBusyId] = useState<string | null>(null);
  const [downTargetId, setDownTargetId] = useState<string | null>(null);

  // opencode agent presets + pending approval requests.
  const [agents, setAgents] = useState<OcAgent[]>([]);
  const [agentId, setAgentId] = useState("build");
  const [permissions, setPermissions] = useState<OcPermissionRequest[]>([]);
  const [questions, setQuestions] = useState<OcQuestionRequest[]>([]);

  // 知识领域展示区：领域名 + 领域描述 + 其下知识库列表。数据由后端
  // /api/kb/my-domains 提供——后端按用户权限（公共隐式放行 / 私有名册）
  // 聚合领域及其下 fastk 知识库，前端不做任何鉴权，只负责展示。
  const [kbDomains, setKbDomains] = useState<KbDomainEntry[]>([]);

  // Chat attach: skill picker + file uploads.
  const [allSkills, setAllSkills] = useState<{ name: string; description: string; dir: string; scope: string }[]>([]);
  const [skillMenuOpen, setSkillMenuOpen] = useState(false);
  // composer 底栏的 Agent / 模型 chip 弹出菜单
  const [agentMenuOpen, setAgentMenuOpen] = useState(false);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [attachments, setAttachments] = useState<{ filename: string; path: string; mime: string; isImage: boolean; size: number; dataUrl?: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // PPTX 模板库：目录走平台侧只读 API（不需要容器在跑），选中项跨消息保持，
  // 直到用户显式取消——因为一份 deck 往往要分好几轮消息迭代。
  const libCatalog = useLibraryCatalog();
  const [libMenu, setLibMenu] = useState<"template" | "style" | null>(null);
  const [selTemplate, setSelTemplate] = useState<LibraryTemplateCard | null>(null);
  const [selPaletteId, setSelPaletteId] = useState<string | null>(null);
  const [selRecipeId, setSelRecipeId] = useState<string | null>(null);
  const selPalette = libCatalog.styles?.palettes.find((p) => p.id === selPaletteId) ?? null;
  const selRecipe = libCatalog.recipes.find((r) => r.id === selRecipeId) ?? null;

  // 快捷技能一键启用：PPT 复用上面的模板库选择，流程图只需选方言。
  const [pptOn, setPptOn] = useState(false);
  const [flowOn, setFlowOn] = useState(false);
  const [flowDialect, setFlowDialect] = useState<FlowDialect>("mermaid");
  const [pptModalOpen, setPptModalOpen] = useState(false);
  const [flowModalOpen, setFlowModalOpen] = useState(false);
  // 标签 Tooltip 里的参数摘要（界面上不直接展开，属于「隐藏在标签内部」的提交数据）。
  const pptDetail = (() => {
    const parts: string[] = [];
    if (selTemplate) parts.push(`模板：${templateLabel(selTemplate)}`);
    if (selPalette) parts.push(`配色：${selPalette.name_zh || selPalette.name}`);
    if (selRecipe) parts.push(`版式：${selRecipe.name_zh || selRecipe.name}`);
    return parts.length ? parts.join("\n") : "模板 / 风格：不指定";
  })();
  const flowDetail = `图表类型：${flowDialect === "mermaid" ? "Mermaid" : "PlantUML"}`;

  // 会话重命名：受控 antd Modal + Input 替代 window.prompt（样式随主题）。
  const [renameTarget, setRenameTarget] = useState<OcSession | null>(null);
  const [renameValue, setRenameValue] = useState("");

  // @-mention autocomplete: activated while typing "@query" in the textarea.
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const [atQuery, setAtQuery] = useState<string | null>(null);
  const [atOptions, setAtOptions] = useState<AtOption[]>([]);
  const [atIndex, setAtIndex] = useState(0);
  const atTimerRef = useRef<number | null>(null);

  // Slash command menu: "/" at the very start of the input opens the command
  // picker; typing "/agents" fully swaps it to an agent picker (client-side
  // pseudo command — the opencode server has no /agents command).
  const [commands, setCommands] = useState<OcCommand[]>([]);
  const [slashQuery, setSlashQuery] = useState<string | null>(null);
  const [slashIndex, setSlashIndex] = useState(0);
  const commandsLoadedRef = useRef(false);

  // Workspace file browser panel.
  const [showFiles, setShowFiles] = useState(false);

  // 可拖拽侧栏宽度（左：会话侧栏；右：工作区文件面板）。持久化到
  // localStorage，刷新后保持；拖拽/双击手柄由 PanelResizer 触发。
  const [sidebarW, setSidebarW] = useState(() => loadWidth(SIDEBAR_WIDTH));
  const [filesW, setFilesW] = useState(() => loadWidth(FILES_WIDTH));
  useEffect(() => { localStorage.setItem(SIDEBAR_WIDTH.key, String(sidebarW)); }, [sidebarW]);
  useEffect(() => { localStorage.setItem(FILES_WIDTH.key, String(filesW)); }, [filesW]);
  const [wsFiles, setWsFiles] = useState<{ path: string; type: "file" | "dir"; size: number }[]>([]);
  const [preview, setPreview] = useState<{ path: string; type: string; mime: string; content?: string; base64?: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  // present_file 交付预览调度状态（见文件顶部注释）。tabs 按 path 去重、
  // version 驱动刷新；autoPresent / dismissed 持久化到 localStorage。
  const [presentTabs, setPresentTabs] = useState<PresentTab[]>([]);
  const [activePresent, setActivePresent] = useState<string | null>(null);
  const [autoPresent, setAutoPresent] = useState<boolean>(() => {
    const v = localStorage.getItem(AUTO_PRESENT_KEY);
    return v === null ? true : v === "1";
  });
  const [dismissedPresent, setDismissedPresent] = useState<Record<string, number>>(() => {
    try {
      const raw = localStorage.getItem(DISMISSED_PRESENT_KEY);
      return raw ? (JSON.parse(raw) as Record<string, number>) : {};
    } catch {
      return {};
    }
  });
  useEffect(() => { localStorage.setItem(AUTO_PRESENT_KEY, autoPresent ? "1" : "0"); }, [autoPresent]);
  useEffect(() => {
    localStorage.setItem(DISMISSED_PRESENT_KEY, JSON.stringify(dismissedPresent));
  }, [dismissedPresent]);
  // SSE / 加载器闭包读取最新的 autoPresent / dismissed，避免因依赖变化反复重挂事件。
  const autoPresentRef = useRef(autoPresent);
  const dismissedPresentRef = useRef(dismissedPresent);
  useEffect(() => { autoPresentRef.current = autoPresent; }, [autoPresent]);
  useEffect(() => { dismissedPresentRef.current = dismissedPresent; }, [dismissedPresent]);
  // present 处理器通过 ref 暴露给只注册一次的 SSE 回调，规避 useCallback 自引用环。
  const presentHandlerRef = useRef<(p: PresentPayload, auto: boolean) => void>(() => {});


  // Workspace panel uploads: files land in the container workspace (tmp/) and
  // the tree is refreshed afterwards. `wsNotice` is a short-lived success hint.
  const [wsUploading, setWsUploading] = useState(false);
  const [wsDownloading, setWsDownloading] = useState(false);
  const [wsDeleting, setWsDeleting] = useState(false);
  const [wsNotice, setWsNotice] = useState("");
  // 平台托管路径前缀（随 /workspace/files 一起下发），删除时命中需二次确认。
  const [wsProtected, setWsProtected] = useState<string[]>([]);
  const wsNoticeTimerRef = useRef<number | null>(null);

  const esRef = useRef<EventSource | null>(null);
  // P1-5: one-time SSE tickets mean the browser can't auto-reconnect; we
  // drive reconnection with this timer (fresh ticket each attempt).
  const sseReconnectRef = useRef<number | null>(null);
  // Retry indirection: SSE error handlers reach the current connectSSE
  // through this ref, so the callbacks don't form a self-referential
  // useCallback cycle.
  const connectSSERef = useRef<() => void>(() => {});
  const lastEventIdRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);
  // The SSE callback is registered once; it reads the live session id from here.
  const sessionIdRef = useRef<string | null>(null);
  // Container's current boot time (epoch ms), mirrored from agentStatus into a
  // ref so message loaders (refreshSessionMessages / selectSession) can do
  // dead-turn reconciliation without depending on the agentStatus state.
  const agentStartedAtRef = useRef<number | undefined>(undefined);
  // Timestamp of the last received `message.*` SSE event, and a debounce timer
  // for message refetches. The platform's SSE pump can drop events while the
  // agent is streaming (opencode severs the upstream connection every ~1s),
  // so the UI re-pulls GET /session/{id}/message as a fallback: when a
  // message completes, and on a slow poll while isGenerating.
  const lastMsgEvtRef = useRef(0);
  const refreshDebounceRef = useRef<number | null>(null);
  // P1-4: one-shot session restore across page refreshes (localStorage).
  const restoredRef = useRef(false);
  // P1-4: browser-side fallback for "how long has the start been running"
  // (the backend's phase_since is preferred whenever it is present).
  const startupAtRef = useRef<number | null>(null);

  // Readiness = container running AND the controller marked it "running"
  // (which only happens after its own health probe + session warmup).
  // Deliberately NOT gated on `healthy`: the image's HEALTHCHECK has a 45s
  // start-period, so `healthy` stays false for a while even when opencode
  // is already serving — gating on it would leave the UI stuck on
  // "starting" right after a successful boot.
  const isAgentRunning = Boolean(agentStatus?.running && agentStatus?.status === "running");

  // Live phase label while a background start is in flight.
  const startupPhaseLabel =
    agentStatus?.status === "creating"
      ? "正在创建容器…"
      : agentStatus?.status === "starting"
      ? "正在启动 opencode 服务…"
      : agentStatus?.status === "warming"
      ? "正在预热模型会话…"
      : "启动中…";

  // Mirror the container's boot time into the ref used by message loaders.
  useEffect(() => {
    agentStartedAtRef.current = agentStatus?.started_at ? agentStatus.started_at * 1000 : undefined;
  }, [agentStatus?.started_at]);

  // ------------------------------------------------------------------
  //  Control plane
  // ------------------------------------------------------------------
  const refreshStatus = useCallback(async () => {
    try {
      setAgentStatus(await api.getAgentStatus());
    } catch (e) {
      console.error("status check failed", e);
    }
  }, []);

  /** Pull pending permission/question requests (GET /api/{permission,question}/request). */
  const refreshPending = useCallback(async () => {
    const [ps, qs] = await Promise.allSettled([
      api.listPermissionRequests(),
      api.listQuestionRequests(),
    ]);
    if (ps.status === "fulfilled") setPermissions(ps.value);
    if (qs.status === "fulfilled") setQuestions(qs.value);
  }, []);

  // 知识领域：登录后即可加载，与容器是否运行无关。后端已按用户权限过滤，
  // 失败时静默保留空列表（展示区显示"暂无"），不打断主流程。
  const loadKbDomains = useCallback(async () => {
    const r = await api.myKbDomains().catch(() => null);
    if (r) setKbDomains(r.domains ?? []);
  }, []);

  useEffect(() => {
    loadKbDomains();
  }, [loadKbDomains]);

  // Skill 列表：优先平台侧 /workspace/skills/all（后端已合并 global/project/
  // builtin 插件 skill，scope 由后端直接标注），失败时回退 opencode 原生
  // GET /skill（经隧道，前端按 location 启发式归类）。
  // allSkills 状态 shape 保持 {name, description, dir, scope}。
  const loadSkills = useCallback(async () => {
    const merged = await api.listAllSkills().catch(() => null);
    if (merged) {
      setAllSkills(merged.skills ?? []);
      return;
    }
    const native = await api.listNativeSkills().catch(() => null);
    if (native) {
      setAllSkills(
        native.map((s) => ({
          name: s.name,
          description: s.description ?? "",
          dir: s.location,
          // 位置启发式：容器 XDG 配置目录→global；插件包内（只读镜像）→
          // builtin；其余（workspace 下）为项目级。
          scope: s.location.includes("/data/config/opencode")
            ? "global"
            : s.location.includes("/opt/agent/builtin-plugins")
              ? "builtin"
              : "project",
        }))
      );
    }
    /* 两个接口都失败时保留现有列表 */
  }, []);

  const loadContainerState = useCallback(async () => {
    // Everything below is served by opencode inside the container.
    loadSkills();
    const [rt, prov, sess, ags, proj] = await Promise.allSettled([
      api.getAgentRuntime(),
      api.getProviders(),
      api.listSessions(),
      api.listAgents(),
      api.listProjects(),
    ]);
    if (rt.status === "fulfilled") setRuntime(rt.value);
    if (prov.status === "fulfilled") {
      setProviders(prov.value);
      setModel((cur) => cur ?? parseModel(prov.value.default));
      if (prov.value.error) setError(prov.value.error);
    }
    if (sess.status === "fulfilled") {
      setSessions(
        sess.value.slice().sort((a, b) => (b.time?.updated ?? 0) - (a.time?.updated ?? 0))
      );
    }
    // 项目清单来自平台 DB（容器停了也能读），失败时保留现有列表。
    if (proj.status === "fulfilled") setProjects(proj.value.projects);
    if (ags.status === "fulfilled") {
      const list = ags.value;
      setAgents(list);
      setAgentId((cur) =>
        list.some((a) => a.id === cur)
          ? cur
          : list.find((a) => !a.hidden && a.mode !== "subagent")?.id ?? cur
      );
    }
    refreshPending();
  }, [refreshPending, loadSkills]);

  // ------------------------------------------------------------------
  //  SSE — opencode's own event stream, relayed by the platform
  // ------------------------------------------------------------------
  /** Replace the open session's turns with the server truth. */
  const refreshSessionMessages = useCallback(async () => {
    const sid = sessionIdRef.current;
    if (!sid) return;
    try {
      const msgs = await api.getMessages(sid);
      if (sessionIdRef.current !== sid || msgs.length === 0) return;
      // Dead-turn reconciliation: close tool calls stranded by a container
      // kill so isGenerating can't wedge on a message that will never finish.
      const next = toTurns(msgs, agentStartedAtRef.current);
      setTurns(next);
      // Server truth beats the streaming flag: if no assistant turn is still
      // streaming, the round is over (covers SSE events lost in transit).
      if (next.some((t) => t.role === "assistant") && !next.some((t) => t.streaming)) {
        setIsGenerating(false);
      }
    } catch {
      /* transient tunnel error — the next poll retries */
    }
  }, []);

  /** Debounced refresh (SSE reports a completed message). */
  const scheduleRefresh = useCallback(() => {
    if (refreshDebounceRef.current !== null) window.clearTimeout(refreshDebounceRef.current);
    refreshDebounceRef.current = window.setTimeout(() => {
      refreshDebounceRef.current = null;
      refreshSessionMessages();
    }, 400);
  }, [refreshSessionMessages]);

  const connectSSE = useCallback(() => {
    esRef.current?.close();
    if (sseReconnectRef.current !== null) {
      window.clearTimeout(sseReconnectRef.current);
      sseReconnectRef.current = null;
    }
    // P1-5: minting a one-time ticket is async; whoever resolves last wins
    // (each resolution closes whatever ES is currently attached).
    api
      .createEventSource(lastEventIdRef.current)
      .then((es) => {
        esRef.current?.close();
        esRef.current = es;
        attachSSEHandlers(es);
      })
      .catch(() => {
        // Ticket mint failed (backend down / session expired) — back off
        // and redial; a 401 already forces a relogin via apiCall.
        setSseDown(true);
        if (sseReconnectRef.current !== null) return;
        sseReconnectRef.current = window.setTimeout(() => {
          sseReconnectRef.current = null;
          connectSSERef.current();
        }, 2000);
      });
  }, [refreshPending, scheduleRefresh]);

  /** Register the event reducers on a freshly attached EventSource. */
  const attachSSEHandlers = useCallback((es: EventSource) => {
    es.onmessage = (event) => {
      let evt: any;
      try {
        evt = JSON.parse(event.data);
      } catch {
        return;
      }
      if (typeof evt.id === "number") lastEventIdRef.current = evt.id;

      const type: string = evt.type || "";
      // `/event` is opencode's streaming SSE surface (the one used by its
      // official web app). It carries payloads in `properties`; the v2
      // `/api/event` surface uses `data`. Accept both at the boundary so the
      // reducer has one normalized shape.
      const data = evt.properties || evt.data || {};

      // Session list events apply regardless of which session is open.
      if (type === "session.created" || type === "session.updated") {
        const info: OcSession | undefined = data.info;
        if (info) {
          setSessions((prev) => {
            const rest = prev.filter((s) => s.id !== info.id);
            return [info, ...rest].sort(
              (a, b) => (b.time?.updated ?? 0) - (a.time?.updated ?? 0)
            );
          });
          setCurrentSession((cur) => (cur?.id === info.id ? info : cur));
        }
        return;
      }
      if (type === "session.deleted") {
        const sid = data.info?.id || data.sessionID || data.id;
        if (typeof sid === "string") {
          setSessions((prev) => prev.filter((s) => s.id !== sid));
          setCurrentSession((cur) => {
            if (cur?.id !== sid) return cur;
            sessionIdRef.current = null;
            setTurns([]);
            setIsGenerating(false);
            return null;
          });
        }
        return;
      }
      // Approval requests arrive as permission.v2.* / question.v2.* (and
      // legacy permission.* / question.* aliases).
      if (
        type.startsWith("permission.") ||
        type.startsWith("question.")
      ) {
        refreshPending();
        return;
      }
      if (type === "agent.disconnected") {
        setError("Agent 未运行，事件流已断开");
        return;
      }

      // Everything else is scoped to one session.
      const sid = data.sessionID;
      if (sid && sid !== sessionIdRef.current) return;

      // present_file signaling: the tool's completed result carries the
      // delivery payload. Capture it as it streams in and hand it to the
      // present scheduler (dedupe / dismiss memory / auto-pop).
      if (type === "message.part.updated") {
        const part = data.part;
        if (
          part?.type === "tool" &&
          part.tool === "present_file" &&
          part.state?.status === "completed"
        ) {
          const payload = parsePresentOutput(part.state.output);
          if (payload) presentHandlerRef.current(payload, true);
        }
      }

      // P1-1: the agent's task list updates — session-scoped, but not a
      // message turn, so it bypasses the turn reducer entirely.
      if (type === "todo.updated") {
        setTodos(parseTodos(data.todos));
        return;
      }

      // Track live message traffic so the generation poll can stand down
      // while SSE is actually delivering (it only kicks in as a fallback
      // when the pump has dropped events).
      if (type.startsWith("message.")) lastMsgEvtRef.current = Date.now();
      if (
        type === "message.updated" &&
        data.info?.role === "assistant" &&
        (data.info.time?.completed || data.info.error)
      ) {
        // Final (or failed) assistant message — pull the authoritative
        // record: its text parts may have been lost to SSE drops.
        scheduleRefresh();
      }

      setTurns((prev) => {
        const r = reduceEvent(prev, type, data);
        if (r.idle) setIsGenerating(false);
        if (r.error) setError(r.error);
        if (r.model) setModel(r.model);
        return r.turns;
      });
    };

    es.onopen = () => setSseDown(false);
    es.onerror = () => {
      // P1-5: the ticket in the URL is one-time, so the browser's native
      // auto-reconnect can't replay it. Redial manually (fresh ticket via
      // connectSSERef → connectSSE) after a short backoff; lastEventId
      // replays the gap.
      es.close();
      esRef.current = null;
      setSseDown(true);
      console.warn("SSE dropped, redialing with a fresh ticket");
      if (sseReconnectRef.current !== null) return;
      sseReconnectRef.current = window.setTimeout(() => {
        sseReconnectRef.current = null;
        connectSSERef.current();
      }, 2000);
    };
  }, [refreshPending, scheduleRefresh]);

  // Keep the retry indirection pointed at the current connectSSE. Declared
  // before the mount effect so the first connectSSE() call can't fire early.
  useEffect(() => {
    connectSSERef.current = connectSSE;
  }, [connectSSE]);

  // On mount: fetch the status once, then auto-warm the container if it
  // isn't up — the user should never have to click "start" and wait. The
  // start endpoint is async (returns immediately); progress arrives via
  // the polling effect below.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const st = await api.getAgentStatus();
        if (cancelled) return;
        setAgentStatus(st);
        if (st.status === "running") return;
        if (["creating", "starting", "warming"].includes(st.status)) {
          // Another tab / request already started it — just poll.
          setStarting(true);
          return;
        }
        // absent / stopped / failed → kick off the background start now.
        setStarting(true);
        const r = await api.startAgent();
        if (cancelled) return;
        setAgentStatus(r);
        if (r.status === "running") {
          setStarting(false); // fast path: container was already up
        } else if (r.status === "failed") {
          setStarting(false);
          setError(r.error || r.message || "自动启动 Agent 失败");
        }
      } catch (e) {
        console.error("status check failed", e);
      }
    })();
    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
      if (sseReconnectRef.current !== null) {
        window.clearTimeout(sseReconnectRef.current);
        sseReconnectRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // While a background start is in flight, poll GET /agent/status (~1s)
  // until the phase settles on running / failed / stopped.
  useEffect(() => {
    if (!starting) return;
    let stop = false;
    (async () => {
      while (!stop) {
        await new Promise((r) => setTimeout(r, 1000));
        if (stop) break;
        try {
          const st = await api.getAgentStatus();
          if (stop) break;
          setAgentStatus(st);
          if (["running", "failed", "stopped"].includes(st.status)) {
            setStarting(false);
            if (st.status === "failed") {
              setError("Agent 启动失败：" + (st.error || st.message || "健康检查超时"));
            }
            break;
          }
        } catch {
          // transient network error — keep polling
        }
      }
    })();
    return () => {
      stop = true;
    };
  }, [starting]);

  // P1-4: browser-side start timestamp — used only when the backend hasn't
  // reported phase_since yet (e.g. the request hasn't landed).
  useEffect(() => {
    if (starting) {
      if (startupAtRef.current === null) startupAtRef.current = Date.now();
    } else {
      startupAtRef.current = null;
    }
  }, [starting]);

  // P1-4: total wait so far while a start is in flight. phase_since is set
  // server-side when the flow began, so a browser that attached mid-start
  // still shows an accurate count; the local timer is the fallback.
  const startupElapsedSec = (() => {
    if (!starting) return null;
    const ps = agentStatus?.phase_since;
    if (typeof ps === "number" && ps > 0) return Math.max(0, Math.round(Date.now() / 1e3 - ps));
    const started = startupAtRef.current;
    return started ? Math.max(0, Math.round((Date.now() - started) / 1e3)) : null;
  })();
  const startupPhaseIdx =
    agentStatus?.status === "creating"
      ? 1
      : agentStatus?.status === "starting"
      ? 2
      : agentStatus?.status === "warming"
      ? 3
      : 0;

  // If the container was already up when the page loaded, attach to it.
  const attachedRef = useRef(false);
  useEffect(() => {
    if (!isAgentRunning || attachedRef.current) return;
    attachedRef.current = true;
    connectSSE();
    loadContainerState();
  }, [isAgentRunning, connectSSE, loadContainerState]);

  const handleStartAgent = async () => {
    setError("");
    setStarting(true);
    try {
      const result = await api.startAgent();
      setAgentStatus(result);
      if (result.status === "running") {
        // Fast path: container was already up.
        setStarting(false);
        attachedRef.current = true;
        connectSSE();
        await loadContainerState();
      } else if (result.status === "failed") {
        setStarting(false);
        setError(result.message || "启动 Agent 失败");
      }
      // otherwise: background start in flight — the polling effect takes
      // over and flips `starting` off when the phase settles.
    } catch (e: any) {
      setStarting(false);
      setError(e.message);
    }
  };

  const handleStopAgent = async () => {
    setBusy("停止容器中…");
    try {
      await api.stopAgent();
      esRef.current?.close();
      esRef.current = null;
      if (sseReconnectRef.current !== null) {
        window.clearTimeout(sseReconnectRef.current);
        sseReconnectRef.current = null;
      }
      attachedRef.current = false;
      lastEventIdRef.current = 0;
      // P1-4: clear connection-health + session-restore markers so a later
      // restart reattaches cleanly and can restore the session once more.
      setSseDown(false);
      restoredRef.current = false;
      setSessions([]);
      setCurrentSession(null);
      sessionIdRef.current = null;
      setTurns([]);
      setProviders(null);
      await refreshStatus();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  };

  const handleReloadConfig = async () => {
    setBusy("重新注入 opencode.json…");
    setError("");
    try {
      await api.reloadConfig();
      // The container restarted: its SSE connection and provider list are new.
      lastEventIdRef.current = 0;
      connectSSE();
      await loadContainerState();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  };

  // ------------------------------------------------------------------
  //  Sessions
  // ------------------------------------------------------------------
  const openSession = useCallback(async (session: OcSession) => {
    setCurrentSession(session);
    sessionIdRef.current = session.id;
    // P1-4: remember the open session so a page refresh can restore it.
    window.localStorage.setItem("oc.lastSession", session.id);
    setTurns([]);
    setIsGenerating(false);
    // P1-1: revert state + task list belong to the session, not the page.
    setRevertedId(null);
    setTodos([]);
    // 任务一：反馈锁定态属于会话 —— 先清空，再异步回填。
    setFeedbackMap({});
    setDownTargetId(null);
    // present_file delivery tabs belong to the session too — reset, then
    // rebuild from restored turns below (no auto-pop on restore).
    setPresentTabs([]);
    setActivePresent(null);
    if (session.model) setModel(session.model);
    if (session.agent) setAgentId(session.agent);
    refreshPending();
    try {
      const next = toTurns(await api.getMessages(session.id), agentStartedAtRef.current);
      setTurns(next);
      setPresentTabs(extractPresentFromTurns(next));
    } catch (e: any) {
      setError(e.message);
    }
    // P1-1: `todo.updated` SSE events are not replayed when a session is
    // opened (or after a page refresh), so fetch the current list once to
    // restore the card. Race-guarded: a fast session switch must not let a
    // stale response overwrite the new session's state.
    api
      .getSessionTodos(session.id)
      .then((raw) => {
        if (sessionIdRef.current === session.id) setTodos(parseTodos(raw));
      })
      .catch(() => {});
    // 任务一：已投过的票同样不会被 SSE 重放 —— 打开会话时拉取一次锁定态。
    // Race-guarded：快速切换会话时，旧响应不得覆盖新会话的状态。
    api
      .getSessionFeedback(session.id)
      .then((r) => {
        if (sessionIdRef.current !== session.id) return;
        const map: Record<string, "up" | "down"> = {};
        for (const f of r.feedback) map[f.message_id] = f.verdict;
        setFeedbackMap(map);
      })
      .catch(() => {});
  }, [refreshPending]);

  // P1-4: after a page refresh, reopen the session the user had open (once
  // the list has loaded). Manual navigation writes sessionIdRef before this
  // runs, so an explicit choice is never overridden.
  useEffect(() => {
    if (restoredRef.current || !isAgentRunning || sessions.length === 0) return;
    restoredRef.current = true;
    if (sessionIdRef.current) return;
    const saved = window.localStorage.getItem("oc.lastSession");
    if (!saved) return;
    const found = sessions.find((s) => s.id === saved);
    if (found) openSession(found);
  }, [sessions, isAgentRunning, openSession]);

  // ------------------------------------------------------------------
  //  P1-1: revert / unrevert / fork / summarize — all opencode-native
  //  session routes, forwarded verbatim through the tunnel.
  // ------------------------------------------------------------------
  const handleRevert = useCallback(
    async (messageId: string) => {
      const sid = sessionIdRef.current;
      if (!sid || revertBusy) return;
      modalApi.confirm({
        title: "回退该回合的文件改动？",
        content: "本回合产生的所有文件改动会还原到请求前的状态。",
        okText: "回退",
        cancelText: "取消",
        okButtonProps: { danger: true },
        onOk: async () => {
          setRevertBusy(true);
          try {
            await api.revertSession(sid, messageId);
            setRevertedId(messageId);
            refreshSessionMessages();
          } catch (e: any) {
            setError(`回退失败：${e?.message ?? e}`);
          } finally {
            setRevertBusy(false);
          }
        },
      });
    },
    [revertBusy, refreshSessionMessages, modalApi]
  );

  const handleUnrevert = useCallback(async () => {
    const sid = sessionIdRef.current;
    if (!sid || revertBusy) return;
    setRevertBusy(true);
    try {
      await api.unrevertSession(sid);
      setRevertedId(null);
      refreshSessionMessages();
    } catch (e: any) {
      setError(`恢复失败：${e?.message ?? e}`);
    } finally {
      setRevertBusy(false);
    }
  }, [revertBusy, refreshSessionMessages]);

  const handleFork = useCallback(
    async (messageId?: string) => {
      const sid = sessionIdRef.current;
      if (!sid || busyLabel) return;
      setBusyLabel("分叉会话中…");
      try {
        const forked = await api.forkSession(sid, messageId);
        setSessions((prev) => [forked, ...prev.filter((s) => s.id !== forked.id)]);
        await openSession(forked);
      } catch (e: any) {
        setError(`分叉失败：${e?.message ?? e}`);
      } finally {
        setBusyLabel("");
      }
    },
    [busyLabel, openSession]
  );

  // 重新生成（方案一：直接替换）。对目标回合前的最后一条 user 消息调用
  // revert —— opencode 只打软标记，下次 prompt 时才会硬删旧分支 —— 然后
  // 从该 user turn 的 blocks 重建原始输入（文本 + file parts）并重发，等效
  // "替换原回答"。SSE 的 message.removed / session.next.prompted 事件以及
  // 既有的防抖刷新会驱动 UI 收敛到新回复。
  const handleRegenerate = useCallback(
    async (assistantId: string) => {
      const sid = sessionIdRef.current;
      if (!sid || isGenerating) return;
      const aIdx = turns.findIndex((t) => t.id === assistantId);
      if (aIdx < 0) return;
      let userTurn: Turn | undefined;
      for (let i = aIdx - 1; i >= 0; i--) {
        if (turns[i].role === "user") {
          userTurn = turns[i];
          break;
        }
      }
      if (!userTurn) return;
      const text = userTurn.blocks
        .filter((b) => b.kind === "text")
        .map((b) => b.text)
        .join("\n");
      const files = userTurn.blocks
        .filter((b) => b.kind === "file")
        .map((b) => ({
          mime: b.mime,
          url: b.url,
          ...(b.filename ? { filename: b.filename } : {}),
        }));
      setIsGenerating(true);
      setRevertedId(null);
      try {
        await api.revertSession(sid, userTurn.id);
        await api.sendPrompt(sid, text, {
          files: files.length ? files : undefined,
          agents: userTurn.agents?.length ? userTurn.agents : undefined,
          agent: userTurn.agent,
          model: userTurn.model,
        });
      } catch (e: any) {
        setError(`重新生成失败：${e?.message ?? e}`);
        setIsGenerating(false);
        refreshSessionMessages();
      }
    },
    [isGenerating, turns, refreshSessionMessages]
  );

  // ------------------------------------------------------------------
  //  任务一：点赞 / 点踩反馈。
  //  提交契约：乐观锁定 → 失败重试一次 → 仍失败回滚并提示。
  //  投票一经落库不可修改（后端对 (user_id, message_id) 幂等）。
  // ------------------------------------------------------------------
  const submitVote = useCallback(
    async (
      assistantId: string,
      verdict: "up" | "down",
      codes?: string[],
      text?: string | null
    ): Promise<boolean> => {
      const sid = sessionIdRef.current;
      if (!sid || feedbackMap[assistantId]) return false;

      // 乐观锁定：立即按投票态渲染，禁用该条按钮。
      setFeedbackMap((m) => ({ ...m, [assistantId]: verdict }));
      setFeedbackBusyId(assistantId);

      // 本轮完整上下文：上一条 user 提问 → 本条 assistant 的全部输出。
      const ctx = buildTurnContext(turns, assistantId);
      const body: FeedbackSubmit = {
        session_id: sid,
        message_id: assistantId,
        user_message_id: ctx?.user_message_id ?? null,
        verdict,
        reason_codes: codes as FeedbackReasonCode[] | undefined,
        reason_text: text ?? null,
        turn_errored: ctx?.turn_errored ?? false,
        model_provider: ctx?.model?.providerID ?? null,
        model_id: ctx?.model?.id ?? null,
        agent: ctx?.agent ?? null,
        context: (ctx ?? undefined) as Record<string, any> | undefined,
      };

      let ok = false;
      for (let attempt = 0; attempt < 2 && !ok; attempt++) {
        try {
          ok = (await api.submitFeedback(body)).ok;
        } catch {
          ok = false;
        }
      }
      setFeedbackBusyId((cur) => (cur === assistantId ? null : cur));
      if (!ok) {
        setFeedbackMap((m) => {
          const next = { ...m };
          delete next[assistantId];
          return next;
        });
        setError("反馈提交失败，请稍后重试");
      }
      return ok;
    },
    [feedbackMap, turns]
  );

  const handleFeedbackUp = useCallback(
    (assistantId: string) => {
      void submitVote(assistantId, "up");
    },
    [submitVote]
  );

  /** 👎 先开弹窗收集原因，真正提交在 handleFeedbackDownSubmit。 */
  const handleFeedbackDown = useCallback((assistantId: string) => {
    setDownTargetId(assistantId);
  }, []);

  const handleFeedbackDownSubmit = useCallback(
    async (codes: string[], text: string | null) => {
      const id = downTargetId;
      if (!id) return;
      if (await submitVote(id, "down", codes, text)) setDownTargetId(null);
    },
    [downTargetId, submitVote]
  );

  const handleSummarize = useCallback(async () => {
    const sid = sessionIdRef.current;
    const m = model ?? currentSession?.model;
    if (!sid || !m || busyLabel) return;
    setBusyLabel("生成摘要中…");
    try {
      await api.summarizeSession(sid, m);
      refreshSessionMessages();
    } catch (e: any) {
      setError(`生成摘要失败：${e?.message ?? e}`);
    } finally {
      setBusyLabel("");
    }
  }, [busyLabel, model, currentSession, refreshSessionMessages]);

  // Fallback poll while a prompt is in flight. opencode severs its event
  // stream ~every second during agent activity, so the platform pump can
  // silently drop the assistant's parts; when that happens no SSE event
  // ever clears isGenerating and the reply never renders. While SSE is
  // delivering message events this poll stands down; otherwise it re-pulls
  // the whole message list every 2.5s until the round completes.
  useEffect(() => {
    if (!isGenerating) return;
    const timer = window.setInterval(() => {
      if (Date.now() - lastMsgEvtRef.current < 4000) return; // SSE is live
      refreshSessionMessages();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [isGenerating, refreshSessionMessages]);

  // directory 缺省 = 全局会话（/workspace）；项目内新建时传项目目录。
  const handleNewSession = async (directory?: string) => {
    setError("");
    try {
      const s = await api.createSession(model, agentId, directory);
      setSessions((prev) => [s, ...prev.filter((x) => x.id !== s.id)]);
      await openSession(s);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleRenameSession = (s: OcSession, e: React.MouseEvent) => {
    e.stopPropagation();
    setRenameTarget(s);
    setRenameValue(s.title || "");
  };

  const handleRenameConfirm = async () => {
    const s = renameTarget;
    const title = renameValue.trim();
    if (!s || !title) return;
    try {
      // opencode's v2 surface has no update route — legacy PATCH returns the
      // bare legacy Session; session.updated on SSE also refreshes the list.
      await api.renameSession(s.id, title);
      setSessions((prev) => prev.map((x) => (x.id === s.id ? { ...x, title } : x)));
      setCurrentSession((cur) => (cur?.id === s.id ? { ...cur, title } : cur));
      setRenameTarget(null);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleDeleteSession = (s: OcSession, e: React.MouseEvent) => {
    e.stopPropagation();
    modalApi.confirm({
      title: "删除会话？",
      content: `「${s.title || s.id.slice(0, 12)}」将被删除，此操作不可恢复。`,
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await api.deleteSession(s.id);
          setSessions((prev) => prev.filter((x) => x.id !== s.id));
          if (sessionIdRef.current === s.id) {
            setCurrentSession(null);
            sessionIdRef.current = null;
            setTurns([]);
            setIsGenerating(false);
          }
        } catch (err: any) {
          setError(err.message);
        }
      },
    });
  };

  // ------------------------------------------------------------------
  //  Projects — workspace 子目录即会话工作目录；分组在渲染时按
  //  session.location.directory 推导，这里只维护项目清单本身。
  // ------------------------------------------------------------------
  const { globalSessions, sessionsByProject } = useMemo(() => {
    const byProject: Record<string, OcSession[]> = {};
    for (const p of projects) byProject[p.id] = [];
    const dirToProject = new Map(projects.map((p) => [p.directory, p.id]));
    const global: OcSession[] = [];
    for (const s of sessions) {
      const pid = s.location?.directory ? dirToProject.get(s.location.directory) : undefined;
      // 未命中任何项目的一律归全局区（含 /workspace 与未知目录），保守不丢会话。
      if (pid) byProject[pid].push(s);
      else global.push(s);
    }
    return { globalSessions: global, sessionsByProject: byProject };
  }, [sessions, projects]);

  const toggleProject = (id: string) =>
    setExpandedProjects((prev) => ({ ...prev, [id]: !prev[id] }));

  const handleNewProjectSession = async (p: ProjectInfo, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!isAgentRunning) return;
    await handleNewSession(p.directory);
    setExpandedProjects((prev) => ({ ...prev, [p.id]: true }));
  };

  const handleDeleteProject = (p: ProjectInfo, e: React.MouseEvent) => {
    e.stopPropagation();
    modalApi.confirm({
      title: "移除项目？",
      content: (
        <span style={{ whiteSpace: "pre-line" }}>
          {`「${p.name}」项目下的所有会话将被删除（不可恢复），目录文件保留在 workspace 中。`}
        </span>
      ),
      okText: "移除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setBusy("移除项目中…");
        setError("");
        try {
          await api.deleteProject(p.id);
          setProjects((prev) => prev.filter((x) => x.id !== p.id));
          // 后端已连带删除项目会话 —— 本地列表同步移除，避免幽灵条目。
          setSessions((prev) => prev.filter((s) => s.location?.directory !== p.directory));
          if (currentSession?.location?.directory === p.directory) {
            setCurrentSession(null);
            sessionIdRef.current = null;
            setTurns([]);
            setIsGenerating(false);
          }
        } catch (err: any) {
          setError(err.message);
        } finally {
          setBusy("");
        }
      },
    });
  };

  const openProjectModal = async () => {
    setProjName("");
    setProjMode("create");
    setProjDir("");
    setProjDirs([]);
    setShowProjectModal(true);
    try {
      // 复用文件浏览接口：workspace 相对路径，type=dir 即候选绑定目录。
      const res = await api.listWorkspaceFiles();
      setProjDirs(
        res.files
          .filter((f) => f.type === "dir" && !f.path.split("/").some((seg) => seg.startsWith(".")))
          .map((f) => f.path)
      );
    } catch {
      /* 容器未启动等情况：picker 为空，仍可用"新建目录"模式 */
    }
  };

  const handleCreateProject = async () => {
    if (projBusy) return;
    if (projMode === "create" && !projName.trim()) return;
    if (projMode === "bind" && !projDir) return;
    setProjBusy(true);
    setError("");
    try {
      // create：目录名 = 项目名；bind：项目名由后端取目录最后一段。
      const p = await api.createProject(
        projMode === "create"
          ? { name: projName.trim(), mode: "create" }
          : { mode: "bind", directory: projDir }
      );
      setProjects((prev) => [p, ...prev]);
      setExpandedProjects((prev) => ({ ...prev, [p.id]: true }));
      setShowProjectModal(false);
      // 绑定已有目录时，该目录下的历史会话会自动归入项目 —— 拉一次列表。
      if (projMode === "bind" && isAgentRunning) {
        try {
          const list = await api.listSessions();
          setSessions(
            list.slice().sort((a, b) => (b.time?.updated ?? 0) - (a.time?.updated ?? 0))
          );
        } catch {
          /* 下一轮轮询会补上 */
        }
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setProjBusy(false);
    }
  };

  /** 侧栏会话行（全局区与项目内共用；项目内加缩进）。 */
  const renderSessionItem = (s: OcSession, nested = false) => (
    <div
      key={s.id}
      style={{
        ...(currentSession?.id === s.id ? styles.sessionItemActive : styles.sessionItem),
        ...(nested ? styles.nestedSessionItem : null),
      }}
      onClick={() => openSession(s)}
    >
      <span style={styles.sessionIcon}><MessageOutlined /></span>
      <span style={styles.sessionItemActiveTitle}>{s.title || s.id.slice(0, 12)}</span>
      <div style={styles.sessionActions}>
        <button
          style={styles.sessionActionBtn}
          title="重命名会话"
          onClick={(e) => handleRenameSession(s, e)}
        >
          <EditOutlined />
        </button>
        <button
          style={styles.sessionActionBtn}
          title="删除会话"
          onClick={(e) => handleDeleteSession(s, e)}
        >
          <DeleteOutlined />
        </button>
      </div>
    </div>
  );

  // ------------------------------------------------------------------
  //  Prompting
  // ------------------------------------------------------------------
  const handleSend = async () => {
    const text = input.trim();
    if ((!text && attachments.length === 0) || !currentSession || isGenerating) return;
    setError("");
    setAtQuery(null);
    setAtOptions([]);
    setSlashQuery(null);

    // Slash command dispatch: "/name args..." goes to the dedicated command
    // endpoint instead of the prompt route. Unknown "/" text falls through
    // as a normal prompt (same as opencode's own TUI behaviour).
    const slashMatch = text.match(/^\/([a-zA-Z0-9_:-]+)(?:\s+([\s\S]*))?$/);
    if (slashMatch) {
      const name = slashMatch[1];
      const args = (slashMatch[2] ?? "").trim();
      if (name.toLowerCase() === "agents") {
        // Client-side pseudo command: switching happens in the menu itself.
        setInput("");
        return;
      }
      let cmds = commands;
      if (cmds.length === 0) {
        try {
          cmds = await api.listCommands();
          setCommands(cmds);
        } catch {
          cmds = [];
        }
      }
      if (cmds.some((c) => c.name === name)) {
        setInput("");
        setAttachments([]);
        setIsGenerating(true);
        // Optimistic bubble; reconciled by session.next.prompted, and
        // isGenerating is cleared by the session.idle SSE event.
        setTurns((prev) => [
          ...prev,
          {
            id: "pending-user",
            role: "user",
            type: "user",
            blocks: [{ kind: "text", id: "pending-user:text", text }],
          },
        ]);
        try {
          await api.runCommand(currentSession.id, name, args);
        } catch (e: any) {
          setError(e.message);
          setIsGenerating(false);
        }
        return;
      }
    }

    // Attachments + "@path" file references → parts of POST /session/{id}/prompt_async.
    // FilePartInput requires an explicit `mime`, which decides how opencode
    // routes the attachment:
    //   - image/* with a data: URL → inline base64 media part, the model sees
    //     the pixels directly (no upload into the container involved);
    //   - "text/plain" with a file:// URL → inlined through the Read tool;
    //   - OpenAI-Chat providers reject every other media type (e.g.
    //     text/markdown), so text-ish files are always sent as text/plain.
    // "@agent" tokens matching a registered agent go into agent parts instead.
    // Unknown tokens stay as plain text — same as opencode's own @mention
    // behaviour, where the mention is turned into a file/agent attachment
    // before the prompt is sent.
    const files: { mime: string; url: string; filename?: string }[] = [];
    const agentNames: string[] = [];
    const seen = new Set<string>();
    for (const a of attachments) {
      const key = a.dataUrl ?? a.path;
      if (seen.has(key)) continue;
      seen.add(key);
      files.push({
        mime: a.isImage ? a.mime : "text/plain",
        url: a.dataUrl ?? `file:///workspace/${a.path}`,
        filename: a.filename,
      });
    }

    const atTokens = collectAtTokens(text);
    if (atTokens.length > 0) {
      // Only subagents (the ones the @-menu offers) become agent mentions.
      const agentSet = new Set(mentionableAgents.map((a) => a.id));
      let knownPaths = new Set(wsFiles.filter((f) => f.type === "file").map((f) => f.path));
      if (knownPaths.size === 0) {
        // The file browser was never opened; fetch the tree once to resolve.
        try {
          const r = await api.listWorkspaceFiles();
          knownPaths = new Set((r.files ?? []).filter((f) => f.type === "file").map((f) => f.path));
          setWsFiles(r.files ?? []);
        } catch {
          /* leave tokens as plain text */
        }
      }
      for (const tok of atTokens) {
        if (seen.has(tok)) continue;
        if (agentSet.has(tok)) {
          seen.add(tok);
          agentNames.push(tok);
          continue;
        }
        if (!knownPaths.has(tok)) continue;
        seen.add(tok);
        files.push({ mime: mimeForPath(tok), url: `file:///workspace/${tok}` });
      }
    }

    // Skills: opencode has no dedicated prompt field for them, so request them
    // explicitly in the text (the skill tool picks them up by name).
    // 模板与预定义风格同理——没有结构化字段，只能把容器内的只读挂载路径和风格
    // 约束拼进文本前缀，agent 据此直接读共享卷，不会把字节复制进工作区。
    const prefixes: string[] = [];
    // 快捷技能（一键启用）与 🧩 菜单手动选择的 skill 合并为一条 skill 声明。
    const quickSkills: string[] = [];
    if (pptOn) quickSkills.push(PPT_SKILL);
    if (flowOn) quickSkills.push(FLOW_SKILL[flowDialect]);
    const activeSkills = Array.from(new Set([...selectedSkills, ...quickSkills]));
    if (activeSkills.length > 0) prefixes.push(`请使用 skill: ${activeSkills.join(", ")}`);
    const libPrompt = buildLibraryPrompt(
      { template: selTemplate, paletteId: selPaletteId, recipeId: selRecipeId },
      libCatalog.styles
    );
    if (libPrompt) prefixes.push(libPrompt);
    if (flowOn) prefixes.push(buildFlowPrompt(flowDialect));
    const finalText = prefixes.length ? `${prefixes.join("\n\n")}\n\n${text}` : text;

    setInput("");
    setAttachments([]);
    setIsGenerating(true);
    // Optimistic bubble; reconciled to the real id by session.next.prompted.
    // 存完整 finalText：服务端回填的权威文本同样带前缀，两边都经 splitSkillMarks
    // 渲染成「标签 + 正文」，避免乐观态与回填态闪一下不一致。
    setTurns((prev) => [
      ...prev,
      {
        id: "pending-user",
        role: "user",
        type: "user",
        agents: agentNames.length ? agentNames : undefined,
        files: files.length
          ? files.map((f) => (f.url.startsWith("data:") ? f.filename ?? "image" : f.url))
          : undefined,
        blocks: [
          { kind: "text", id: "pending-user:text", text: finalText },
          ...(attachments.map((a, i) => ({
            kind: "text" as const,
            id: `pending-user:file-${i}`,
            text: a.isImage ? `🖼️ ${a.filename}` : `📎 ${a.filename}`,
          }))),
        ],
      },
    ]);
    try {
      await api.sendPrompt(currentSession.id, finalText, {
        files: files.length ? files : undefined,
        agents: agentNames.length ? agentNames : undefined,
        agent: promptAgent,
        model: promptModel,
      });
      setPromptAgent(undefined);
      setPromptModel(undefined);
    } catch (e: any) {
      setError(e.message);
      setIsGenerating(false);
    }
  };

  /** Read a browser File as a base64 data URL (for inline image parts). */
  const readFileAsDataUrl = (file: File): Promise<string> =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () => reject(new Error(`读取 ${file.name} 失败`));
      reader.readAsDataURL(file);
    });

  const handleFilesPicked = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError("");
    try {
      for (const f of Array.from(files)) {
        if (f.type.startsWith("image/")) {
          // Images never touch the container: the browser encodes them as a
          // base64 data URL that goes straight into the prompt's file part,
          // so the model sees the pixels directly.
          const dataUrl = await readFileAsDataUrl(f);
          setAttachments((prev) => [
            ...prev,
            { filename: f.name, path: "", mime: f.type, isImage: true, size: f.size, dataUrl },
          ]);
          continue;
        }
        const r = await api.uploadChatFile(f);
        setAttachments((prev) => [
          ...prev.filter((a) => a.path !== r.path),
          { filename: r.filename, path: r.path, mime: r.mime, isImage: r.isImage, size: r.size },
        ]);
        // Auto-reference the upload so the model (and the user) can see it.
        setInput((prev) => {
          const ref = `@${r.path}`;
          if (prev.includes(ref)) return prev;
          return prev ? (prev.endsWith(" ") ? prev + ref : prev + " " + ref) : ref;
        });
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  // ------------------------------------------------------------------
  //  @-mention file autocomplete + slash command menu
  // ------------------------------------------------------------------
  /** Extract the active "@query" before the caret, or null when not in a mention. */
  const detectAtMention = (text: string, caret: number): string | null => {
    const before = text.slice(0, caret);
    const at = before.lastIndexOf("@");
    if (at === -1) return null;
    const query = before.slice(at + 1);
    if (/\s/.test(query)) return null; // a space ends the mention
    return query;
  };

  /** The active "/cmd" token when the input starts with "/" (before caret). */
  const detectSlashCommand = (text: string, caret: number): string | null => {
    const before = text.slice(0, caret);
    if (!before.startsWith("/")) return null; // commands only at message start
    const query = before.slice(1);
    if (/\s/.test(query)) return null; // a space ends command selection
    return query;
  };

  /** Called on every input change: opens/closes the menus, debounced search. */
  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const text = e.target.value;
    setInput(text);
    const caret = e.target.selectionStart ?? text.length;
    // The slash menu (message start) and the @-menu are mutually exclusive.
    const sq = detectSlashCommand(text, caret);
    setSlashQuery(sq);
    setSlashIndex(0);
    if (sq !== null) {
      setAtQuery(null);
      setAtOptions([]);
      // Lazy-load the command list on first slash usage; plugins can register
      // more, so a failed fetch retries on the next slash input.
      if (commands.length === 0 && !commandsLoadedRef.current) {
        commandsLoadedRef.current = true;
        api
          .listCommands()
          .then(setCommands)
          .catch(() => {
            setCommands([]);
            commandsLoadedRef.current = false;
          });
      }
      return;
    }
    const q = detectAtMention(text, caret);
    setAtQuery(q);
    if (q === null) {
      setAtOptions([]);
      return;
    }
    if (atTimerRef.current) window.clearTimeout(atTimerRef.current);
    atTimerRef.current = window.setTimeout(async () => {
      // Agent mentions first when the orchestrator is active.
      const agentOpts: AtOption[] = showAgentMentions
        ? mentionableAgents
            .filter((a) => q === "" || a.id.toLowerCase().includes(q.toLowerCase()))
            .map((a) => ({ kind: "agent" as const, name: a.id, description: a.description }))
        : [];
      try {
        // Empty query right after "@": show workspace files as a starter list.
        let fileOpts: AtOption[] = [];
        if (q === "") {
          fileOpts = wsFiles
            .filter((f) => f.type === "file")
            .slice(0, 15)
            .map((f) => ({ kind: "file" as const, path: f.path }));
        } else {
          const found = await api.findFiles(q, 15);
          fileOpts = (Array.isArray(found) ? found : []).map((p) => ({
            kind: "file" as const,
            path: p,
          }));
        }
        setAtOptions([...agentOpts, ...fileOpts]);
      } catch {
        setAtOptions(agentOpts);
      }
      setAtIndex(0);
    }, 200);
  };

  /** Replace the active "@query" with "@name " and keep the caret after it. */
  const insertAtToken = (token: string) => {
    const ta = inputRef.current;
    const caret = ta?.selectionStart ?? input.length;
    const before = input.slice(0, caret);
    const at = before.lastIndexOf("@");
    if (at === -1) return;
    const next = input.slice(0, at) + "@" + token + " " + input.slice(caret);
    setInput(next);
    setAtQuery(null);
    setAtOptions([]);
    // Move caret past the inserted reference.
    requestAnimationFrame(() => {
      const pos = at + token.length + 2;
      ta?.focus();
      ta?.setSelectionRange(pos, pos);
    });
  };

  /** Apply the highlighted @-menu entry: agent mention or file reference. */
  const applyAtSelection = (opt: AtOption) => {
    insertAtToken(opt.kind === "agent" ? opt.name : opt.path);
  };

  /** Keyboard routing while the @-menu is open. Returns true if handled. */
  const handleAtKeyDown = (e: React.KeyboardEvent): boolean => {
    if (atQuery === null || atOptions.length === 0) return false;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setAtIndex((i) => (i + 1) % atOptions.length);
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setAtIndex((i) => (i - 1 + atOptions.length) % atOptions.length);
      return true;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      applyAtSelection(atOptions[atIndex]);
      return true;
    }
    if (e.key === "Escape") {
      setAtQuery(null);
      setAtOptions([]);
      return true;
    }
    return false;
  };

  // ------------------------------------------------------------------
  //  Workspace file browser
  // ------------------------------------------------------------------
  const loadWsFiles = useCallback(async () => {
    try {
      const r = await api.listWorkspaceFiles();
      setWsFiles(r.files ?? []);
      setWsProtected(r.protected ?? []);
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    if (showFiles && isAgentRunning) {
      setPreview(null);
      loadWsFiles();
    }
  }, [showFiles, isAgentRunning, loadWsFiles]);

  /** Show a short-lived hint under the workspace panel header. */
  const showWsNotice = useCallback((msg: string) => {
    setWsNotice(msg);
    if (wsNoticeTimerRef.current) window.clearTimeout(wsNoticeTimerRef.current);
    wsNoticeTimerRef.current = window.setTimeout(() => setWsNotice(""), 4000);
  }, []);

  /**
   * Upload picked files straight into the container workspace (they land in
   * tmp/, the same endpoint chat attachments use) and refresh the file tree.
   * The files are then addressable via @-mention — no prompt is sent.
   */
  const handleWsFilesPicked = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setWsUploading(true);
    setError("");
    try {
      const uploaded: string[] = [];
      for (const f of Array.from(files)) {
        const r = await api.uploadChatFile(f);
        uploaded.push(r.path);
      }
      await loadWsFiles();
      showWsNotice(
        `已上传 ${uploaded.length} 个文件至 ${uploaded[0].split("/")[0] || "工作区"}，可在对话中 @ 引用`
      );
    } catch (e: any) {
      setError(e.message);
    } finally {
      setWsUploading(false);
    }
  };

  const openPreview = async (path: string) => {
    setPreviewLoading(true);
    setPreview({ path, type: "loading", mime: "" });
    try {
      const r = await api.readWorkspaceFile(path);
      setPreview({ path, ...r });
    } catch (e: any) {
      setPreview({ path, type: "error", mime: "", content: e.message });
    } finally {
      setPreviewLoading(false);
    }
  };

  /**
   * 处理一条 present_file 成功信令。auto=true 来自实时 SSE（受 autoPresent 与
   * dismissed 关闭记忆约束）；auto=false 是用户主动点开（总是打开）。渲染本身
   * 完全复用已有的 openPreview → FilesPanel 管线（pptx 走 pptx-wasm）。
   */
  const handlePresent = (p: PresentPayload, auto: boolean) => {
    // 弹出条件：手动点开必弹；自动信令需 autoPresent 开启，且该 path 未被用户
    // 关闭过（focus 信令例外 —— Agent 认为它最重要，值得突破关闭记忆）。
    const dismissed = !!dismissedPresentRef.current[p.path];
    const shouldPop = !auto || (autoPresentRef.current && !(dismissed && !p.focus));

    // focus 信令清除该 path 的关闭记忆（Agent 主动重新交付）。
    if (p.focus && dismissed) {
      setDismissedPresent((d) => {
        if (!(p.path in d)) return d;
        const rest = { ...d };
        delete rest[p.path];
        return rest;
      });
    }

    // 去重 + version 自增（驱动渲染器刷新）+ LRU 上限。函数式更新，SSE 连发
    // 尚未重渲染时也不会互相覆盖。
    setPresentTabs((tabs) => {
      const idx = tabs.findIndex((t) => t.path === p.path);
      const tab: PresentTab = {
        path: p.path,
        title: p.title,
        kind: p.kind,
        note: p.note,
        version: idx >= 0 ? tabs[idx].version + 1 : 1,
      };
      return idx >= 0
        ? tabs.map((t, i) => (i === idx ? tab : t))
        : [...tabs, tab].slice(-PRESENT_TAB_MAX);
    });
    setActivePresent(p.path);

    if (shouldPop) {
      setShowFiles(true);
      void openPreview(p.path);
    }
  };

  /** 用户点标签：切到该文件并加载预览（不受 autoPresent/dismissed 约束）。 */
  const selectPresent = (path: string) => {
    setActivePresent(path);
    setShowFiles(true);
    void openPreview(path);
  };

  /** 用户关标签：移除 + 写入关闭记忆（此后同 path 不再自动弹），并退出该预览。 */
  const closePresent = (path: string) => {
    setPresentTabs((tabs) => tabs.filter((t) => t.path !== path));
    setActivePresent((cur) => {
      if (cur !== path) return cur;
      const rest = presentTabs.filter((t) => t.path !== path);
      return rest.length ? rest[rest.length - 1].path : null;
    });
    setDismissedPresent((d) => ({ ...d, [path]: Date.now() }));
    setPreview((pv) => (pv && pv.path === path ? null : pv));
  };

  // 让只注册一次的 SSE 回调始终调到最新的 handlePresent（读最新 state/refs）。
  useEffect(() => {
    presentHandlerRef.current = handlePresent;
  });

  /** Batch-download the selected workspace paths as one zip (backend packs it). */
  const handleWsDownload = async (paths: string[]) => {
    if (!paths.length) return;
    setWsDownloading(true);
    setError("");
    try {
      await api.downloadWorkspaceFiles(paths);
      showWsNotice(`已下载 ${paths.length} 个路径（zip 压缩包）`);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setWsDownloading(false);
    }
  };

  /**
   * 删除选中的工作区文件/目录（目录连同子树）。两步确认：先常规确认；若选中项
   * 命中平台托管路径（项目 opencode.json、.opencode/），再追加一次强制确认，
   * 用户放行后才带 force=true 请求后端。
   */
  const handleWsDelete = (paths: string[]) => {
    if (!paths.length) return;
    const hit = paths.filter((p) =>
      wsProtected.some((root) => p === root || p.startsWith(root + "/"))
    );

    const runDelete = async (force: boolean) => {
      setWsDeleting(true);
      setError("");
      try {
        const r = await api.deleteWorkspaceFiles(paths, force);
        await loadWsFiles();
        showWsNotice(
          r.failed?.length
            ? `已删除 ${r.deleted.length} 个路径，${r.failed.length} 个失败`
            : `已删除 ${r.deleted.length} 个路径`
        );
      } catch (e: any) {
        setError(e.message);
      } finally {
        setWsDeleting(false);
      }
    };

    modalApi.confirm({
      title: `删除 ${paths.length} 个路径？`,
      content: hit.length
        ? "所选内容将被永久删除（目录连同其中的全部文件），此操作不可恢复。其中包含平台托管路径，下一步需再次确认。"
        : "所选内容将被永久删除（目录连同其中的全部文件），此操作不可恢复。",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => {
        if (!hit.length) return runDelete(false);
        modalApi.confirm({
          title: "包含平台托管路径",
          content: (
            <div style={{ lineHeight: 1.7 }}>
              <div>以下路径由平台托管，删除会导致项目级配置或 skill 丢失：</div>
              <div style={{ margin: "6px 0 0", wordBreak: "break-all" }}>
                {hit.map((p) => (
                  <div key={p} style={{ fontFamily: "ui-monospace, monospace", fontSize: "12px" }}>
                    {p}
                  </div>
                ))}
              </div>
            </div>
          ),
          okText: "强制删除",
          cancelText: "取消",
          okButtonProps: { danger: true },
          onOk: () => runDelete(true),
        });
      },
    });
  };

  /** Append "@path" to the input (from the file panel), then refocus it. */
  const insertAtReference = (path: string) => {
    setInput((prev) => {
      const ref = `@${path}`;
      if (prev.includes(ref)) return prev;
      return prev ? (prev.endsWith(" ") ? prev + ref : prev + " " + ref) : ref;
    });
    inputRef.current?.focus();
  };

  const toggleSkill = (name: string) => {
    setSelectedSkills((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]
    );
  };

  const handleInterrupt = async () => {
    if (!currentSession) return;
    try {
      await api.interruptSession(currentSession.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setIsGenerating(false);
    }
  };

  // ------------------------------------------------------------------
  //  Model switching — writes straight into opencode's session state
  // ------------------------------------------------------------------
  const modelOptions = useMemo(() => {
    const out: { key: string; label: string; ref: ModelRef; baseURL?: string | null }[] = [];
    for (const p of providers?.providers ?? []) {
      for (const m of p.models) {
        out.push({
          key: `${p.id}/${m.id}`,
          label: `${p.name} · ${m.name}`,
          ref: { providerID: p.id, id: m.id },
          baseURL: p.baseURL,
        });
      }
    }
    return out;
  }, [providers]);

  const activeBaseURL = useMemo(
    () => modelOptions.find((o) => o.key === modelKey(model))?.baseURL,
    [modelOptions, model]
  );

  const handleModelChange = async (key: string) => {
    const opt = modelOptions.find((o) => o.key === key);
    if (!opt) return;
    setModel(opt.ref);
    if (!currentSession) return; // applies to the next new session
    try {
      await api.setSessionModel(currentSession.id, opt.ref);
    } catch (e: any) {
      setError(e.message);
    }
  };

  // ------------------------------------------------------------------
  //  Agent switching / permission & question approvals
  // ------------------------------------------------------------------
  const handleAgentChange = async (id: string) => {
    setAgentId(id);
    if (!currentSession) return; // applies to the next new session
    try {
      await api.setSessionAgent(currentSession.id, id);
      setCurrentSession((cur) => (cur ? { ...cur, agent: id } : cur));
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handlePermissionReply = async (requestId: string, reply: OcPermissionReply) => {
    if (!currentSession) return;
    try {
      await api.replyPermission(currentSession.id, requestId, reply);
      setPermissions((prev) => prev.filter((p) => p.id !== requestId));
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleQuestionReply = async (
    sessionId: string,
    requestId: string,
    answers: string[][]
  ) => {
    try {
      // v1 reply endpoint carries no sessionID in its path; sessionId is kept
      // in the signature for call-site stability.
      await api.replyQuestion(sessionId, requestId, answers);
      setQuestions((prev) => prev.filter((q) => q.id !== requestId));
    } catch (e: any) {
      setError(e.message);
    }
  };

  const handleQuestionReject = async (sessionId: string, requestId: string) => {
    try {
      await api.rejectQuestion(sessionId, requestId);
      setQuestions((prev) => prev.filter((q) => q.id !== requestId));
    } catch (e: any) {
      setError(e.message);
    }
  };

  const activePermissions = permissions.filter((p) => p.sessionID === currentSession?.id);
  const activeQuestions = questions.filter((q) => q.sessionID === currentSession?.id);
  // Approval cards are pushed via SSE, but a dropped event (the pump's
  // reconnect gap, a throttled background tab) must not strand the user
  // without a way to answer. Poll lightly while a run is active or while
  // approvals are pending.
  useEffect(() => {
    if (!isGenerating && activeQuestions.length === 0 && activePermissions.length === 0)
      return;
    const t = window.setInterval(refreshPending, 5000);
    return () => window.clearInterval(t);
  }, [isGenerating, activeQuestions.length, activePermissions.length, refreshPending]);
  // Subagents are invoked by the primary agent; only primary agents are
  // selectable as a session's agent.
  const primaryAgents = useMemo(
    () => agents.filter((a) => !a.hidden && a.mode !== "subagent"),
    [agents]
  );
  // Subagents the orchestrator manages — surfaced in the @-menu so the user
  // can @-mention them (sent as prompt.agents).
  const mentionableAgents = useMemo(
    () => agents.filter((a) => !a.hidden && a.mode === "subagent"),
    [agents]
  );
  // Subagents are injected via prompt.agents regardless of which primary
  // agent leads the session, so surface them in the @-menu whenever the
  // runtime exposes any (e.g. oh-my-opencode-slim's orchestrator crew).
  // Gating on a specific agent id (orchestrator) left the menu empty with
  // the default session agent.
  const showAgentMentions = mentionableAgents.length > 0;

  // Slash menu options, filtered locally from the cached command list. Once
  // the query fully reads "agents", the list swaps to the agent picker.
  const slashOptions = useMemo<SlashOption[]>(() => {
    if (slashQuery === null) return [];
    const q = slashQuery.toLowerCase();
    if (q === "agents") {
      return primaryAgents.map((a) => ({
        kind: "agent" as const,
        name: a.id,
        description: a.description,
      }));
    }
    const out: SlashOption[] = [];
    if (q === "" || "agents".startsWith(q)) {
      out.push({ kind: "agentsCmd", name: "agents", description: "查看并切换当前会话的 Agent" });
    }
    for (const c of commands) {
      // opencode registers skills on GET /command with source "skill"; they
      // are knowledge packs, not user-invocable commands, so keep the menu
      // to real commands (and MCP-provided ones).
      if (c.source === "skill") continue;
      if (c.name.toLowerCase().includes(q)) {
        out.push({ kind: "command", name: c.name, description: c.description, source: c.source });
      }
    }
    return out;
  }, [slashQuery, commands, primaryAgents]);

  /** Apply the highlighted menu entry: pick agent / expand /agents / insert "/name ". */
  const applySlashSelection = (index?: number) => {
    if (slashQuery === null) return;
    const opt = slashOptions[index ?? slashIndex];
    if (!opt) return;
    if (opt.kind === "agent") {
      // Agent picker: switch the session and clear the input.
      setInput("");
      setSlashQuery(null);
      handleAgentChange(opt.name);
      return;
    }
    if (opt.kind === "agentsCmd") {
      // Rewrite the input to "/agents" so the agent picker takes over.
      setInput("/agents");
      setSlashQuery("agents");
      setSlashIndex(0);
      requestAnimationFrame(() => {
        const ta = inputRef.current;
        ta?.focus();
        ta?.setSelectionRange(7, 7);
      });
      return;
    }
    // Insert "/name " and let the user append arguments; Enter then dispatches.
    setInput(`/${opt.name} `);
    setSlashQuery(null);
    requestAnimationFrame(() => {
      const ta = inputRef.current;
      ta?.focus();
      const pos = opt.name.length + 2;
      ta?.setSelectionRange(pos, pos);
    });
  };

  /** Keyboard routing while the slash menu is open. Returns true if handled. */
  const handleSlashKeyDown = (e: React.KeyboardEvent): boolean => {
    if (slashQuery === null || slashOptions.length === 0) return false;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSlashIndex((i) => (i + 1) % slashOptions.length);
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setSlashIndex((i) => (i - 1 + slashOptions.length) % slashOptions.length);
      return true;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      applySlashSelection();
      return true;
    }
    if (e.key === "Escape") {
      setSlashQuery(null);
      return true;
    }
    return false;
  };

  const handleViewLogs = async () => {
    try {
      setLogs((await api.getAgentLogs()).logs || "(empty)");
    } catch (e: any) {
      setError(e.message);
    }
  };

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, activePermissions.length, activeQuestions.length]);

  // ------------------------------------------------------------------
  //  Render
  // ------------------------------------------------------------------
  return (
    <div style={styles.container}>
      <div style={{ ...styles.sidebar, width: sidebarW }}>
        <div style={styles.sidebarHeader}>
          <div style={styles.brand}>
            <span style={styles.brandIcon}>🤖</span>
            <span style={styles.brandText}>Agent Platform</span>
          </div>
          <div style={styles.userInfo}>
            <span style={styles.userAvatar}>{username[0]?.toUpperCase()}</span>
            {/* minWidth:0 才允许用户名在按钮变多（管理 / 模板库 / 退出）时收缩省略。 */}
            <span style={{ ...styles.userName, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{username}</span>
            {role === "admin" && onOpenLibrary && (
              <button style={styles.logoutBtn} onClick={onOpenLibrary} title="PPTX 模板库管理（仅管理员）">
                模板库
              </button>
            )}
            {role === "admin" && onOpenKbAccess && (
              <button style={styles.logoutBtn} onClick={onOpenKbAccess} title="知识库权限矩阵（仅管理员）">
                权限
              </button>
            )}
            {role === "admin" && onOpenAdmin && (
              <button style={styles.logoutBtn} onClick={onOpenAdmin} title="Docker 容器管理（仅管理员）">
                管理
              </button>
            )}
            <button style={styles.logoutBtn} onClick={onLogout}>退出</button>
          </div>
        </div>

        <div style={styles.statusPanel}>
          <div style={styles.statusRow}>
            <span
              style={{
                ...styles.statusDot,
                background: isAgentRunning
                  ? "var(--green)"
                  : starting
                  ? "var(--scope-project)"
                  : agentStatus?.status === "stopped"
                  ? "var(--amber)"
                  : "var(--text-3)",
              }}
            />
            <span style={styles.statusText}>
              {busy
                ? busy
                : starting
                ? startupPhaseLabel
                : isAgentRunning
                ? "opencode serve 运行中"
                : agentStatus?.status === "stopped"
                ? "容器已停止"
                : "容器未启动"}
            </span>
          </div>
          {agentStatus?.container_name && (
            <div style={styles.containerName}><CodeOutlined /> {agentStatus.container_name}</div>
          )}
          <div style={styles.statusDetail}>
            状态: {agentStatus?.status || "absent"} | 健康: {agentStatus?.healthy ? "✓" : "✗"}
          </div>

          {runtime && (
            <div style={styles.runtimeBox}>
              <div>
                <span style={styles.runtimeKey}>运行时 </span>
                <span style={styles.runtimeVal}>{runtime.runtime}</span>
              </div>
              <div>
                <span style={styles.runtimeKey}>镜像 </span>
                <span style={styles.runtimeVal}>{runtime.image}</span>
              </div>
              <div>
                <span style={styles.runtimeKey}>工作区 </span>
                <span style={styles.runtimeVal}>{runtime.workdir}</span>
              </div>
              <div>
                <span style={styles.runtimeKey}>配置 </span>
                <span style={styles.runtimeVal}>
                  {runtime.config?.mounted ? "已挂载宿主 opencode.json" : "使用容器默认配置"}
                </span>
              </div>
              {!!runtime.config?.stripped?.length && (
                <div>
                  <span style={styles.runtimeKey}>已剥离 </span>
                  <span style={styles.runtimeVal}>{runtime.config.stripped.join(", ")}</span>
                </div>
              )}
            </div>
          )}

          <div style={styles.statusButtons}>
            {!isAgentRunning ? (
              <button style={styles.startBtn} onClick={handleStartAgent} disabled={!!busy || starting}>
                {starting ? "启动中…" : "启动 Agent"}
              </button>
            ) : (
              <button style={styles.stopBtn} onClick={handleStopAgent} disabled={!!busy}>
                停止 Agent
              </button>
            )}
            <button style={styles.logBtn} onClick={handleViewLogs}>日志</button>
            <button style={styles.logBtn} onClick={() => setShowConfig(true)}>配置管理</button>
          </div>
        </div>

        {/* 快捷入口（D12）：意见反馈 + 心愿墙。两者都是普通用户能力，
            不需要 role === "admin" 判定；管理员专属入口在上方 userInfo 行。 */}
        <div style={styles.quickActionsRow}>
          <button
            style={styles.quickActionButton}
            onClick={() => setShowOpinion(true)}
            title="提交 Bug 或功能建议"
          >
            💬 意见反馈
          </button>
          <button
            style={styles.quickActionButton}
            onClick={onOpenWishes}
            disabled={!onOpenWishes}
            title="看看大家都在期待什么，为心愿助力"
          >
            🌟 心愿墙
          </button>
        </div>

        {/* 知识领域展示区：紧邻上方的"工作区"运行时信息，按领域聚合展示当前
            用户有权访问的知识库。数据来自后端 /api/kb/my-domains，后端已按
            权限（公共隐式放行 / 私有名册）过滤，前端不做鉴权。 */}
        <div style={styles.kbPanel}>
          <div style={styles.kbPanelHeader}>
            <span style={styles.kbPanelTitle}><InboxOutlined /> 知识领域</span>
            <span style={styles.kbCount}>{kbDomains.length}</span>
            <button
              style={styles.kbRefresh}
              title="刷新知识领域列表"
              onClick={loadKbDomains}
            >
              <ReloadOutlined />
            </button>
          </div>
          {kbDomains.length === 0 ? (
            <div style={styles.kbEmpty}>暂无可访问的知识领域</div>
          ) : (
            <div style={styles.kbList}>
              {kbDomains.map((d) => (
                <div key={d.id} style={styles.kbItem}>
                  <div style={kbDomainStyles.cardTitle}>
                    {d.name}
                    <span style={kbDomainStyles.typeBadge(d.key_type === "public")}>
                      {d.key_type === "public" ? "公共" : "私有"}
                    </span>
                  </div>
                  {d.description && <div style={styles.kbDesc}>{d.description}</div>}
                  {d.databases.map((kb) => (
                    <div key={kb.name} style={kbDomainStyles.dbRow}>
                      <div style={styles.kbName}>{kb.name}</div>
                      {kb.description && <div style={styles.kbDesc}>{kb.description}</div>}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>

        <div style={styles.sessionsSection}>
          <div style={styles.sessionsHeader}>
            <span>会话列表</span>
            <button
              style={styles.newSessionBtn}
              title="新建全局会话"
              onClick={() => handleNewSession()}
              disabled={!isAgentRunning}
            >
              +
            </button>
          </div>
          <div style={styles.sessionsList}>
            {globalSessions.length === 0 && (
              <div style={styles.emptySessions}>
                {isAgentRunning ? "点击 + 创建新会话" : "请先启动 Agent"}
              </div>
            )}
            {globalSessions.map((s) => renderSessionItem(s))}

            {/* 项目区：会话按 location.directory 归入项目，行点击展开/收起 */}
            <div style={styles.projectsHeader}>
              <span>项目</span>
              <button
                style={styles.newSessionBtn}
                title="新建项目"
                onClick={openProjectModal}
              >
                +
              </button>
            </div>
            {projects.length === 0 && (
              <div style={styles.emptyProjects}>暂无项目，点击 + 新建或绑定 workspace 目录</div>
            )}
            {projects.map((p) => {
              const expanded = !!expandedProjects[p.id];
              const pSessions = sessionsByProject[p.id] ?? [];
              return (
                <div key={p.id}>
                  <div
                    style={styles.projectRow}
                    title={p.directory}
                    onClick={() => toggleProject(p.id)}
                  >
                    <span style={styles.projectCaret}>{expanded ? <DownOutlined style={{ fontSize: 10 }} /> : <RightOutlined style={{ fontSize: 10 }} />}</span>
                    <span style={styles.sessionIcon}><FolderOutlined /></span>
                    <span style={styles.projectName}>{p.name}</span>
                    <span style={styles.projectCount}>{pSessions.length}</span>
                    <div style={styles.sessionActions}>
                      <button
                        style={styles.sessionActionBtn}
                        title="在项目中新建会话"
                        disabled={!isAgentRunning}
                        onClick={(e) => handleNewProjectSession(p, e)}
                      >
                        <PlusOutlined />
                      </button>
                      <button
                        style={styles.sessionActionBtn}
                        title="移除项目（删除项目会话，保留目录）"
                        disabled={!isAgentRunning}
                        onClick={(e) => handleDeleteProject(p, e)}
                      >
                        <DeleteOutlined />
                      </button>
                    </div>
                  </div>
                  {expanded && pSessions.length === 0 && (
                    <div style={styles.emptyProjectSessions}>暂无会话，点击 + 新建</div>
                  )}
                  {expanded && pSessions.map((s) => renderSessionItem(s, true))}
                </div>
              );
            })}
          </div>
        </div>

        <div style={styles.archPanel}>
          <div style={styles.archTitle}>四层架构</div>
          <div style={styles.archLayer}>
            <span style={styles.archLayerDot("browser")} /> 浏览器层 (React SPA)
          </div>
          <div style={styles.archLayer}>
            <span style={styles.archLayerDot("platform")} /> 平台控制层 (FastAPI 反向代理)
          </div>
          <div style={styles.archLayer}>
            <span style={styles.archLayerDot("container")} /> 容器执行层 (opencode serve)
          </div>
          <div style={styles.archLayer}>
            <span style={styles.archLayerDot("shared")} /> 共享服务层 (Postgres / Redis)
          </div>
        </div>
      </div>

      {/* 会话侧栏右缘调宽手柄：向右拖加宽，双击恢复 300px */}
      <PanelResizer
        title="拖动调整会话侧栏宽度（双击恢复默认）"
        onDelta={(dx) => setSidebarW((w) => clampWidth(w + dx, SIDEBAR_WIDTH))}
        onReset={() => setSidebarW(SIDEBAR_WIDTH.def)}
      />

      <div style={styles.mainArea}>
        {/* 顶栏常驻（原型 .modelbar）：左侧会话标题 + 右侧 SSE 状态 / 工具 / 主题切换。
            模型与 Agent 选择已收纳到 composer 底栏的 chip 菜单。 */}
        <div style={styles.modelBar}>
          <div style={styles.mbLeft}>
            <div style={styles.mbSess}>
              <svg
                style={styles.mbSessIcon}
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              </svg>
              <span style={styles.chipText}>
                {currentSession?.title ||
                  (isAgentRunning ? "会话进行中" : "Agent 未运行")}
              </span>
            </div>
          </div>
          <div style={styles.mbRight}>
            {isAgentRunning && (
              <span style={styles.sseInd} title={sseDown ? "事件流断开，自动重连中" : "实时事件流已连接"}>
                <span
                  className={sseDown ? undefined : "dot-live"}
                  style={{
                    ...styles.dot,
                    background: sseDown ? "var(--red)" : "var(--green)",
                  }}
                />
                {sseDown ? "SSE 重连中" : "SSE 已连接"}
              </span>
            )}
            {isAgentRunning && (
              <>
                <button
                  className="icon-btn"
                  style={styles.iconBtn}
                  onClick={handleSummarize}
                  disabled={!!busy || !!busyLabel || !model}
                  title="让模型总结当前会话（生成摘要消息）"
                >
                  {busyLabel === "生成摘要中…" ? <LoadingOutlined /> : <ThunderboltFilled />}
                </button>
                <button
                  className="icon-btn"
                  style={styles.iconBtn}
                  onClick={handleReloadConfig}
                  disabled={!!busy}
                  title="重载容器内 opencode 配置（来源：opencode /config）"
                >
                  <ReloadOutlined />
                </button>
                <button
                  className="icon-btn"
                  style={styles.iconBtn}
                  onClick={() => setShowFiles((v) => !v)}
                  title={showFiles ? "隐藏工作区文件面板" : "显示工作区文件面板"}
                >
                  <FolderOpenOutlined />
                </button>
              </>
            )}
            <ThemeToggle />
          </div>
        </div>

        {error && (
          <div style={styles.errorBanner}>
            <span>{error}</span>
            <button style={styles.errorClose} onClick={() => setError("")}>×</button>
          </div>
        )}

        {/* P1-4: the stream self-heals, but the user deserves to know that
            replies may lag while it reconnects. */}
        {isAgentRunning && sseDown && (
          <div style={styles.sseDownBanner}>
            ⚠ 实时事件流已断开，正在自动重连…（新消息可能延迟显示）
          </div>
        )}

        <div style={styles.chatBody}>
        <div style={styles.chatColumn}>
        {!isAgentRunning ? (
          <Welcome
            onStart={handleStartAgent}
            disabled={!!busy}
            phase={starting ? agentStatus?.status || "creating" : undefined}
            phaseIdx={startupPhaseIdx}
            elapsedSec={startupElapsedSec}
          />
        ) : !currentSession ? (
          <div style={styles.noSession}>
            <div style={styles.noSessionIcon}>💬</div>
            <p style={styles.noSessionText}>选择一个会话，或创建新会话开始对话</p>
            <button style={styles.newSessionLargeBtn} onClick={() => handleNewSession()}>
              创建新会话
            </button>
          </div>
        ) : (
          <>
            <div style={styles.messagesArea}>
              {/* P1-1: the agent's live task list (todo.updated events). */}
              {todos.length > 0 && <TodoList todos={todos} />}
              {/* 重新生成按钮只挂在最后一条 assistant 回复上（其后没有其它回合）。 */}
              {turns.map((t) => {
                const last = turns[turns.length - 1];
                const regenTargetId =
                  last && last.role === "assistant" && !last.streaming ? last.id : null;
                // 任务一：只有已完成的 assistant 回复可评价（流式中不显示）。
                const canVote =
                  t.role === "assistant" && !t.streaming && !!currentSession;
                return (
                  <TurnView
                    key={t.id}
                    turn={t}
                    username={username}
                    reverted={revertedId === t.id}
                    revertBusy={revertBusy}
                    canRevert={!!currentSession && !isGenerating}
                    onRevert={handleRevert}
                    onUnrevert={handleUnrevert}
                    onFork={t.role === "user" && !isGenerating ? handleFork : undefined}
                    onRegenerate={
                      t.id === regenTargetId && !!currentSession && !isGenerating
                        ? handleRegenerate
                        : undefined
                    }
                    feedbackVerdict={feedbackMap[t.id]}
                    feedbackBusy={feedbackBusyId === t.id}
                    onFeedbackUp={canVote ? handleFeedbackUp : undefined}
                    onFeedbackDown={canVote ? handleFeedbackDown : undefined}
                  />
                );
              })}
              {isGenerating && !turns.some((t) => t.streaming) && (
                <div style={styles.msgAssistant}>
                  <div style={styles.msgAvatarAssistant}>🤖</div>
                  <div style={styles.msgContent}>
                    <div style={styles.msgRole}>
                      opencode <span style={styles.streaming}>思考中…</span>
                    </div>
                  </div>
                </div>
              )}

              {activePermissions.map((p) => (
                <div key={p.id} style={styles.permCard}>
                  <div style={styles.permTitle}>🔑 工具权限请求 · {p.action}</div>
                  {p.resources.map((r, i) => (
                    <div key={i} style={styles.permRes}>{r}</div>
                  ))}
                  {/* P1-5: show the tool's raw input arguments so the user can
                      make an informed allow/reject decision. */}
                  {p.metadata && Object.keys(p.metadata).length > 0 && (
                    <pre style={styles.permMeta}>
                      {JSON.stringify(p.metadata, null, 2)}
                    </pre>
                  )}
                  <div style={styles.permActions}>
                    <button
                      style={styles.permAllowBtn}
                      onClick={() => handlePermissionReply(p.id, "once")}
                    >
                      允许一次
                    </button>
                    <button
                      style={styles.permAlwaysBtn}
                      onClick={() => handlePermissionReply(p.id, "always")}
                    >
                      总是允许
                    </button>
                    <button
                      style={styles.permRejectBtn}
                      onClick={() => handlePermissionReply(p.id, "reject")}
                    >
                      拒绝
                    </button>
                  </div>
                </div>
              ))}

              {activeQuestions.map((q) => (
                <QuestionCard
                  key={q.id}
                  request={q}
                  onSubmit={handleQuestionReply}
                  onReject={handleQuestionReject}
                />
              ))}

              <div ref={bottomRef} />
            </div>

            <div style={styles.inputArea}>
              <div style={styles.composer}>
                {/* 快捷技能：一键启用 PPT / 流程图；启用后参数收进下方标签内部 */}
                <div style={styles.skillQuickRow}>
                  <span style={styles.skillQuickLabel}>
                    <ThunderboltFilled style={{ fontSize: 12 }} /> 快捷技能
                  </span>
                  <Button
                    size="small"
                    type={pptOn ? "primary" : "default"}
                    icon={<AppstoreAddOutlined />}
                    style={styles.quickSkillBtn}
                    onClick={() => setPptModalOpen(true)}
                  >
                    PPT生成
                  </Button>
                  <Button
                    size="small"
                    type={flowOn ? "primary" : "default"}
                    icon={<DeploymentUnitOutlined />}
                    style={styles.quickSkillBtn}
                    onClick={() => setFlowModalOpen(true)}
                  >
                    流程图生成
                  </Button>
                </div>
                {(pptOn || flowOn) && (
                  <div style={styles.skillTagRow}>
                    {pptOn && (
                      <Tooltip
                        title={
                          <span style={{ whiteSpace: "pre-line" }}>
                            {`skill: ${PPT_SKILL}\n${pptDetail}`}
                          </span>
                        }
                      >
                        <Tag
                          color="purple"
                          closable
                          onClose={() => setPptOn(false)}
                          onClick={() => setPptModalOpen(true)}
                          style={styles.skillTag}
                        >
                          PPT生成
                        </Tag>
                      </Tooltip>
                    )}
                    {flowOn && (
                      <Tooltip
                        title={
                          <span style={{ whiteSpace: "pre-line" }}>
                            {`skill: ${FLOW_SKILL[flowDialect]}\n${flowDetail}`}
                          </span>
                        }
                      >
                        <Tag
                          color="cyan"
                          closable
                          onClose={() => setFlowOn(false)}
                          onClick={() => setFlowModalOpen(true)}
                          style={styles.skillTag}
                        >
                          流程图生成
                        </Tag>
                      </Tooltip>
                    )}
                    <span style={styles.skillTagHint}>启用中的技能会随消息一同提交</span>
                  </div>
                )}

                {/* PPT生成：模板 / 配色 / 版式配方三组单选，确认后启用 pptx-generator */}
                <Modal
                  open={pptModalOpen}
                  title="PPT生成 · 选择模板与风格"
                  width={560}
                  okText={pptOn ? "保存" : "启用技能"}
                  cancelText="取消"
                  onOk={() => {
                    setPptOn(true);
                    setPptModalOpen(false);
                  }}
                  onCancel={() => setPptModalOpen(false)}
                  okButtonProps={{ icon: <ThunderboltFilled /> }}
                >
                  <div style={styles.modalPickGroup}>
                    <div style={styles.modalPickTitle}>PPT 模板</div>
                    <Radio.Group
                      value={selTemplate?.id ?? ""}
                      onChange={(e) =>
                        setSelTemplate(
                          e.target.value ? libCatalog.templates.find((t) => t.id === e.target.value) ?? null : null
                        )
                      }
                      style={styles.modalRadioGroup}
                    >
                      <Radio value="" style={styles.modalRadio}>
                        不指定<span style={styles.modalRadioNote}>（由 agent 自行组织结构）</span>
                      </Radio>
                      {libCatalog.templates.map((t) => (
                        <Radio key={t.id} value={t.id} style={styles.modalRadio}>
                          {templateLabel(t)}
                          <span style={styles.modalRadioNote}>
                            （{t.slides} 页 · {t.aspect}）
                          </span>
                        </Radio>
                      ))}
                      {!libCatalog.loading && libCatalog.templates.length === 0 && (
                        <div style={styles.modalRadioNote}>模板库为空，可在「模板库」管理页导入 .pptx</div>
                      )}
                    </Radio.Group>
                  </div>
                  <div style={styles.modalPickGroup}>
                    <div style={styles.modalPickTitle}>配色风格</div>
                    <Radio.Group
                      value={selPaletteId ?? ""}
                      onChange={(e) => setSelPaletteId(e.target.value || null)}
                      style={styles.modalRadioGroup}
                    >
                      <Radio value="" style={styles.modalRadio}>
                        不指定
                      </Radio>
                      {libCatalog.palettes.map((p) => (
                        <Radio key={p.id} value={p.id} style={styles.modalRadio}>
                          {p.name_zh || p.name}
                          <span style={{ display: "inline-flex", gap: "2px", marginLeft: "6px", verticalAlign: "middle" }}>
                            {p.colors.slice(0, 5).map((c) => (
                              <i key={c} style={{ ...styles.libChipSwatch, background: c }} />
                            ))}
                          </span>
                        </Radio>
                      ))}
                    </Radio.Group>
                  </div>
                  <div style={styles.modalPickGroup}>
                    <div style={styles.modalPickTitle}>版式配方</div>
                    <Radio.Group
                      value={selRecipeId ?? ""}
                      onChange={(e) => setSelRecipeId(e.target.value || null)}
                      style={styles.modalRadioGroup}
                    >
                      <Radio value="" style={styles.modalRadio}>
                        不指定
                      </Radio>
                      {libCatalog.recipes.map((r) => (
                        <Radio key={r.id} value={r.id} style={styles.modalRadio}>
                          {r.name_zh || r.name}
                          <span style={styles.modalRadioNote}>（{r.character}）</span>
                        </Radio>
                      ))}
                    </Radio.Group>
                  </div>
                </Modal>

                {/* 流程图生成：mermaid / plantuml 单选 */}
                <Modal
                  open={flowModalOpen}
                  title="流程图生成 · 选择图表语法"
                  width={460}
                  okText={flowOn ? "保存" : "启用技能"}
                  cancelText="取消"
                  onOk={() => {
                    setFlowOn(true);
                    setFlowModalOpen(false);
                  }}
                  onCancel={() => setFlowModalOpen(false)}
                  okButtonProps={{ icon: <ThunderboltFilled /> }}
                >
                  <Radio.Group
                    value={flowDialect}
                    onChange={(e) => setFlowDialect(e.target.value as FlowDialect)}
                    style={{ display: "flex", flexDirection: "column", gap: "10px" }}
                  >
                    <Radio value="mermaid" style={styles.modalRadio}>
                      Mermaid
                      <span style={styles.modalRadioNote}>
                        （pretty-mermaid · 美化后的 Mermaid 源码，产出 .mmd）
                      </span>
                    </Radio>
                    <Radio value="plantuml" style={styles.modalRadio}>
                      PlantUML
                      <span style={styles.modalRadioNote}>（plantuml · 只交付 .puml 源文件，平台内不渲染图片）</span>
                    </Radio>
                  </Radio.Group>
                </Modal>

                {/* 会话重命名（原 window.prompt） */}
                <Modal
                  open={renameTarget !== null}
                  title="重命名会话"
                  width={420}
                  okText="保存"
                  cancelText="取消"
                  onOk={handleRenameConfirm}
                  onCancel={() => setRenameTarget(null)}
                  destroyOnClose
                >
                  <Input
                    autoFocus
                    value={renameValue}
                    placeholder="新的会话标题"
                    maxLength={80}
                    onChange={(e) => setRenameValue(e.target.value)}
                    onPressEnter={handleRenameConfirm}
                  />
                </Modal>

                <div className="input-card" style={styles.inputRowCard}>

                  {/* 本条消息上下文 chips：模板/风格选择（跨消息保持）、显式 skill 与附件。
                      PPT 技能启用后模板/配色/配方数据收进「PPT生成」标签内部，卡内不再重复展示。 */}
                  {(attachments.length > 0 || selectedSkills.length > 0 || (!pptOn && (selTemplate || selPalette || selRecipe))) && (
                    <div style={styles.promptChips}>
                      {!pptOn && selTemplate && (
                        <span
                          style={styles.libChip}
                          title={`共享卷只读挂载：${selTemplate.path}（${selTemplate.slides} 页 · 不会复制进工作区）`}
                        >
                          🎞 <span style={styles.chipText}>{templateLabel(selTemplate)}</span>
                          <button style={styles.chipRemove} onClick={() => setSelTemplate(null)} title="移除模板">
                            <CloseOutlined style={{ fontSize: 9 }} />
                          </button>
                        </span>
                      )}
                      {!pptOn && selPalette && (
                        <span style={styles.libChip} title={selPalette.tips || selPalette.use_cases.join(" / ")}>
                          🎨 <span style={styles.chipText}>{selPalette.name_zh || selPalette.name}</span>
                          <span style={{ display: "inline-flex", gap: "2px" }}>
                            {selPalette.colors.slice(0, 5).map((c) => (
                              <i key={c} style={{ ...styles.libChipSwatch, background: c }} />
                            ))}
                          </span>
                          <button style={styles.chipRemove} onClick={() => setSelPaletteId(null)} title="移除调色板">
                            <CloseOutlined style={{ fontSize: 9 }} />
                          </button>
                        </span>
                      )}
                      {!pptOn && selRecipe && (
                        <span style={styles.libChip} title={`${selRecipe.character} · 适合：${selRecipe.best_for}`}>
                          🧊 <span style={styles.chipText}>{selRecipe.name_zh || selRecipe.name}</span>
                          <button style={styles.chipRemove} onClick={() => setSelRecipeId(null)} title="移除风格配方">
                            <CloseOutlined style={{ fontSize: 9 }} />
                          </button>
                        </span>
                      )}
                      {selectedSkills.map((s) => (
                        <span key={s} style={styles.skillChip} title="本条消息显式指定的 skill">
                          🧩 <span style={styles.chipText}>{s}</span>
                          <button
                            style={styles.chipRemove}
                            onClick={() => toggleSkill(s)}
                            title="移除"
                          >
                            <CloseOutlined style={{ fontSize: 9 }} />
                          </button>
                        </span>
                      ))}
                      {attachments.map((a) => (
                        <span
                          key={a.dataUrl ?? a.path}
                          style={styles.attachChip}
                          title={`${a.path || a.filename} (${Math.ceil(a.size / 1024)}KB)${a.dataUrl ? " · base64 直传" : ""}`}
                        >
                          {a.isImage ? <PictureOutlined style={{ color: "var(--green)" }} /> : <PaperClipOutlined />}{" "}
                          <span style={styles.chipText}>{a.filename}</span>
                          <button
                            style={styles.chipRemove}
                            onClick={() =>
                              setAttachments((prev) =>
                                prev.filter((x) => (x.dataUrl ?? x.path) !== (a.dataUrl ?? a.path))
                              )
                            }
                            title="移除"
                          >
                            <CloseOutlined style={{ fontSize: 9 }} />
                          </button>
                        </span>
                      ))}
                    </div>
                  )}

                <div style={styles.atMenuWrap}>
                  <textarea
                    ref={inputRef}
                    style={styles.textInput}
                    placeholder={
                      uploading
                        ? "上传文件中…"
                        : "输入消息…（/ 执行命令，@ 引用文件，Enter 发送，Shift+Enter 换行）"
                    }
                    value={input}
                    onChange={handleInputChange}
                    onKeyDown={(e) => {
                      if (handleSlashKeyDown(e)) return;
                      if (handleAtKeyDown(e)) return;
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        handleSend();
                      }
                    }}
                    rows={1}
                  />
                  {slashQuery !== null && slashOptions.length > 0 && (
                    <div style={styles.atMenu}>
                      <div style={styles.atMenuHeader}>
                        {slashQuery.toLowerCase() === "agents"
                          ? "选择 Agent · ↑↓ 选择 · Enter 切换 · Esc 关闭"
                          : "命令 · ↑↓ 选择 · Enter/Tab 选中 · Esc 关闭"}
                      </div>
                      {slashOptions.map((o, i) => (
                        <div
                          key={`${o.kind}:${o.name}`}
                          style={{ ...styles.atItem, ...(i === slashIndex ? styles.atItemActive : {}) }}
                          onClick={() => applySlashSelection(i)}
                          onMouseEnter={() => setSlashIndex(i)}
                        >
                          <span style={styles.atItemIcon}>{o.kind === "agent" ? "🤖" : "⌘"}</span>
                          <span style={styles.cmdName}>
                            {o.kind === "agent" ? o.name : `/${o.name}`}
                          </span>
                          {o.description && <span style={styles.cmdDesc}>{o.description}</span>}
                          {o.kind === "command" && o.source && (
                            <span style={styles.cmdSource}>{o.source}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {atQuery !== null && atOptions.length > 0 && (
                    <div style={styles.atMenu}>
                      <div style={styles.atMenuHeader}>
                        {showAgentMentions
                          ? "提及 Agent / 引用文件 · ↑↓ 选择 · Enter/Tab 插入 · Esc 关闭"
                          : "引用工作区文件 · ↑↓ 选择 · Enter/Tab 插入 · Esc 关闭"}
                      </div>
                      {atOptions.map((o, i) => (
                        <div
                          key={o.kind === "agent" ? `a:${o.name}` : `f:${o.path}`}
                          style={{ ...styles.atItem, ...(i === atIndex ? styles.atItemActive : {}) }}
                          onClick={() => applyAtSelection(o)}
                          onMouseEnter={() => setAtIndex(i)}
                        >
                          {o.kind === "agent" ? (
                            <>
                              <span style={styles.atItemIcon}>🤖</span>
                              <span style={styles.atItemAgentName}>{o.name}</span>
                              {o.description && (
                                <span style={styles.atItemAgentDesc}>{o.description}</span>
                              )}
                            </>
                          ) : (
                            <>
                              <span style={styles.atItemIcon}>📄</span>
                              <span style={styles.atItemPath}>{o.path}</span>
                            </>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* 底部工具条（原型 .input-bar）：附件 / Skill / 本条 Agent / 本条模型 / 发送 */}
                <div style={styles.inputBar}>
                  {(agentMenuOpen || modelMenuOpen) && (
                    <div
                      style={styles.skillBackdrop}
                      onClick={() => {
                        setAgentMenuOpen(false);
                        setModelMenuOpen(false);
                      }}
                    />
                  )}
                  <div style={styles.inputTools}>
                    <div style={styles.skillPickerWrap}>
                      <button
                        className="icon-btn"
                        style={styles.iconBtn}
                        onClick={() => {
                          setSkillMenuOpen((v) => !v);
                          setAgentMenuOpen(false);
                          setModelMenuOpen(false);
                          setLibMenu(null);
                        }}
                        title="为这条消息显式指定 skill"
                        disabled={allSkills.length === 0}
                      >
                        {selectedSkills.length > 0 ? <><ProfileOutlined />×{selectedSkills.length}</> : <ProfileOutlined />}
                      </button>
                      {skillMenuOpen && (
                        <>
                          <div style={styles.skillBackdrop} onClick={() => setSkillMenuOpen(false)} />
                          <div style={styles.skillMenu}>
                            <div style={styles.skillMenuHeader}>选择要显式使用的 skill</div>
                            {allSkills.length === 0 && (
                              <div style={styles.skillEmpty}>暂无可用 skill（可在配置面板添加）</div>
                            )}
                            {allSkills.map((s) => (
                              <label key={`${s.scope}-${s.name}`} style={styles.skillItem}>
                                <input
                                  type="checkbox"
                                  checked={selectedSkills.includes(s.name)}
                                  onChange={() => toggleSkill(s.name)}
                                />
                                <span style={styles.skillItemName}>{s.name}</span>
                                <span
                                  style={
                                    s.scope === "project"
                                      ? styles.scopeProject
                                      : s.scope === "builtin"
                                        ? styles.scopeBuiltin
                                        : styles.scopeGlobal
                                  }
                                >
                                  {s.scope === "project" ? "项目" : s.scope === "builtin" ? "内置" : "全局"}
                                </span>
                              </label>
                            ))}
                            {allSkills.length > 0 && (
                              <button style={styles.skillMenuClose} onClick={() => setSkillMenuOpen(false)}>
                                完成{selectedSkills.length > 0 ? `（已选 ${selectedSkills.length}）` : ""}
                              </button>
                            )}
                          </div>
                        </>
                      )}
                    </div>

                    {/* PPTX 模板 / 预定义风格：文件在共享卷里单副本只读挂载，
                        选中项只作为 prompt 前缀注入，不下载也不进工作区。 */}
                    <div style={styles.libPickerWrap}>
                      <button
                        className="icon-btn"
                        style={styles.iconBtn}
                        onClick={() => {
                          setLibMenu((v) => (v === "template" ? null : "template"));
                          setSkillMenuOpen(false);
                          setAgentMenuOpen(false);
                          setModelMenuOpen(false);
                        }}
                        title="选择 PPT 模板（共享库只读挂载，不占用工作区空间）"
                      >
                        {selTemplate ? <AppstoreAddOutlined style={{ color: accentIcon }} /> : <AppstoreAddOutlined />}
                      </button>
                      {libMenu === "template" && (
                        <>
                          <div style={styles.skillBackdrop} onClick={() => setLibMenu(null)} />
                          <TemplatePickerMenu
                            catalog={libCatalog}
                            selectedId={selTemplate?.id ?? null}
                            onPick={setSelTemplate}
                            onClose={() => setLibMenu(null)}
                          />
                        </>
                      )}
                    </div>

                    <div style={styles.libPickerWrap}>
                      <button
                        className="icon-btn"
                        style={styles.iconBtn}
                        onClick={() => {
                          setLibMenu((v) => (v === "style" ? null : "style"));
                          setSkillMenuOpen(false);
                          setAgentMenuOpen(false);
                          setModelMenuOpen(false);
                        }}
                        title="选择预定义风格（调色板 / 组件配方）"
                      >
                        {selPalette || selRecipe ? <PictureOutlined style={{ color: accentIcon }} /> : <PictureOutlined />}
                      </button>
                      {libMenu === "style" && (
                        <>
                          <div style={styles.skillBackdrop} onClick={() => setLibMenu(null)} />
                          <StylePickerMenu
                            catalog={libCatalog}
                            paletteId={selPaletteId}
                            recipeId={selRecipeId}
                            onPickPalette={setSelPaletteId}
                            onPickRecipe={setSelRecipeId}
                            onClose={() => setLibMenu(null)}
                          />
                        </>
                      )}
                    </div>

                    <input
                      ref={fileInputRef}
                      type="file"
                      multiple
                      hidden
                      onChange={(e) => handleFilesPicked(e.target.files)}
                    />
                    <button
                      className="icon-btn"
                      style={styles.iconBtn}
                      onClick={() => fileInputRef.current?.click()}
                      title="附加图片（base64 直传）或上传文件到工作空间"
                      disabled={uploading}
                    >
                      {uploading ? <LoadingOutlined /> : <PaperClipOutlined />}
                    </button>
                  </div>

                  {/* 本条消息 Agent（未选则随会话默认） */}
                  <div style={styles.skillPickerWrap}>
                    <button
                      className="ctx-chip"
                      style={{ ...styles.ctxChip, ...(promptAgent ? styles.ctxChipActive : {}) }}
                      onClick={() => {
                        setAgentMenuOpen((v) => !v);
                        setModelMenuOpen(false);
                        setSkillMenuOpen(false);
                      }}
                      title="选择本条消息使用的 Agent（未选则用会话默认）"
                    >
                      <span style={styles.chipText}><MessageOutlined /> {promptAgent ?? agentId}</span>
                      <span style={styles.ctxChipArrow}><DownOutlined style={{ fontSize: 10 }} /></span>
                    </button>
                    {agentMenuOpen && (
                      <div className="ps-menu" style={styles.psMenu}>
                        <div style={styles.psMenuTitle}>本条消息 Agent · 会话默认：{agentId}</div>
                        <div
                          className="ps-menu-item"
                          style={{ ...styles.psMenuItem, ...(promptAgent ? {} : styles.psMenuItemActive) }}
                          onClick={() => setPromptAgent(undefined)}
                        >
                          会话默认（{agentId}）
                        </div>
                        {primaryAgents.map((a) => (
                          <div
                            key={a.id}
                            className="ps-menu-item"
                            style={{
                              ...styles.psMenuItem,
                              ...(promptAgent === a.id ? styles.psMenuItemActive : {}),
                            }}
                            onClick={() => {
                              setPromptAgent(a.id);
                              setAgentMenuOpen(false);
                            }}
                          >
                            <span style={styles.chipText}>{a.id}</span>
                          </div>
                        ))}
                        {promptAgent && promptAgent !== agentId && (
                          <>
                            <div style={styles.psMenuDivider} />
                            <button
                              className="ps-menu-item"
                              style={styles.psMenuAction}
                              onClick={() => {
                                handleAgentChange(promptAgent);
                                setPromptAgent(undefined);
                                setAgentMenuOpen(false);
                              }}
                            >
                              将「{promptAgent}」设为会话默认 Agent
                            </button>
                          </>
                        )}
                      </div>
                    )}
                  </div>

                  {/* 本条消息模型（未选则随会话默认） */}
                  <div style={styles.skillPickerWrap}>
                    <button
                      className="ctx-chip"
                      style={{ ...styles.ctxChip, ...(promptModel ? styles.ctxChipActive : {}) }}
                      onClick={() => {
                        setModelMenuOpen((v) => !v);
                        setAgentMenuOpen(false);
                        setSkillMenuOpen(false);
                      }}
                      title="选择本条消息使用的模型（未选则用会话默认）"
                    >
                      <span style={styles.chipText}>
                        {modelOptions.find((o) => o.key === modelKey(promptModel ?? model))?.label ??
                          "默认模型"}
                      </span>
                      <span style={styles.ctxChipArrow}><DownOutlined style={{ fontSize: 10 }} /></span>
                    </button>
                    {modelMenuOpen && (
                      <div className="ps-menu" style={styles.psMenu}>
                        <div style={styles.psMenuTitle}>本条消息模型</div>
                        <div
                          className="ps-menu-item"
                          style={{ ...styles.psMenuItem, ...(promptModel ? {} : styles.psMenuItemActive) }}
                          onClick={() => setPromptModel(undefined)}
                        >
                          会话默认（
                          {modelOptions.find((o) => o.key === modelKey(model))?.label ?? "默认"}）
                        </div>
                        {modelOptions.map((o) => (
                          <div
                            key={o.key}
                            className="ps-menu-item"
                            style={{
                              ...styles.psMenuItem,
                              ...(modelKey(promptModel) === o.key ? styles.psMenuItemActive : {}),
                            }}
                            onClick={() => {
                              setPromptModel(o.ref);
                              setModelMenuOpen(false);
                            }}
                          >
                            <span style={styles.chipText}>{o.label}</span>
                          </div>
                        ))}
                        {promptModel && modelKey(promptModel) !== modelKey(model) && (
                          <>
                            <div style={styles.psMenuDivider} />
                            <button
                              className="ps-menu-item"
                              style={styles.psMenuAction}
                              onClick={() => {
                                handleModelChange(modelKey(promptModel));
                                setPromptModel(undefined);
                                setModelMenuOpen(false);
                              }}
                            >
                              设为会话默认模型
                            </button>
                          </>
                        )}
                      </div>
                    )}
                  </div>

                  <div style={styles.ibSpacer} />
                  {isGenerating ? (
                    <button
                      className="send-btn"
                      style={styles.abortBtn}
                      onClick={handleInterrupt}
                      title="停止生成"
                    >
                      <StopOutlined />
                    </button>
                  ) : (
                    <button
                      className="send-btn"
                      style={styles.sendBtn}
                      onClick={handleSend}
                      disabled={!input.trim() && attachments.length === 0}
                      title="发送（Enter）"
                    >
                      <SendOutlined />
                    </button>
                  )}
                </div>
              </div>

              <div style={styles.inputHints}>
                <span>Enter 发送 · Shift+Enter 换行</span>
                <span>/ 执行命令 · @ 引用文件</span>
              </div>
            </div>
          </div>
          </>
        )}
        </div>

        {showFiles && isAgentRunning && (
          <>
            {/* 文件面板左缘调宽手柄：向左拖加宽，双击恢复 320px */}
            <PanelResizer
              title="拖动调整文件面板宽度（双击恢复默认）"
              onDelta={(dx) => setFilesW((w) => clampWidth(w - dx, FILES_WIDTH))}
              onReset={() => setFilesW(FILES_WIDTH.def)}
            />
            <FilesPanel
              width={filesW}
              files={wsFiles}
              preview={preview}
              previewLoading={previewLoading}
              onOpen={openPreview}
              onRefresh={loadWsFiles}
              onInsert={insertAtReference}
              onBack={() => setPreview(null)}
              onClose={() => setShowFiles(false)}
              onUpload={handleWsFilesPicked}
              uploading={wsUploading}
              onDownload={handleWsDownload}
              downloading={wsDownloading}
              onDelete={handleWsDelete}
              deleting={wsDeleting}
              notice={wsNotice}
              presentTabs={presentTabs}
              activePresent={activePresent}
              onSelectPresent={selectPresent}
              onClosePresent={closePresent}
              autoPresent={autoPresent}
              onToggleAutoPresent={() => setAutoPresent((v) => !v)}
            />
          </>
        )}
        </div>
      </div>

      {logs && (
        <div style={styles.modal} onClick={() => setLogs("")}>
          <div style={styles.modalContent} onClick={(e) => e.stopPropagation()}>
            <div style={styles.modalHeader}>
              <span>容器日志 · opencode serve</span>
              <button style={styles.modalClose} onClick={() => setLogs("")}>×</button>
            </div>
            <pre style={styles.modalBody}>{logs}</pre>
          </div>
        </div>
      )}

      {showProjectModal && (
        <div style={styles.modal} onClick={() => setShowProjectModal(false)}>
          <div style={styles.projModalContent} onClick={(e) => e.stopPropagation()}>
            <div style={styles.modalHeader}>
              <span>新建项目</span>
              <button style={styles.modalClose} onClick={() => setShowProjectModal(false)}>×</button>
            </div>
            <div style={styles.projModalBody}>
              <label style={styles.projLabel}>项目目录</label>
              <div style={styles.projRadioRow}>
                <label style={styles.projRadio}>
                  <input
                    type="radio"
                    checked={projMode === "create"}
                    onChange={() => setProjMode("create")}
                  />
                  新建目录（projects/ 下自动生成）
                </label>
                <label style={styles.projRadio}>
                  <input
                    type="radio"
                    checked={projMode === "bind"}
                    onChange={() => setProjMode("bind")}
                  />
                  绑定 workspace 已有目录
                </label>
              </div>
              {projMode === "create" ? (
                <>
                  <label style={styles.projLabel}>项目名称</label>
                  <input
                    style={styles.projInput}
                    value={projName}
                    maxLength={50}
                    placeholder="例如：官网改版"
                    autoFocus
                    onChange={(e) => setProjName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleCreateProject();
                    }}
                  />
                </>
              ) : projDirs.length === 0 ? (
                <div style={styles.projDirEmpty}>
                  暂无可绑定目录（需 Agent 至少启动过，且 workspace 下存在非隐藏子目录）
                </div>
              ) : (
                <div style={styles.projDirList}>
                  {projDirs.map((d) => (
                    <div
                      key={d}
                      style={projDir === d ? styles.projDirItemActive : styles.projDirItem}
                      onClick={() => setProjDir(d)}
                    >
                      📁 {d}
                    </div>
                  ))}
                </div>
              )}
              <div style={styles.projHint}>
                {projMode === "create"
                  ? "将创建目录 projects/<项目名称>，目录名与项目名称一致，创建后不可改名。"
                  : "项目名称自动使用所选目录的名称；目录中的历史会话自动归入该项目。"}
                项目目录即会话工作目录，移除项目不影响目录文件。
              </div>
            </div>
            <div style={styles.projFooter}>
              <button style={styles.projCancelBtn} onClick={() => setShowProjectModal(false)}>
                取消
              </button>
              <button
                style={styles.projSubmitBtn}
                disabled={
                  projBusy ||
                  (projMode === "create" && !projName.trim()) ||
                  (projMode === "bind" && !projDir)
                }
                onClick={handleCreateProject}
              >
                {projBusy ? "创建中…" : "创建"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 任务一：点踩原因收集弹窗（提交成功后关闭；取消则不锁定）。 */}
      {downTargetId && (
        <FeedbackModal
          busy={feedbackBusyId === downTargetId}
          onCancel={() => setDownTargetId(null)}
          onSubmit={handleFeedbackDownSubmit}
        />
      )}

      {showConfig && <ConfigPanel onClose={() => setShowConfig(false)} />}

      {/* 意见反馈弹窗（F2/F3）：bug 提交即关闭；feature 提交后原地切到
          「转心愿」引导态，发布成功后可询问跳转到心愿墙。 */}
      <OpinionFeedbackModal
        open={showOpinion}
        onClose={() => setShowOpinion(false)}
        onNavigate={(p) => {
          if (p === "wishes") onOpenWishes?.();
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
//  Workspace file browser panel
// ---------------------------------------------------------------------------

type WsFile = { path: string; type: "file" | "dir"; size: number };
type PreviewState = { path: string; type: string; mime: string; content?: string; base64?: string };

type TreeNode = WsFile & { name: string; children: TreeNode[] };

/** Flat find(1) output → nested tree, dirs first then files, each sorted. */
function buildTree(files: WsFile[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", type: "dir", size: 0, children: [] };
  const dirs = new Map<string, TreeNode>([["", root]]);
  const ensureDir = (path: string): TreeNode => {
    const hit = dirs.get(path);
    if (hit) return hit;
    const parts = path.split("/");
    const name = parts.pop()!;
    const parent = ensureDir(parts.join("/"));
    const node: TreeNode = { name, path, type: "dir", size: 0, children: [] };
    parent.children.push(node);
    dirs.set(path, node);
    return node;
  };
  for (const f of files) {
    if (f.type === "dir") {
      ensureDir(f.path);
      continue;
    }
    const parts = f.path.split("/");
    const name = parts.pop()!;
    ensureDir(parts.join("/")).children.push({
      name, path: f.path, type: "file", size: f.size, children: [],
    });
  }
  const sortRec = (n: TreeNode) => {
    n.children.sort((a, b) =>
      a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1
    );
    n.children.forEach(sortRec);
  };
  sortRec(root);
  return root.children;
}

/**
 * 勾选一个目录即覆盖其整棵子树，因此把已被选中祖先包含的路径剔除，避免同一
 * 文件在打包下载时被写入两次、或在删除时被重复请求。
 */
function pruneCoveredPaths(selected: Set<string>): string[] {
  const all = Array.from(selected);
  return all.filter((p) => !all.some((q) => q !== p && p.startsWith(q + "/")));
}

const fmtSize = (n: number) =>
  n < 1024 ? `${n}B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)}K` : `${(n / 1024 / 1024).toFixed(1)}M`;

function TreeRow({
  depth, icon, label, onClick, children, checkbox,
}: {
  depth: number; icon: string; label: string;
  onClick?: () => void; children?: React.ReactNode;
  checkbox?: { checked: boolean; onChange: (checked: boolean) => void };
}) {
  const [hover, setHover] = useState(false);
  return (
    <div
      style={{
        ...styles.treeRow,
        paddingLeft: `${8 + depth * 14}px`,
        ...(hover ? styles.treeRowHover : {}),
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={onClick}
    >
      {checkbox && (
        <input
          type="checkbox"
          style={{ margin: 0, flexShrink: 0, cursor: "pointer" }}
          checked={checkbox.checked}
          title="勾选后可批量下载"
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => checkbox.onChange(e.target.checked)}
        />
      )}
      <span style={styles.atItemIcon}>{icon}</span>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
      {children}
    </div>
  );
}

function TreeView({
  nodes, depth, collapsed, onToggle, onOpenFile, onInsert, activePath,
  isSelected, onToggleSelect,
}: {
  nodes: TreeNode[];
  depth: number;
  collapsed: Set<string>;
  onToggle: (path: string) => void;
  onOpenFile: (path: string) => void;
  onInsert: (path: string) => void;
  activePath?: string;
  isSelected: (path: string) => boolean;
  onToggleSelect: (path: string, checked: boolean) => void;
}) {
  return (
    <>
      {nodes.map((n) =>
        n.type === "dir" ? (
          <div key={`d-${n.path}`}>
            <TreeRow
              depth={depth}
              icon={collapsed.has(n.path) ? "📁" : "📂"}
              label={n.name}
              onClick={() => onToggle(n.path)}
              checkbox={{
                checked: isSelected(n.path),
                onChange: (c) => onToggleSelect(n.path, c),
              }}
            />
            {!collapsed.has(n.path) && (
              <TreeView
                nodes={n.children}
                depth={depth + 1}
                collapsed={collapsed}
                onToggle={onToggle}
                onOpenFile={onOpenFile}
                onInsert={onInsert}
                activePath={activePath}
                isSelected={isSelected}
                onToggleSelect={onToggleSelect}
              />
            )}
          </div>
        ) : (
          <TreeRow
            key={`f-${n.path}`}
            depth={depth}
            icon="📄"
            label={n.name}
            onClick={() => onOpenFile(n.path)}
            checkbox={{
              checked: isSelected(n.path),
              onChange: (c) => onToggleSelect(n.path, c),
            }}
          >
            {activePath === n.path && <span style={{ fontSize: "10px", color: "var(--scope-project)" }}>预览中</span>}
            <span style={styles.treeFileSize}>{fmtSize(n.size)}</span>
            <button
              style={styles.previewBack}
              title="插入 @ 引用到输入框"
              onClick={(e) => {
                e.stopPropagation();
                onInsert(n.path);
              }}
            >
              @
            </button>
          </TreeRow>
        )
      )}
    </>
  );
}

/** Minimal markdown rendering for the preview pane (headings/lists/code/bold). */
function MiniMarkdown({ src }: { src: string }) {
  const out: React.ReactNode[] = [];
  let key = 0;
  let inCode = false;
  let codeBuf: string[] = [];

  const inline = (s: string): React.ReactNode => {
    const parts: React.ReactNode[] = [];
    const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(s))) {
      if (m.index > last) parts.push(s.slice(last, m.index));
      if (m[0].startsWith("**")) {
        parts.push(<strong key={key++} style={styles.mdStrong}>{m[0].slice(2, -2)}</strong>);
      } else {
        parts.push(<code key={key++} style={styles.mdCode}>{m[0].slice(1, -1)}</code>);
      }
      last = m.index + m[0].length;
    }
    if (last < s.length) parts.push(s.slice(last));
    return parts;
  };

  for (const line of src.split("\n")) {
    if (line.trim().startsWith("```")) {
      if (inCode && codeBuf.length) {
        out.push(<pre key={key++} style={styles.mdCodeBlock}>{codeBuf.join("\n")}</pre>);
        codeBuf = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      codeBuf.push(line);
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      out.push(
        <div key={key++} style={h[1].length <= 1 ? styles.mdTitle : styles.mdH2}>
          {inline(h[2])}
        </div>
      );
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      out.push(
        <div key={key++} style={styles.mdLi}>{inline(line.replace(/^\s*[-*+]\s+/, ""))}</div>
      );
      continue;
    }
    if (!line.trim()) {
      out.push(<div key={key++} style={{ height: "8px" }} />);
      continue;
    }
    out.push(<p key={key++} style={styles.mdP}>{inline(line)}</p>);
  }
  if (inCode && codeBuf.length) {
    out.push(<pre key={key++} style={styles.mdCodeBlock}>{codeBuf.join("\n")}</pre>);
  }
  return <>{out}</>;
}

/**
 * Split markitdown's PPTX extraction into slides. markitdown separates decks
 * with "<!-- Slide number: N -->" comments; within a slide the first
 * non-empty line is the title (markitdown may or may not prefix it with "#"),
 * the rest is body text. Tolerant by design — the exact per-shape output
 * varies between markitdown versions.
 */
function parsePptxSlides(src: string): { title: string; body: string }[] {
  const slides: { title: string; body: string }[] = [];
  const parts = src.split(/<!--\s*Slide number:\s*\d+\s*-->/);
  for (const raw of parts) {
    const lines = raw.split("\n");
    let i = 0;
    while (i < lines.length && !lines[i].trim()) i++;
    if (i >= lines.length) continue;
    const title = lines[i].replace(/^#+\s*/, "").trim() || "(无标题)";
    const body = lines.slice(i + 1).join("\n").trim();
    slides.push({ title, body });
  }
  return slides;
}

/**
 * pptx-wasm 的 React 渲染组件按需加载（JS + wasm 模块约 330KB gzip），只有
 * 用户真的打开 pptx 预览时才拉取这个 chunk，不影响主包体积。
 */
const PresentationViewer = lazy(() =>
  import("pptx-wasm/react").then((m) => ({ default: m.PresentationViewer }))
);

/**
 * PPTX preview: high-fidelity client-side rendering with pptx-wasm. The
 * backend serves the deck's raw bytes (api.readWorkspaceFileRaw →
 * GET /workspace/file-raw) and pptx-wasm parses and paints the deck on a
 * canvas in the browser — shapes, tables, images and CJK text render as
 * authored. The markitdown slide outline (type:"pptx" content) stays as the
 * fallback view (raw fetch or renderer failure) and as a deliberate toggle
 * for quick text skimming.
 */
function PptxSlidePreview({
  path, content, onDownload, downloading, onInsert,
}: {
  path: string;
  content: string;
  onDownload: (paths: string[]) => void;
  downloading: boolean;
  onInsert: (path: string) => void;
}) {
  const slides = useMemo(() => parsePptxSlides(content), [content]);

  // Raw deck bytes for the renderer (null until fetched); rawError explains a
  // failed fetch or a failed parse, in which case the outline takes over.
  const [raw, setRaw] = useState<ArrayBuffer | null>(null);
  const [rawError, setRawError] = useState("");
  const [page, setPage] = useState({ count: 0, current: 0 });
  const [zoom, setZoom] = useState(1);
  const viewer = useRef<PresentationViewerHandle>(null);

  useEffect(() => {
    let alive = true;
    setRaw(null);
    setRawError("");
    api.readWorkspaceFileRaw(path)
      .then((buf) => { if (alive) setRaw(buf); })
      .catch((e) => { if (alive) setRawError(e.message || String(e)); });
    return () => { alive = false; };
  }, [path]);

  const canRender = raw !== null && !rawError;
  const total = page.count || slides.length;

  const outlineView = slides.length === 0 ? (
    // Extraction produced nothing slide-shaped — show the raw text.
    <MiniMarkdown src={content} />
  ) : (
    <div style={styles.pptxSlides}>
      {slides.map((s, i) => (
        <div key={i} style={styles.pptxSlide}>
          <div style={styles.pptxSlideNum}>{i + 1}</div>
          <div style={styles.pptxSlideInner}>
            <div style={styles.pptxSlideTitle}>{s.title}</div>
            {s.body.split("\n").map((line, j) => {
              const t = line.trim();
              if (!t) return <div key={j} style={{ height: 6 }} />;
              // Markdown table rows (markitdown renders pptx tables as
              // pipe tables) pass through monospaced.
              if (t.startsWith("|")) {
                return <div key={j} style={styles.pptxSlideTable}>{line}</div>;
              }
              // Strip stray "#" prefixes (e.g. markitdown's "### Notes:").
              const stripped = t.replace(/^#{1,6}\s+/, "");
              const bullet = stripped.match(/^([-*•]|\d+[.)])\s+(.*)$/);
              if (bullet) {
                return (
                  <div key={j} style={styles.pptxSlideLine}>• {bullet[2]}</div>
                );
              }
              return <div key={j} style={styles.pptxSlideLine}>{stripped}</div>;
            })}
          </div>
        </div>
      ))}
    </div>
  );

  return (
    <>
      {canRender ? (
        <Suspense fallback={<div style={styles.pptxRenderHint}>渲染引擎加载中…</div>}>
        <div style={styles.pptxPreviewShell}>
          <aside style={styles.pptxThumbRail} aria-label="幻灯片缩略图导航">
            {Array.from({ length: total }, (_, i) => (
              <button
                key={i}
                type="button"
                style={{ ...styles.pptxThumb, ...(i === page.current ? styles.pptxThumbActive : {}) }}
                onClick={() => {
                  setPage((p) => ({ ...p, current: i }));
                  viewer.current?.goTo(i);
                }}
                aria-label={`第 ${i + 1} 页`}
                aria-current={i === page.current ? "page" : undefined}
              >
                <PresentationViewer
                  src={raw}
                  slide={i}
                  fit="contain"
                  keyboard={false}
                  height="100%"
                  style={styles.pptxThumbViewer}
                />
                <span>{i + 1}</span>
              </button>
            ))}
          </aside>
          <main style={styles.pptxStage}>
            <PresentationViewer
              ref={viewer}
              src={raw}
              slide={page.current}
              height="100%"
              fit="contain"
              zoom={zoom}
              keyboard={false}
              style={styles.pptxThumbViewer}
              onLoad={(info) => setPage({ count: info.slideCount, current: 0 })}
              onSlideChange={(i) => setPage((p) => ({ ...p, current: i }))}
              onError={(e) => {
                // Renderer could not parse the deck — fall back to outline.
                setRawError(`渲染失败：${e.message}`);
              }}
            />
          </main>
          <footer style={styles.pptxFooter}>
            <span>{page.current + 1}/{total}</span>
            <button style={styles.pptxModeBtn} onClick={() => setZoom((z) => Math.max(0.6, z - 0.1))} aria-label="缩小">−</button>
            <span>{Math.round(zoom * 100)}%</span>
            <button style={styles.pptxModeBtn} onClick={() => setZoom((z) => Math.min(1.8, z + 0.1))} aria-label="放大">＋</button>
            <button style={styles.pptxModeBtn} onClick={() => onInsert(`${path} 第${page.current + 1}页`)}>引用当前页</button>
            <button style={styles.pptxModeBtn} onClick={() => onDownload([path])} disabled={downloading}>
              {downloading ? "打包中…" : "⬇ 下载"}
            </button>
          </footer>
        </div>
        </Suspense>
      ) : raw === null && !rawError ? (
        <div style={styles.pptxRenderHint}>正在获取演示文稿…</div>
      ) : (
        <>
          <div style={styles.pptxBar}>
            <span>📊 共 {total} 张幻灯片 · 文本大纲预览{rawError ? `（${rawError}）` : ""}</span>
          </div>
          {outlineView}
        </>
      )}
    </>
  );
}

function FilesPanel({
  files, preview, previewLoading, onOpen, onRefresh, onInsert, onBack, onClose,
  onUpload, uploading, notice, onDownload, downloading, onDelete, deleting, width,
  presentTabs, activePresent, onSelectPresent, onClosePresent,
  autoPresent, onToggleAutoPresent,
}: {
  files: WsFile[];
  preview: PreviewState | null;
  previewLoading: boolean;
  onOpen: (path: string) => void;
  onRefresh: () => void;
  onInsert: (path: string) => void;
  onBack: () => void;
  onClose: () => void;
  onUpload: (files: FileList | null) => void;
  uploading: boolean;
  notice?: string;
  onDownload: (paths: string[]) => void;
  downloading: boolean;
  /** 删除选中路径（含目录树）；确认弹窗在父组件里，这里只负责发起。 */
  onDelete: (paths: string[]) => void;
  deleting: boolean;
  /** 面板宽度（px）——由父组件的拖拽手柄控制，默认用样式表里的 320px。 */
  width?: number;
  /** present_file 交付的多标签（去重后的产物文件）。 */
  presentTabs: PresentTab[];
  activePresent: string | null;
  onSelectPresent: (path: string) => void;
  onClosePresent: (path: string) => void;
  /** 是否允许 Agent 交付时自动弹出预览。 */
  autoPresent: boolean;
  onToggleAutoPresent: () => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  const tree = useMemo(() => buildTree(files), [files]);
  const fileCount = useMemo(() => files.filter((f) => f.type === "file").length, [files]);

  // A path counts as selected when itself or any ancestor directory is
  // checked, so ticking a dir visually covers its whole subtree.
  const isSelected = (path: string) => {
    let p = path;
    for (;;) {
      if (selected.has(p)) return true;
      const i = p.lastIndexOf("/");
      if (i < 0) return false;
      p = p.slice(0, i);
    }
  };
  const toggleSelect = (path: string, checked: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(path);
      else next.delete(path);
      return next;
    });
  const rootPaths = tree.map((n) => n.path);
  const allSelected =
    rootPaths.length > 0 && rootPaths.every((p) => selected.has(p));

  // 树刷新后剔除已不存在的选中项：删除完成后原路径已消失，留着会让按钮一直
  // 显示可点并对着幽灵路径发请求。
  useEffect(() => {
    setSelected((prev) => {
      if (prev.size === 0) return prev;
      const alive = new Set(files.map((f) => f.path));
      const next = new Set(Array.from(prev).filter((p) => alive.has(p)));
      return next.size === prev.size ? prev : next;
    });
  }, [files]);

  // 下载与删除共用同一份去重后的选中项。
  const actionPaths = useMemo(() => pruneCoveredPaths(selected), [selected]);
  const handleDownload = () => onDownload(actionPaths);

  const baseName = (p: string) => p.split("/").pop() || p;

  return (
    <div style={width != null ? { ...styles.filesPanel, width } : styles.filesPanel}>
      {/* Hidden picker feeding the workspace upload endpoint. */}
      <input
        ref={uploadInputRef}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          onUpload(e.target.files);
          e.target.value = ""; // allow re-picking the same file
        }}
      />
      <div style={styles.filesPanelHeader}>
        {preview ? (
          <>
            <button style={styles.previewBack} onClick={onBack} title="返回文件列表">← 返回</button>
            <span
              style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              title={preview.path}
            >
              {baseName(preview.path)}
            </span>
            <button style={styles.previewBack} title="插入 @ 引用到输入框" onClick={() => onInsert(preview.path)}>
              @ 引用
            </button>
          </>
        ) : (
          <>
            <span style={{ flex: 1 }}>📁 工作区（{fileCount} 个文件）</span>
            <button
              style={styles.previewBack}
              onClick={() => setSelected(allSelected ? new Set() : new Set(rootPaths))}
              title={allSelected ? "清空选择" : "全选顶层条目"}
              disabled={rootPaths.length === 0}
            >
              {allSelected ? "清空" : "全选"}
            </button>
            <button
              style={{ ...styles.wsUploadBtn, ...((downloading || actionPaths.length === 0) ? { opacity: 0.6 } : {}) }}
              onClick={handleDownload}
              title="打包下载选中的文件/目录（zip）"
              disabled={downloading || actionPaths.length === 0}
            >
              {downloading ? "⏳ 打包中…" : `⬇ 下载${actionPaths.length ? `(${actionPaths.length})` : ""}`}
            </button>
            <button
              style={{ ...styles.wsDeleteBtn, ...((deleting || actionPaths.length === 0) ? { opacity: 0.6 } : {}) }}
              onClick={() => onDelete(actionPaths)}
              title="删除选中的文件/目录（目录连同其中全部内容，不可恢复）"
              disabled={deleting || actionPaths.length === 0}
            >
              {deleting ? "⏳ 删除中…" : `🗑 删除${actionPaths.length ? `(${actionPaths.length})` : ""}`}
            </button>
            <button
              style={{ ...styles.wsUploadBtn, ...(uploading ? { opacity: 0.6 } : {}) }}
              onClick={() => uploadInputRef.current?.click()}
              title="上传文件到后端工作空间（存入 tmp/）"
              disabled={uploading}
            >
              {uploading ? "⏳ 上传中…" : "⬆ 上传"}
            </button>
            <button style={styles.previewBack} onClick={onRefresh} title="刷新">刷新</button>
            <button style={styles.previewBack} onClick={onClose} title="关闭面板">×</button>
          </>
        )}
      </div>

      {presentTabs.length > 0 && (
        <div style={styles.presentStrip}>
          {presentTabs.map((t) => (
            <div
              key={t.path}
              style={
                t.path === activePresent
                  ? { ...styles.presentTab, ...styles.presentTabActive }
                  : styles.presentTab
              }
              onClick={() => onSelectPresent(t.path)}
              title={`${t.path}${t.note ? ` · ${t.note}` : ""}`}
            >
              <span style={styles.presentTabLabel}>{t.title}</span>
              <button
                style={styles.presentTabClose}
                title="关闭此交付标签（后续不再自动弹出该文件）"
                onClick={(e) => {
                  e.stopPropagation();
                  onClosePresent(t.path);
                }}
              >
                ×
              </button>
            </div>
          ))}
          <button
            style={styles.presentAutoBtn}
            onClick={onToggleAutoPresent}
            title={autoPresent ? "点击关闭：Agent 交付时不再自动弹出预览" : "点击开启：Agent 交付时自动弹出预览"}
          >
            {autoPresent ? "自动弹出：开" : "自动弹出：关"}
          </button>
        </div>
      )}

      {!preview && notice && <div style={styles.wsNotice}>✓ {notice}</div>}

      {preview ? (
        preview.type === "loading" ? (
          <div style={styles.previewBody}>加载中…</div>
        ) : preview.type === "error" ? (
          <div style={styles.previewBody}>
            <div style={styles.turnError}>⚠ {preview.content}</div>
          </div>
        ) : preview.type === "text" && preview.mime === "text/html" ? (
          // Static sandbox: no scripts, no same-origin access.
          <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
            <iframe
              style={{ ...styles.previewFrame, flex: 1 }}
              srcDoc={preview.content}
              sandbox=""
              title={preview.path}
            />
          </div>
        ) : (
          <div style={styles.previewBody}>
            {previewLoading && <div>加载中…</div>}
            {preview.type === "text" && preview.mime === "text/markdown" && (
              <MiniMarkdown src={preview.content || ""} />
            )}
            {preview.type === "text" && preview.mime !== "text/markdown" && (
              <pre style={styles.previewPre}>{preview.content}</pre>
            )}
            {preview.type === "image" && (
              <img
                style={styles.previewImg}
                src={`data:${preview.mime};base64,${preview.base64}`}
                alt={preview.path}
              />
            )}
            {preview.type === "pptx" && (
              <PptxSlidePreview
                path={preview.path}
                content={preview.content || ""}
                onDownload={onDownload}
                downloading={downloading}
                onInsert={onInsert}
              />
            )}
            {preview.type === "binary" && (
              <div style={styles.previewBinary}>
                二进制文件（{preview.mime}），无法在此预览。
                <br />
                可通过 @ 引用让 Agent 处理。
              </div>
            )}
          </div>
        )
      ) : (
        <div style={styles.filesPanelBody}>
          {files.length === 0 ? (
            <div style={{ padding: "16px 12px", fontSize: "12px", color: "var(--text-3)" }}>
              工作区为空（或读取失败，请刷新重试）
            </div>
          ) : (
            <TreeView
              nodes={tree}
              depth={0}
              collapsed={collapsed}
              onToggle={toggle}
              onOpenFile={onOpen}
              onInsert={onInsert}
              isSelected={isSelected}
              onToggleSelect={toggleSelect}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
//  Sub-components
// ---------------------------------------------------------------------------

/** "/workspace/foo" → "foo" (display only — paths are container-absolute). */
const stripWorkspace = (p: string): string =>
  p.startsWith("/workspace/") ? p.slice(11) : p;

/** Diff line → syntax highlight style (adds/dels/hunk/headers). */
const diffLineStyle = (line: string): CSSProperties | undefined =>
  line.startsWith("+") && !line.startsWith("+++")
    ? styles.diffLineAdd
    : line.startsWith("-") && !line.startsWith("---")
    ? styles.diffLineDel
    : line.startsWith("@@")
    ? styles.diffLineHunk
    : line.startsWith("Index:") || line.startsWith("===")
    ? styles.diffLineMeta
    : undefined;

function TurnView({
  turn,
  username,
  reverted,
  revertBusy,
  canRevert,
  onRevert,
  onUnrevert,
  onFork,
  onRegenerate,
  feedbackVerdict,
  feedbackBusy,
  onFeedbackUp,
  onFeedbackDown,
}: {
  turn: Turn;
  username: string;
  reverted: boolean;
  revertBusy: boolean;
  canRevert: boolean;
  onRevert: (messageId: string) => void;
  onUnrevert: () => void;
  onFork?: (messageId?: string) => void;
  /** 传入即在该 assistant 回复的角色行显示"重新生成"按钮。 */
  onRegenerate?: (assistantId: string) => void;
  /** 任务一：已锁定的投票；undefined 表示未投票。 */
  feedbackVerdict?: "up" | "down";
  feedbackBusy?: boolean;
  /** 两者同时传入才在该 assistant 回复下方渲染反馈条。 */
  onFeedbackUp?: (assistantId: string) => void;
  onFeedbackDown?: (assistantId: string) => void;
}) {
  if (turn.role === "system") {
    const text = turn.blocks.map((b) => ("text" in b ? b.text : "")).join(" ");
    return (
      <div style={styles.msgSystem}>
        <span style={styles.systemNote}>{text}</span>
      </div>
    );
  }

  const isUser = turn.role === "user";
  // 用户消息可能带「请使用 skill: …」/ 约束前缀（见 handleSend），
  // 展示时解析为技能标签并从正文剥离；无命中标记时原样返回。
  const firstTextBlock = isUser ? turn.blocks.find((b) => b.kind === "text") : undefined;
  const skillParsed =
    firstTextBlock && firstTextBlock.kind === "text" ? splitSkillMarks(firstTextBlock.text) : null;
  const skillTags = skillParsed?.tags ?? [];
  const stripped =
    skillParsed && skillParsed.tags.length > 0 && firstTextBlock
      ? { id: firstTextBlock.id, text: skillParsed.rest }
      : { id: "", text: null as string | null };
  return (
    <div style={isUser ? styles.msgUser : styles.msgAssistant}>
      <div style={isUser ? styles.msgAvatarUser : styles.msgAvatarAssistant}>
        {isUser ? username[0]?.toUpperCase() : "🤖"}
      </div>
      <div style={styles.msgContent}>
        <div style={styles.msgRole}>
          <span>{isUser ? "You" : turn.agent ? `opencode · ${turn.agent}` : "opencode (容器内)"}</span>
          {turn.model && (
            <span style={styles.modelTag}>
              {turn.model.providerID}/{turn.model.id}
            </span>
          )}
          {turn.streaming && <span style={styles.streaming}>streaming…</span>}
          {isUser && onFork && (
            <button
              style={styles.forkBtn}
              title="从此处分叉：复制到新会话（保留到本回合为止的历史）"
              onClick={() => onFork(turn.id)}
            >
              ⑂ 分叉
            </button>
          )}
          {!isUser && onRegenerate && (
            <button
              style={styles.regenBtn}
              title="重新生成：回退本回合并按原输入重新请求（替换当前回复）"
              onClick={() => onRegenerate(turn.id)}
            >
              ↻ 重新生成
            </button>
          )}
        </div>

        {/* @-mentioned agents on this user message */}
        {isUser && turn.agents && turn.agents.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "6px", justifyContent: "flex-end" }}>
            {turn.agents.map((a) => (
              <span key={a} style={styles.agentChip}>🤖 @{a}</span>
            ))}
          </div>
        )}

        {/* 技能标签：提交数据（模板/风格/图表类型）隐藏在标签内，界面只展示标签 */}
        {isUser && skillTags.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "6px", justifyContent: "flex-end" }}>
            {skillTags.map((t) => (
              <Tooltip key={`${t.label}:${t.detail}`} title={<span style={{ whiteSpace: "pre-line" }}>{t.detail}</span>}>
                <span style={styles.agentChip}>⚡ {t.label}</span>
              </Tooltip>
            ))}
          </div>
        )}

        {turn.blocks.map((b) => {
          // 用户消息的首个文本块可能被注入过 skill / 约束前缀，展示时剥离。
          if (isUser && b.kind === "text" && stripped.text !== null && b.id === stripped.id) {
            return <BlockView key={b.id} block={{ ...b, text: stripped.text }} streaming={!!turn.streaming} />;
          }
          return <BlockView key={b.id} block={b} streaming={!!turn.streaming} />;
        })}

        {/* P1-1: per-round file diffs with revert / unrevert */}
        {isUser && turn.diffs && turn.diffs.length > 0 && (
          <DiffCard
            diffs={turn.diffs}
            reverted={reverted}
            busy={revertBusy}
            canAct={canRevert}
            onRevert={onRevert ? () => onRevert(turn.id) : undefined}
            onUnrevert={onUnrevert}
          />
        )}

        {turn.error && <div style={styles.turnError}>⚠ {turn.error}</div>}

        {/* 任务一：点赞 / 点踩 —— 投票后锁定并显示"感谢反馈"。 */}
        {!isUser && onFeedbackUp && onFeedbackDown && (
          <FeedbackBar
            verdict={feedbackVerdict}
            busy={feedbackBusy}
            onUp={() => onFeedbackUp(turn.id)}
            onDown={() => onFeedbackDown(turn.id)}
          />
        )}

        {(turn.cost != null || turn.tokens) && (
          <div style={styles.turnMeta}>
            {turn.tokens &&
              `tokens ↑${turn.tokens.input ?? 0} ↓${turn.tokens.output ?? 0}`}
            {turn.cost != null && `  ·  $${turn.cost.toFixed(4)}`}
          </div>
        )}
      </div>
    </div>
  );
}

/** P1-1: per-round diff card — expandable file rows with syntax-highlighted
 *  patches, plus revert / unrevert actions for the whole round. */
function DiffCard({
  diffs,
  reverted,
  busy,
  canAct,
  onRevert,
  onUnrevert,
}: {
  diffs: FileDiff[];
  reverted: boolean;
  busy: boolean;
  canAct: boolean;
  onRevert?: () => void;
  onUnrevert?: () => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const totalAdd = diffs.reduce((s, d) => s + (d.additions ?? 0), 0);
  const totalDel = diffs.reduce((s, d) => s + (d.deletions ?? 0), 0);
  return (
    <div style={styles.diffCard}>
      <div style={styles.diffCardTitle}>
        <span>📝 本回合文件变更 · {diffs.length} 个文件</span>
        <span>
          <span style={styles.diffStatAdd}>+{totalAdd}</span>{" "}
          <span style={styles.diffStatDel}>−{totalDel}</span>
        </span>
      </div>
      {diffs.map((d) => {
        const open = expanded === d.file;
        return (
          <div key={d.file}>
            <div
              style={styles.diffFileRow}
              onClick={() => setExpanded(open ? null : d.file)}
            >
              <span style={styles.diffFileStatus(d.status ?? "changed")}>
                {d.status ?? "changed"}
              </span>
              <span style={styles.diffFileName} title={d.file}>
                {stripWorkspace(d.file)}
              </span>
              <span style={{ fontSize: "11px", flexShrink: 0 }}>
                <span style={styles.diffStatAdd}>+{d.additions ?? 0}</span>{" "}
                <span style={styles.diffStatDel}>−{d.deletions ?? 0}</span>
              </span>
              <span style={styles.diffChevron}>
                {d.patch ? (open ? "▾" : "▸") : ""}
              </span>
            </div>
            {open && d.patch && (
              <pre style={styles.diffPatchBody}>
                {d.patch.split("\n").map((line, i) => (
                  <div
                    key={i}
                    style={{ ...styles.diffLine, ...(diffLineStyle(line) ?? {}) }}
                  >
                    {line || " "}
                  </div>
                ))}
              </pre>
            )}
          </div>
        );
      })}
      {(onRevert || (reverted && onUnrevert)) && (
        <div style={styles.diffActions}>
          {reverted ? (
            <>
              <span style={styles.revertedNote}>✓ 已回退此回合的文件改动</span>
              <button
                style={styles.unrevertBtn}
                onClick={onUnrevert}
                disabled={busy || !canAct}
              >
                恢复改动
              </button>
            </>
          ) : (
            <button
              style={styles.revertBtn}
              onClick={onRevert}
              disabled={busy || !canAct}
            >
              ⏪ 回退此回合的文件改动
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** P1-1: live todo list rendered from `todo.updated` SSE events. */
function TodoList({ todos }: { todos: TodoItem[] }) {
  const done = todos.filter((t) => t.status === "completed").length;
  const labels: Record<string, string> = {
    pending: "待办",
    in_progress: "进行中",
    completed: "完成",
    cancelled: "已取消",
  };
  return (
    <div style={styles.todoCard}>
      <div style={styles.todoTitle}>
        <span>📋 任务清单</span>
        <span>
          {done}/{todos.length}
        </span>
      </div>
      {todos.map((t, i) => (
        <div key={i} style={styles.todoItem}>
          <span style={styles.todoStatus(t.status)}>{labels[t.status] || t.status}</span>
          <span
            style={{
              ...styles.todoContent,
              ...(t.status === "completed" || t.status === "cancelled"
                ? styles.todoDone
                : {}),
            }}
          >
            {t.content}
          </span>
        </div>
      ))}
    </div>
  );
}

function BlockView({ block, streaming }: { block: Block; streaming: boolean }) {
  const [open, setOpen] = useState(false);

  if (block.kind === "text") {
    if (!block.text && !streaming) return null;
    // fastk 引用标记（[[chunk:db/id]]）解析为可点击徽章；无标记时等价原渲染。
    return <TextWithChunkRefs text={block.text} cursor={streaming} />;
  }

  if (block.kind === "reasoning") {
    if (!block.text) return null;
    return <div style={styles.reasoningBox}>💭 {block.text}</div>;
  }

  // Protocol-capability blocks (opencode part types) rendered inline instead
  // of falling through to the generic tool accordion below.

  if (block.kind === "patch") {
    if (!block.files.length) return null;
    const hash = block.hash ? block.hash.slice(0, 8) : "";
    return (
      <div style={styles.patchBox} title={block.files.join("\n")}>
        <span>📝</span>
        <span style={styles.patchFiles}>
          {block.files.length === 1
            ? stripWorkspace(block.files[0])
            : `工作区变更 · ${block.files.length} 个文件`}
        </span>
        {hash && <span style={styles.patchHash}>@{hash}</span>}
      </div>
    );
  }

  if (block.kind === "compaction") {
    return (
      <div style={styles.compactionBox}>
        ⚡ 上下文已压缩{block.auto ? "（自动）" : ""}——更早的消息被摘要替代
        {block.overflow ? "（上下文溢出触发）" : ""}
      </div>
    );
  }

  if (block.kind === "subtask") {
    return (
      <div style={styles.subtaskBox} title={block.prompt}>
        <span style={styles.subtaskAgent}>🧩 {block.agent}</span>
        <span>{block.description || "子任务委派"}</span>
      </div>
    );
  }

  if (block.kind === "agentTag") {
    return block.name ? <div style={styles.agentTagChip}>🤖 {block.name}</div> : null;
  }

  if (block.kind === "retry") {
    return (
      <div style={styles.retryBox} title={block.error}>
        ⚠ 第 {block.attempt} 次尝试失败，已自动重试
        {block.error ? `：${block.error}` : ""}
      </div>
    );
  }

  if (block.kind === "file") {
    const name =
      block.filename || stripWorkspace(block.url.replace(/^file:\/\//, ""));
    return name ? <div style={styles.fileChip}>📎 {name}</div> : null;
  }

  const detail =
    block.status === "error"
      ? block.error
      : block.output ||
        (block.input ? JSON.stringify(block.input, null, 2) : "");

  return (
    <div style={styles.toolBox}>
      <div style={styles.toolHeader} onClick={() => setOpen((v) => !v)}>
        <span style={styles.toolDot(block.status)} />
        <span style={styles.toolName}>{block.name}</span>
        <span style={styles.toolStatus}>
          {block.status} {detail ? (open ? "▾" : "▸") : ""}
        </span>
      </div>
      {open && detail && <pre style={styles.toolBody}>{detail}</pre>}
    </div>
  );
}

/** Pending question approval card. Local state holds selected labels per question. */
function QuestionCard({
  request,
  onSubmit,
  onReject,
}: {
  request: OcQuestionRequest;
  onSubmit: (sessionId: string, requestId: string, answers: string[][]) => void;
  onReject: (sessionId: string, requestId: string) => void;
}) {
  // selections[i] = labels selected for questions[i]; custom input is appended
  // to the reply because opencode treats custom answers as free-form labels.
  const [selections, setSelections] = useState<string[][]>(() =>
    request.questions.map(() => [])
  );
  const [customTexts, setCustomTexts] = useState<string[]>(() =>
    request.questions.map(() => "")
  );

  const toggle = (qi: number, label: string, multiple: boolean) => {
    setSelections((prev) =>
      prev.map((sel, i) => {
        if (i !== qi) return sel;
        if (sel.includes(label)) return sel.filter((l) => l !== label);
        return multiple ? [...sel, label] : [label];
      })
    );
  };

  const submit = () => {
    const answers = selections.map((sel, i) => {
      const custom = customTexts[i].trim();
      return custom ? [...sel, custom] : sel;
    });
    onSubmit(request.sessionID, request.id, answers);
  };

  const allAnswered = selections.every(
    (sel, i) => sel.length > 0 || customTexts[i].trim().length > 0
  );

  return (
    <div style={styles.quesCard}>
      <div style={styles.quesTitle}>❓ Agent 需要你的输入</div>
      {request.questions.map((q, qi) => (
        <div key={qi} style={{ marginBottom: "8px" }}>
          {q.header && <div style={styles.quesTitle}>{q.header}</div>}
          <div style={styles.quesQuestion}>{q.question}</div>
          <div style={styles.quesOptions}>
            {q.options.map((o) => {
              const selected = selections[qi]?.includes(o.label);
              return (
                <label key={o.label} style={styles.quesOption}>
                  <input
                    type={q.multiple ? "checkbox" : "radio"}
                    checked={selected}
                    onChange={() => toggle(qi, o.label, !!q.multiple)}
                  />
                  <span style={styles.quesOptionLabel}>{o.label}</span>
                  {o.description && (
                    <span style={styles.quesOptionDesc}>— {o.description}</span>
                  )}
                </label>
              );
            })}
            {q.custom && (
              <input
                type="text"
                style={styles.quesCustomInput}
                placeholder="或输入自定义回答…"
                value={customTexts[qi]}
                onChange={(e) =>
                  setCustomTexts((prev) =>
                    prev.map((t, i) => (i === qi ? e.target.value : t))
                  )
                }
              />
            )}
          </div>
        </div>
      ))}
      <div style={styles.quesActions}>
        <button style={styles.quesSubmitBtn} onClick={submit} disabled={!allAnswered}>
          提交
        </button>
        <button style={styles.quesCancelBtn} onClick={() => onReject(request.sessionID, request.id)}>
          取消
        </button>
      </div>
    </div>
  );
}

function Welcome({
  onStart,
  disabled,
  phase,
  phaseIdx,
  elapsedSec,
}: {
  onStart: () => void;
  disabled: boolean;
  phase?: string;
  phaseIdx?: number;
  elapsedSec?: number | null;
}) {
  const features = [
    ["🔒", "强隔离", "每用户独立容器：文件系统 / 进程 / 网络 / 资源命名空间隔离"],
    ["🛡️", "安全加固", "非 root + cap-drop ALL + no-new-privileges + 只读根文件系统"],
    ["🧩", "零业务耦合", "平台不实现任何 agent 逻辑，全部能力来自容器内 opencode serve"],
    ["🔄", "崩溃自愈", "双层健康检查 + restart policy + /workspace 与 /data 卷持久化"],
  ];
  const phaseText =
    phase === "creating"
      ? "正在创建容器…"
      : phase === "starting"
      ? "正在启动 opencode 服务…"
      : phase === "warming"
      ? "正在预热模型会话…"
      : "启动中…";
  // P1-4: concrete startup progress, e.g. "阶段 2/3 · 已等待 6s"
  const hint =
    phase && (phaseIdx || elapsedSec != null)
      ? `${phaseIdx ? `阶段 ${phaseIdx}/3` : ""}${
          phaseIdx && elapsedSec != null ? " · " : ""
        }${elapsedSec != null ? `已等待 ${elapsedSec}s` : ""}`
      : null;
  return (
    <div style={styles.welcome}>
      <div style={styles.welcomeIcon}>🐳</div>
      <h2 style={styles.welcomeTitle}>Agent Docker Platform</h2>
      <p style={styles.welcomeText}>
        浏览器 → 平台控制层 → 容器执行层（opencode serve）→ 共享服务层
      </p>
      <div style={styles.welcomeFeatures}>
        {features.map(([icon, title, desc]) => (
          <div key={title} style={styles.welcomeFeature}>
            <span style={styles.welcomeFeatureIcon}>{icon}</span>
            <div>
              <div style={styles.welcomeFeatureTitle}>{title}</div>
              <div style={styles.welcomeFeatureDesc}>{desc}</div>
            </div>
          </div>
        ))}
      </div>
      {phase ? (
        <div style={styles.welcomeStarting}>
          <span style={styles.welcomeSpinner}>⏳</span>
          <span>
            {phaseText}
            {hint && <span style={styles.welcomeProgress}>（{hint}）</span>}
          </span>
        </div>
      ) : (
        <button style={styles.welcomeBtn} onClick={onStart} disabled={disabled}>
          启动 Agent 容器
        </button>
      )}
    </div>
  );
}
