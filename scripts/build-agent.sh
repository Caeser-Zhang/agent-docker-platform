#!/usr/bin/env bash
# 重新构建用户 Agent 容器镜像
#
# 修改 agent-image/ 下的代码后运行此脚本重新构建镜像。
# 镜像标签从 backend/.env 的 AGENT_AGENT_IMAGE 读取。
# 构建完成后，新创建的会话会使用新镜像；已有容器不受影响。
#
# Usage:
#   bash scripts/build-agent.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 从 backend/.env 读取镜像标签，如果读取失败则使用默认值
ENV_FILE="$PROJECT_DIR/backend/.env"
if [ -f "$ENV_FILE" ]; then
  AGENT_IMAGE=$(grep -oP '^AGENT_AGENT_IMAGE=\K.*' "$ENV_FILE" | tr -d '"' | tr -d "'" || true)
fi
AGENT_IMAGE="${AGENT_IMAGE:-agent-demo:1.3.0}"

echo "== building agent image: $AGENT_IMAGE =="

if ! docker build -t "$AGENT_IMAGE" ./agent-image; then
  echo "-- plain build failed, retrying with --network=host (WSL2 DNS workaround)…"
  docker build --network=host -t "$AGENT_IMAGE" ./agent-image || {
    echo "ERROR: agent image build failed" >&2
    exit 1
  }
fi

echo "== done =="
echo "Agent 镜像 '$AGENT_IMAGE' 已构建。新创建的会话将使用此镜像。"