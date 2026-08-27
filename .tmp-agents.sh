#!/bin/sh
AUTH="opencode:$OPENCODE_SERVER_PASSWORD"
BASE="http://127.0.0.1:4096"
# create a session
SID=$(curl -s -u "$AUTH" -X POST "$BASE/api/session" \
  -H 'Content-Type: application/json' \
  -d '{"agent":"build","location":{"directory":"/workspace"}}' \
  | grep -oE '"id":"[^"]+"' | head -1 | cut -d'"' -f4)
echo "SID=$SID"
# create a test file in workspace
echo "hello file content" > /workspace/tmp/at-test-file.txt 2>/dev/null || echo "write failed"
ls -la /workspace/tmp/at-test-file.txt 2>/dev/null
# send prompt with files attachment (file:// uri)
echo "=== send with file:// uri ==="
curl -s -u "$AUTH" -X POST "$BASE/api/session/$SID/prompt" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":{"text":"read the attached file","files":[{"uri":"file:///workspace/tmp/at-test-file.txt","mime":"text/plain","name":"at-test-file.txt"}],"agents":[{"name":"explorer"}]}}' | head -c 800
echo ""
