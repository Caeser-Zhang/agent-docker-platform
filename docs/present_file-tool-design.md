# `present_file` 工具实现设计文档

> 项目：Agent Workspace（OpenCode serve API，本地桥接 + Docker 沙箱双通道）
> 版本：v0.1 草案 ｜ 日期：2026-09-07
> 定位：Agent 侧"展示信令"工具 —— 只发事件、不碰渲染，前端据此弹出预览窗口

---

## 1. 目标与非目标

**目标**
1. Agent 在产出交付文件（HTML / MD / 图片 / PDF / pptx 等）后，通过一次工具调用通知前端打开预览；
2. 工具本身与前端解耦：工具只负责产生标准化的 UI 信令事件，渲染完全由前端承接；
3. 同时覆盖本地桥接与 Docker 沙箱两条通道；
4. 对模型友好：参数少、语义明确、系统提示词可约束调用时机。

**非目标**
- 不做文件内容渲染（前端职责）；
- 不做文件托管/上传（沿用现有 workspace 文件服务）；
- 不做预览的持久化收藏（后续迭代）。

---

## 2. 实现形态选型

| 方案 | 说明 | 结论 |
|---|---|---|
| A. OpenCode 内置自定义工具 | 在 agent 配置/插件里注册 tool，事件直接进 OpenCode Bus | ✅ **推荐**。事件天然走 `/event` SSE，零额外链路 |
| B. MCP 工具 | 包装为 MCP server 提供给 Agent | 备选。适合"Agent 端不可改造、只支持 MCP"的场景（如 fastdb 集成模式） |
| C. Skill 引导（提示词 + shell 命令） | skill 里教模型执行 `curl /internal/emit` | 兜底。不可控、易失败，不推荐 |

采用 A 为主、B 为兼容形态：两者最终都收敛到同一个事件协议（见 §4）。

---

## 3. 工具定义

### 3.1 参数 Schema

```jsonc
{
  "name": "present_file",
  "description": "向用户展示一个已生成的交付文件，前端会自动弹出预览窗口。仅在任务完成、文件已写入磁盘后调用；不要用于展示中间产物或仅被读取过的文件。",
  "parameters": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "文件相对路径（相对 workspace 根），如 outputs/report.html"
      },
      "title": {
        "type": "string",
        "description": "预览窗口标题，用户视角的简短命名，如 '季度趋势报告'"
      },
      "mode": {
        "type": "string",
        "enum": ["auto", "inline", "browser"],
        "description": "auto=由前端按类型路由（默认）；inline=强制面板内预览；browser=强制浏览器标签打开（localhost URL 场景）"
      },
      "focus": {
        "type": "boolean",
        "description": "是否立即聚焦到该预览，多文件时仅最后一个设 true，默认 true"
      },
      "note": {
        "type": "string",
        "description": "一句话说明这个文件是什么、看什么（≤50 字）"
      }
    },
    "required": ["path"]
  }
}
```

**设计要点**
- 参数刻意精简：模型调用成功率高；`mode`/`focus`/`note` 均可选，缺省合理。
- 路径用**相对路径**，绝对路径由执行器按通道解析（本地/Docker 前缀不同），避免模型拼错平台路径。
- `description` 中写死调用纪律（只展示最终交付物），配合系统提示词双重约束。

### 3.2 系统提示词补充（约束调用时机）

```
## 交付展示规范
- 当任务产出以下类型文件且应被用户查看时，必须调用 present_file：
  .html .htm .md .markdown .png .jpg .jpeg .svg .gif .webp .pdf .pptx .xlsx .docx .csv
- 只在文件已完整写入磁盘后调用；禁止对流式生成中的半成品调用。
- 一次回复内最多调用 3 次；多文件时用数组路径无法表达，逐个调用并只对最重要的文件设 focus=true。
- 调用后用一句话说明文件内容，不要复述文件正文。
```

---

## 4. 事件协议（工具 ↔ 前端契约）

工具执行器不做任何渲染，唯一的副作用是向 OpenCode 事件总线发布一条事件，随 `/event` SSE 流到达前端。

### 4.1 事件结构

```jsonc
// type: "file.presented"  （自定义业务事件，挂在 OpenCode Bus 上）
{
  "type": "file.presented",
  "properties": {
    "sessionID": "sess_abc123",          // 来源会话
    "callID": "call_x9",                 // 关联的 tool_call id，便于去重/追溯
    "path": "outputs/report.html",       // workspace 相对路径（规范化后）
    "absPath": "D:/ws/xxx/outputs/report.html",  // 本地通道的绝对路径
    "url": null,                         // Docker/Gateway 通道: "http://gateway:7810/files/sess_abc123/outputs/report.html"
    "title": "季度趋势报告",
    "note": "含近 8 周数据与可信度分级",
    "mode": "auto",
    "focus": true,
    "mime": "text/html",                 // 执行器按后缀推断
    "kind": "webpage",                   // webpage|markdown|image|pdf|office|data|code|unknown
    "size": 184320,
    "ts": 1788787200000
  }
}
```

**契约原则**
- `path` 是稳定主键（前端去重、关闭记忆均按 path）；
- `absPath` 与 `url` 二选一非空，由执行器按通道决定（§5）；
- `kind` 由执行器（而非前端）判定，前端路由表只认 kind，新增类型只改执行器不改前端。

### 4.2 kind 路由表（执行器侧判定）

| 后缀 | kind | 前端预期 |
|---|---|---|
| html/htm | webpage | iframe 预览 |
| md/markdown | markdown | markdown 渲染 |
| png/jpg/svg/gif/webp | image | img 标签 |
| pdf | pdf | pdf.js / 内嵌 viewer |
| pptx/xlsx/docx | office | 卡片 + 下载（或服务端转换，P2） |
| csv/json | data | 表格/结构化查看器 |
| 其余文本 | code | 代码高亮 |

---

## 5. 执行器实现

### 5.1 核心流程

```
模型发起 tool_call(present_file)
  → 执行器校验：path 存在、在 workspace 内、类型在支持列表
  → 通道解析：
      本地桥接：absPath = join(workspaceRoot, path)；url = null
      Docker：  url = gateway 静态代理地址；absPath = null
  → 判定 mime / kind
  → 发布 Bus 事件 file.presented
  → 返回 tool result（给模型确认，简短）
```

### 5.2 OpenCode 插件形态（TypeScript 示意）

```typescript
import type { Plugin } from "@opencode-ai/plugin"
import { existsSync, statSync } from "node:fs"
import { join, resolve, sep } from "node:path"
import { lookup } from "node:mime"          // 或内置后缀映射

const KIND_MAP: Record<string, string> = {
  ".html": "webpage", ".htm": "webpage",
  ".md": "markdown", ".markdown": "markdown",
  ".png": "image", ".jpg": "image", ".jpeg": "image",
  ".svg": "image", ".gif": "image", ".webp": "image",
  ".pdf": "pdf",
  ".pptx": "office", ".xlsx": "office", ".docx": "office",
  ".csv": "data", ".json": "data",
}

export const presentFilePlugin: Plugin = async ({ project, client, $ }) => {
  return {
    tool: {
      present_file: async (input: { path: string; title?: string; mode?: string; focus?: boolean; note?: string }) => {
        // 1. 路径安全：禁止越出 workspace
        const root = resolve(project.worktree)
        const abs = resolve(root, input.path)
        if (!abs.startsWith(root + sep) && abs !== root) {
          return { ok: false, error: "path escapes workspace" }
        }
        // 2. 存在性
        if (!existsSync(abs)) return { ok: false, error: `file not found: ${input.path}` }
        const size = statSync(abs).size
        // 3. 类型判定
        const ext = abs.slice(abs.lastIndexOf(".")).toLowerCase()
        const kind = KIND_MAP[ext] ?? "unknown"
        const mime = lookup(abs) ?? "application/octet-stream"
        // 4. 通道解析（通道由环境变量注入，本地桥接 vs 容器内）
        const isSandbox = !!process.env.SANDBOX_SESSION_ID
        const url = isSandbox
          ? `http://${process.env.GATEWAY_HOST}/files/${process.env.SANDBOX_SESSION_ID}/${input.path}`
          : null
        // 5. 发布事件（OpenCode Bus → /event SSE）
        await client.bus.publish({
          type: "file.presented",
          properties: {
            sessionID: process.env.OPENCODE_SESSION_ID!,
            callID: process.env.OPENCODE_CALL_ID!,
            path: input.path.replaceAll("\\", "/"),
            absPath: isSandbox ? null : abs,
            url,
            title: input.title ?? abs.slice(abs.lastIndexOf("/") + 1),
            note: input.note ?? null,
            mode: input.mode ?? "auto",
            focus: input.focus ?? true,
            mime, kind, size, ts: Date.now(),
          },
        })
        return { ok: true, presented: input.path, kind }
      },
    },
  }
}
```

> 注：`client.bus.publish` 为示意 API，落地时以当前 OpenCode 版本的 Bus/事件接口为准；若版本无自定义事件通道，退化为「执行器直接向 SSE 网关写入一行自定义 event」，协议不变。

### 5.3 MCP 兼容形态（方案 B）

当 Agent 端只支持 MCP 时：把上面同一套校验+发事件逻辑包成 MCP server，工具名与 schema 完全一致，事件改由 MCP server 调用内部 HTTP 端点 `POST /internal/emit-event` 注入 SSE 流。**前端无感知，协议不变。**

### 5.4 错误处理与降级

| 场景 | 行为 |
|---|---|
| path 越界 / 不存在 | tool result 返回 error，模型可自行纠正后重试 |
| 类型不在 kind 表 | 仍发事件，kind=unknown，前端展示通用文件卡片 |
| Bus 发布失败 | tool result 返回 error；模型在正文给出文件路径兜底 |
| 重复调用同一 path | 不拦截（模型可能更新文件后重展示），靠前端去重与版本刷新 |

---

## 6. 安全设计

1. **路径封闭**：只允许 workspace 内相对路径，`..` 解析后必须仍在根内；
2. **iframe 沙箱前提**：webpage 类文件默认将在 sandbox iframe 中渲染，工具侧无需过滤内容，但 Gateway 静态代理需设置 `Content-Security-Policy` 响应头基线（禁外发请求可选）；
3. **无敏感操作**：工具只读元信息（存在性/大小），不读取文件内容入上下文，避免大文件撑爆 token；
4. **调用频次**：系统提示词限 3 次/回复；执行器可加会话级滑动窗口限流（如 10 次/分钟）防失控。

---

## 7. 测试要点

- 路径校验：`../` 逃逸、绝对路径注入、Windows 反斜杠；
- 双通道：本地桥接产生 absPath、Docker 产生 url，事件字段互斥；
- 大文件：>50MB 只传元信息，前端卡片提示"过大，下载查看"；
- 端到端：模型生成 HTML → 调用 present_file → SSE 事件到达 → 前端 3s 内弹窗。
