---
name: fastk-search
description: Search the FastDB knowledge bases (global specs, AI-code notes, project indexes) with the fastk CLI. Use when looking up platform specs, design docs, prior decisions, code knowledge, or any content stored in the platform knowledge bases.
---

# fastk-search — fastk 知识库检索

使用内置的 `fastk` CLI（`/usr/local/bin/fastk`，只读）检索平台的 fastk 知识库。
fastk 服务（fastdb serve fastapi，`/fastk/api` 前缀）运行在宿主机上，经
`host.docker.internal:8000` 可达；环境变量 `FASTDB_BASE_URL` 已由平台注入，
不要自行覆盖，除非用户明确要求其他地址。

## 库名约定

`--db` 接受逻辑名或物理库名，未指定时默认 `global`（映射到 `fastdb`）：

| 逻辑名 | 物理库   | 内容                       |
|--------|----------|----------------------------|
| global | fastdb   | 平台全局共享知识/规范      |
| —      | vl_test  | 多模态（图文）知识库       |

其余库先用 `fastk databases` 查看实际清单，再按物理库名引用。

## 渐进式检索工作流

1. **探库**：`fastk databases`
2. **浏览文档清单**（每个文件的 chunk 数可判断文档大小）：
   `fastk files --db global --limit 50`
3. **看文档结构**（先拿 headings，再决定读哪段）：
   `fastk toc --db global "docs/spec-gateway.md"`
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
