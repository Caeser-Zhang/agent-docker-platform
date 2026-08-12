#!/usr/bin/env bash
# Second-stage probe: nail down the exact request/response shapes the platform
# reverse-proxy and the frontend need (providers, session create, prompt, SSE).
set -uo pipefail

OC_BIN="${1:-/tmp/oc}"
PORT="${2:-4400}"
ROOT=/tmp/octest2
BASE="http://127.0.0.1:$PORT"

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
      "models": {
        "glm-5.2": { "name": "GLM-5.2", "limit": { "context": 200000, "output": 32768 } },
        "qwen3.7-max": { "name": "Qwen3.7-Max", "limit": { "context": 192000, "output": 32768 } }
      }
    },
    "headroom": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Headroom Proxy",
      "options": { "apiKey": "sk-test", "baseURL": "http://host.docker.internal:8787/v1" },
      "models": { "glm-5.2": { "name": "GLM-5.2" } }
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

cd "$ROOT/ws"
"$OC_BIN" serve --port "$PORT" --hostname 127.0.0.1 --print-logs > "$ROOT/serve.log" 2>&1 &
SERVE_PID=$!

for i in $(seq 1 40); do
  curl -s -m 2 "$BASE/api/health" > /dev/null 2>&1 && break
  sleep 1
done

show() { # show <label> <url>
  local code
  code=$(curl -s -o "$ROOT/out.json" -w '%{http_code}' -m 8 "$2")
  echo "--- $1 -> $code"
  head -c 1400 "$ROOT/out.json"; echo; echo
}

echo "########## configured providers ##########"
show "GET /config/providers" "$BASE/config/providers"
show "GET /provider"         "$BASE/provider"
show "GET /config"           "$BASE/config"

echo "########## session create ##########"
SESSION_JSON=$(curl -s -m 10 -X POST "$BASE/api/session" \
  -H 'content-type: application/json' \
  -d '{"cwd":"'"$ROOT"'/ws","agent":"build"}')
echo "$SESSION_JSON" | head -c 900; echo
SID=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('id') or d.get('id',''))" <<< "$SESSION_JSON")
echo "SID=$SID"

echo "########## session list / detail ##########"
show "GET /api/session" "$BASE/api/session"
show "GET /api/session/$SID" "$BASE/api/session/$SID"
show "GET /api/session/$SID/message" "$BASE/api/session/$SID/message"

echo "########## model switch ##########"
for body in '{"model":"headroom/glm-5.2"}' '{"providerID":"headroom","modelID":"glm-5.2"}'; do
  code=$(curl -s -o "$ROOT/out.json" -w '%{http_code}' -m 8 -X POST "$BASE/api/session/$SID/model" \
    -H 'content-type: application/json' -d "$body")
  echo "--- POST model $body -> $code"; head -c 500 "$ROOT/out.json"; echo
done

echo "########## SSE (global /api/event) ##########"
( timeout 25 curl -sN "$BASE/api/event" > "$ROOT/events.txt" ) &
SSE_PID=$!
sleep 2

echo "########## prompt ##########"
code=$(curl -s -o "$ROOT/out.json" -w '%{http_code}' -m 20 -X POST "$BASE/api/session/$SID/prompt" \
  -H 'content-type: application/json' \
  -d '{"prompt":{"text":"say hi in 3 words"}}')
echo "prompt -> $code"; head -c 900 "$ROOT/out.json"; echo

wait $SSE_PID 2>/dev/null
echo "########## captured event types ##########"
grep -o '"type":"[^"]*"' "$ROOT/events.txt" | sort | uniq -c | sort -rn | head -30
echo "########## first 3 raw events ##########"
head -c 2000 "$ROOT/events.txt"; echo

echo "########## serve.log tail ##########"
tail -25 "$ROOT/serve.log"

kill "$SERVE_PID" 2>/dev/null
echo done
