# fastk-mcp

面向**远程 FastDB 知识库服务**的 MCP（Model Context Protocol）服务端，提供渐进式搜索与结构化 Markdown 展示。

FastDB 自带的 MCP 只支持本地 SDK、返回裸 `list[dict]`、且无渐进式搜索。本项目补足三个空白：

1. **远程连接** — 用 `httpx` 直连 FastDB 的 REST API（`/fastdb/api`），一个 TOML 配置声明多个远程知识库。
2. **渐进式搜索** — 工具面「由浅入深钻取」(`list_files → toc → search/query/grep`) + `search` 内部「漏斗精排」。
3. **展示逻辑** — 结果按文档分组去重、Markdown 格式化、含 score/section/chunk 元数据。

## 安装

```bash
uv pip install fastk-mcp
# 或
pip install fastk-mcp
```

依赖 `httpx>=0.24.0` 与 `fastmcp>=3.4`。

## 配置

**方式一：TOML 文件**（推荐多库场景）

复制 `fastk-mcp.toml.example` 为 `fastk-mcp.toml`，填写远程服务地址与库名。

```toml
default_database = "prod"

[[databases]]
name = "prod"
base_url = "http://localhost:8000"
db_name = "my-knowledge-base"
api_key = ""
```

**方式二：环境变量**（免文件，单库场景）

```bash
FASTK_MCP_BASE_URL=http://localhost:8000
FASTK_MCP_DB_NAME=my-knowledge-base
FASTK_MCP_API_KEY=        # 可选
FASTK_MCP_TIMEOUT=30.0    # 可选
```

`base_url` 是服务根地址，`/fastdb/api` 后缀由客户端自动补全。

## 打包

在项目根目录构建源码包（sdist）与 wheel，输出到 `dist/`：

```bash
uv build
```

只构建 wheel：

```bash
uv build --wheel
```

发布到 PyPI：

```bash
uv publish
```

`uv build` 会调用 `hatchling` 后端（见 `pyproject.toml` 的 `[build-system]`），在 `dist/` 下生成 `fastk_mcp-<version>-py3-none-any.whl` 与 `fastk_mcp-<version>.tar.gz`。

## 运行

```bash
fastk-mcp path/to/fastk-mcp.toml
# 或通过环境变量指定配置
FASTK_MCP_CONFIG=path/to/fastk-mcp.toml fastk-mcp
```

默认以 stdio 传输启动，可直接接入 Claude / Cursor / TraeCode 等 MCP 客户端。

## 工具

| 工具 | 说明 |
| --- | --- |
| `list_databases` | 列出已配置的远程知识库 |
| `list_files` | 列出文档（含 chunk 数、描述、更新时间） |
| `get_stats` | 文件数与 chunk 数 |
| `toc` | 查看某文档的目录（标题层级） |
| `search` | 混合检索（dense + FTS），支持 RRF/DBSF、alpha/threshold、过滤器 |
| `query` | 纯过滤器查询（无向量检索） |
| `grep` | 正则匹配 chunk 文本 |

## 渐进式搜索

- **钻取**：先用 `list_files` 找到目标文档 → `toc` 看目录 → 再用 `search/query/grep` 定点检索。
- **漏斗**：`search` 默认 `progressive=True`，先宽召回（`max(topk * funnel_ratio, funnel_min)`），再做多样性裁剪（单文档最多贡献 `topk // 3` 条），避免个别文档霸榜。

## 过滤器语法

`filters` 参数接受 Pythonic 表达式，如：

- `path == 'docs/guide.md'`
- `section.contains('简介')`
- `file_id in ['a', 'b']`
- `path == 'docs/guide.md' and section.contains('部署')`

## 项目结构

```
src/fastk_mcp/
  config.py    # TOML 配置模型与加载
  client.py    # httpx 直连 FastDB 连接池
  display.py   # Markdown 结果格式化
  tools.py     # 工具实现（含漏斗搜索）
  server.py    # FastMCP 服务端与 CLI 入口
```