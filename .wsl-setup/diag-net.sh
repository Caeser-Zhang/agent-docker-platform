#!/usr/bin/env bash
# Diagnose host.docker.internal reachability from inside the backend container.
set -u
C="docker compose -f /mnt/d/Project/agent-docker-demo/docker-compose.yml exec -T backend"

echo "== resolve host.docker.internal =="
$C getent hosts host.docker.internal || echo "NO RESOLVE"

echo "== gateway / routes =="
$C sh -c "cat /etc/hosts; ip route 2>/dev/null || true"

echo "== try port 8000 on candidates =="
for H in host.docker.internal 172.17.0.1 192.168.65.2; do
  $C python -c "
import socket,sys
h='$H'
try:
    s=socket.create_connection((h,8000),timeout=3); print(h,'OK'); s.close()
except Exception as e:
    print(h,'FAIL',type(e).__name__,e)
" 2>/dev/null || echo "$H python-fail"
done

echo "== WSL side: what listens on 8000 =="
ss -tlnp 2>/dev/null | grep :8000 || echo "nothing on 8000"
