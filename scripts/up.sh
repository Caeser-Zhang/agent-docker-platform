#!/usr/bin/env bash
# Universal startup script — works in any Linux environment or WSL2.
#
# Idempotent and safe to re-run. Steps:
#   1. Wait for the Docker daemon (a WSL VM may have just booted)
#   2. Build the per-user agent image (agent-demo:1.0.0) if it's missing;
#      on network failure retry with --network=host (WSL2 DNS workaround)
#   3. docker compose up -d --build  (rebuilds platform images when code changed)
#   4. Wait for backend health, then print the state of every layer
#
# Usage (from the repo root or anywhere):
#   bash scripts/up.sh
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

AGENT_IMAGE="agent-demo:1.0.0"

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

echo "== agent image =="
if [ -n "$(docker images -q "$AGENT_IMAGE")" ]; then
  echo "$AGENT_IMAGE already built"
else
  echo "building $AGENT_IMAGE (first build takes a few minutes)…"
  if ! docker build -t "$AGENT_IMAGE" ./agent-image; then
    echo "-- plain build failed, retrying with --network=host (WSL2 DNS workaround)…"
    docker build --network=host -t "$AGENT_IMAGE" ./agent-image || {
      echo "ERROR: agent image build failed" >&2
      exit 1
    }
  fi
fi

echo "== compose up (build + start) =="
docker compose up -d --build || exit 1

echo "== waiting for backend =="
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:9123/api/health 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo "backend healthy after ${i}s"
    break
  fi
  sleep 1
done

echo
bash "$(dirname "${BASH_SOURCE[0]}")/verify.sh"
