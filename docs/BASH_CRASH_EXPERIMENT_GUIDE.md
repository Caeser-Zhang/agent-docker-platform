# Bash 工具崩溃问题：根因验证与规避实验指导

> 适用问题：项目部署在 **Linux 服务器**上时，每次调用 opencode 的 **bash 工具**都会立即崩溃并导致容器重启（必现）；同样的部署在 **WSL** 中不会崩溃。
>
> 之前初步定位为 "bun 崩溃"。本指导的结论是：**bun 崩溃只是直接死因（果），真正的环境差异根因是 AppArmor 生效性不同**。本文给出完整的验证实验步骤与规避方案。

---

## 1. 背景与崩溃链条（已确认的事实）

以下事实全部来自项目代码本身，可直接核对：

| # | 事实 | 代码位置 |
|---|------|----------|
| 1 | agent 容器的 **PID 1 就是 opencode 本身**（`exec opencode "$@"`，无 tini/init 包装） | `agent-image/entrypoint.sh` 末行 |
| 2 | opencode 是 **bun 打包的 standalone 单二进制**（runtime 镜像内无 node） | `agent-image/Dockerfile` 两阶段构建 |
| 3 | bash 工具每次调用都走 bun 的 **spawn 子进程路径**（`Bun.spawn` / `posix_spawn`）去启动 `/bin/bash` | opencode 内置工具 |
| 4 | 容器带有硬编码加固参数，其中含 `apparmor=docker-default` | `backend/app/services/container_manager.py` → `_build_run_kwargs()` |
| 5 | 容器配置了 `restart_policy: unless-stopped`，崩溃后自动拉起 | 同上 |

**崩溃链条**：

```
bash 工具调用
  → bun spawn 子进程失败/异常（某 syscall 被拒，返回 EPERM 等）
  → bun 在该失败路径上存在已知段错误缺陷（空指针解引用，SIGSEGV）
  → PID 1 死亡 → 容器退出（ExitCode 139）
  → restart: unless-stopped 自动重启容器
  → 现象："调用 bash 工具必现崩溃并重启容器"
```

**关键外部证据**（bun/opencode 已知缺陷）：

- opencode issue #11043：bash 工具 + spawn 失败时，底层 `uv_spawn` 空指针解引用，立即 `Segmentation fault at address 0x0`。bun 不健壮地校验/处理 spawn 错误（EPERM/EACCES/ENOENT/EAGAIN）。
- NVIDIA OpenShell PR #1078（同构问题的独立验证）：*"Windows WSL2 Docker ... ship without AppArmor, so ... is not blocked there"* —— **WSL2 内核不启用 AppArmor，真实 Ubuntu/Debian 服务器启用**。

**为什么"服务器崩 / WSL 不崩"**：两个环境传给 Docker 的参数完全相同（backend/.env 无覆盖），但内核层效果不同：

| 环境 | AppArmor 状态 | `apparmor=docker-default` 实际效果 |
|------|--------------|-------------------------------------|
| WSL2（微软定制内核） | 未启用 | **被静默忽略**，容器等效 unconfined |
| 真实 Linux 服务器 | 启用（Ubuntu/Debian 默认） | **docker-default profile 真实生效** |

当 spawn 路径上某个系统调用被 AppArmor（或联动的 seccomp）拒绝返回 EPERM 时，bun 直接走进段错误路径。bash 工具每次调用都走同一条代码路径 → **必现**。

**次要嫌疑**（需一并排查，见实验 6）：

1. Docker 版本 / 默认 seccomp profile 差异：服务器上 docker-ce 若较旧（<23.x），默认 seccomp 对新 syscall（如 `clone3`）返回 EPERM 而非 ENOSYS，是容器内 spawn 失败的经典来源。
2. 镜像漂移：两边 `agent-demo` 镜像是否同一 digest。
3. cgroup v1/v2 差异（若 ExitCode 是 137 而非 139，方向转向 OOM：`mem_limit=2g` + 无 swap）。

---

## 2. 实验前准备

```bash
# 变量：替换为实际用户 UID
AGENT=agent-<uid>
# 在【Linux 服务器】宿主机上执行（对照实验在 WSL 宿主机上执行同名命令）
```

记录基线信息（两个环境各跑一次，输出存档对比）：

```bash
docker info | grep -iE 'apparmor|seccomp|cgroup' > env-$(hostname).txt
docker images --digests | grep agent-demo >> env-$(hostname).txt
docker version --format '{{.Server.Version}}' >> env-$(hostname).txt
```

---

## 3. 实验一：确认死因（排除 OOM，确认 SIGSEGV）⏱ 1 分钟

触发一次 bash 工具调用使其崩溃后，在宿主机执行：

```bash
docker inspect $AGENT --format 'ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} RestartCount={{.RestartCount}}'
```

**判定**：

| 结果 | 结论 | 下一步 |
|------|------|--------|
| `ExitCode=139`，`OOMKilled=false` | SIGSEGV，bun 段错误，符合主假设 | 继续实验二 |
| `ExitCode=137`，`OOMKilled=true` | cgroup OOM，不是 bun 崩溃 | 转向内存排查：调高 `mem_limit` 或加 swap，本文不适用 |
| `ExitCode=139` + 频繁 `RestartCount` 增长 | 确认"崩溃→重启"循环来自 `restart_policy` | 继续实验二 |

---

## 4. 实验二：拿到崩溃现场证据 ⏱ 2 分钟

```bash
# 1) 容器日志：找 bun crash report（典型输出 "Segmentation fault at address 0x0"）
docker logs $AGENT --tail 200

# 2) 宿主机内核日志：找 AppArmor / seccomp 拒绝记录（根因直接证据）
dmesg -T | grep -iE 'apparmor|denied|seccomp' | tail -30
# 若无输出可尝试：
journalctl -k --since "1 hour ago" | grep -iE 'apparmor|denied' | tail -30
```

**判定**：

- 出现 `apparmor="DENIED" ... profile="docker-default"` → **根因锤死**，直接跳到 §7 规避方案。
- 无 DENIED 记录但仍 segfault → seccomp 不产生 dmesg 日志，继续实验三（二分法）。
- 日志中出现 `Segmentation fault at address 0x0` → 与 opencode #11043 的崩溃模式吻合，佐证 bun spawn 缺陷。

---

## 5. 实验三：决定性二分实验 ⏱ 5 分钟

核心思路：用与 `_build_run_kwargs()` 相同的加固参数手动起容器，**逐项放开安全限制**，每次触发一次 bash 工具调用，找出崩溃消失的开关。

### 步骤 3.1：基线复现（应崩溃）

```bash
docker rm -f agent-test 2>/dev/null
docker run -d --name agent-test \
  --user 1000:1000 --read-only \
  --tmpfs /tmp:size=256m --tmpfs /home/agent:size=512m,uid=1000 \
  --cpus 2.0 --memory 2g --pids-limit 200 \
  --ulimit nofile=8192:8192 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --security-opt apparmor=docker-default \
  <agent-image> serve --hostname 0.0.0.0 --port 4096
# 通过平台或 curl 触发一次 bash 工具调用，观察是否崩溃
```

### 步骤 3.2：仅放开 AppArmor（主假设验证）

```bash
docker rm -f agent-test
docker run -d --name agent-test \
  --user 1000:1000 --read-only \
  --tmpfs /tmp:size=256m --tmpfs /home/agent:size=512m,uid=1000 \
  --cpus 2.0 --memory 2g --pids-limit 200 \
  --ulimit nofile=8192:8192 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --security-opt apparmor=unconfined \
  <agent-image> serve --hostname 0.0.0.0 --port 4096
# 再次触发 bash 工具调用
```

### 步骤 3.3：若仍崩溃，继续逐项二分

按顺序每次只放开一项，直到崩溃消失：

```bash
# a) 放开 pids-limit（撞到 200 上限时 spawn 返回 EAGAIN）
--pids-limit 500            # 或去掉

# b) 放开 cap-drop（保留基本 caps）
# 删除 --cap-drop ALL

# c) 放开 no-new-privileges
# 删除 --security-opt no-new-privileges:true

# d) 放开 read-only（检查 /var 等 bun 可能写入的路径）
# 删除 --read-only（保留 tmpfs 观察）

# e) 放开 seccomp（验证旧版 Docker 默认 seccomp 拒绝 clone3 等新 syscall 的可能）
--security-opt seccomp=unconfined
```

### 判定矩阵

| 崩溃消失的开关 | 根因 | 对应规避 |
|----------------|------|----------|
| `apparmor=unconfined` | **AppArmor docker-default 拒绝 bun spawn 所需操作**（主假设） | §7.1 |
| `pids-limit` 提高 | pids 额度撞顶 → spawn EAGAIN → bun 崩溃 | §7.3 |
| 删除 `cap-drop ALL` | 缺 capability → spawn EPERM | 调整 cap 集合 |
| `seccomp=unconfined` | 旧 Docker 默认 seccomp 拒绝新 syscall | 升级 Docker / §7.3 |
| 删除 `read-only` | bun 需要写某只读路径 | 补充 tmpfs 挂载点 |
| 全部放开仍崩溃 | 镜像/内核本身差异 | 对比镜像 digest 与内核版本，考虑升级 opencode |

---

## 6. 实验四（补充）：WSL 对照与环境取证

在 WSL 宿主机上执行，与服务器输出对比：

```bash
docker info | grep -iE 'apparmor|seccomp|cgroup'
# 预期：WSL 显示 apparmor 未启用/不存在相关安全驱动 → 解释 security_opt 形同虚设

docker images --digests | grep agent-demo
# 预期：两边 digest 一致 → 排除镜像漂移；不一致 → 先同步镜像再复测
```

---

## 7. 规避方案（按实验结果分层落地）

### 7.1 立即规避：显式禁用 AppArmor（对应实验 3.2 命中）

**注意：仅删除 `apparmor=docker-default` 这一行没有用** —— AppArmor 启用的宿主上 Docker 默认就给容器套 docker-default profile。必须**显式覆盖为 unconfined**。

修改 [container_manager.py](../backend/app/services/container_manager.py) `_build_run_kwargs()`：

```python
"security_opt": [
    "no-new-privileges:true",
    "apparmor=unconfined",   # 显式禁用，与 WSL 行为对齐
],
```

更稳妥：做成**按宿主探测的配置项**而非硬编码 —— 在 `config.py` 增加环境变量 `AGENT_CONTAINER_APPARMOR`（缺省 `unconfined`，需要强隔离的宿主再显式指定）：

```python
# config.py
container_apparmor: str = "unconfined"   # 可选: docker-default / unconfined

# container_manager.py
"security_opt": [
    "no-new-privileges:true",
    f"apparmor={settings.container_apparmor}",
],
```

> 安全提示：`apparmor=unconfined` 会失去一层 LSM 防护。已有的 `cap_drop ALL` + `no-new-privileges` + 非 root + 只读根分区仍然保留，整体安全水位可接受；对隔离要求高的宿主可编写自定义 AppArmor profile（放行 bun spawn 所需操作、其余沿用 docker-default），工作量更大但保留防护。

### 7.2 架构级方案：不要让 opencode 当 PID 1（根治"崩一次 = 整个容器重启"）

当前任何一次 bun 崩溃 = 会话中断 + 容器重启 + 现场（日志/core）随 tmpfs 丢失。把 `entrypoint.sh` 末尾的 `exec opencode` 换成轻量 supervisor 循环：

```bash
# entrypoint.sh 末尾
while true; do
  opencode "$@" --hostname 0.0.0.0 --port "$OPENCODE_PORT" || {
    echo "[supervisor] opencode exited code=$? at $(date)" >&2
    sleep 1
  }
done
```

效果：单次崩溃只损失当前工具调用（上层平台已有重试），进程秒级重启、崩溃日志保留在容器 stdout、会话状态在 /data 卷不受影响 —— 把"必现灾难"降级为"可观测的单次失败"。

### 7.3 中期加固

- **升级 opencode**：1.18.16 内嵌的 bun 1.3.x 仍有未清的 segfault 问题；opencode 后续版本已给 bash 工具加入 spawn 前的目录校验（PR #6763），并持续修复 spawn/shell 崩溃。升级前用实验三的同款二分法回归验证。
- **提高 pids-limit**：200 偏紧（opencode 的 LSP、插件、工具子进程共用额度），spawn 撞到 EAGAIN 同样会踩 bun 的脆弱错误路径。建议 ≥ 500。
- **升级服务器 Docker**（若实验 3.3-e 命中）：≥ 23.x 的默认 seccomp profile 对未知 syscall 返回 ENOSYS 而非 EPERM。
- **开启 core dump**（可选）：容器加 `ulimits: [{"name": "core", "soft": -1, "hard": -1}]`，配合宿主 core_pattern 写到持久卷，下次崩溃可拿到完整崩溃栈。

---

## 8. 参考证据

- opencode issue #11043 — bash 工具 + cwd/spawn 失败 → `uv_spawn` 空指针解引用 → `Segmentation fault at address 0x0`
- opencode PR #6763 — bash 工具 spawn 前加入 isDir 校验
- NVIDIA OpenShell PR #1078 — "Windows WSL2 Docker ... ship without AppArmor"（WSL2 无 AppArmor 的独立验证）
- bun issue #24455 / #24012 — OpenCode 场景 Segfault at 0x0、spawn 错误处理不健壮
- Docker 旧版 seccomp 拒绝 `clone3` 返回 EPERM → 容器内 spawn 类调用失败的已知模式

---

## 9. 实验记录表（执行时填写）

| 日期 | 环境 | 实验 | 结果 | 判定 |
|------|------|------|------|------|
| | 服务器 | 实验 1：ExitCode/OOMKilled | | |
| | 服务器 | 实验 2：dmesg DENIED | | |
| | 服务器 | 实验 3.1：基线复现 | | |
| | 服务器 | 实验 3.2：apparmor=unconfined | | |
| | 服务器 | 实验 3.3：逐项二分 | | |
| | WSL | 实验 4：对照取证 | | |
| | 服务器 | 规避方案落地后回归 | | |
