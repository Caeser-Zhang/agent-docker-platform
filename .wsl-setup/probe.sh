#!/usr/bin/env bash
# 探测 WSL 内 8000 端口既有 fastdb 服务的 API 前缀与数据源
echo "== /fastk/api probe =="
curl -s -o /tmp/probe1.json -w 'status: %{http_code}\n' -X POST http://localhost:8000/fastk/api/databases/fastdb/query -H 'Content-Type: application/json' --data '{"filter": "1", "limit": 1}'
head -c 500 /tmp/probe1.json; echo
echo "== /fastdb/api probe =="
curl -s -o /tmp/probe2.json -w 'status: %{http_code}\n' -X POST http://localhost:8000/fastdb/api/databases/fastdb/query -H 'Content-Type: application/json' --data '{"filter": "1", "limit": 1}'
head -c 500 /tmp/probe2.json; echo
