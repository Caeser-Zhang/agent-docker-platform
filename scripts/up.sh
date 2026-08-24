#!/usr/bin/env bash
# 一键构建+启动所有平台服务
#
# 与 start.sh 不同，up.sh 会先构建 Agent 镜像（如果缺失）和平台镜像，
# 然后启动服务。适合首次部署或全量更新场景。
#
# 如果只需要启动已构建好的服务，使用：
#   bash scripts/start.sh
#
# 如果只需要重新构建某个组件，使用对应的 build-*.sh：
#   bash scripts/build-backend.sh   # 后端代码变更
#   bash scripts/build-frontend.sh  # 前端代码变更
#   bash scripts/build-agent.sh     # Agent 镜像变更
#
# Usage:
#   bash scripts/up.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 从 backend/.env 读取 Agent 镜像标签
ENV_FILE="$PROJECT_DIR/backend/.env"
AGENT_IMAGE="agent-demo:1.1.0"
if [ -f "$ENV_FILE" ]; then
  AGENT_IMAGE=$(grep -oP '^AGENT_AGENT_IMAGE=\K.*' "$ENV_FILE" | tr -d '"' | tr -d "'" || echo "$AGENT_IMAGE")
fi

# ---- 等待 Docker daemon ----
echo "== waiting for docker daemon =="
ready=0
for i in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    echo "docker ready after ${i}s"
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  echo "ERROR: docker daemon not reachable — is Docker installed and running?" >&2
  exit 1
fi

# ---- 构建 Agent 镜像（如果缺失） ----
echo "== agent image ($AGENT_IMAGE) =="
if [ -n "$(docker images -q "$AGENT_IMAGE" 2>/dev/null)" ]; then
  echo "$AGENT_IMAGE already built"
else
  echo "building $AGENT_IMAGE (first build takes a few minutes)…"
  bash "$(dirname "${BASH_SOURCE[0]}")/build-agent.sh"
fi

# ---- 构建并启动平台服务 ----
echo "== compose up (build + start) =="
docker compose up -d --build || exit 1

# ---- 等待后端健康检查 ----
echo "== waiting for backend =="
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:9123/api/health 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo "backend healthy after ${i}s"
    break
  fi
  sleep 1
done

# ---- 验证 ----
echo
bash "$(dirname "${BASH_SOURCE[0]}")/verify.sh"