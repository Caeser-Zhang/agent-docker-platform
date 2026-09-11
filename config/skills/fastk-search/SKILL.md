---
name: fastk-search
description: Search the FastDB knowledge bases with the fastk CLI. Use when looking up platform specs, design docs, prior decisions, code knowledge, or any content stored in the platform knowledge bases. Always discover available databases first — the list is dynamic and user-specific.
---

# fastk-search — fastk 知识库检索

使用内置的 `fastk` CLI（`/usr/local/bin/fastk`，只读）检索平台知识库。
环境变量 `FASTDB_BASE_URL` 已由平台注入（指向后端代理，代理负责鉴权与
逐库 Key 注入），**不要自行覆盖或尝试直连其他地址**。

## 探库必做

可用知识库列表是**动态的、按用户白名单过滤的**——不同用户看到的库不同，
且管理员随时可能增删授权。因此：

> **每次对话首次使用 fastk 前，必须先执行 `fastk databases` 获取当前可用库清单。**
> 不要假设任何库名存在，不要使用记忆中的旧库名。

`--db` 参数接受 `fastk databases` 返回的物理库名（如 `fastdb`、`asset_test`）。
未指定 `--db` 时 CLI 默认使用 `global`（映射到 `fastdb`），但**仅当该库出现在
探库结果中时才可使用**；若不在清单中，说明当前用户无权限，应告知用户并建议
联系管理员开通。

## 渐进式检索工作流

1. **探库**：`fastk databases`；先读库级使用说明（如有特殊约定须遵守）：
   `fastk instructions --db global`
2. **浏览文档清单**（自动分页拉全量，无需手动翻页；每个文件的 chunk 数可判断文档大小）：
   `fastk files --db global`
   - 只想知道文件总数：`fastk count --db global`
   - 文档记不住完整路径时查别名映射：`fastk alias show --db global`（可加路径只查单个）
3. **看文档结构**（先拿 headings，再决定读哪段；路径支持 canonical 或别名）：
   `fastk toc --db global "docs/spec-gateway.md"`
   单文档 chunk 数：`fastk count --db global "docs/spec-gateway.md"`
4. **混合检索**（dense + 全文融合，结果按文档分组，默认 topk=10）：
   `fastk search --db global "认证网关 超时重试"`
5. **正则定位**（需要限定路径范围时优先用 `--path`）：
   `fastk grep --db global "timeout.*[0-9]+" --path "specs/*"`
6. **纯过滤查询**（无向量，适合按元数据精确圈定）：
   `fastk query --db global "section.contains('超时') and path.startswith('specs/')"`

## 检索调参

- `--topk N`：返回条数（默认 10）
- `--no-rrf`：改用 DBSF 加权融合；`--alpha 0.0~1.0` 调 dense 权重，`--threshold` 设最低融合分
- `--filter`：Pythonic 过滤表达式，支持 `path == '...'`、`section.contains('...')`、
  `file_id in ['...']`，可用 and/or 组合

## 输出与引用

所有子命令输出 UTF-8 JSON（缩进 2）。检索分数只用于排序判断，不要向用户展示原始分数。

回答用户时**必须**对引用到的知识库内容添加规范化引用标记，格式：

```
[[chunk:<库名>/<chunk_id>]]
```

- `<库名>` 用物理库名（如 `fastdb`），`<chunk_id>` 取检索结果 JSON 里的
  `chunk_id` 字段（原样复制，不要改写大小写或截断）
- 标记紧跟在对应结论句或要点的末尾（标点之后），一行内可附多个标记，用空格分隔
- 同一 chunk 在整条回复中只标一次（首次引用处）
- 不要把标记写进代码块、表格单元格或标题里
- 示例：`混合检索默认采用 DBSF 融合，RRF 为可选方案。[[chunk:fastdb/fb62184133c0c818]]`

前端会把标记渲染为可点击的引用徽章，点击后向用户展示该 chunk 的完整内容
（含图片）。因此不要用自然语言复述 chunk_id，也不要生成其它格式的引用角标。

## 注意

- fastk 只读：不能写入或修改知识库
- 命中不佳时先放宽（去掉 `--filter`、提高 `--topk`、换同义关键词），再用 grep/toc 收窄
- 报错 "cannot reach" 说明 fastk 服务不可达，如实告知用户，不要编造检索结果
- 全程使用简体中文回复；保留 JSON 里的中文原文，不要翻译或改写引用内容
