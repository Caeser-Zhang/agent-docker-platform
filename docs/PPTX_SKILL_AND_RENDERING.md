# PPTX 生成 Skill 模版能力与前端渲染方案

本文分两部分：第一部分分析 `pptx-generator` skill 对「指定 PPT 模版」的支持能力与边界；第二部分总结当前前端 PPTX 预览的完整渲染方案（数据链路、组件结构、降级策略），作为后续维护与迭代的基线文档。

---

## 一、pptx-generator skill 模版支持分析

### 1.1 结论先行

**可以指定模版，共两条路径，但形态不同：**

| 路径 | 模版形态 | 能力 | 边界 |
| --- | --- | --- | --- |
| ① 文件级模版 | 用户提供现成 `.pptx` 文件 | 复用原有版式/母版/图片/主题，可改文本、删页、复制页、重排页 | 仅 XML 文本级操作，无母版/版式占位符注入 API |
| ② 设计参数模版 | 指定调色板、风格、字体等参数 | 从零生成风格统一的 deck，多页视觉一致性由设计系统保证 | 不是复用某个具体文件，而是「软模版」 |

**明确做不到的：** 把外部 `.pptx` 作为 PptxGenJS 的输入底版（PptxGenJS 只能导出、不能读入已有 pptx）。因此「拿模版文件直接当底版生成新内容」必须走路径①的 XML 工作流，而非 PptxGenJS。

### 1.2 skill 的三种工作模式

`agent-image/builtin-skills/pptx-generator/SKILL.md` 的 Quick Reference 按场景路由：

| 模式 | 路由 | 用途 |
| --- | --- | --- |
| 读取 | pptx-markitdown | 提取已有 pptx 的文本大纲（也被后端 `/file-content` 复用） |
| 基于模版编辑 | `references/editing.md` | 用户提供 .pptx 后在其上改文本、增删排序页 |
| 从零生成 | SKILL.md 主流程 + design-system.md + slide-types.md | 无模版可用时，按设计系统产出新 deck |

### 1.3 路径①：文件级模版（真正的模版复用）

**用户侧操作链路：**

1. 通过前端「工作区文件面板」上传 `.pptx` → `POST /workspace/files/upload` → 落到容器 `tmp/` 目录；
2. 在对话中明确指示「基于 `tmp/xxx.pptx` 模版生成/编辑」，引用该路径；
3. Agent 按路由进入 `editing.md` 工作流。

**editing.md 的 XML 工作流（7 步）：**

1. `cp` 原文件为 `template.pptx`（保留原件不动）；
2. markitdown 分析每页内容与结构；
3. 规划页映射（强调版式多样性，避免全部套同一版式）；
4. Python `zipfile` 解包（pptx 本质是 ZIP 包）；
5. 结构操作：
   - 删页/重排：改 `presentation.xml` 的 `<p:sldIdLst>`；
   - 复制页：必须同步 `Content_Types.xml`、`presentation.xml`、`presentation.xml.rels`，否则包损坏；
6. 用 Edit 工具改 `slide{N}.xml` 中的文本（可子代理并行）；
7. 清理孤立文件（rels 指向已删页的残留）后重新打包——**必须先写 `/tmp/` 再移入目标目录**，因为 Python zipfile 的 seek 操作在 Docker bind mount 上会失败。

**已知坑位（editing.md 已收录）：**

- 模版中的槽位 ≠ 源条目：多余元素整组删除，不要硬塞；
- 多项内容用多个 `<a:p>` 分行，不拼接进一段；
- 智能引号等特殊字符必须用 XML 实体（`&ldquo;` 等）；
- 解析 XML 用 `defusedxml.minidom`。

### 1.4 路径②：设计参数模版（软模版）

从零生成时可指定以下「设计参数」，产出风格统一的 deck（`design-system.md`）：

- **调色板**：18 套预置调色板（名称/色值/风格/适用场景，如 `#15 Pure Tech Blue` 适用于 Cloud/AI 主题），选定后严格限定，禁止混入调色板外颜色；
- **风格配方**：4 种——Sharp / Soft / Rounded / Pill，各含圆角、间距、页边距完整规格与组件映射表；
- **字体配对**：中文 Microsoft YaHei / 英文 Arial + 配对表，正文禁粗体；
- **theme 五键契约**（MANDATORY）：`primary / secondary / accent / light / bg`，禁止其他键名；
- **页面类型**：5 种（封面 2 版式、目录 4 版式、章节页 4 版式、内容页 6 子型、总结页 4 版式，见 `slide-types.md`）。

### 1.5 硬性约束（两条路径通用）

- 容器 rootfs 只读，**禁止 pip/npm install**（pptx-markitdown、pptxgenjs@4.0.1 等依赖已预装在 agent 镜像内）；
- 禁渐变、禁动画；
- 页码徽章必填：`x: 9.3", y: 5.1"`，封面除外；
- 生成后必须 QA 校验（compile.js 汇总 + 结构检查）。

### 1.6 实操建议（如何对 Agent 说）

- 想复用公司模版：上传 .pptx →「基于 `tmp/公司模版.pptx` 改成…：第 1 页标题换成 X，删掉第 3 页，第 5 页复制一份讲 Y」；
- 想要某种风格：不传文件，直接说「用 Pure Tech Blue 调色板、Sharp 风格，做一份 AI 产品介绍」；
- 混合需求：先按路径①搭好模版结构，文本内容由 Agent 在槽位内替换，这是当前 skill 设计的标准用法。

---

## 二、前端 PPTX 预览渲染方案

### 2.1 总体架构

**双接口数据链路 + 客户端 canvas 高保真渲染 + 三层降级：**

```
Agent 产出 deck.pptx（容器内）
        │
        ├── GET /workspace/file-content   → markitdown 文本大纲（type:"pptx" 消息内容，降级与快览用）
        │
        └── GET /workspace/file-raw       → 原始二进制字节（≤25MB）
                    │
                    ▼
        pptx-wasm（Rust→WASM，浏览器端）
                    │
        ┌───────────┴───────────┐
   缩略图栏（N 个小实例）      主画布（受控实例）
   slide={i} fit=contain      slide/zoom 受控
        └───────────┬───────────┘
                    ▼
            底部控制栏（页码/缩放/引用/下载）
```

### 2.2 渲染引擎选型：pptx-wasm

- **库**：`pptx-wasm` v0.3.0（MIT 协议），Rust 实现、编译为 WASM，在浏览器 Worker 中解析 OOXML 并绘制到 canvas；
- **能力**：形状、表格、图片、CJK 文本按原样渲染；
- **保真度**：14 个 fixtures 结构保真度基准中误差 **1.23%**，为同类方案最优（对比 `pptx-preview` 误差 33.19%）；
- **React 组件**：`PresentationViewer`，支持 `src / slide / fit / zoom / keyboard / onLoad / onSlideChange / onError`；ref 句柄 `PresentationViewerHandle` 提供 `next / previous / goTo`。

### 2.3 组件结构（`frontend/src/components/Chat.tsx` → `PptxSlidePreview`）

布局为三段式（样式见 `chatStyles.ts` 的 `pptxPreviewShell / pptxThumbRail / pptxStage / pptxFooter`）：

| 区域 | 实现 | 说明 |
| --- | --- | --- |
| 缩略图栏 | 每页一个 `<button>` 内嵌独立 `PresentationViewer`（`slide={i} fit="contain" keyboard={false}`） | 固定 16:9，点击跳页：`setPage` + `viewer.current?.goTo(i)`；`aria-current` 标记当前页 |
| 主画布 | 受控 `PresentationViewer`（`slide={page.current} zoom={zoom}`，ref 挂 handle） | `onLoad` 回填总页数；`onSlideChange` 同步页码；`onError` 触发降级 |
| 控制栏 | 页码 `x/y`、缩放 −/＋（0.6–1.8，步进 0.1）、「引用当前页」、下载 | 窄面板自动换行 |

**关键设计：缩略图与主画布复用同一 pptx-wasm 引擎**，没有第二套渲染器（不做截图缓存、不做 SVG 近似），代价是 N 个实例共享解析结果；`keyboard={false}` 全局关闭，避免与聊天输入框抢按键。

### 2.4 懒加载机制

```tsx
const PresentationViewer = lazy(() =>
  import("pptx-wasm/react").then((m) => ({ default: m.PresentationViewer }))
);
```

JS + wasm 模块约 **330KB gzip**，只有用户真正打开 pptx 预览时才拉取该 chunk，主包体积不受影响。渲染区由 `<Suspense fallback="渲染引擎加载中…">` 包裹。

### 2.5 后端接口清单与限额（`backend/app/routers/workspace.py`）

| 接口 | 作用 | 限额/特判 |
| --- | --- | --- |
| `GET /workspace/file-content` | 取文件预览内容 | pptx 特判：容器内 markitdown 提取文本大纲，2MB 通用预览上限不适用；失败落回通用二进制预览 |
| `GET /workspace/file-raw` | 取 pptx 原始字节 | 仅 `.pptx`；`MAX_PPTX_RAW = 25MB`；二进制 Response |
| `GET /workspace/files/download` | 打包下载 | `MAX_DOWNLOAD_PATHS = 200` 个路径，总量 `MAX_DOWNLOAD_TOTAL = 100MB` |
| `POST /workspace/files/upload` | 上传（模版文件入口） | 落容器 `tmp/` |

### 2.6 三层降级策略

| 层级 | 触发条件 | 展示形态 |
| --- | --- | --- |
| 1. 高保真渲染 | raw 字节获取成功且 pptx-wasm 解析成功 | 缩略图栏 + 主画布 + 控制栏 |
| 2. 文本大纲 | raw 获取失败（超 25MB / 网络错误）或渲染器 `onError` 解析失败 | 顶部提示条「📊 共 N 张幻灯片 · 文本大纲预览（原因）」+ 白卡片大纲（标题 + 列表 + 表格行等宽字体） |
| 3. 原文兜底 | markitdown 提取不到任何分页结构（`parsePptxSlides` 返回空） | `MiniMarkdown` 直接渲染原始文本 |

大纲解析（`parsePptxSlides`）按 markitdown 的 `<!-- Slide number: N -->` 注释切片，每片首个非空行为标题，容错处理 `#` 前缀与版本差异。

### 2.7 状态机与加载态

- `raw === null && !rawError`：「正在获取演示文稿…」（`useEffect` 内 `alive` 标志防竞态，path 变更时重置状态）；
- `canRender = raw !== null && !rawError`：进入主渲染分支；
- `rawError` 非空：携带原因进入降级分支。

### 2.8 引用当前页机制

控制栏「引用当前页」按钮执行：

```tsx
onInsert(`${path} 第${page.current + 1}页`)
```

把「文件路径 + 页号」插入聊天输入框，用户可直接发送。Agent 收到后可结合 skill 的 reading 模式（pptx-markitdown 按页提取）精确定位该页内容，形成「预览 → 引用 → 对话修改」的闭环。

### 2.9 设计参照

预览区的视觉结构（缩略图导航 + 主画布 + 底部工具栏）参照 `frontend/design/hifi-redesign.html` 高保真设计稿实现；本方案刻意去掉了编辑区（只读展示），编辑仍由 Agent 通过 skill 在容器内完成。
