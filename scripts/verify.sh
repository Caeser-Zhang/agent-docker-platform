#!/usr/bin/env bash
# Stack health check: containers, agent image, endpoints, shipped UI bundle.
# Run any time after `up.sh` (or standalone) to confirm all layers are up.
set -u

echo "== containers =="
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
echo

echo "== agent image (needed to spawn per-user containers) =="
docker images agent-demo:1.0.0 --format '{{.Repository}}:{{.Tag}}  {{.Size}}'
[ -z "$(docker images -q agent-demo:1.0.0)" ] && \
  echo "WARN: agent-demo:1.0.0 MISSING — run: docker build -t agent-demo:1.0.0 ./agent-image"
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
    grep -q "$s" /tmp/bundle.js && echo "  OK   $s" || echo "  MISS $s (old image? run: docker compose up -d --build)"
  done
else
  echo "WARN: could not locate the JS bundle — frontend not serving?"
fi
