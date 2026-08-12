#!/usr/bin/env bash
# Probe a locally-downloaded opencode binary to capture its real HTTP contract.
# Used during development to keep the platform's reverse proxy in sync with
# whatever `opencode serve` actually exposes.
#
#   bash scripts/probe-opencode.sh /tmp/oc 4399
set -uo pipefail

OC_BIN="${1:-/tmp/oc}"
PORT="${2:-4399}"
ROOT=/tmp/octest

rm -rf "$ROOT"
mkdir -p "$ROOT"/config/opencode "$ROOT"/share "$ROOT"/cache "$ROOT"/state "$ROOT"/ws

cat > "$ROOT/config/opencode/opencode.json" <<'JSON'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "bailian": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "bailian",
      "options": { "apiKey": "sk-test", "baseURL": "https://dashscope.aliyuncs.com/compatible-mode/v1" },
      "models": { "glm-5.2": { "name": "GLM-5.2" }, "qwen3.7-max": { "name": "Qwen3.7-Max" } }
    }
  },
  "model": "bailian/glm-5.2"
}
JSON

export HOME="$ROOT"
export XDG_CONFIG_HOME="$ROOT/config"
export XDG_DATA_HOME="$ROOT/share"
export XDG_CACHE_HOME="$ROOT/cache"
export XDG_STATE_HOME="$ROOT/state"
export OPENCODE_DISABLE_AUTOUPDATE=1

"$OC_BIN" serve --port "$PORT" --hostname 127.0.0.1 --print-logs > "$ROOT/serve.log" 2>&1 &
SERVE_PID=$!
echo "serve pid=$SERVE_PID"

for i in $(seq 1 40); do
  if curl -s -m 2 "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1; then
    echo "ready after ${i}s"
    break
  fi
  sleep 1
done

echo "=== /api/health ==="
curl -s "http://127.0.0.1:$PORT/api/health"; echo

echo "=== OpenAPI paths ==="
curl -s "http://127.0.0.1:$PORT/doc" > "$ROOT/openapi.json"
wc -c "$ROOT/openapi.json"
python3 - "$ROOT/openapi.json" <<'PY'
import json, sys
try:
    spec = json.load(open(sys.argv[1]))
except Exception as exc:
    print("openapi parse failed:", exc)
    sys.exit(0)
verbs = ("get", "post", "put", "patch", "delete")
for path, ops in sorted(spec.get("paths", {}).items()):
    methods = ",".join(sorted(v.upper() for v in ops if v in verbs))
    print(f"{methods:<20} {path}")
PY

echo "=== probes ==="
for ep in /api/provider /api/model /api/agent /api/config /api/session /api/app /api/project; do
  code=$(curl -s -o "$ROOT/out.json" -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT$ep")
  echo "--- GET $ep -> $code"
  head -c 700 "$ROOT/out.json"; echo
done

echo "=== serve.log tail ==="
tail -30 "$ROOT/serve.log"

kill "$SERVE_PID" 2>/dev/null
wait "$SERVE_PID" 2>/dev/null
echo "done"
