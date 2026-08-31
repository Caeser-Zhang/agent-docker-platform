---
name: fastk-search
description: Search the FastDB knowledge bases (global specs, AI-code notes, project indexes) with the fastk CLI. Use when looking up platform specs, design docs, prior decisions, code knowledge, or any content stored in the platform knowledge bases.
---

# fastk-search — FastDB 知识库检索

使用内置的 `fastk` CLI（`/usr/local/bin/fastk`，只读）检索平台的 FastDB 知识库。
FastDB 服务经 `host.docker.internal:8000` 可达，环境变量 `FASTDB_BASE_URL` 可覆盖地址。

## 库名约定

`--db` 接受逻辑名或物理库名，未指定时默认 `global`：

| 逻辑名 | 物理库       | 内容               |
|--------|--------------|--------------------|
| global | specs_global | 平台全局共享知识/规范 |
| aicode | aicode       | AI 代码知识库       |
| test   | test_db      | 测试库             |

其余库（如带哈希后缀的项目索引）先用 `fastk databases` 查看实际清单，再按物理库名引用。

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

所有子命令输出 UTF-8 JSON（缩进 2）。回答用户时引用必须带来源：`file_path` + 命中
`section`/chunk 标题；检索分数只用于排序判断，不要向用户展示原始分数。

## 注意

- fastk 只读：不能写入或修改知识库
- 命中不佳时先放宽（去掉 `--filter`、提高 `--topk`、换同义关键词），再用 grep/toc 收窄
- 报错 "cannot reach FastDB" 说明服务不可达，如实告知用户，不要编造检索结果
