#!/usr/bin/env bash
# 验证 fastk serve /fastk/api query 接口（按 chunk_id 过滤）
echo "== fastdb 库 chunk fb62184133c0c818 =="
curl -s -X POST http://localhost:8000/fastk/api/databases/fastdb/query \
  -H 'Content-Type: application/json' \
  --data '{"filter": "chunk_id == '"'"'fb62184133c0c818'"'"'", "limit": 5}' | head -c 700
echo
echo "== vl_test 库 chunk 4b632f199a038522 (red.png) =="
curl -s -X POST http://localhost:8000/fastk/api/databases/vl_test/query \
  -H 'Content-Type: application/json' \
  --data '{"filter": "chunk_id == '"'"'4b632f199a038522'"'"'", "limit": 5}' | head -c 700
echo
echo "== fastdb 库 search 冒烟 =="
curl -s -X POST 'http://localhost:8000/fastk/api/databases/fastdb/search?limit=2' \
  -H 'Content-Type: application/json' \
  --data '{"query": "chunk", "topk": 2}' | head -c 400
echo
