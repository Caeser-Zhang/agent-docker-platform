#!/usr/bin/env bash
# diag-agent-crash.sh — agent 容器崩溃一键诊断（在【问题复现的那台服务器】上运行）
#
# 用法：
#   bash scripts/diag-agent-crash.sh            # 诊断所有 agent-* 容器
#   bash scripts/diag-agent-crash.sh <容器名>   # 只诊断指定容器
#
# 原理：按退出码分流。Docker 退出码是判定死因的第一证据：
#   137 = SIGKILL  → OOMKilled=true 是内存超限；false 是被外部 kill（回收任务/人为）
#   139 = SIGSEGV  → 段错误，看容器日志里的 Bun crash stack
#   143 = SIGTERM  → 正常 stop（backend 回收路径）
#   0   = 正常退出
set -u

CONTAINER="${1:-}"

say() { printf '\n\033[1;36m===== %s =====\033[0m\n' "$*"; }

# --- 1. 宿主机环境（确认 glibc / 内核 / Docker 版本）---
say "宿主机环境"
echo "date:        $(date -Is)"
echo "hostname:    $(hostname)"
echo "kernel:      $(uname -r)"
echo "arch:        $(uname -m)"
if command -v ldd >/dev/null 2>&1; then
  echo "host glibc:  $(ldd --version | head -1)"
fi
docker version --format 'docker:      {{.Server.Version}} (api {{.Server.APIVersion}})' 2>/dev/null

# --- 2. 找到目标容器 ---
say "agent 容器清单"
if [ -n "$CONTAINER" ]; then
  docker ps -a --filter "name=$CONTAINER" --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
else
  docker ps -a --filter "name=agent" --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
fi

# --- 3. 逐容器取证 ---
inspect_one() {
  local c="$1"
  say "容器 $c — 死因分析"
  local meta started finished lifetime exit oom restarts
  meta=$(docker inspect "$c" 2>/dev/null) || { echo "inspect 失败（容器已删除？）"; return; }

  exit=$(printf '%s' "$meta" | grep -o '"ExitCode":[0-9]*' | head -1 | cut -d: -f2)
  oom=$(printf '%s' "$meta" | grep -o '"OOMKilled":\(true\|false\)' | head -1 | cut -d: -f2)
  restarts=$(printf '%s' "$meta" | grep -o '"RestartCount":[0-9]*' | head -1 | cut -d: -f2)
  started=$(printf '%s' "$meta" | grep -o '"StartedAt":"[^"]*"' | head -1 | cut -d'"' -f4)
  finished=$(printf '%s' "$meta" | grep -o '"FinishedAt":"[^"]*"' | head -1 | cut -d'"' -f4)

  if [ -n "$started" ] && [ -n "$finished" ] && [ "$finished" != "0001-01-01T00:00:00Z" ]; then
    lifetime=$(python3 - "$started" "$finished" <<'PY' 2>/dev/null || echo "?"
import sys
from datetime import datetime
s, f = sys.argv[1], sys.argv[2]
fmt = "%Y-%m-%dT%H:%M:%S"
try:
    s = datetime.fromisoformat(s.replace("Z", "+00:00"))
    f = datetime.fromisoformat(f.replace("Z", "+00:00"))
    m = int((f - s).total_seconds() // 60)
    print(f"{m} 分钟")
except Exception:
    print("?")
PY
)
  else
    lifetime="(运行中)"
  fi

  echo "ExitCode:     $exit"
  echo "OOMKilled:    $oom"
  echo "RestartCount: $restarts"
  echo "StartedAt:    $started"
  echo "FinishedAt:   $finished"
  echo "存活时长:     $lifetime"

  # 退出码分流判定
  case "$exit" in
    137)
      if [ "$oom" = "true" ]; then
        echo ">>> 判定: 内存超限被 OOM-Kill。检查 mem_limit 与 opencode/Bun 内存行为。"
      else
        echo ">>> 判定: 被外部 SIGKILL（非 OOM）。查 backend idle 回收日志 / docker events / 是否人为 kill。"
      fi
      ;;
    139)
      echo ">>> 判定: SIGSEGV 段错误 — Bun crash 嫌疑最大。下方日志应有 bun.report stack。"
      ;;
    143)
      echo ">>> 判定: SIGTERM 优雅停止 — backend stop_for_user()（10s 超时后 docker 会升级为 kill→137）。"
      ;;
    0)
      echo ">>> 判定: 正常退出（exit 0）。"
      ;;
    "")
      echo ">>> 容器运行中，无退出记录。"
      ;;
    *)
      echo ">>> 判定: 非典型退出码 $exit，结合下方日志分析。"
      ;;
  esac

  say "容器 $c — 最后 80 行日志（重点看 Segmentation fault / bun.report / panic）"
  docker logs --tail 80 "$c" 2>&1 | tail -80

  say "容器 $c — crash 关键词扫描（全文日志）"
  docker logs "$c" 2>&1 | grep -inE 'segmentation|sigsegv|bun\.report|panic|crash|slotvisitor|shellinterpreter|illegal' | tail -30 || echo "（未命中 crash 关键词）"
}

if [ -n "$CONTAINER" ]; then
  inspect_one "$CONTAINER"
else
  # 最多取 6 个最近退出的 agent 容器，避免输出爆炸
  mapfile -t targets < <(docker ps -a --filter "name=agent" --filter "status=exited" --format '{{.Names}}' | head -6)
  if [ "${#targets[@]}" -eq 0 ]; then
    echo "没有已退出的 agent 容器——若问题刚复现，请确认容器名前缀或指定容器名重跑。"
  else
    for t in "${targets[@]}"; do inspect_one "$t"; done
  fi
fi

# --- 4. Docker 事件流（回溯 2 小时内谁动过容器）---
say "docker events 近 2 小时（die/kill/oom 事件）"
timeout 10 docker events --since 2h --until "$(date -Is)" --filter 'event=die' --filter 'event=kill' --filter 'event=oom' 2>/dev/null | head -40 || true

say "诊断完成 — 请把以上全部输出贴回给 WorkBuddy 分析"

exit 0
