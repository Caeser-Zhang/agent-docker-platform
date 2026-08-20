#!/usr/bin/env bash
# Mirror the Windows-side working copy into the WSL2 filesystem.
#
# The project is edited on D:\Project\agent-docker-demo but must RUN from the
# ext4 filesystem inside WSL: building images off /mnt/d is an order of
# magnitude slower and loses the executable bit on entrypoint scripts.
#
# Usage (from inside WSL):
#   bash scripts/wsl-sync.sh
# or from Windows:
#   wsl -d Ubuntu-24.04 -- bash /mnt/d/Project/agent-docker-demo/scripts/wsl-sync.sh
set -euo pipefail

SRC="${SRC:-/mnt/d/Project/agent-docker-demo}"
DST="${DST:-$HOME/agent-docker-demo}"

mkdir -p "$DST"
rsync -a --delete \
  --exclude '.git/' \
  --exclude 'node_modules/' \
  --exclude 'frontend/dist/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '/.env' \
  "$SRC"/ "$DST"/

# CRLF from the Windows editor breaks shebangs and `set -e` inside containers.
find "$DST" -type f \( -name '*.sh' -o -name 'Dockerfile' -o -name 'entrypoint.sh' \) \
  -exec sed -i 's/\r$//' {} +
chmod +x "$DST"/scripts/*.sh "$DST"/agent-image/entrypoint.sh 2>/dev/null || true

echo "synced $SRC -> $DST"
ls -la "$DST"
