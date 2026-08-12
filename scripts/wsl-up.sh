#!/usr/bin/env bash
# Bring the whole Agent Docker Platform stack up inside WSL2.
#
# Usage (from Windows):
#   wsl -d Ubuntu-24.04 -- bash /home/<user>/agent-docker-demo/scripts/wsl-up.sh
#
# It waits for the Docker daemon (the WSL VM may have just booted), then
# starts the compose stack and prints the health of every layer.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "== WSL uptime =="
uptime

echo "== waiting for docker daemon =="
for i in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    echo "docker ready after ${i}s"
    break
  fi
  sleep 1
done

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon not reachable" >&2
  exit 1
fi

echo "== starting compose stack =="
docker compose up -d

echo "== waiting for backend =="
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:9123/api/health 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo "backend healthy after ${i}s"
    break
  fi
  sleep 1
done

echo "== containers =="
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

echo "== endpoints =="
echo -n "frontend http://localhost:3000 -> "
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000
echo -n "backend  http://localhost:9123/api/health -> "
curl -s -w '\n' http://localhost:9123/api/health
