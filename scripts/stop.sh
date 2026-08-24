#!/usr/bin/env bash
# 停止所有平台服务（前端、后端、PostgreSQL、SearXNG）
#
# 不会停止用户 Agent 容器，如需停止 Agent 容器请使用管理面板。
#
# Usage:
#   bash scripts/stop.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "== stopping platform services =="
docker compose down

echo "== done =="