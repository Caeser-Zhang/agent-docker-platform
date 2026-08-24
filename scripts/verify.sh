#!/usr/bin/env bash
# 平台健康检查：容器、镜像、端点、前端产物
#
# 可在 start.sh / up.sh 之后独立运行，确认所有层正常运行。
#
# Usage:
#   bash scripts/verify.sh
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 从 backend/.env 读取 Agent 镜像标签
AGENT_IMAGE="agent-demo:1.1.0"
ENV_FILE="$PROJECT_DIR/backend/.env"
if [ -f "$ENV_FILE" ]; then
  AGENT_IMAGE=$(grep -oP '^AGENT_AGENT_IMAGE=\K.*' "$ENV_FILE" | tr -d '"' | tr -d "'" || echo "$AGENT_IMAGE")
fi

echo "== containers =="
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo

echo "== agent image ($AGENT_IMAGE) =="
docker images "$AGENT_IMAGE" --format '{{.Repository}}:{{.Tag}}  {{.Size}}' 2>/dev/null || \
  echo "WARN: $AGENT_IMAGE MISSING — run: bash scripts/build-agent.sh"
echo

printf "backend  http://localhost:9123/api/health -> "
curl -s http://localhost:9123/api/health; echo
printf "frontend http://localhost:3000            -> "
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000

echo "== UI bundle sanity (spot-check a few feature strings) =="
js=$(curl -s http://localhost:3000/ | grep -oE 'assets/index-[^"]+\.js' | head -1)
if [ -n "$js" ]; then
  echo "bundle: $js"
  curl -s "http://localhost:3000/$js" > /tmp/bundle.js
  for s in "总是允许" "重命名会话" "Agent 需要你的输入"; do
    grep -q "$s" /tmp/bundle.js && echo "  OK   $s" || echo "  MISS $s (old image? run: bash scripts/build-frontend.sh && bash scripts/start.sh)"
  done
else
  echo "WARN: could not locate the JS bundle — frontend not serving?"
fi