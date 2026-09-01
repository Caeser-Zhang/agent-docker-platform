---
name: fastk-analyze
description: Analyze fastk knowledge-base content — inventories, coverage stats, cross-database comparison, spec summarization with citations. Use when the user asks to summarize, compare, audit, or analyze knowledge-base contents rather than answer a single lookup question.
---

# fastk-analyze — fastk 知识库分析

与 fastk-search 技能的分工：search 回答"哪里有 X"（单点检索）；本技能回答
"库里有什么、覆盖是否完整、不同库/文档之间是什么关系"（面上分析）。
底层同样是只读的 `fastk` CLI（`/usr/local/bin/fastk`），库名约定与检索语法
见 fastk-search 技能，此处不重复。

## 分析工作流

1. **定范围**：`fastk databases` + `fastk stats --db <db>` —— 统计口径一律以
   stats 返回的文件数/chunk 数为准，不凭感觉估计；有库级说明先读
   `fastk instructions --db <db>`（分析角度和口径可能受其约束）
2. **建清单**：`fastk files --db <db>`（CLI 自动分页拉全量，无需手动翻页；
   想抽查时可用 `--limit`/`--offset`）—— 与 stats 的文件数互相应证
3. **摸结构**：对重点文档逐个 `fastk toc --db <db> <file_path>` 展开骨架
   （路径支持 canonical 或别名，`fastk alias show --db <db> [PATH]` 可查映射）；
   `fastk count --db <db> <file_path>` 可快速核对单文档 chunk 数
4. **取证**：用 `search` / `grep` / `query` 定位关键段落（用法见 fastk-search 技能）
5. **产出**：给出带出处的分析结论

## 常用分析模式

- **覆盖度盘点**：files 全清单 → toc 逐个 → 输出"主题 → 文档 → 缺口"对照表
- **跨库对比**：同一主题分别在 `global` 与 `aicode`（或项目索引）检索，
  对比两边命中的文档与说法差异
- **规范一致性审计**：`grep` 固定模式（版本号、接口路径、日期等）跨文档核对
- **主题摘要**：search 圈定相关文档 → toc 选段 → 汇总成结构化摘要

## 硬性要求

- 每条结论标注来源：`file_path`（+ section/chunk 标题），禁止无出处断言
- 引用了具体 chunk 内容的结论，句末**必须**追加规范化引用标记
  `[[chunk:<库名>/<chunk_id>]]`（`chunk_id` 原样取自工具返回 JSON；格式细节见
  fastk-search 技能的"输出与引用"一节），前端会渲染为可点击徽章
- 数量、大小、覆盖比例等数字一律取自 `stats`/`files`/`grep` 等工具返回值
- 库不可达或为空时如实说明，不要编造内容填充分析
- 分析输出建议结构：范围 → 方法（用了哪些命令）→ 发现 → 结论/建议
- 全程使用简体中文回复；表格对齐中文宽度，数字用半角，标点用全角中文标点
