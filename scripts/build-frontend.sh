#!/usr/bin/env bash
# 重新构建前端镜像
#
# 修改 frontend/ 下的代码后运行此脚本重新构建前端镜像，
# 然后运行 `bash scripts/start.sh` 使新镜像生效。
#
# Usage:
#   bash scripts/build-frontend.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "== building frontend image =="
docker compose build frontend

echo "== done =="
echo "前端镜像已构建。运行 'bash scripts/start.sh' 启动新镜像。"