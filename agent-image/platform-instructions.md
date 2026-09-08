# Agent Workspace 平台交付规范

## 交付展示规范（present_file）

平台前端依赖 `present_file` 工具的信号弹出预览窗口。**任务完成时，只要本次任务创建或更新了
应被用户查看的文件，就必须调用 `present_file`**，否则用户看不到产出。

**必须调用的文件类型**

```
.html .htm .md .markdown .png .jpg .jpeg .gif .svg .webp
.pdf .pptx .xlsx .docx .csv .json .mp4 .mp3
```

**调用纪律**

- 只在文件**已完整写入磁盘后**调用；禁止对正在流式生成的半成品调用。
- `path` 用相对 workspace 根的路径（如 `outputs/report.html`），不要传绝对路径。
- `title` 用用户视角的简短中文命名（如「季度趋势报告」），不要用文件名。
- 一次回复内最多调用 3 次。文件多于 3 个时，只展示最重要的 3 个，其余在正文列出路径。
- 多个文件时逐个调用，只对最重要的那一个设 `focus: true`，其余设 `false`。
- `note` 写一句话说明「这是什么、看什么」（≤50 字）。
- 调用后用一句话说明文件内容，**不要复述文件正文**。

**不要调用的情况**

- 只读取过、未由本次任务产出的文件。
- 中间产物、临时脚本、缓存、日志、依赖目录（`node_modules` 等）。
- 源码类文件（`.py` `.ts` `.go` 等）—— 用户看 diff 即可，无需预览。
- **子代理不要调用**：由 `task` 工具派生的 agent（oracle / librarian / explorer /
  designer / fixer 等）只把结果返回给主代理，交付展示统一由主代理负责。

**示例**

任务「生成季度销售分析报告」完成后：

```
present_file({ path: "outputs/report.html", title: "季度销售分析报告",
               note: "含近 8 周趋势与区域拆分", focus: true })
present_file({ path: "outputs/summary.md", title: "结论摘要",
               note: "三条核心结论与风险提示", focus: false })
```
