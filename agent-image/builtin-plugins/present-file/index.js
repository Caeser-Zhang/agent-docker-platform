/**
 * present_file — 交付文件"展示信令"工具（Agent Workspace 平台内置）。
 *
 * 形态：opencode server 插件（v1 shape —— default export 带 `id` + `server()`，
 * 由 PluginLoader.resolvePluginEntrypoint 读 package.json 的 `main` 载入，
 * readV1Plugin(mode="detect") 命中后调用 server(input, options)）。
 * 插件路径由后端 _discover_builtin_plugins 读同目录 manifest.json 注入到
 * opencode.json 的 `plugin` 数组，镜像只读、用户无法摘除。
 *
 * 工具注册链路（opencode v1.18.25 tool/registry.ts）：
 *   Hooks.tool 的每个键 → fromPlugin(id, def) → Tool.Def
 * 键名即工具名，所以这里用 `present_file` 保证模型看到的名字与系统提示词一致。
 *
 * ⚠️ args 必须用 zod v4：registry 的 isZodType() 以 "_zod" in value 判定，
 *    zod v3 的 schema 只有 _def，会被判为非 zod 而静默降级到 legacyJsonSchema()，
 *    把 schema 对象本身当 JSON Schema 用，产出错误的工具签名。
 *
 * 事件通道说明（为什么不用自定义 Bus 事件）：
 *   opencode v1.18.25 没有对外发布任意总线事件的入口 —— POST /tui/publish 只接受
 *   四种固定 TUI 事件（prompt.append / command.execute / toast.show / session.select），
 *   且 properties 是封闭 schema，塞不进 path/kind 等自定义字段。
 *   因此本工具把"信令"放在 tool result 里：registry 的 fromPlugin 会把
 *   result.output 原样写进 tool part，随 message.part.updated 经 /event SSE →
 *   backend sse_pump → 前端。前端 tool Block 已携带 name/status/input/output，
 *   于是无需任何自定义事件通道，且会话恢复天然免费（前端从历史消息重建 turns 时
 *   会再次看到这些 completed 的 present_file 调用）。
 *
 * 本工具只读文件元信息（存在性/大小/后缀），绝不读取文件内容进上下文。
 */

import { z } from "zod"
import { existsSync, statSync } from "node:fs"
import { basename, extname, isAbsolute, relative, resolve } from "node:path"

// ---------------------------------------------------------------------------
// 分类表：kind 由工具侧判定，前端路由表只认 kind —— 新增类型只改这里不改前端。
// ---------------------------------------------------------------------------

const KIND_BY_EXT = {
  ".html": "webpage",
  ".htm": "webpage",
  ".md": "markdown",
  ".markdown": "markdown",
  ".mdx": "markdown",
  ".png": "image",
  ".jpg": "image",
  ".jpeg": "image",
  ".gif": "image",
  ".webp": "image",
  ".svg": "image",
  ".bmp": "image",
  ".ico": "image",
  ".avif": "image",
  ".mp4": "video",
  ".webm": "video",
  ".mov": "video",
  ".m4v": "video",
  ".mp3": "audio",
  ".wav": "audio",
  ".ogg": "audio",
  ".m4a": "audio",
  ".flac": "audio",
  ".pdf": "pdf",
  // pptx 单独成 kind：平台前端已有 pptx-wasm 高保真渲染器（PptxSlidePreview），
  // 不能和 xlsx/docx 一起落进"office → 下载卡片"的兜底分支。
  ".pptx": "pptx",
  ".xlsx": "office",
  ".xls": "office",
  ".docx": "office",
  ".doc": "office",
  ".csv": "data",
  ".tsv": "data",
  ".json": "data",
  ".ndjson": "data",
  ".txt": "text",
  ".log": "text",
}

const MIME_BY_EXT = {
  ".html": "text/html",
  ".htm": "text/html",
  ".md": "text/markdown",
  ".markdown": "text/markdown",
  ".mdx": "text/markdown",
  ".txt": "text/plain",
  ".log": "text/plain",
  ".csv": "text/csv",
  ".tsv": "text/tab-separated-values",
  ".json": "application/json",
  ".ndjson": "application/x-ndjson",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".svg": "image/svg+xml",
  ".bmp": "image/bmp",
  ".ico": "image/x-icon",
  ".avif": "image/avif",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
  ".mov": "video/quicktime",
  ".m4v": "video/x-m4v",
  ".mp3": "audio/mpeg",
  ".wav": "audio/wav",
  ".ogg": "audio/ogg",
  ".m4a": "audio/mp4",
  ".flac": "audio/flac",
  ".pdf": "application/pdf",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".xls": "application/vnd.ms-excel",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".doc": "application/msword",
}

// ---------------------------------------------------------------------------
// 会话级滑动窗口限流：防止模型失控刷事件（系统提示词已限 3 次/回复，这里是兜底）。
// ---------------------------------------------------------------------------

const RATE_LIMIT = 10
const RATE_WINDOW_MS = 60_000
const RATE_MAP_MAX = 256
const recent = new Map()

function rateLimited(key) {
  const now = Date.now()
  const hits = (recent.get(key) ?? []).filter((t) => now - t < RATE_WINDOW_MS)
  if (hits.length >= RATE_LIMIT) {
    recent.set(key, hits)
    return true
  }
  hits.push(now)
  recent.set(key, hits)
  // 容器是长驻进程，会话数无上界 —— 顺手清掉已完全过期的键，避免缓慢泄漏。
  if (recent.size > RATE_MAP_MAX) {
    for (const [k, v] of recent) {
      if (v.length === 0 || now - v[v.length - 1] > RATE_WINDOW_MS) recent.delete(k)
    }
  }
  return false
}

/** 统一的失败返回：status 仍是 completed，模型读到 error 字段可自行纠正重试。 */
function fail(error) {
  return { output: JSON.stringify({ ok: false, error }) }
}

/**
 * 解析真正的 workspace 根目录。
 *
 * 平台权威来源是后端注入的 AGENT_WORKDIR（=/workspace）：容器把它 bind-mount
 * 到 workspace 卷，前端 workspace API 也以此为根拼接相对路径，因此 payload.path
 * 必须相对它，前端才取得到文件。
 *
 * ⚠️ 不能优先用 opencode 的 ctx.worktree —— 当工作区不是 git 仓库时（本平台
 *    /workspace 就没有 .git），opencode 的 git-root 探测会一路向上退化到文件系统
 *    根 "/"，导致 root="/" 而文件其实在 /workspace 下，定位必然失败。故 worktree
 *    排到最后，且对 "/" 这种明显不是工作区的退化值做健全性跳过。
 * process.cwd() 可靠：entrypoint 在 exec opencode 前已 `cd /workspace`。
 */
function resolveWorkspaceRoot(ctx) {
  const candidates = [
    process.env.AGENT_WORKDIR,
    process.cwd(),
    ctx?.directory,
    ctx?.worktree,
  ]
  for (const c of candidates) {
    if (typeof c !== "string" || !c.trim()) continue
    const r = resolve(c)
    if (r === "/" || r === "\\") continue // 退化值，不是真实工作区
    return r
  }
  return resolve(process.cwd())
}

/**
 * 把模型给的路径规范化成相对 root 的路径（posix 分隔符）。
 * 基于真实 root 动态剥前缀，而不是硬编码 /workspace —— 这样即使 AGENT_WORKDIR
 * 被部署成别的值也正确。
 */
function toRelative(input, root) {
  let p = String(input ?? "").trim().replaceAll("\\", "/")
  if (!p) return ""
  const normRoot = root.replaceAll("\\", "/").replace(/\/+$/, "") // e.g. "/workspace"
  // 绝对路径落在 root 下：/workspace/outputs/x -> outputs/x
  if (normRoot && (p === normRoot || p.startsWith(normRoot + "/"))) {
    p = p.slice(normRoot.length)
  } else {
    // 模型把 root 的最后一段当相对前缀：workspace/outputs/x -> outputs/x
    const base = normRoot.split("/").filter(Boolean).pop()
    if (base && p.startsWith(base + "/")) p = p.slice(base.length + 1)
  }
  while (p.startsWith("./")) p = p.slice(2)
  return p.replace(/^\/+/, "")
}

const presentFileTool = {
  description: [
    "向用户展示一个已生成的交付文件，前端会自动弹出预览窗口。",
    "仅在任务完成、文件已完整写入磁盘后调用；不要用于展示中间产物或仅被读取过的文件。",
    "path 用相对 workspace 根的路径，如 outputs/report.html。",
  ].join(" "),

  args: {
    path: z.string().describe("文件相对路径（相对 workspace 根），如 outputs/report.html"),
    title: z.string().optional().describe("预览窗口标题，用户视角的简短命名，如 '季度趋势报告'"),
    mode: z
      .enum(["auto", "inline", "browser"])
      .optional()
      .describe("auto=前端按类型路由（默认）；inline=强制面板内预览；browser=强制新标签打开"),
    focus: z.boolean().optional().describe("是否立即聚焦该预览；多文件时仅最重要的一个设 true，默认 true"),
    note: z.string().optional().describe("一句话说明这个文件是什么、看什么（≤50 字）"),
  },

  async execute(args, ctx) {
    // workspace 根先于路径规范化确定：toRelative 需要它来动态剥前缀，且它必须
    // 与前端 workspace API 的根一致（后端注入的 AGENT_WORKDIR），否则 payload.path
    // 前端取不到。详见 resolveWorkspaceRoot 的说明。
    const root = resolveWorkspaceRoot(ctx)
    const rawPath = toRelative(args?.path, root)
    if (!rawPath) return fail("path is required")

    const sessionID = ctx?.sessionID ?? ""
    if (sessionID && rateLimited(sessionID)) {
      return fail("rate limited: too many present_file calls, wait a minute")
    }

    const abs = resolve(root, rawPath)

    // 路径封闭：解析后必须仍在 workspace 内。relative() 以 .. 开头或仍是绝对路径
    // 即为逃逸，比字符串前缀比较更可靠（不受尾分隔符/大小写影响）。
    const rel = relative(root, abs)
    if (!rel || rel.startsWith("..") || isAbsolute(rel)) return fail("path escapes workspace")

    if (!existsSync(abs)) return fail(`file not found: ${rawPath}`)

    let stat
    try {
      stat = statSync(abs)
    } catch (err) {
      return fail(`cannot stat file: ${err?.message ?? err}`)
    }
    if (stat.isDirectory()) return fail("path is a directory, not a file")

    const ext = extname(abs).toLowerCase()
    // 未知类型不拦截：仍发信令，kind=unknown，前端展示通用文件卡片。
    const kind = KIND_BY_EXT[ext] ?? "unknown"
    const mime = MIME_BY_EXT[ext] ?? "application/octet-stream"
    const title = String(args?.title ?? "").trim() || basename(abs)

    const payload = {
      ok: true,
      sessionID: sessionID || null,
      // callID 不在 ToolContext 的公开类型里，但运行时确实带（registry 用它打
      // tool.call_id span 属性）。取到就用，取不到留 null，前端不依赖它。
      callID: typeof ctx?.callID === "string" ? ctx.callID : null,
      path: rel.replaceAll("\\", "/"),
      absPath: abs.replaceAll("\\", "/"),
      // 本平台没有 gateway 静态代理：前端一律用 path 走 backend workspace API 取内容，
      // 所以 url 恒为 null，保留字段只为与设计文档的事件契约对齐。
      url: null,
      title,
      note: args?.note ? String(args.note) : null,
      mode: args?.mode ?? "auto",
      focus: args?.focus ?? true,
      mime,
      kind,
      size: stat.size,
      ts: Date.now(),
    }

    // title 会成为 tool part 的显示标题（registry 透传 result.title），
    // 聊天流里折叠成一行的就是它。metadata 目前前端未消费，先带上便于排查。
    try {
      ctx?.metadata?.({ title: `已展示：${title}`, metadata: { kind, path: payload.path } })
    } catch {
      // metadata 是尽力而为的 UI 提示，失败不影响信令本身
    }

    return { title: `已展示：${title}`, output: JSON.stringify(payload) }
  },
}

export default {
  id: "present-file",
  async server() {
    return { tool: { present_file: presentFileTool } }
  },
}
