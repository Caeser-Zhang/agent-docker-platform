# 前端预览适配设计文档 —— 事件捕获与渲染逻辑

> 项目：Agent Workspace 前端 ｜ 版本：v0.1 草案 ｜ 日期：2026-09-07
> 配套文档：`present_file-tool-design.md`（工具侧与事件协议）
> 职责：捕获 `file.presented` 事件 → 弹出预览窗口 → 按 kind 路由渲染 → 防打扰与会话恢复

---

## 1. 总体架构

```
OpenCode /event SSE 流
  └─ 事件捕获层 EventCapture        —— 统一解析，分发 file.presented
       └─ 预览调度层 PreviewStore    —— 去重、关闭记忆、多标签管理（Zustand）
            └─ 预览路由层 resolveViewer —— kind → 渲染器组件映射
                 └─ 渲染器矩阵         —— iframe / markdown / img / pdf.js / 卡片
                      └─ 预览面板容器   —— 右侧抽屉 / 多标签页，双主题
```

**分层原则**：捕获层不认识"业务"，调度层不认识"渲染"，路由层不认识"网络"——每层可独立替换。

---

## 2. 事件捕获层

### 2.1 SSE 订阅与过滤

```typescript
// src/lib/preview/eventCapture.ts
import type { PresentEvent } from "./types"

export function subscribePresentEvents(
  eventSource: EventSource,                 // OpenCode /event 流（已有连接则复用）
  handlers: { onPresent: (e: PresentEvent) => void },
) {
  eventSource.addEventListener("message", (msg) => {
    const event = safeParse(msg.data)
    if (event?.type !== "file.presented") return   // 非目标事件直接放行
    const e = validatePresentEvent(event.properties)   // 校验必填字段
    if (!e) return console.warn("[preview] invalid present event", event)
    handlers.onPresent(e)
  })
}

function validatePresentEvent(p: any): PresentEvent | null {
  if (!p?.path || !p?.kind) return null
  if (!p.absPath && !p.url) return null          // 二者必须有其一（工具侧契约）
  return {
    sessionID: p.sessionID,
    callID: p.callID,
    path: p.path,
    src: p.url ?? `file://${p.absPath}`,         // 统一为可访问 src
    channel: p.url ? "gateway" : "local",
    title: p.title ?? p.path.split("/").pop()!,
    note: p.note ?? null,
    mode: p.mode ?? "auto",
    focus: p.focus ?? true,
    kind: p.kind,
    mime: p.mime,
    size: p.size ?? 0,
    ts: p.ts ?? Date.now(),
  }
}
```

### 2.2 捕获层要点
- **复用已有 SSE 连接**：不新建 EventSource，挂在现有事件分发器上（项目里已有 OpenCode 事件总线则直接挂总线）；
- **通道无关**：捕获层只消费 `src`（工具侧已把 absPath/url 统一），不感知本地/Docker；
- **容错**：非法事件丢弃并告警，不阻塞事件流。

---

## 3. 预览调度层（Store）

### 3.1 状态设计

```typescript
// src/stores/previewStore.ts
import { create } from "zustand"
import { persist } from "zustand/middleware"   // 关闭记忆持久化到 localStorage

interface PreviewTab {
  id: string                    // path
  path: string
  src: string
  title: string
  note: string | null
  kind: string
  mime: string
  size: number
  version: number               // 同 path 重复展示时 +1，用于刷新渲染器
}

interface PreviewState {
  tabs: PreviewTab[]
  activeTab: string | null
  panelOpen: boolean
  dismissed: Record<string, number>   // path -> ts，手动关闭记忆
  autoOpen: boolean                   // 全局开关，默认 true（用户可关）

  push: (e: PresentEvent) => void
  setActive: (id: string) => void
  close: (id: string) => void
  toggleAutoOpen: () => void
}

export const usePreviewStore = create<PreviewState>()(
  persist(
    (set, get) => ({
      tabs: [], activeTab: null, panelOpen: false,
      dismissed: {}, autoOpen: true,

      push: (e) => set((s) => {
        // 1) 手动关闭过的路径不自动弹
        if (s.dismissed[e.path] && !e.focus) return {}
        // 2) 已有同路径标签 → 原位刷新（版本号驱动重渲染），不新开
        const idx = s.tabs.findIndex((t) => t.path === e.path)
        const tab: PreviewTab = {
          id: e.path, path: e.path, src: e.src, title: e.title,
          note: e.note, kind: e.kind, mime: e.mime,
          size: e.size, version: idx >= 0 ? s.tabs[idx].version + 1 : 1,
        }
        const tabs = idx >= 0
          ? s.tabs.map((t, i) => (i === idx ? tab : t))
          : [...s.tabs, tab].slice(-8)          // 上限 8 个标签，LRU 淘汰
        // 3) 自动打开条件：全局开关 && focus 事件 && 面板未开或事件要求聚焦
        const shouldOpen = s.autoOpen && e.focus
        return {
          tabs,
          activeTab: tab.id,
          panelOpen: s.panelOpen || shouldOpen,
          dismissed: e.focus ? omit(s.dismissed, e.path) : s.dismissed,  // 重新展示则清除记忆
        }
      }),

      setActive: (id) => set({ activeTab: id, panelOpen: true }),
      close: (id) => set((s) => ({
        tabs: s.tabs.filter((t) => t.id !== id),
        activeTab: s.activeTab === id ? (s.tabs.at(-2)?.id ?? null) : s.activeTab,
        panelOpen: s.tabs.length > 1,
        dismissed: { ...s.dismissed, [id]: Date.now() },   // 手动关闭 → 记忆
      })),
      toggleAutoOpen: () => set((s) => ({ autoOpen: !s.autoOpen })),
    }),
    {
      name: "preview-tabs",
      partialize: (s) => ({ autoOpen: s.autoOpen, dismissed: s.dismissed }),
      // 持久化只存偏好与关闭记忆，tabs/panelOpen 不存（会话恢复由 §6 处理）
    },
  ),
)
```

### 3.2 调度规则汇总

| 场景 | 行为 |
|---|---|
| 新文件 + focus | 弹出面板并聚焦该标签 |
| 新文件 + 非 focus（同回复第 2+ 个文件） | 面板已开则静默加标签；未开则仍弹（首个触发） |
| 同 path 再次展示 | 原标签刷新 version，不重复开 |
| 手动关闭后同 path 再展示 | 不自动弹，聊天流中仍保留 artifact 卡片可点开 |
| 标签数 > 8 | LRU 淘汰最旧 |
| autoOpen=false（用户关了自动弹） | 不弹，仅出现"查看"卡片，点击打开 |

---

## 4. 预览路由层（kind → 渲染器）

```typescript
// src/components/preview/resolveViewer.tsx
import { lazy, Suspense } from "react"

const WebViewer   = lazy(() => import("./viewers/WebViewer"))
const MdViewer    = lazy(() => import("./viewers/MdViewer"))
const ImgViewer   = lazy(() => import("./viewers/ImgViewer"))
const PdfViewer   = lazy(() => import("./viewers/PdfViewer"))
const DataViewer  = lazy(() => import("./viewers/DataViewer"))
const CodeViewer  = lazy(() => import("./viewers/CodeViewer"))
const FileCard    = lazy(() => import("./viewers/FileCard"))   // 通用兜底

export function resolveViewer(kind: string) {
  const map: Record<string, React.LazyExoticComponent<any>> = {
    webpage: WebViewer, markdown: MdViewer, image: ImgViewer,
    pdf: PdfViewer, data: DataViewer, code: CodeViewer,
  }
  return map[kind] ?? FileCard
}
```

### 4.1 各渲染器要点

**WebViewer（webpage）— 安全重点**
```tsx
export default function WebViewer({ tab }: { tab: PreviewTab }) {
  // 1) 本地通道：file:// 协议 iframe 不可用 → 请求前端 dev server / 静态服务包装
  //    方案：前端已有本地静态中间件（express/vite proxy）暴露 /local-file?abs=...
  // 2) gateway 通道：src 直接是 http URL
  const src = tab.src.startsWith("file://")
    ? `/api/local-file?abs=${encodeURIComponent(tab.src.slice(7))}`
    : tab.src
  return (
    <iframe
      key={tab.version}                       // version 变化 → 整体重载
      src={src}
      className="h-full w-full"
      sandbox="allow-scripts allow-forms"     // 关键：无 allow-same-origin，隔离脚本
      referrerPolicy="no-referrer"
      title={tab.title}
    />
  )
}
```
- `sandbox` 不含 `allow-same-origin`：HTML 内脚本可跑但拿不到宿主 origin，防越权；
- Gateway 侧对静态文件响应追加 `Content-Security-Policy: default-src 'self'` 基线（双保险）；
- `key={tab.version}` 保证文件更新后 iframe 强制重载。

**MdViewer（markdown）**
```tsx
import Markdown from "react-markdown"
import remarkGfm from "remark-gfm"
// 主题：跟随全局亮/暗主题变量；md 内图片若为相对路径 → 同 WebViewer 的 /api/local-file 包装
```

**ImgViewer / PdfViewer**
- image：`<img src>` 直出，超 20MB 提示下载；
- pdf：优先 `<embed src>`（Chromium 内置 viewer），失败降级 pdf.js，再降级 FileCard。

**DataViewer（csv/json）**
- csv ≤ 5MB：papaparse 解析虚拟表格（可复用现有表格组件）；
- json：折叠树查看器；超限降级 CodeViewer。

**CodeViewer / FileCard**
- 代码高亮 + 复制按钮；
- FileCard：图标 + 文件名 + 大小 + 下载按钮（office 类型 pptx/xlsx/docx 走这里；P2 规划服务端转 HTML 预览）。

### 4.2 渲染降级链

```
webpage: sandbox iframe → gateway url 直开新标签
markdown: react-markdown → FileCard
pdf:      embed → pdf.js → FileCard
data:     表格/树 → CodeViewer → FileCard
office:   (P2 服务端转换) → FileCard（下载）
```

---

## 5. 预览面板容器（UI 层）

```
┌─ 主界面 ──────────────────────────────────────┐
│ 聊天区（中）           │ 预览面板（右侧抽屉）    │
│                       │ ┌──────────────────┐  │
│  ...消息流            │ │[报告.html][周报.md]×│  │  ← 多标签
│  ┌ artifact 卡片 ┐    │ ├──────────────────┤  │
│  │ 季度趋势报告   │    │ │                  │  │
│  │ [查看预览]     │    │ │   渲染器内容       │  │
│  └───────────────┘    │ │                  │  │
│                       │ └──────────────────┘  │
│                       │ [在新窗口打开][下载][×] │
└───────────────────────────────────────────────┘
```

交互规格：
- **面板形态**：右侧 480px 抽屉，可拖宽（240–800px），可折叠；
- **artifact 卡片**：每条 `file.presented` 同时在聊天流渲染一张卡片（来源：监听同一 store 的 push 动作写入消息流装饰层），面板关闭后卡片是唯一入口；
- **工具栏**：新窗口打开（browser mode 强制走此路径）、下载、关闭；
- **双主题**：跟随全局主题 token（`--color-background-*` / `--color-text-*`），iframe 内容不受主题影响属预期；
- **大文件（>50MB）**：不进渲染器，直接 FileCard + "文件较大，建议下载查看"；
- **移动端/窄屏**：抽屉变为全屏覆盖层。

---

## 6. Docker 沙箱通道的前端适配

| 事项 | 方案 |
|---|---|
| 文件访问 | 事件带 gateway url，前端直接请求，无需感知容器 |
| HTML 相对资源 | Gateway 静态代理以 `path` 所在目录为 root，相对 css/img 自动可达 |
| 本地通道 file:// | 前端本地静态端点 `/api/local-file?abs=` 包装（**仅允许 workspaceRoot 内路径**，服务端复用工具侧同样的前缀校验，防 SSRF/任意读） |
| 会话恢复 | 切换回历史会话时，前端调 `GET /sessions/:id/presented`（工具执行器顺带落库 presented 记录）恢复标签列表；也可简化为从历史消息流中的 artifact 卡片重建 |

---

## 7. 与聊天流的联动（可选增强，P1）

- present_file 的 tool_call 在消息流中折叠为一行"已展示：季度趋势报告"，点击即 `setActive(path)`；
- Agent 后续迭代文件并再次 present 时，卡片上出现"已更新"徽标（version>1）。

---

## 8. 实施拆分

| 阶段 | 内容 | 预估 |
|---|---|---|
| P0 | 事件捕获 + Store + iframe/图片/Markdown 三种渲染器 + 关闭记忆 | 1–2 天 |
| P1 | pdf/data/code 渲染器、artifact 卡片联动、会话恢复、autoOpen 开关 UI | 1–2 天 |
| P2 | office 服务端转换预览、多标签搜索、预览历史 | 视需求 |

## 9. 测试清单
- 事件流：非法事件丢弃、SSE 断线重连后不丢 focus 事件（可用 last-event-id 补偿）；
- 去重：同 path 三连发 → 单标签 version=3；
- 关闭记忆：关闭后重发不弹，卡片可点开；
- iframe 隔离：预览恶意 HTML（`window.top` 访问、外部请求）验证 sandbox 生效；
- 双通道：本地 file:// 包装与 gateway url 均可达；
- 大文件：50MB+ 图片/HTML 走 FileCard 降级。
