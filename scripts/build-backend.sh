#!/usr/bin/env bash
# 重新构建后端镜像
#
# 修改 backend/ 下的代码后运行此脚本重新构建后端镜像，
# 然后运行 `bash scripts/start.sh` 使新镜像生效。
#
# Usage:
#   bash scripts/build-backend.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "== building backend image =="
docker compose build backend

echo "== done =="
echo "后端镜像已构建。运行 'bash scripts/start.sh' 启动新镜像。"