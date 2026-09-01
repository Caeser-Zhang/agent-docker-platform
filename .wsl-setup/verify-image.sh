#!/usr/bin/env bash
# Verify fastk server query + chunk image endpoints.
set -u
BASE="http://localhost:8000/fastk/api"

echo "== query by chunk_id (fastdb) =="
curl -s "$BASE/databases/fastdb/query" -X POST \
  -H 'Content-Type: application/json' \
  -d '{"filter":"chunk_id == '"'"'fb62184133c0c818'"'"'","limit":1}' \
  --max-time 10 | head -c 600
echo

echo "== query by chunk_id (vl_test, has image) =="
curl -s "$BASE/databases/vl_test/query" -X POST \
  -H 'Content-Type: application/json' \
  -d '{"filter":"chunk_id == '"'"'4b632f199a038522'"'"'","limit":1}' \
  --max-time 10 | head -c 600
echo

echo "== chunk image (vl_test red.png) =="
curl -s -o /tmp/red.png -w 'status=%{http_code} type=%{content_type} size=%{size_download}B\n' \
  "$BASE/databases/vl_test/images?chunk_id=4b632f199a038522" --max-time 10
file /tmp/red.png 2>/dev/null || true

echo "== chunk image 404 (unknown chunk) =="
curl -s -w 'status=%{http_code}\n' \
  "$BASE/databases/vl_test/images?chunk_id=deadbeefdeadbeef" --max-time 10
