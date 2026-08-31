#!/usr/bin/env bash
# 启动所有平台服务（不执行构建）
#
# 如果需要重新构建某个组件的镜像，请先运行对应的 build-*.sh 脚本：
#   bash scripts/build-backend.sh   # 后端代码变更后
#   bash scripts/build-frontend.sh  # 前端代码变更后
#   bash scripts/build-agent.sh     # Agent 镜像变更后
#   然后运行本脚本启动服务。
#
# 如果需要一键构建+启动，请按 README 中的 build-*.sh + start.sh 顺序执行。
#
# Usage:
#   bash scripts/start.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

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

# ---- 启动服务（不构建） ----
echo "== starting services =="
docker compose up -d || exit 1

# ---- 等待后端健康检查 ----
echo "== waiting for backend =="
# The backend has no host port by design (P1-5); reach it through the
# frontend nginx reverse proxy on :3000.
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3000/api/health 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo "backend healthy after ${i}s"
    break
  fi
  sleep 1
done

# ---- 验证 ----
echo
bash "$(dirname "${BASH_SOURCE[0]}")/verify.sh"