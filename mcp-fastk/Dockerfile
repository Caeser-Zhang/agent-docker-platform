# fastk-mcp — platform-managed remote MCP server for FastDB knowledge bases.
# Serves MCP over streamable HTTP (default endpoint /mcp) so that per-user
# opencode containers connect with type=remote. FastDB itself runs on the
# host (WSL) and is reached via host.docker.internal.

FROM python:3.12-slim

WORKDIR /app

# Pin fastmcp to the version the upstream project was developed against.
RUN pip install --no-cache-dir "fastmcp==3.4.7" "httpx>=0.24.0"

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

# Container config: base_url points at host.docker.internal (FastDB on host).
COPY fastk-mcp.toml /app/fastk-mcp.toml

ENV FASTK_MCP_CONFIG=/app/fastk-mcp.toml \
    FASTK_MCP_HTTP_PORT=8001

EXPOSE 8001

CMD ["fastk-mcp"]
