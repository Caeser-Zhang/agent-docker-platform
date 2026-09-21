/** API client for the platform control layer.
 *
 * Two kinds of call live here:
 *   - platform calls  (/api/auth, /api/agent)  — login and container lifecycle
 *   - tunnelled calls (/api/tunnel/oc/...)     — forwarded verbatim to
 *     `opencode serve` inside the user's container
 *
 * Everything agent-related uses opencode's own routes and payload shapes,
 * verified against opencode 1.18.16's live OpenAPI document (`/doc`):
 *   GET,POST /api/session               { data: SessionV2Info[] | SessionV2Info }
 *   GET  /api/session/{id}              { data: SessionV2Info }
 *   POST /api/session/{id}/prompt       { prompt: { text } } → { data: SessionInputAdmitted }
 *   POST /api/session/{id}/model        { model: ModelRef }   → 204
 *   POST /api/session/{id}/agent        { agent: string }     → 204
 *   POST /api/session/{id}/interrupt    → 204
 *   GET  /session/{id}/message           [{ info, parts }]  (legacy surface, bare array)
 *   GET  /api/model                     { location, data: ModelV2Info[] }
 *   GET  /api/agent                     { location, data: AgentV2Info[] } (native only — listAgents uses legacy /agent for plugin agents)
 *   GET  /api/permission/request        { location, data: PermissionV2Request[] }
 *   POST /api/session/{id}/permission/{rid}/reply  { reply: once|always|reject } → 204
 *   GET  /api/question/request          { location, data: QuestionV2Request[] }
 *   POST /api/session/{id}/question/{rid}/reply    { answers: string[][] }       → 204
 *   POST /api/session/{id}/question/{rid}/reject   → 204
 *
 * Session delete/rename has no v2 route — it lives on the legacy surface:
 *   DELETE /session/{id}   → bare true
 *   PATCH  /session/{id}   { title } → bare legacy Session
 */

import type { FileDiff } from "./oc/messages";

const API_BASE = "/api";
/** Prefix that reverse-proxies straight into the container's opencode server. */
const OC = "/tunnel/oc";

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  username: string;
  /** "user" | "admin" — gates the Docker management panel. */
  role?: string;
}

export interface AgentStatus {
  running: boolean;
  healthy: boolean;
  status: string;
  /**
   * Epoch seconds when the CURRENT start flow began (P1-4): set server-side
   * on entering "creating" and kept across phase advances, so any browser —
   * even one that attached mid-start — can show an accurate total wait.
   */
  phase_since?: number | null;
  /**
   * Epoch seconds of the container's current boot (DB record). Chat uses it
   * to converge assistant turns stranded by a container kill: any message
   * created before this instant cannot still be streaming.
   */
  started_at?: number | null;
  container_name: string | null;
  workspace: string | null;
  message: string;
  error?: string | null;
}

export interface AgentRuntime {
  runtime: string;
  image: string;
  port: number;
  workdir: string;
  network: string;
  config: {
    source: string;
    mounted: boolean;
    providers: string[];
    stripped: string[];
    host_alias: string;
  };
}

/** opencode ModelRef — the exact shape its API requires. */
export interface ModelRef {
  providerID: string;
  id: string;
  variant?: string;
}

export interface ProviderOption {
  id: string;
  name: string;
  baseURL?: string | null;
  models: { id: string; name: string }[];
}

export interface ProvidersResponse {
  providers: ProviderOption[];
  default: string | null;
  smallModel?: string | null;
  error?: string;
  source?: AgentRuntime["config"];
}

/** One user-owned LLM provider stored in the platform DB (masked view). */
export interface UserLlmProvider {
  id: string;
  provider_id: string;
  name: string | null;
  npm: string;
  baseURL: string | null;
  hasApiKey: boolean;
  models: Record<string, any>;
  created_at: string;
  updated_at: string;
}

/** The user's active LLM selection (empty when none is set). */
export interface ActiveLlm {
  provider_id: string | null;
  model: string | null;
}

/** Payload for creating a user LLM provider. */
export interface UserLlmProviderInput {
  provider_id: string;
  name?: string | null;
  npm?: string;
  base_url?: string | null;
  api_key?: string | null;
  models?: Record<string, any> | null;
}

/** Masked view of a user-scoped MCP server (secrets never leave the backend). */
export interface UserMcpServer {
  id: string;
  name: string;
  type: "local" | "remote";
  enabled: boolean;
  created_at: string;
  updated_at: string;
  url?: string | null;
  hasHeaders?: boolean;
  command?: string[] | null;
  hasEnv?: boolean;
  cwd?: string | null;
  timeout?: number | null;
}

/** Built-in MCP server as seen through the current user's own toggle. */
export interface BuiltinMcpItem {
  name: string;
  type?: string | null;
  /** Admin-controlled platform-wide state; a false here wins over `my_enabled`. */
  platform_enabled: boolean;
  /** The current user's personal choice (defaults to inherit = true). */
  my_enabled: boolean;
  /** What the user's container actually ends up with. */
  effective_enabled: boolean;
}

/** Payload for creating/updating a user MCP server. */
export interface UserMcpInput {
  name?: string;
  type?: "local" | "remote";
  enabled?: boolean;
  command?: string[] | null;
  url?: string | null;
  headers?: Record<string, string> | null;
  environment?: Record<string, string> | null;
  cwd?: string | null;
  timeout?: number | null;
}

/** Subset of opencode's SessionV2Info that the UI renders. */
export interface OcSession {
  id: string;
  title: string;
  agent?: string;
  model?: ModelRef;
  cost?: number;
  tokens?: { input: number; output: number; reasoning: number };
  time: { created: number; updated: number };
  location?: { directory: string };
}

/** opencode SSE envelope (GET /api/event), re-emitted by the platform pump. */
export interface OcEvent {
  /** Platform-assigned monotonic id used for replay after reconnect. */
  id: number;
  event_id?: string;
  type: string;
  data: Record<string, any>;
  durable?: { aggregateID: string; seq: number; version: number };
  location?: { directory: string };
}

/** opencode location info, part of the v2 list envelope. */
export interface OcLocation {
  directory: string;
  workspaceID?: string;
  project?: { id: string; directory: string };
}

/** Platform-side project record (POST/GET /api/projects). Sessions belong to
 *  a project when session.location.directory === project.directory. */
export interface ProjectInfo {
  id: string;
  name: string;
  directory: string;
  origin: "created" | "bound";
  createdAt?: string;
  updatedAt?: string;
}

/** Pagination envelope: GET /api/session, GET /api/session/{id}/message. */
export interface OcPage<T> {
  data: T[];
  cursor: { previous: string | null; next: string | null };
}

/** Location envelope: GET /api/agent, /api/model, /api/permission/request, /api/question/request. */
export interface OcEnvelope<T> {
  location: OcLocation;
  data: T[];
}

/** opencode AgentV2Info (GET /api/agent) — fields the UI renders. */
export interface OcAgent {
  id: string;
  model?: ModelRef;
  system?: string;
  description?: string;
  mode?: "subagent" | "primary" | "all";
  hidden?: boolean;
  /** Present on the legacy /agent surface: true for opencode's own agents. */
  native?: boolean;
  steps?: number;
}

/** opencode command entry (GET /command) — global list incl. plugin commands. */
export interface OcCommand {
  name: string;
  description?: string;
  source?: string;
  template?: string;
}

/** opencode ModelV2Info (GET /api/model) — fields the UI renders. */
export interface OcModel {
  id: string;
  providerID: string;
  family?: string;
  name: string;
  status?: "alpha" | "beta" | "deprecated" | "active";
  enabled?: boolean;
  limit?: { context: number; input?: number; output: number };
}

/** opencode PermissionV2Reply. */
export type OcPermissionReply = "once" | "always" | "reject";

/** opencode PermissionV2Request (GET /api/permission/request). */
export interface OcPermissionRequest {
  /** "per_*" */
  id: string;
  sessionID: string;
  action: string;
  resources: string[];
  save?: string[];
  metadata?: Record<string, unknown>;
  source?: { type: "tool"; messageID: string; callID: string };
}

export interface OcQuestionOption {
  label: string;
  description?: string;
}

/** One question inside a QuestionV2Request. */
export interface OcQuestion {
  question: string;
  header?: string;
  options: OcQuestionOption[];
  multiple?: boolean;
  custom?: boolean;
}

/** opencode QuestionV2Request (GET /api/question/request). */
export interface OcQuestionRequest {
  /** "que_*" */
  id: string;
  sessionID: string;
  questions: OcQuestion[];
  tool?: { messageID: string; callID: string };
}

/**
 * fastk 知识库 chunk — 引用徽章点击后经平台代理（/api/fastk/chunk）取回的
 * 完整内容。图片 URL 是平台侧相对路径，需带 Authorization 以 Blob 方式加载。
 */
export interface FastkChunk {
  chunk_id: string;
  text: string;
  path: string;
  section: string;
  chunk_index: number;
  /** 图片引用：内容寻址 `asset:` 键 JSON 数组（新）或旧绝对路径；仅展示用，取图走 images。 */
  image_path: string | null;
  /** 全部附图：alt 为 markdown `![alt](…)` 的替换文本，用于把图放回原位。 */
  images: { url: string; alt: string }[];
}

/** One-shot docker stats sample (admin container list). */
export interface AdminContainerStats {
  cpu_percent: number;
  mem_usage_mb: number;
  mem_limit_mb: number;
  mem_percent: number;
  pids?: number;
}

/** A user container as seen by the admin panel. */
export interface AdminContainer {
  user_id: string;
  username: string | null;
  /** Employee number (工号) from the users table. */
  uid: string | null;
  container_name: string;
  /** Status from the agent_containers DB record ("unmanaged" = no record). */
  db_status: string;
  /** Live status from the Docker daemon ("absent" = no container). */
  docker_status: string;
  /** Docker healthcheck status, e.g. "healthy" / "unhealthy" / null. */
  health: string | null;
  image: string;
  /** Full sha256 ID of the image the container actually runs. */
  image_id?: string | null;
  /** True when the container's image differs from the currently loaded agent image. */
  image_stale?: boolean;
  started_at: string | null;
  last_activity: string | null;
  restart_count: number;
  last_error: string | null;
  stats?: AdminContainerStats | null;
}

export interface AdminContainerLogs {
  user_id: string;
  tail: number;
  logs: string;
}

/** One tunnel-proxied request recorded platform-side (opencode has no access log of its own). */
export interface AdminRequestLogEntry {
  id: number;
  method: string;
  /** opencode path incl. query string, e.g. "/api/session?limit=20". */
  path: string;
  status_code: number;
  duration_ms: number;
  created_at: string;
}

export interface AdminRequestLogs {
  user_id: string;
  logs: AdminRequestLogEntry[];
}

export interface AdminOverview {
  users: { total: number; admins: number };
  containers: {
    records: number;
    by_status: Record<string, number>;
    docker_total: number;
    docker_running: number;
  };
  platform: {
    image: string;
    network: string;
    port: number;
    cpu_limit: number;
    memory_limit: string;
  };
}

// --- PPTX 模板库（named volume 单副本共享，容器侧只读挂载） ----------------
// 模板字节不进用户工作区：后端把它们放在 agent-pptx-lib 卷上，用户容器以 ro
// 方式挂载同一路径，所以 N 个用户只占 1 份空间。API 返回的 path 是容器内路径，
// 前端把它注入 prompt，agent 直接从自己的挂载点读取。

/** 画廊卡片：GET /api/library/templates 的条目，字段对齐后端 _card()。 */
export interface LibraryTemplateCard {
  id: string;
  name: string | null;
  name_zh: string | null;
  description: string | null;
  tags: string[];
  enabled: boolean;
  license: string | null;
  /** seed（仓库种子）/ sample（开发期样例，默认不对普通用户可见）/ upload。 */
  source: string | null;
  slides: number;
  aspect: string;
  layouts: number;
  /** 新增内容页应沿用的版式名（入库时按多数派版式推导）。 */
  content_layout: string | null;
  page_type_summary: Record<string, number> | null;
  /** 管理员绑定的调色板 id（对应 LibraryStyles.palettes）；未绑定为 null。 */
  palette: string | null;
  /** 入库时统计的实际用色 top5（#RRGGBB）—— 没有缩略图时充当占位色块。 */
  palette_hint: string[];
  /** 管理员绑定的风格配方 id（sharp/soft/rounded/pill）。 */
  recipe: string | null;
  fonts: { theme: Record<string, string | null>; used: string[] } | null;
  has_chart_part: boolean;
  animated_slides: number;
  size_bytes: number;
  /** false = 尚无缩略图，画廊走调色板占位（两段式流程）。 */
  has_thumb: boolean;
  /** 套用模板时必须替换掉的占位文案清单。 */
  must_replace: string[];
  created_at: string;
  /** 用户容器内的绝对路径 —— 注入 prompt 的就是这个值。 */
  path: string;
  thumbUrl: string;
}

export interface LibraryPageType {
  index: number;
  part: string;
  layout: string | null;
  type: string;
  title: string;
  text_chars: number;
  images: number;
}

export interface LibraryResidualText {
  part: string;
  pattern: string;
  text: string;
}

export interface LibraryNormalizeSummary {
  slides_in: number;
  slides_out: number;
  input_bytes: number;
  output_bytes: number;
  saved_bytes: number;
  dropped_slides: number;
  orphan_media_removed: number;
  images_slimmed: number;
}

/** 完整目录记录（含残留文本、校验告警），管理员检查用。 */
export interface LibraryTemplateDetail extends LibraryTemplateCard {
  source_sha256: string;
  slide_size_inches: number[] | null;
  page_types: LibraryPageType[];
  content_layout_note: string | null;
  embedded_fonts: string[];
  media_count: number;
  residual_texts: LibraryResidualText[];
  warnings: string[];
  normalize: LibraryNormalizeSummary;
}

export interface LibraryPalette {
  id: string;
  name: string;
  name_zh: string;
  /** 5 个 #RRGGBB，顺序即文档原始顺序。 */
  colors: string[];
  style: string;
  use_cases: string[];
  tips: string;
  dark_mode_required: boolean;
}

export interface LibraryRecipe {
  id: string;
  name: string;
  name_zh: string;
  character: string;
  best_for: string;
  corner_radius: Record<string, number>;
  spacing: Record<string, number | number[]>;
  components: Record<string, number>;
  radius_by_height: Record<string, number | string>;
  pill_tip?: string;
}

/** GET /api/library/styles —— 与卷内 styles/*.json 同一份数据。 */
export interface LibraryStyles {
  version: number;
  source: string;
  units: string;
  palettes: LibraryPalette[];
  recipes: LibraryRecipe[];
  recipe_selection_guide: { type: string; recipes: string[]; reason: string }[];
  typography: Record<string, unknown>;
  rules: Record<string, string[]>;
  /** 容器内 styles 目录（agent 可本地读取，无需 HTTP 往返）。 */
  dir: string;
}

export interface LibraryStats {
  root: string;
  templates: number;
  enabled: number;
  with_thumb: number;
  total_bytes: number;
  raw_input_bytes: number;
  saved_bytes: number;
  by_source: Record<string, number>;
  /** 恒为 1：一份物理副本被所有容器共享（O(1)，不随用户数增长）。 */
  copies: number;
}

export interface LibraryIngestInput {
  file: File;
  name?: string;
  nameZh?: string;
  description?: string;
  tags?: string[];
  palette?: string;
  recipe?: string;
  license?: string;
  source?: string;
  enabled?: boolean;
  optimizeImages?: boolean;
  dropPromo?: boolean;
  mustReplace?: string[];
}

export interface LibraryIngestReport {
  input_bytes: number;
  output_bytes: number;
  saved_bytes: number;
  saved_percent: number;
  slides_in: number;
  slides_out: number;
  dropped_slides: {
    index: number;
    part: string;
    matched_strong: string[];
    matched_weak: string[];
  }[];
  orphan_media_removed: string[];
  images_slimmed: {
    part: string;
    new_part: string;
    action: string;
    before_bytes: number;
    after_bytes: number;
    saved_bytes: number;
  }[];
  vendor_tags_removed: string[];
  branding_scrubbed: string[];
  residual_texts: LibraryResidualText[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

export interface LibraryIngestResult {
  status: string;
  /** false = 相同源字节已入库，本次是 no-op（按内容哈希幂等）。 */
  created: boolean;
  template: LibraryTemplateCard;
  detail: LibraryTemplateDetail;
  report: LibraryIngestReport;
}

/** 仅 EDITABLE_FIELDS 生效；后端 exclude_none，所以传 null 等于没传。 */
export interface LibraryTemplatePatch {
  name?: string;
  name_zh?: string;
  description?: string;
  tags?: string[];
  palette?: string;
  recipe?: string;
  must_replace?: string[];
  license?: string;
  source?: string;
  enabled?: boolean;
  content_layout_note?: string;
}

export interface LibrarySeedResult {
  status: string;
  scanned: number;
  ingested: number;
  skipped_existing: number;
  failed: { file: string; error: string }[];
  samples_skipped: number;
}

// --- Knowledge domains (admin) ---------------------------------------------
// A DOMAIN is one API key covering one-or-more fastk databases; it is the unit
// of authorisation. key_type "public" grants every user implicitly, "private"
// requires a roster entry. See docs/KB_DOMAIN_DESIGN.md.
export interface KbUser {
  user_id: string;
  username: string;
  uid: string | null;
  role: string;
}

export type KbKeyType = "public" | "private";

export interface KbDomainInfo {
  id: string;
  name: string;
  description: string;
  key_type: KbKeyType;
  has_api_key: boolean;
  db_count: number;
  member_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface KbDomainMember {
  user_id: string;
  username: string;
  uid: string | null;
  created_at: string | null;
}

export interface KbDomainDetail {
  id: string;
  name: string;
  description: string;
  key_type: KbKeyType;
  has_api_key: boolean;
  created_at: string | null;
  updated_at: string | null;
  databases: { kb_name: string }[];
  members: KbDomainMember[];
  total_users: number;
  active_member_count: number;
}

/** Import preview — nothing is written until the admin commits the token. */
export interface KbImportPreview {
  preview_token: string;
  source: "text" | "csv" | "xlsx";
  source_meta: {
    filename?: string;
    header_detected?: boolean;
    column_index?: number;
    columns?: { index: number; label: string }[];
  };
  matched: { user_id: string; username: string; uid: string | null }[];
  already: { user_id: string; username: string; uid: string | null }[];
  unmatched: string[];
  domain_id: string;
  domain_name: string;
}

/** One accessible domain as shown to the user — card header + its databases. */
export interface KbDomainEntry {
  id: string;
  name: string;
  description: string;
  key_type: KbKeyType;
  databases: { name: string; description: string }[];
}

async function apiCall<T>(
  path: string,
  options: RequestInit & { raw?: boolean } = {}
): Promise<T> {
  const { raw, ...init } = options;
  const token = localStorage.getItem("token");
  const resp = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  // Session-expired interception: a 401 on an authenticated call means the
  // JWT is invalid/expired — every subsequent call would fail identically,
  // so drop the stale session and return to the login page. Unauthenticated
  // calls (login/register) carry no token, so their 401s still surface to
  // the caller as normal error messages.
  if (resp.status === 401 && token) {
    for (const key of ["token", "username", "userId", "role"]) {
      localStorage.removeItem(key);
    }
    window.location.reload();
    throw new Error("登录已过期，请重新登录");
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    // Platform errors use {detail}; opencode errors use {message}.
    // 状态码附在 Error 上（§7.5 适配 1）：调用方需要区分 409（已转化）/
    // 429（限流）/ 422（校验）分流提示。既有 catch (e) { e.message } 不受影响。
    const e = new Error(
      err.detail || err.message || `HTTP ${resp.status}`
    ) as Error & { status?: number };
    e.status = resp.status;
    throw e;
  }
  // raw：跳过 JSON 解析直接返回 Response（附件字节等二进制场景，§7.5 适配 3）。
  if (raw) return resp as T;
  if (resp.status === 204) return undefined as T;
  // Some upstream answers carry no body (and proxies may empty one out);
  // resp.json() on "" throws "Unexpected end of JSON input".
  const text = await resp.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

// --- UX feedback / metrics types (任务一 & 任务二) -------------------------

/** 点踩原因码白名单（与后端 feedback.REASON_CODES 一致）。 */
export type FeedbackReasonCode =
  | "misunderstood"
  | "wrong_answer"
  | "tool_failure"
  | "too_verbose"
  | "ignored_constraints"
  | "interrupted"
  | "other";

export interface FeedbackSubmit {
  session_id: string;
  message_id: string;
  user_message_id?: string | null;
  verdict: "up" | "down";
  reason_codes?: FeedbackReasonCode[];
  reason_text?: string | null;
  turn_errored?: boolean;
  model_provider?: string | null;
  model_id?: string | null;
  agent?: string | null;
  /** 本轮完整上下文快照（前端组装）。 */
  context?: Record<string, any>;
}

export interface FeedbackResult {
  ok: boolean;
  already: boolean;
  verdict: "up" | "down";
  message_id: string;
}

export interface SessionFeedbackItem {
  message_id: string;
  verdict: "up" | "down";
  created_at: string | null;
}

export type UxGranularity = "day" | "week" | "month" | "year";

export interface UxQuery {
  granularity?: UxGranularity;
  days?: number;
  user_id?: string;
  model_provider?: string;
}
export interface UxRoundsQuery extends UxQuery {
  session_id?: string;
  only_failed?: boolean;
  limit?: number;
  offset?: number;
}
export interface UxFeedbackQuery {
  granularity?: UxGranularity;
  days?: number;
  verdict?: "up" | "down";
  limit?: number;
  offset?: number;
}

export interface UxOverview {
  granularity: UxGranularity;
  window_days: number;
  filters: { user_id: string | null; model_provider: string | null };
  l0_user: {
    active_users: number;
    requests: number;
    sessions: number;
    new_users: number;
  };
  l1_outcome: {
    rounds_total: number;
    round_success_rate: number | null;
    error_rate: number | null;
    task_rounds: number;
    task_success_rate: number | null;
    error_breakdown: Record<string, number>;
  };
  l2_efficiency: {
    duration_avg_ms: number | null;
    duration_p50_ms: number | null;
    duration_p90_ms: number | null;
    duration_p99_ms: number | null;
    total_cost: number;
    avg_cost: number | null;
  };
  l3_process: {
    tool_calls: number;
    tool_errors: number;
    tool_accuracy: number | null;
    total_tokens: number;
    avg_tokens_per_round: number | null;
    tokens_per_success: number | null;
    token_split: {
      input: number;
      output: number;
      reasoning: number;
      cache_read: number;
      cache_write: number;
    };
  };
  l4_satisfaction: {
    thumbs_up: number;
    thumbs_down: number;
    total: number;
    satisfaction_rate: number | null;
    down_reasons: Record<string, number>;
  };
}

export interface UxTrendPoint {
  date: string;
  rounds: number;
  success_rate: number | null;
  duration_avg_ms: number | null;
  duration_p90_ms: number | null;
  tool_accuracy: number | null;
  total_tokens: number;
  cost: number;
  satisfaction_rate: number | null;
  thumbs_up: number;
  thumbs_down: number;
}
export interface UxTrends {
  granularity: UxGranularity;
  window_days: number;
  series: UxTrendPoint[];
}

export interface UxUserActivityPoint {
  bucket: string;
  active_users: number;
  requests: number;
  rounds: number;
  sessions: number;
  new_users: number;
}
export interface UxUserActivity {
  granularity: UxGranularity;
  window_days: number;
  series: UxUserActivityPoint[];
  totals: {
    active_users: number;
    requests: number;
    sessions: number;
    new_users: number;
  };
}

export interface UxToolRow {
  tool_name: string;
  calls: number;
  errors: number;
  accuracy: number | null;
}
export interface UxTools {
  granularity: UxGranularity;
  window_days: number;
  tools: UxToolRow[];
}

export interface UxToolCallQuery {
  granularity?: UxGranularity;
  days?: number;
  user_id?: string;
  session_id?: string;
  only_failed?: boolean;
  limit?: number;
  offset?: number;
}
export interface UxToolCallRow {
  id: number;
  user_id: string;
  user_name: string | null;
  user_uid: string | null;
  session_id: string;
  round_seq: number;
  tool_name: string;
  status: string | null;
  is_error: boolean;
  error_text: string | null;
  duration_ms: number | null;
  source: string;
  created_at: string | null;
}
export interface UxToolCalls {
  total: number;
  limit: number;
  offset: number;
  tool_calls: UxToolCallRow[];
}

export interface UxRoundRow {
  id: number;
  user_id: string;
  user_name: string | null;
  user_uid: string | null;
  session_id: string;
  round_seq: number;
  message_id: string;
  is_task: boolean;
  succeeded: boolean;
  task_success: boolean | null;
  errored: boolean;
  error_text: string | null;
  error_name: string | null;
  error_status_code: number | null;
  duration_ms: number | null;
  total_tokens: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  reasoning_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  cost: number | null;
  tool_calls: number;
  tool_errors: number;
  model_provider: string | null;
  model_id: string | null;
  agent: string | null;
  source: string;
  created_at: string | null;
}
export interface UxRounds {
  total: number;
  limit: number;
  offset: number;
  rounds: UxRoundRow[];
}

export interface UxLlmQuery {
  granularity?: UxGranularity;
  days?: number;
  user_id?: string;
  provider_id?: string;
}
export interface UxLlmProvider {
  provider_id: string;
  calls: number;
  errors: number;
  error_rate: number | null;
  sse_calls: number;
  status_counts: Record<string, number>;
  ttft_avg_ms: number | null;
  ttft_p50_ms: number | null;
  ttft_p90_ms: number | null;
  ttft_p99_ms: number | null;
  duration_avg_ms: number | null;
  duration_p50_ms: number | null;
  duration_p90_ms: number | null;
  duration_p99_ms: number | null;
}
export interface UxLlm {
  granularity: UxGranularity;
  window_days: number;
  filters: { user_id: string | null; provider_id: string | null };
  totals: { calls: number; errors: number; error_rate: number | null };
  providers: UxLlmProvider[];
}

export interface UxFeedbackRow {
  id: number;
  user_id: string;
  session_id: string;
  message_id: string;
  user_message_id: string | null;
  verdict: "up" | "down";
  reason_codes: string[];
  reason_text: string | null;
  turn_errored: boolean;
  model_provider: string | null;
  model_id: string | null;
  agent: string | null;
  context: Record<string, any>;
  context_truncated: boolean;
  created_at: string | null;
}
export interface UxFeedback {
  total: number;
  limit: number;
  offset: number;
  feedback: UxFeedbackRow[];
}

/** Build a query string from a params object, dropping undefined/null. */
function uxQuery(params: Record<string, any>): string {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  // --- Auth -------------------------------------------------------------
  async register(username: string, password: string): Promise<TokenResponse> {
    return apiCall("/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },

  async login(username: string, password: string): Promise<TokenResponse> {
    return apiCall("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },

  // --- Agent container lifecycle (platform control plane) ---------------
  async getAgentStatus(): Promise<AgentStatus> {
    return apiCall("/agent/status");
  },

  async getAgentRuntime(): Promise<AgentRuntime> {
    return apiCall("/agent/runtime");
  },

  async startAgent(): Promise<AgentStatus> {
    return apiCall("/agent/start", { method: "POST", body: JSON.stringify({}) });
  },

  async stopAgent(): Promise<AgentStatus> {
    return apiCall("/agent/stop", { method: "POST" });
  },

  async getAgentLogs(): Promise<{ logs: string }> {
    return apiCall("/agent/logs");
  },

  // --- Admin — platform-wide Docker management (role=admin only) --------
  async getAdminOverview(): Promise<AdminOverview> {
    return apiCall("/admin/overview");
  },

  async getAdminContainers(stats = true): Promise<{ containers: AdminContainer[] }> {
    return apiCall(`/admin/containers?stats=${stats}`);
  },

  async getAdminContainerLogs(userId: string, tail = 200): Promise<AdminContainerLogs> {
    return apiCall(`/admin/containers/${userId}/logs?tail=${tail}`);
  },

  /** Recent tunnel request logs for a user (method/path/status/duration), newest first. */
  async getAdminRequestLogs(userId: string, limit = 200): Promise<AdminRequestLogs> {
    return apiCall(`/admin/containers/${userId}/request-logs?limit=${limit}`);
  },

  async adminRestartContainer(userId: string): Promise<{ ok: boolean; message: string }> {
    return apiCall(`/admin/containers/${userId}/restart`, { method: "POST" });
  },

  async adminStopContainer(userId: string): Promise<{ ok: boolean; message: string }> {
    return apiCall(`/admin/containers/${userId}/stop`, { method: "POST" });
  },

  /** Recreate the container from the currently loaded agent image (volumes kept). */
  async adminRecreateContainer(userId: string): Promise<{ ok: boolean; message: string }> {
    return apiCall(`/admin/containers/${userId}/recreate`, { method: "POST" });
  },

  async adminDestroyContainer(userId: string): Promise<{ ok: boolean; message: string }> {
    return apiCall(`/admin/containers/${userId}/destroy`, { method: "POST" });
  },

  // --- UX feedback (任务一：点赞/点踩) ------------------------------------
  /** 提交一条 assistant 回复的反馈。幂等：重复提交返回 already=true。 */
  async submitFeedback(body: FeedbackSubmit): Promise<FeedbackResult> {
    return apiCall("/feedback", { method: "POST", body: JSON.stringify(body) });
  },

  /** 加载某会话下当前用户已提交的反馈（回填锁定态）。 */
  async getSessionFeedback(sessionId: string): Promise<{ session_id: string; feedback: SessionFeedbackItem[] }> {
    return apiCall(`/feedback/session/${encodeURIComponent(sessionId)}`);
  },

  // --- Admin UX 看板 (任务二：用户体验指标) --------------------------------
  async uxOverview(params: UxQuery = {}): Promise<UxOverview> {
    return apiCall(`/admin/ux/overview${uxQuery(params)}`);
  },
  async uxTrends(params: UxQuery = {}): Promise<UxTrends> {
    return apiCall(`/admin/ux/trends${uxQuery(params)}`);
  },
  async uxUserActivity(params: UxQuery = {}): Promise<UxUserActivity> {
    return apiCall(`/admin/ux/user-activity${uxQuery(params)}`);
  },
  async uxTools(params: UxQuery = {}): Promise<UxTools> {
    return apiCall(`/admin/ux/tools${uxQuery(params)}`);
  },
  async uxToolCalls(params: UxToolCallQuery = {}): Promise<UxToolCalls> {
    return apiCall(`/admin/ux/tool-calls${uxQuery(params)}`);
  },
  async uxRounds(params: UxRoundsQuery = {}): Promise<UxRounds> {
    return apiCall(`/admin/ux/rounds${uxQuery(params)}`);
  },
  async uxLlm(params: UxLlmQuery = {}): Promise<UxLlm> {
    return apiCall(`/admin/ux/llm${uxQuery(params)}`);
  },
  async uxFeedback(params: UxFeedbackQuery = {}): Promise<UxFeedback> {
    return apiCall(`/admin/ux/feedback${uxQuery(params)}`);
  },
  /** 按需回补某用户某会话的历史指标（容器须运行中）。 */
  async uxBackfill(userId: string, sessionId: string): Promise<{ ok: boolean; session_id: string; records: number; inserted: number }> {
    return apiCall("/admin/ux/backfill", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, session_id: sessionId }),
    });
  },

  // --- Admin — knowledge domains (one key covering many databases) ---------
  /** Every user, for the roster picker. */
  async adminListKbUsers(): Promise<{ items: KbUser[] }> {
    return apiCall("/admin/kb-users");
  },

  /** All domains with db/member counts (names + presence flags, never the key). */
  async adminListKbDomains(): Promise<{ items: KbDomainInfo[] }> {
    return apiCall("/admin/kb-domains");
  },

  /** Create an empty domain; databases and roster are attached afterwards. */
  async adminCreateKbDomain(body: {
    name: string;
    description?: string;
    key_type?: KbKeyType;
    api_key?: string;
  }): Promise<{ id: string; name: string; key_type: KbKeyType }> {
    return apiCall("/admin/kb-domains", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  /** One domain in full: metadata, attached databases, active roster. */
  async adminGetKbDomain(id: string): Promise<KbDomainDetail> {
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}`);
  },

  /**
   * Rename / re-describe / rotate the key / flip key_type. Flipping the type
   * additionally requires `confirm_name` to equal the current domain name, so
   * a stray toggle cannot silently open or close a domain.
   */
  async adminUpdateKbDomain(
    id: string,
    body: {
      name?: string;
      description?: string;
      key_type?: KbKeyType;
      confirm_name?: string;
      api_key?: string;
    }
  ): Promise<{ id: string; name: string; key_type: KbKeyType }> {
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  },

  /** Delete a domain together with its database links and roster. */
  async adminDeleteKbDomain(id: string): Promise<{ id: string; affected_members: number }> {
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // --- Domain databases ----------------------------------------------------
  async adminListKbDomainDbs(id: string): Promise<{ items: { kb_name: string }[] }> {
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}/dbs`);
  },

  /**
   * Attach a physical database. `added: false` means it was already in this
   * domain; a database claimed by another domain fails with 409.
   */
  async adminAddKbDomainDb(
    id: string,
    kbName: string
  ): Promise<{ domain_id: string; kb_name: string; added: boolean }> {
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}/dbs`, {
      method: "POST",
      body: JSON.stringify({ kb_name: kbName }),
    });
  },

  /** Detach a database — access through this domain stops on the next request. */
  async adminRemoveKbDomainDb(
    id: string,
    kbName: string
  ): Promise<{ domain_id: string; kb_name: string }> {
    return apiCall(
      `/admin/kb-domains/${encodeURIComponent(id)}/dbs/${encodeURIComponent(kbName)}`,
      { method: "DELETE" }
    );
  },

  // --- Domain roster (private domains only) --------------------------------
  async adminListKbDomainMembers(id: string): Promise<{ items: KbDomainMember[] }> {
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}/members`);
  },

  /** Whitelist one user by username or 工号 (revives a soft-deleted row). */
  async adminGrantKbDomainMember(
    id: string,
    ident: { username?: string; uid?: string }
  ): Promise<{ domain_id: string; user_id: string; username: string }> {
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}/members`, {
      method: "POST",
      body: JSON.stringify(ident),
    });
  },

  /** Revoke a user (soft delete — stamps revoked_at). */
  async adminRevokeKbDomainMember(
    id: string,
    userId: string
  ): Promise<{ domain_id: string; user_id: string }> {
    return apiCall(
      `/admin/kb-domains/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`,
      { method: "DELETE" }
    );
  },

  /**
   * Parse a roster source WITHOUT writing anything — pasted text, a .csv or an
   * .xlsx. For files the 工号 column is auto-detected from header keywords;
   * pass `column` to re-pick one. Nothing is granted until `adminKbImportCommit`.
   */
  async adminKbImportPreview(
    id: string,
    source: { text?: string; file?: File; column?: number }
  ): Promise<KbImportPreview> {
    const form = new FormData();
    if (source.file) form.append("file", source.file);
    if (source.text != null && source.text !== "") form.append("text", source.text);
    if (source.column != null) form.append("column", String(source.column));
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}/import/preview`, {
      method: "POST",
      body: form,
    });
  },

  /** Commit a preview token (single-use, domain-bound, 10-minute TTL). */
  async adminKbImportCommit(
    id: string,
    previewToken: string
  ): Promise<{ domain_id: string; granted: number; skipped: number }> {
    return apiCall(`/admin/kb-domains/${encodeURIComponent(id)}/import/commit`, {
      method: "POST",
      body: JSON.stringify({ preview_token: previewToken }),
    });
  },

  // --- User — domains accessible to the caller ------------------------------
  /**
   * The caller's accessible domains, each aggregating the databases they may
   * reach. The backend resolves permissions and filters the fastk catalog, so
   * the frontend never touches /databases directly nor authorises anything.
   */
  async myKbDomains(): Promise<{ domains: KbDomainEntry[] }> {
    return apiCall("/kb/my-domains");
  },

  // --- LLM configuration (read from opencode's own /config) -------------
  async getProviders(): Promise<ProvidersResponse> {
    return apiCall("/tunnel/providers");
  },

  /** Re-inject the host opencode.json and restart the container. */
  async reloadConfig(): Promise<{ reloaded: boolean }> {
    return apiCall("/tunnel/config/reload", { method: "POST" });
  },

  // --- Sessions (opencode /api/session) ---------------------------------
  async listSessions(): Promise<OcSession[]> {
    const r = await apiCall<OcPage<OcSession>>(`${OC}/api/session`);
    return r.data ?? [];
  },

  /**
   * opencode derives the project from `location.directory`; /workspace is the
   * per-user volume mounted into the container. Project-scoped sessions pass
   * the project's directory instead.
   */
  async createSession(model?: ModelRef, agent = "coder", directory = "/workspace"): Promise<OcSession> {
    const body: Record<string, any> = {
      agent,
      location: { directory },
    };
    if (model) body.model = model;
    const r = await apiCall<{ data: OcSession }>(`${OC}/api/session`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    return r.data;
  },

  async getSession(sessionId: string): Promise<OcSession> {
    const r = await apiCall<{ data: OcSession }>(`${OC}/api/session/${sessionId}`);
    return r.data;
  },

  async getMessages(sessionId: string): Promise<Array<{ info: any; parts: any[] }>> {
    // v1.18.16: the v2 route (/api/session/{id}/message) returns an empty
    // {data:[],cursor} envelope for these sessions — the real messages live
    // on the legacy surface, as a bare array of { info, parts } records.
    const r = await apiCall<any[]>(`${OC}/session/${sessionId}/message`);
    return Array.isArray(r) ? r : [];
  },

  /**
   * Send a prompt via POST /session/{id}/prompt_async (async variant).
   * Payload shape (verified against the running opencode server's OpenAPI doc):
   *   { parts: TextPartInput | FilePartInput | AgentPartInput[] }
   * - text:  { type: "text", text }
   * - file:  { type: "file", mime, url, filename? }  — `mime` is REQUIRED and
   *   decides how opencode handles the attachment: only "text/plain" (inlined
   *   via the Read tool) and image/* (base64 media part) are accepted by
   *   OpenAI-Chat providers; anything else (e.g. text/markdown for .md) is
   *   rejected with "does not support media type", so text-ish files must be
   *   sent as text/plain and images with their real mime. `url` accepts both
   *   file:// paths (container workspace) and data: URLs — the browser-side
   *   base64 encoding the chat attachments use for images, so nothing has to
   *   be uploaded into the container first.
   * - agent: { type: "agent", name }  (@-mentions of subagents)
   *
   * Unlike the synchronous POST /session/{id}/message (which blocks until the
   * whole agent run finishes and is therefore prone to proxy/browser timeouts
   * on long tasks), prompt_async forks the run and returns 204 immediately —
   * this is exactly what the official opencode Web UI does. Message content
   * is delivered afterwards via the SSE `session.next.prompted` event, which
   * our reconcile logic in oc/messages.ts already handles.
   */
  async sendPrompt(
    sessionId: string,
    text: string,
    opts?: {
      files?: { mime: string; url: string; filename?: string }[];
      agents?: string[];
      agent?: string;
      model?: ModelRef;
    }
  ): Promise<any> {
    const parts: any[] = [];
    for (const f of opts?.files ?? []) {
      parts.push({ type: "file", mime: f.mime, url: f.url, ...(f.filename ? { filename: f.filename } : {}) });
    }
    for (const name of opts?.agents ?? []) {
      parts.push({ type: "agent", name });
    }
    parts.push({ type: "text", text: text || (opts?.files?.length ? "(attachments)" : "") });
    return apiCall(`${OC}/session/${sessionId}/prompt_async`, {
      method: "POST",
      body: JSON.stringify({
        parts,
        ...(opts?.agent ? { agent: opts.agent } : {}),
        ...(opts?.model
          ? { model: { providerID: opts.model.providerID, modelID: opts.model.id } }
          : {}),
      }),
    });
  },

  async interruptSession(sessionId: string): Promise<any> {
    return apiCall(`${OC}/api/session/${sessionId}/interrupt`, { method: "POST" });
  },

  /**
   * File diffs of one completed round (P1-1). Legacy surface, bare array —
   * same per-file shape the user message's `info.summary.diffs` carries.
   */
  async getSessionDiff(sessionId: string, messageID: string): Promise<FileDiff[]> {
    const r = await apiCall<any[]>(`${OC}/session/${sessionId}/diff?messageID=${encodeURIComponent(messageID)}`);
    return Array.isArray(r) ? r : [];
  },

  /** Revert the file changes a round produced (P1-1, legacy surface). */
  async revertSession(sessionId: string, messageID: string): Promise<any> {
    return apiCall(`${OC}/session/${sessionId}/revert`, {
      method: "POST",
      body: JSON.stringify({ messageID }),
    });
  },

  /**
   * Session todo list (P1-1). Legacy surface, bare Todo[] — used to restore
   * the card when a session is opened or the page refreshed; live updates
   * arrive afterwards via `todo.updated` SSE events (not replayed on open).
   */
  async getSessionTodos(sessionId: string): Promise<unknown[]> {
    const r = await apiCall<any[]>(`${OC}/session/${sessionId}/todo`);
    return Array.isArray(r) ? r : [];
  },

  /** Restore the changes of the last revert (P1-1, legacy surface). */
  async unrevertSession(sessionId: string): Promise<any> {
    return apiCall(`${OC}/session/${sessionId}/unrevert`, { method: "POST" });
  },

  /** Fork a session at (or before) a message into a new session (P1-1). */
  async forkSession(sessionId: string, messageID?: string): Promise<OcSession> {
    return apiCall(`${OC}/session/${sessionId}/fork`, {
      method: "POST",
      body: JSON.stringify(messageID ? { messageID } : {}),
    });
  },

  /** Ask the model to summarize the session (P1-1, compaction summary). */
  async summarizeSession(sessionId: string, model: ModelRef): Promise<any> {
    return apiCall(`${OC}/session/${sessionId}/summarize`, {
      method: "POST",
      body: JSON.stringify({ providerID: model.providerID, modelID: model.id }),
    });
  },

  /** Switch the model for an existing session (opencode ModelRef payload). */
  async setSessionModel(sessionId: string, model: ModelRef): Promise<any> {
    return apiCall(`${OC}/api/session/${sessionId}/model`, {
      method: "POST",
      body: JSON.stringify({ model }),
    });
  },

  async setSessionAgent(sessionId: string, agent: string): Promise<any> {
    return apiCall(`${OC}/api/session/${sessionId}/agent`, {
      method: "POST",
      body: JSON.stringify({ agent }),
    });
  },

  /**
   * Rename a session. v2 has no update route, so this uses the legacy
   * PATCH /session/{id}, which returns the bare legacy Session object.
   */
  async renameSession(sessionId: string, title: string): Promise<OcSession> {
    return apiCall<OcSession>(`${OC}/session/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
  },

  /** Delete a session (legacy DELETE /session/{id}, returns bare true). */
  async deleteSession(sessionId: string): Promise<boolean> {
    return apiCall<boolean>(`${OC}/session/${sessionId}`, { method: "DELETE" });
  },

  /** All models across providers (GET /api/model). */
  async listModels(): Promise<OcModel[]> {
    const r = await apiCall<OcEnvelope<OcModel>>(`${OC}/api/model`);
    return r.data ?? [];
  },

  /**
   * Pending permission requests — opencode v1 GET /permission (bare array).
   * The v2 /api/permission/request endpoint does NOT see pendings registered
   * by tools (separate route scope), so we must use the v1 surface.
   */
  async listPermissionRequests(): Promise<OcPermissionRequest[]> {
    const r = await apiCall<OcPermissionRequest[]>(`${OC}/permission`);
    return Array.isArray(r) ? r : [];
  },

  /**
   * Answer a permission request — v1 POST /permission/{requestID}/reply.
   * Path carries no sessionID; body {reply, message?}.
   */
  async replyPermission(
    sessionId: string,
    requestId: string,
    reply: OcPermissionReply,
    message?: string
  ): Promise<void> {
    void sessionId;
    return apiCall(`${OC}/permission/${requestId}/reply`, {
      method: "POST",
      body: JSON.stringify(message ? { reply, message } : { reply }),
    });
  },

  /** Pending question requests — opencode v1 GET /question (bare array). */
  async listQuestionRequests(): Promise<OcQuestionRequest[]> {
    const r = await apiCall<OcQuestionRequest[]>(`${OC}/question`);
    return Array.isArray(r) ? r : [];
  },

  /**
   * Answer questions — v1 POST /question/{requestID}/reply.
   * `answers` aligns with `questions` in order; each entry is an array of
   * selected option labels. Path carries no sessionID.
   */
  async replyQuestion(
    sessionId: string,
    requestId: string,
    answers: string[][]
  ): Promise<void> {
    void sessionId;
    return apiCall(`${OC}/question/${requestId}/reply`, {
      method: "POST",
      body: JSON.stringify({ answers }),
    });
  },

  /** Dismiss a question request — v1 POST /question/{requestID}/reject. */
  async rejectQuestion(sessionId: string, requestId: string): Promise<void> {
    void sessionId;
    return apiCall(`${OC}/question/${requestId}/reject`, {
      method: "POST",
    });
  },

  /**
   * Agent presets opencode exposes (build / plan / general / ... plus plugin
   * agents). NOTE: v2 GET /api/agent only lists native agents on opencode
   * 1.18.x — agents registered by plugins (oh-my-opencode-slim's
   * orchestrator / designer / oracle / ...) are absent from it. The legacy
   * GET /agent returns the full registry (native + plugin); it uses "name"
   * instead of "id" and returns a bare array.
   */
  async listAgents(): Promise<OcAgent[]> {
    const r = await apiCall<any[]>(`${OC}/agent`);
    return (Array.isArray(r) ? r : [])
      .filter((a) => !a.hidden)
      .map((a) => ({ ...a, id: a.name }));
  },

  /**
   * Global slash commands (GET /command). Note: this route lives outside the
   * /api prefix, like /find/file. Includes commands registered by plugins;
   * returns a bare array (no envelope).
   */
  async listCommands(): Promise<OcCommand[]> {
    const r = await apiCall<OcCommand[]>(`${OC}/command`);
    return Array.isArray(r) ? r : [];
  },

  /**
   * Run a slash command in a session. Payload verified against the running
   * opencode server: both `command` and `arguments` are required strings
   * (pass "" when the command takes no arguments). Returns { info: Message }.
   */
  async runCommand(sessionId: string, command: string, args: string): Promise<any> {
    return apiCall(`${OC}/session/${sessionId}/command`, {
      method: "POST",
      body: JSON.stringify({ command, arguments: args }),
    });
  },

  /** Raw escape hatch onto any opencode route not wrapped above. */
  async oc<T>(path: string, options: RequestInit = {}): Promise<T> {
    return apiCall<T>(`${OC}${path.startsWith("/") ? path : `/${path}`}`, options);
  },

  // --- User-scoped LLM providers + active selection (/api/user-config) ---
  async listUserLlmProviders(): Promise<{ providers: UserLlmProvider[] }> {
    return apiCall("/user-config/llm");
  },

  async createUserLlmProvider(payload: UserLlmProviderInput): Promise<UserLlmProvider> {
    return apiCall("/user-config/llm", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async updateUserLlmProvider(id: string, payload: Partial<UserLlmProviderInput>): Promise<UserLlmProvider> {
    return apiCall(`/user-config/llm/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async deleteUserLlmProvider(id: string): Promise<void> {
    return apiCall(`/user-config/llm/${id}`, { method: "DELETE" });
  },

  async getActiveLlm(): Promise<ActiveLlm> {
    return apiCall("/user-config/active-llm");
  },

  async setActiveLlm(providerId: string | null, model?: string | null): Promise<ActiveLlm> {
    return apiCall("/user-config/active-llm", {
      method: "PUT",
      body: JSON.stringify({ provider_id: providerId, model: model ?? null }),
    });
  },

  // --- User-scoped MCP servers (/api/user-config/mcp) -------------------
  async listUserMcp(): Promise<{ mcp: UserMcpServer[] }> {
    return apiCall("/user-config/mcp");
  },

  async createUserMcp(payload: UserMcpInput): Promise<UserMcpServer> {
    return apiCall("/user-config/mcp", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async updateUserMcp(id: string, payload: Partial<UserMcpInput>): Promise<UserMcpServer> {
    return apiCall(`/user-config/mcp/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async deleteUserMcp(id: string): Promise<void> {
    return apiCall(`/user-config/mcp/${id}`, { method: "DELETE" });
  },

  // --- Per-user enable/disable of built-in MCP servers ------------------
  async listUserBuiltinMcp(): Promise<{ mcp: BuiltinMcpItem[] }> {
    return apiCall("/user-config/builtin-mcp");
  },

  async toggleUserBuiltinMcp(name: string, enabled: boolean): Promise<BuiltinMcpItem & { applied: boolean }> {
    return apiCall(`/user-config/builtin-mcp/${name}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    });
  },

  // --- Config management (host opencode.json) ---------------------------
  async getConfigOverview(): Promise<{
    providers: Record<string, any>;
    mcp: Record<string, any>;
    skills: { name: string; description: string; dir: string }[];
  }> {
    return apiCall("/config");
  },

  async listProvidersConfig(): Promise<{ providers: Record<string, any> }> {
    return apiCall("/config/providers");
  },

  async upsertProvider(providerId: string, config: any): Promise<any> {
    return apiCall(`/config/providers/${providerId}`, {
      method: "POST",
      body: JSON.stringify(config),
    });
  },

  async deleteProvider(providerId: string): Promise<any> {
    return apiCall(`/config/providers/${providerId}`, { method: "DELETE" });
  },

  async listMcp(): Promise<{ mcp: Record<string, any> }> {
    return apiCall("/config/mcp");
  },

  async upsertMcp(name: string, config: any): Promise<any> {
    return apiCall(`/config/mcp/${name}`, {
      method: "POST",
      body: JSON.stringify(config),
    });
  },

  async toggleMcp(name: string, enabled: boolean): Promise<any> {
    return apiCall(`/config/mcp/${name}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    });
  },

  async deleteMcp(name: string): Promise<any> {
    return apiCall(`/config/mcp/${name}`, { method: "DELETE" });
  },

  async listSkills(): Promise<{ skills: { name: string; description: string; dir: string }[] }> {
    return apiCall("/config/skills");
  },

  async getSkill(name: string): Promise<{ name: string; description: string; content: string; dir: string }> {
    return apiCall(`/config/skills/${name}`);
  },

  async upsertSkill(name: string, content: string): Promise<any> {
    return apiCall(`/config/skills/${name}`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
  },

  async deleteSkill(name: string): Promise<any> {
    return apiCall(`/config/skills/${name}`, { method: "DELETE" });
  },

  // --- Built-in (plugin-provided) skill visibility ----------------------
  async listBuiltinSkills(): Promise<{
    reachable: boolean;
    skills: { name: string; description: string; dir: string; enabled: boolean }[];
  }> {
    return apiCall("/config/builtin-skills");
  },

  async toggleBuiltinSkill(name: string, enabled: boolean): Promise<any> {
    return apiCall(`/config/builtin-skills/${name}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    });
  },

  async reloadConfigIntoContainer(): Promise<{ reloaded: boolean; message: string }> {
    return apiCall("/config/reload", { method: "POST" });
  },

  // --- Workspace (project-scope) config & skills -----------------------
  async getProjectConfig(): Promise<{
    scope: string;
    exists: boolean;
    created: boolean;
    valid: boolean;
    content: string;
    config: Record<string, any>;
  }> {
    return apiCall("/workspace/config");
  },

  async saveProjectConfig(content: string): Promise<{ status: string; message: string }> {
    return apiCall("/workspace/config", {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
  },

  async listProjectSkills(): Promise<{ skills: { name: string; description: string; dir: string; scope: string }[] }> {
    return apiCall("/workspace/skills");
  },

  async getProjectSkill(name: string): Promise<{ name: string; description: string; content: string; dir: string; scope: string }> {
    return apiCall(`/workspace/skills/${name}`);
  },

  async upsertProjectSkill(name: string, content: string): Promise<any> {
    return apiCall(`/workspace/skills/${name}`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
  },

  async deleteProjectSkill(name: string): Promise<any> {
    return apiCall(`/workspace/skills/${name}`, { method: "DELETE" });
  },

  async importSkillsZip(file: File): Promise<{ status: string; imported: { name: string; description: string; dir: string; scope: string; fileCount: number }[]; message: string }> {
    const form = new FormData();
    form.append("file", file);
    return apiCall("/workspace/skills/import", {
      method: "POST",
      body: form,
    });
  },

  // --- Chat attach: skill picker + file upload --------------------------
  /** Skill 选择器的主数据源：平台侧合并列表，后端返回 global/project/builtin
   *  三类（builtin 为运行中的容器内 opencode 插件注册的 skill），scope 已标注。 */
  async listAllSkills(): Promise<{ skills: { name: string; description: string; dir: string; scope: string }[] }> {
    return apiCall("/workspace/skills/all");
  },

  /**
   * opencode 原生 skill 列表（v1 面 GET /skill，返回裸数组）。每项含
   * name / description? / location（容器内绝对路径）/ content。现仅作
   * listAllSkills 失败时的回退。
   */
  async listNativeSkills(): Promise<
    { name: string; description?: string; location: string; content: string }[]
  > {
    const r = await apiCall<any[]>(`${OC}/skill`);
    return Array.isArray(r) ? r : [];
  },

  async uploadChatFile(file: File): Promise<{
    status: string;
    path: string;
    absPath: string;
    filename: string;
    size: number;
    mime: string;
    isImage: boolean;
  }> {
    const form = new FormData();
    form.append("file", file);
    return apiCall("/workspace/files/upload", {
      method: "POST",
      body: form,
    });
  },

  // --- Workspace file browser -------------------------------------------
  /**
   * `protected` 为平台托管路径前缀（项目 opencode.json、.opencode/），删除
   * 时命中需弹二次确认并带 force=true。清单由后端下发，避免两端各写一份。
   */
  async listWorkspaceFiles(): Promise<{
    files: { path: string; type: "file" | "dir"; size: number }[];
    protected?: string[];
  }> {
    return apiCall("/workspace/files");
  },

  // --- Projects (project-scoped sessions) --------------------------------
  // The platform stores only the roster; session membership is derived from
  // session.location.directory === project.directory.
  async listProjects(): Promise<{ projects: ProjectInfo[] }> {
    return apiCall("/projects");
  },

  /**
   * mode "create": fresh dir at /workspace/projects/{name}（目录名=项目名）;
   * mode "bind": existing dir，项目名自动取目录最后一段（不传 name）。
   */
  async createProject(opts: {
    name?: string;
    mode: "create" | "bind";
    directory?: string;
  }): Promise<ProjectInfo> {
    return apiCall("/projects", {
      method: "POST",
      body: JSON.stringify(opts),
    });
  },

  /** Unbinds the project and deletes its sessions; the directory is kept. */
  async deleteProject(id: string): Promise<{ deleted: boolean; sessionsDeleted: number }> {
    return apiCall(`/projects/${id}`, { method: "DELETE" });
  },

  async readWorkspaceFile(path: string): Promise<{
    type: "text" | "image" | "binary" | "pptx";
    mime: string;
    content?: string;
    base64?: string;
    size?: number;
  }> {
    return apiCall(`/workspace/file-content?path=${encodeURIComponent(path)}`);
  },

  /**
   * 获取 pptx 的原始字节（浏览器端 pptx-wasm 高保真渲染用）。与
   * downloadWorkspaceFiles 一样不能走 apiCall（它假定 JSON 响应体）；
   * 401 处理保持一致。
   */
  async readWorkspaceFileRaw(path: string): Promise<ArrayBuffer> {
    const token = localStorage.getItem("token");
    const resp = await fetch(
      `${API_BASE}/workspace/file-raw?path=${encodeURIComponent(path)}`,
      {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      }
    );
    if (resp.status === 401 && token) {
      for (const key of ["token", "username", "userId", "role"]) {
        localStorage.removeItem(key);
      }
      window.location.reload();
      throw new Error("登录已过期，请重新登录");
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
    }
    return resp.arrayBuffer();
  },

  /**
   * 批量下载工作区文件/目录：后端打包为 zip 以二进制响应返回，这里取回
   * Blob 并触发浏览器保存。不能走 apiCall（它假定 JSON 响应体）；401
   * 处理与 apiCall / fetchFastkChunkImage 保持一致。
   */
  async downloadWorkspaceFiles(paths: string[]): Promise<void> {
    const token = localStorage.getItem("token");
    const resp = await fetch(`${API_BASE}/workspace/files/download`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ paths }),
    });
    if (resp.status === 401 && token) {
      for (const key of ["token", "username", "userId", "role"]) {
        localStorage.removeItem(key);
      }
      window.location.reload();
      throw new Error("登录已过期，请重新登录");
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "workspace-files.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },

  /**
   * 批量删除工作区文件/目录（目录连同子树）。force 仅在用户对平台托管路径
   * 二次确认后传 true；否则后端命中托管路径会返回 409。
   */
  async deleteWorkspaceFiles(
    paths: string[],
    force = false
  ): Promise<{ status: "ok" | "partial"; deleted: string[]; failed: string[] }> {
    return apiCall("/workspace/files/delete", {
      method: "POST",
      body: JSON.stringify({ paths, force }),
    });
  },

  /**
   * Fuzzy file search backed by opencode's own /find/file endpoint (the same
   * index the @-mention picker uses internally). Returns workspace-relative
   * paths. Note: this route lives outside the /api prefix.
   */
  async findFiles(query: string, limit = 20): Promise<string[]> {
    return apiCall(
      `${OC}/find/file?query=${encodeURIComponent(query)}&limit=${limit}&type=file`
    );
  },

  // --- fastk 知识库引用（assistant 消息中的 chunk 引用徽章） -------------
  /** 按 chunk_id 取回知识库 chunk 完整内容（含元数据）。 */
  async getFastkChunk(db: string, chunkId: string): Promise<FastkChunk> {
    return apiCall(
      `/fastk/chunk?db=${encodeURIComponent(db)}&chunk_id=${encodeURIComponent(chunkId)}`
    );
  },

  /**
   * chunk 附图（Blob）。<img> 标签无法携带 Authorization 头，所以先在此
   * 带鉴权取回 Blob，再由调用方转 objectURL 展示。401 处理与 apiCall 一致。
   * index 指向该 chunk 的第 index 张附图（缺省第一张）。
   */
  async fetchFastkChunkImage(db: string, chunkId: string, index = 0): Promise<Blob> {
    const token = localStorage.getItem("token");
    const resp = await fetch(
      `${API_BASE}/fastk/chunk-image?db=${encodeURIComponent(db)}&chunk_id=${encodeURIComponent(chunkId)}&index=${index}`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} }
    );
    if (resp.status === 401 && token) {
      for (const key of ["token", "username", "userId", "role"]) {
        localStorage.removeItem(key);
      }
      window.location.reload();
      throw new Error("登录已过期，请重新登录");
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    return resp.blob();
  },

  // --- SSE --------------------------------------------------------------
  // P1-5: the long-lived JWT never goes in the SSE URL anymore (it leaks via
  // logs/history). Swap it for a one-time, 60s ticket first. Because the
  // ticket is single-use, the browser's native EventSource auto-reconnect
  // can't replay it — Chat.tsx drives reconnection manually with fresh
  // tickets instead.
  async createEventSource(lastEventId: number = 0): Promise<EventSource> {
    const { ticket } = await apiCall<{ ticket: string }>("/tunnel/ticket", {
      method: "POST",
    });
    const url = `${API_BASE}/tunnel/events?ticket=${encodeURIComponent(
      ticket
    )}&lastEventId=${lastEventId}`;
    return new EventSource(url);
  },

  // --- PPTX 模板库（用户侧只读） ----------------------------------------
  /** 已上架、非 sample 的模板列表 —— 选择器展示的就是这些。 */
  async listLibraryTemplates(): Promise<{
    templates: LibraryTemplateCard[];
    count: number;
  }> {
    return apiCall("/library/templates");
  },

  /** 预定义调色板 / 风格配方 / 排版规则。 */
  async getLibraryStyles(): Promise<LibraryStyles> {
    return apiCall("/library/styles");
  },

  async getLibraryTemplate(id: string): Promise<LibraryTemplateDetail> {
    return apiCall(`/library/templates/${encodeURIComponent(id)}`);
  },

  /**
   * 首页缩略图。<img> 无法携带 Authorization 头，所以带鉴权取回 Blob 再由调用方
   * 转 objectURL（同 fetchFastkChunkImage）。has_thumb=false 时后端返回 404，
   * 这里吞成 null，画廊据此退化为调色板占位。401 处理与 apiCall 一致。
   */
  async fetchLibraryThumb(id: string): Promise<Blob | null> {
    const token = localStorage.getItem("token");
    const resp = await fetch(
      `${API_BASE}/library/templates/${encodeURIComponent(id)}/thumb`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} }
    );
    if (resp.status === 401 && token) {
      for (const key of ["token", "username", "userId", "role"]) {
        localStorage.removeItem(key);
      }
      window.location.reload();
      throw new Error("登录已过期，请重新登录");
    }
    if (resp.status === 404) return null;
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    return resp.blob();
  },

  /**
   * 模板原始字节（管理员）：在浏览器端用 pptx-wasm 渲染首页并生成缩略图。
   * 走 admin 路由——用户侧 /file 对 source="sample" 的素材一律 404。
   */
  async adminFetchLibraryFile(id: string): Promise<ArrayBuffer> {
    const token = localStorage.getItem("token");
    const resp = await fetch(
      `${API_BASE}/admin/library/templates/${encodeURIComponent(id)}/file`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} }
    );
    if (resp.status === 401 && token) {
      for (const key of ["token", "username", "userId", "role"]) {
        localStorage.removeItem(key);
      }
      window.location.reload();
      throw new Error("登录已过期，请重新登录");
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    return resp.arrayBuffer();
  },

  /** 管理员侧缩略图：与 fetchLibraryThumb 同语义，但能看到 sample 模板。 */
  async adminFetchLibraryThumb(id: string): Promise<Blob | null> {
    const token = localStorage.getItem("token");
    const resp = await fetch(
      `${API_BASE}/admin/library/templates/${encodeURIComponent(id)}/thumb`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} }
    );
    if (resp.status === 401 && token) {
      for (const key of ["token", "username", "userId", "role"]) {
        localStorage.removeItem(key);
      }
      window.location.reload();
      throw new Error("登录已过期，请重新登录");
    }
    if (resp.status === 404) return null;
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    return resp.blob();
  },

  // --- PPTX 模板库（管理员） --------------------------------------------
  async adminLibraryStats(): Promise<LibraryStats> {
    return apiCall("/admin/library/stats");
  },

  /** enabled 省略 = 全部（含已下架）；true = 仅上架；false = 仅下架。 */
  async adminListLibraryTemplates(enabled?: boolean): Promise<{
    templates: LibraryTemplateCard[];
    count: number;
  }> {
    const qs = enabled === undefined ? "" : `?enabled=${enabled}`;
    return apiCall(`/admin/library/templates${qs}`);
  },

  async adminGetLibraryTemplate(id: string): Promise<LibraryTemplateDetail> {
    return apiCall(`/admin/library/templates/${encodeURIComponent(id)}`);
  },

  /**
   * 入库一个模板：后端跑规范化流水线（剥推广页 / 清孤儿 media / 图片瘦身 /
   * 品牌清洗 / 残留文本扫描 / 校验）后落盘，返回规范化报告供人工复核。
   * 表单字段承载不了数组，tags 与 must_replace 以逗号或换行分隔。
   */
  async adminIngestLibraryTemplate(
    input: LibraryIngestInput
  ): Promise<LibraryIngestResult> {
    const form = new FormData();
    form.append("file", input.file, input.file.name);
    const text: Record<string, string | undefined> = {
      name: input.name,
      name_zh: input.nameZh,
      description: input.description,
      palette: input.palette,
      recipe: input.recipe,
      license: input.license,
      source: input.source,
      tags: input.tags?.length ? input.tags.join(",") : undefined,
      must_replace: input.mustReplace?.length
        ? input.mustReplace.join("\n")
        : undefined,
    };
    for (const [key, value] of Object.entries(text)) {
      if (value !== undefined && value !== "") form.append(key, value);
    }
    form.append("enabled", String(input.enabled ?? true));
    form.append("optimize_images", String(input.optimizeImages ?? true));
    form.append("drop_promo", String(input.dropPromo ?? true));
    return apiCall("/admin/library/templates", { method: "POST", body: form });
  },

  async adminUpdateLibraryTemplate(
    id: string,
    patch: LibraryTemplatePatch
  ): Promise<{ status: string; template: LibraryTemplateCard }> {
    return apiCall(`/admin/library/templates/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
  },

  /** 上传首页预览图（浏览器渲染出的 PNG）；后端用 Pillow 重编码并限尺寸。 */
  async adminUploadLibraryThumb(
    id: string,
    image: Blob
  ): Promise<{ status: string; has_thumb: boolean; thumbUrl: string }> {
    const form = new FormData();
    form.append("file", image, `${id}.png`);
    return apiCall(`/admin/library/templates/${encodeURIComponent(id)}/thumb`, {
      method: "PUT",
      body: form,
    });
  },

  async adminDeleteLibraryTemplate(
    id: string
  ): Promise<{ status: string; deleted: string }> {
    return apiCall(`/admin/library/templates/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  /** 重跑仓库种子目录的增量入库（按源 sha 去重，可反复调用）。 */
  async adminSeedLibrary(allowSamples = false): Promise<LibrarySeedResult> {
    return apiCall("/admin/library/seed", {
      method: "POST",
      body: JSON.stringify({ allow_samples: allowSamples }),
    });
  },
};

// --- 意见反馈 & 心愿墙（设计文档 §7.5） ------------------------------------

export type OpinionStatus =
  | "open"
  | "resolved"
  | "evaluating"
  | "planned"
  | "developing";
export type FeedbackCategory = "bug" | "feature";
export type WishStatus = "evaluating" | "planned" | "developing" | "done";
export type WishType = "model" | "memory" | "ux" | "other";
export type WishScope = "all" | "mine" | "favorited" | "boosted";
export type WishSort =
  | "boost_desc"
  | "boost_asc"
  | "favorite_desc"
  | "favorite_asc"
  | "created_desc"
  | "created_asc";

export interface OpinionItem {
  id: number;
  category: FeedbackCategory;
  name: string;
  uid: string | null;
  content: string;
  status: OpinionStatus;
  linked_wish_id: number | null;
  attachment_count: number;
  created_at: string;
}
export interface OpinionListResp {
  items: OpinionItem[];
  total: number;
  page: number;
  page_size: number;
}

/** GET /api/admin/opinions/stats（D34 看板卡片）。 */
export interface OpinionCategoryStats {
  total: number;
  by_status: Record<string, number>;
  with_attachment: number;
  unresolved_7d: number; // 仅 bug 有意义
  linked_to_wish: number; // 仅 feature 有意义
  conversion_rate: number; // 仅 feature 有意义
}
export interface OpinionStatsResp {
  bug: OpinionCategoryStats;
  feature: OpinionCategoryStats;
}

export interface OpinionPatchResp {
  id: number;
  status: OpinionStatus;
  category: FeedbackCategory;
}

export interface OpinionAttachmentItem {
  id: number;
  width: number;
  height: number;
  size_bytes: number;
  content_type: string;
  url: string | null; // 仅管理员端点返回（D35）
  created_at: string | null;
}
export interface OpinionAttachmentListResp {
  feedback_id: number;
  items: OpinionAttachmentItem[];
}

export interface ToWishResp {
  feedback_id: number;
  wish_id: number;
  status: OpinionStatus;
  linked_wish_id: number;
}

/** 提交回执里的 attachments 只是「已上传 N 张」的凭据：无 url（D35，
 *  提交者本人不可回看截图），形状比管理员端点的 OpinionAttachmentItem 窄。 */
export interface OpinionCreateResp {
  id: number;
  category: FeedbackCategory;
  status: OpinionStatus;
  created_at: string;
  attachments: {
    id: number;
    width: number;
    height: number;
    size_bytes: number;
    content_type: string;
  }[];
}

export interface WishItem {
  id: number;
  title: string;
  description: string;
  type: WishType;
  status: WishStatus;
  boost_count: number;
  favorite_count: number;
  created_at: string;
  updated_at: string | null;
  my_boosted: boolean;
  my_favorited: boolean;
  is_mine: boolean;
  author_name: string | null; // 仅管理员请求时非空
  deleted_at: string | null; // 仅管理员 include_hidden 时非空
  linked_feedback_id: number | null; // 仅 POST 响应回填
}
export interface WishListResp {
  items: WishItem[];
  total: number;
  page: number;
  page_size: number;
}
export interface WishActionResp {
  wish_id: number;
  action: "boost" | "favorite";
  active: boolean;
  boost_count: number;
  favorite_count: number;
}
export interface WishStats {
  total: number;
  by_status: Record<WishStatus, number>;
  mine: number;
  my_boosted: number;
  my_favorited: number;
}

/** 提交意见反馈（multipart，D33）。images 为空则不 append 该 part；
 *  4xx 时 Error 上带 status（429 限流 / 413 图过大 / 422 校验）。 */
export async function submitOpinion(body: {
  category: FeedbackCategory;
  content: string;
  images?: File[];
}): Promise<OpinionCreateResp> {
  const fd = new FormData();
  fd.append("category", body.category);
  fd.append("content", body.content);
  for (const f of body.images ?? []) fd.append("images", f); // 同名重复 part（F22）
  return apiCall<OpinionCreateResp>("/opinions", { method: "POST", body: fd });
}

// —— 以下 /api/admin/opinions/* 全部要求管理员角色（后端 require_admin，403 边界）——
// 注意：apiCall 会自动拼 API_BASE（"/api"），这里的 path 不要再带 /api 前缀。

/** 管理侧反馈列表：status 数组由 uxQuery 序列化为逗号串（F14 已核实）。 */
export async function listOpinions(params: {
  page?: number;
  page_size?: number;
  category?: FeedbackCategory;
  status?: OpinionStatus[];
  q?: string;
}): Promise<OpinionListResp> {
  return apiCall<OpinionListResp>(
    `/admin/opinions${uxQuery({
      ...params,
      status: params.status?.length ? params.status.join(",") : undefined,
    })}`
  );
}

export async function getOpinionStats(): Promise<OpinionStatsResp> {
  return apiCall<OpinionStatsResp>("/admin/opinions/stats");
}

/** 改状态 / 改分类（二者正交：改分类不联动状态、不触发转心愿，§5.1）。 */
export async function patchOpinion(
  id: number,
  body: Partial<{ status: OpinionStatus; category: FeedbackCategory }>
): Promise<OpinionPatchResp> {
  return apiCall<OpinionPatchResp>(`/admin/opinions/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function listOpinionAttachments(
  id: number
): Promise<OpinionAttachmentListResp> {
  return apiCall<OpinionAttachmentListResp>(`/admin/opinions/${id}/attachments`);
}

/** F20 惯例：<img> 无法携带 Authorization → 取 Blob 再 createObjectURL。
 *  404（文件丢失）经适配 1 抛出带 status: 404 的 Error，调用方渲染占位灰块。 */
export async function fetchOpinionAttachmentBlob(aid: number): Promise<Blob> {
  const res = await apiCall<Response>(`/admin/opinions/attachments/${aid}`, {
    raw: true,
  });
  return res.blob();
}

/** 管理员通道：一键把反馈转为心愿（重复转化 → Error.status === 409）。 */
export async function opinionToWish(
  id: number,
  body: { title: string; description?: string; type?: WishType }
): Promise<ToWishResp> {
  return apiCall<ToWishResp>(`/admin/opinions/${id}/to-wish`, {
    method: "POST",
    body: JSON.stringify({
      title: body.title,
      description: body.description ?? "",
      type: body.type ?? "other",
    }),
  });
}

// —— 心愿墙（登录用户均可访问；status 改动 / 删除恢复由后端按角色 403）——

export async function listWishes(params: {
  q?: string;
  status?: WishStatus[];
  scope?: WishScope;
  sort?: WishSort;
  page?: number;
  page_size?: number;
  include_hidden?: boolean; // 仅管理员生效（服务端角色收敛）
}): Promise<WishListResp> {
  return apiCall<WishListResp>(
    `/wishes${uxQuery({
      ...params,
      status: params.status?.length ? params.status.join(",") : undefined,
    })}`
  );
}

/** source_feedback_id：用户自助转心愿通道（D30①），缺省时等同普通发布。 */
export async function createWish(body: {
  title: string;
  description?: string;
  type?: WishType;
  source_feedback_id?: number;
}): Promise<WishItem> {
  return apiCall<WishItem>("/wishes", {
    method: "POST",
    body: JSON.stringify({
      title: body.title,
      description: body.description ?? "",
      type: body.type ?? "other",
      source_feedback_id: body.source_feedback_id,
    }),
  });
}

/** 作者可改 title/description/type；status 仅管理员（作者传 status → 403）。 */
export async function patchWish(
  id: number,
  body: Partial<{
    title: string;
    description: string;
    type: WishType;
    status: WishStatus;
  }>
): Promise<WishItem> {
  return apiCall<WishItem>(`/wishes/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

/** 软删除（管理员专属，普通用户 → 403）。 */
export async function deleteWish(
  id: number
): Promise<{ id: number; deleted: boolean }> {
  return apiCall(`/wishes/${id}`, { method: "DELETE" });
}

/** 恢复软删除的心愿（管理员专属）。 */
export async function restoreWish(
  id: number
): Promise<{ id: number; deleted: boolean }> {
  return apiCall(`/wishes/${id}/restore`, { method: "POST" });
}

/** 助力 / 收藏 toggle（D22：再次点击即取消）。 */
export async function toggleWishAction(
  id: number,
  action: "boost" | "favorite"
): Promise<WishActionResp> {
  return apiCall<WishActionResp>(`/wishes/${id}/actions`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}

export async function getWishStats(): Promise<WishStats> {
  return apiCall<WishStats>("/wishes/stats");
}

// —— AI 文本润色（输入区 ✨ 按钮；平台直连容器同款模型，非流式一次性返回）——

export interface PolishResult {
  text: string;
  model: string; // "providerID/id"，提示条展示用
  elapsed_ms: number;
}

/**
 * 润色输入区草稿。``model`` 传当前会话选中项即可跟随用户选择；缺省时后端
 * 用宿主默认模型。失败抛出的 Error 带 ``status``（400 内容问题 / 429 太频繁 /
 * 502 上游失败 / 503 无可用模型），调用方原样展示 detail。
 */
export async function polishText(
  text: string,
  model?: ModelRef | null
): Promise<PolishResult> {
  return apiCall<PolishResult>("/text/polish", {
    method: "POST",
    body: JSON.stringify({
      text,
      model: model ? { providerID: model.providerID, id: model.id } : null,
    }),
  });
}
