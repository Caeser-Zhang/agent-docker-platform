# 用户容器管理 — 需求分解文档

## 1. 需求总览

将「用户绑定 Docker 容器」及「容器的创建、启停、监控、管理」分解为 **4 大模块、16 条需求**，覆盖从用户注册到容器销毁的完整生命周期，并设计统一管理监控界面。

**设计原则**：平台不实现任何 Agent 逻辑，所有 AI 能力来自容器内的 `opencode serve`。平台职责 = 容器生命周期管理 + 配置注入 + 透明反向代理 + 统一监控管理。

---

## 2. 模块分解

### 模块一：用户绑定（R1–R3）

#### R1 — 用户注册与认证
- **已有**：JWT 认证 + 注册/登录（`auth.py`, `routers/auth.py`）
- **需新增**：
  - `User` 表增加 `role` 字段（`user` / `admin`）
  - 权限中间件 `require_admin`：管理后台 API 仅 admin 可访问
  - 用户创建时的默认配额初始化

#### R2 — 容器绑定关系
- **已有**：`AgentContainer.user_id` 实现 1:1 映射，容器名 `agent-{user_id}` 幂等
- **需新增**：
  - 绑定关系可视化：前端展示「我的容器」卡片（容器名、状态、IP、运行时长）
  - 解绑/重绑能力：admin 可将容器迁移到新镜像版本

#### R3 — 首次登录自动创建
- **已有**：`ensure_container()` 幂等创建（存在则复用，不存在则创建）
- **需新增**：
  - **R3.1 自定义工作空间**：每用户独立 Named Volume（已有 `agent-ws-{user_id}`），admin 可配额限制
  - **R3.2 资源配额**：per-user CPU/内存/PID 限制（当前全局统一，需改为可配置）
    - DB 表 `user_quotas`：`user_id, cpu_limit, memory_limit, pids_limit, disk_limit`
    - 配额超限拒绝创建，返回 402/429
  - 延迟创建策略：用户注册时不创建容器，首次 `start_agent` 时创建

---

### 模块二：容器生命周期（R4–R8）

#### R4 — 容器创建
- **已有**：`_build_run_kwargs()` 全量安全加固参数
  - `--user 1000:1000`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`
  - Named Volume (`/workspace`, `/data`), tmpfs (`/tmp`, `/home/agent`)
  - CPU/内存/PID 限制, healthcheck, restart policy
  - 配置注入 (`put_archive` 写入 `opencode.json`)
- **需新增**：
  - 创建前配额校验（调用 `user_quotas` 表）
  - 创建失败自动回滚（删容器 + 删半建卷）
  - 创建事件写入审计日志

#### R5 — 容器启动
- **已有**：
  - `start_for_user()` → `ensure_container()` → `_health_probe()` → `sse_pump_manager.start_pump()`
  - 健康探测 120s 超时，探测 opencode 自身 `GET /api/health`
  - SSE 泵自动连接容器 `/api/event`
  - `recover()` 平台重启后自动重新挂载 SSE 泵
- **需新增**：
  - 启动耗时统计（记录 `creating → running` 的 wall time）
  - 启动失败自动重试（最多 `max_restart_per_hour` 次）

#### R6 — 容器停止
- **已有**：
  - `stop_for_user()` → 先停 SSE 泵 → `container.stop(timeout=10)` → 更新 DB
  - 优雅停止，保留卷
- **需新增**：
  - 停止前 flush 会话状态（通知 opencode 保存）
  - 停止事件写入审计日志

#### R7 — 容器重启
- **已有**：`reload_config()` → 停容器 → 重新注入配置 → 启动 → 重连 SSE 泵
- **需新增**：
  - **R7.1 配置热重载（不停机）**：
    - 通过 `docker cp`（`put_archive`）直接将新 `opencode.json` 写入容器卷
    - 向 opencode 发送 SIGHUP 或调用其 reload 端点（如有）
    - 如果 opencode 不支持热重载，则 fallback 到重启策略
  - 重启原因记录（手动 / 配置变更 / 镜像升级 / 崩溃自愈）

#### R8 — 容器销毁
- **已有**：`destroy_container()` → `container.remove(force=True)` + 删两个 Named Volume
- **需新增**：
  - **R8.1 卷备份**：
    - 销毁前将 `/workspace` 和 `/data` 卷导出为 tar.gz
    - API: `POST /api/admin/containers/{user_id}/backup` → 返回下载链接
    - 备份保留 N 天后自动清理
  - **R8.2 空闲回收**（已有 `idle_reclaim_loop`，30 分钟无活动自动停止）：
    - 需新增：空闲 N 天后自动销毁（而不仅是停止）
    - 阈值可配置：`idle_stop_threshold`（30min, 已有）、`idle_destroy_threshold`（7d, 新增）
  - 销毁确认对话框（UI 二次确认）
  - 销毁事件写入审计日志

---

### 模块三：监控与告警（R9–R12）

#### R9 — 实时资源监控
- **需新增**：
  - 后端定时拉取 `docker stats` API（每 15s），存入 `container_metrics` 表
  - 采集指标：CPU%, 内存使用/限制, 网络上行/下行, 磁盘读/写, PID 数
  - API: `GET /api/admin/containers/{user_id}/metrics?range=1h|24h|7d`
  - 前端：容器详情面板实时展示资源使用条形图
  - WebSocket 或 SSE 推送实时 metrics 到管理后台

#### R10 — 全量容器状态总览
- **已有**：`list_all_containers()` 返回所有 `managed-by=agent-platform` 标签的容器
- **需新增**：
  - 分页 + 过滤 + 搜索（按用户名/容器名/状态/镜像版本）
  - 聚合统计：总容器数、running/idle/stopped/failed 分布
  - API: `GET /api/admin/containers?page=1&size=20&status=running&search=keyword`
  - 返回字段：用户名, 容器名, 状态, 镜像, IP, 运行时长, CPU%, MEM%, 最近活动时间

#### R11 — 事件日志
- **需新增**：
  - **R11.1 容器日志**（已有 `get_container_logs()`，tail 100 行）：
    - 改为流式推送（WebSocket / SSE），管理后台可实时查看 stdout/stderr
    - 支持日志搜索 + 时间范围过滤
  - **R11.2 操作审计日志**：
    - DB 表 `operation_logs`：`id, user_id, action, target, detail, timestamp, ip`
    - action 枚举：`start`, `stop`, `restart`, `destroy`, `reload_config`, `backup`, `quota_change`
    - API: `GET /api/admin/audit-logs?page=1&size=50&action=start&user_id=xxx`
    - 前端：审计日志时间线视图

#### R12 — 告警通知
- **需新增**：
  - 告警规则引擎：
    - 容器 `failed` 状态 → 立即告警
    - CPU > 90% 持续 5 分钟 → 告警
    - 内存 > 95% 持续 3 分钟 → 告警
    - 容器意外退出（非用户主动停止）→ 告警
    - 磁盘使用 > 80% → 告警
  - 通知渠道：
    - SSE 推送到管理后台（实时弹窗）
    - Webhook（可配置 Slack/飞书/钉钉机器人 URL）
  - 告警去重：同一容器同一规则 5 分钟内不重复
  - 告警恢复通知：状态恢复正常时发送 resolve 事件

---

### 模块四：管理界面（R13–R16）

#### R13 — 管理后台（admin only）
- **需新增**：
  - 路由守卫：前端检测 `user.role === "admin"` 才允许访问 `/admin` 路由
  - 三栏布局：
    - 左栏：用户/容器列表（状态指示灯 + 搜索 + 分页）
    - 右栏：容器详情面板（基本信息 + 实时资源 + 操作按钮）
    - 顶部：全局统计 bar（总容器数 + 各状态分布 + 集群资源汇总）
  - 详情面板展示：
    - 容器名、镜像、IP、端口、运行时间、创建时间
    - CPU/MEM/磁盘/PID 实时使用率（条形图）
    - 配置来源（宿主 opencode.json）、已剥离字段、最近配置重载时间

#### R14 — 批量操作
- **需新增**：
  - 多选容器（checkbox）
  - 批量启动、批量停止、批量重启、批量销毁
  - 批量配置重载（选中的容器全部重新注入配置）
  - 批量操作进度条 + 结果汇总（成功 N / 失败 M + 失败详情）

#### R15 — 配额管理
- **需新增**：
  - **R15.0 配额列表**：
    - API: `GET /api/admin/quotas` → 全量用户配额列表
    - 前端：表格展示每用户的 CPU/内存/PID/磁盘限制
  - **R15.1 配额编辑**：
    - API: `PUT /api/admin/quotas/{user_id}` → 更新配额
    - 修改后需重启容器才生效（提示 admin）
  - **R15.2 镜像管理**：
    - API: `GET /api/admin/images` → 列出本地 agent-demo 镜像版本
    - API: `POST /api/admin/containers/{user_id}/migrate` → 迁移到新镜像
    - 前端：镜像版本下拉选择 + 迁移按钮

#### R16 — 用户自助面板
- **已有**：Chat.tsx 侧边栏有「启动 Agent」「停止 Agent」「日志」「配置管理」按钮
- **需新增**：
  - **R16.0 我的容器状态卡片**：
    - 展示容器名、状态指示灯、运行时长、IP、镜像版本
    - 实时刷新（SSE 或定时轮询）
  - **R16.1 快照/恢复**：
    - API: `POST /api/agent/snapshot` → 导出当前 workspace + data 卷为 tar.gz
    - API: `POST /api/agent/restore` → 从 tar.gz 恢复卷
    - 前端：快照列表 + 下载 + 恢复按钮
  - **R16.2 资源使用概览**：
    - 展示当前容器的 CPU/MEM/磁盘使用率（简化版，非 admin 全量视图）

---

## 3. 数据模型变更

### 新增表

```sql
-- 用户配额
CREATE TABLE user_quotas (
    user_id       VARCHAR(36) PRIMARY KEY REFERENCES users(id),
    cpu_limit     REAL DEFAULT 2.0,        -- CPU 核数
    memory_limit  VARCHAR(20) DEFAULT '2g',
    pids_limit    INTEGER DEFAULT 200,
    disk_limit    VARCHAR(20) DEFAULT '5g',  -- workspace 卷上限
    created_at    TIMESTAMP DEFAULT NOW(),
    updated_at    TIMESTAMP DEFAULT NOW()
);

-- 容器指标（时序数据，可定期清理）
CREATE TABLE container_metrics (
    id            SERIAL PRIMARY KEY,
    user_id       VARCHAR(36) NOT NULL,
    cpu_percent   REAL,
    memory_used   BIGINT,     -- bytes
    memory_limit  BIGINT,
    net_rx        BIGINT,
    net_tx        BIGINT,
    disk_read     BIGINT,
    disk_write    BIGINT,
    pids          INTEGER,
    collected_at  TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_metrics_user_time ON container_metrics(user_id, collected_at);

-- 操作审计日志
CREATE TABLE operation_logs (
    id            SERIAL PRIMARY KEY,
    user_id       VARCHAR(36) NOT NULL,
    action        VARCHAR(50) NOT NULL,  -- start/stop/restart/destroy/...
    target        VARCHAR(100),          -- 目标容器名
    detail        TEXT,                  -- JSON 详情
    operator_id   VARCHAR(36) NOT NULL,  -- 谁操作的
    ip            VARCHAR(45),
    created_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_audit_user ON operation_logs(user_id);
CREATE INDEX idx_audit_action ON operation_logs(action);
CREATE INDEX idx_audit_time ON operation_logs(created_at);

-- 告警记录
CREATE TABLE alerts (
    id            SERIAL PRIMARY KEY,
    user_id       VARCHAR(36) NOT NULL,
    rule          VARCHAR(100) NOT NULL,  -- cpu_high/mem_high/failed/...
    severity      VARCHAR(20) DEFAULT 'warning',  -- info/warning/critical
    message       TEXT,
    status        VARCHAR(20) DEFAULT 'firing',   -- firing/resolved
    triggered_at  TIMESTAMP DEFAULT NOW(),
    resolved_at   TIMESTAMP,
    UNIQUE(user_id, rule, triggered_at)  -- 去重
);
```

### 修改表

```sql
-- User 表增加角色字段
ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'user';  -- user/admin

-- AgentContainer 表增加字段
ALTER TABLE agent_containers ADD COLUMN image_version VARCHAR(100);
ALTER TABLE agent_containers ADD COLUMN last_restarted_at TIMESTAMP;
ALTER TABLE agent_containers ADD COLUMN restart_reason VARCHAR(50);
ALTER TABLE agent_containers ADD COLUMN workspace_size BIGINT DEFAULT 0;
```

---

## 4. API 端点规划

### 管理后台 API（admin only，`/api/admin`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/containers` | 全量容器列表（分页+过滤+搜索） |
| GET | `/api/admin/containers/{user_id}` | 单个容器详情 |
| POST | `/api/admin/containers/{user_id}/start` | 管理员启动用户容器 |
| POST | `/api/admin/containers/{user_id}/stop` | 管理员停止用户容器 |
| POST | `/api/admin/containers/{user_id}/restart` | 管理员重启用户容器 |
| DELETE | `/api/admin/containers/{user_id}` | 管理员销毁用户容器 |
| POST | `/api/admin/containers/{user_id}/backup` | 导出卷备份 |
| GET | `/api/admin/containers/{user_id}/metrics` | 容器资源指标 |
| GET | `/api/admin/containers/{user_id}/logs/stream` | 容器日志实时流 (SSE) |
| POST | `/api/admin/containers/batch` | 批量操作 |
| GET | `/api/admin/quotas` | 配额列表 |
| PUT | `/api/admin/quotas/{user_id}` | 编辑配额 |
| GET | `/api/admin/images` | 镜像版本列表 |
| POST | `/api/admin/containers/{user_id}/migrate` | 迁移到新镜像 |
| GET | `/api/admin/audit-logs` | 操作审计日志 |
| GET | `/api/admin/alerts` | 告警列表 |
| GET | `/api/admin/stats` | 全局统计 (容器数/资源汇总) |

### 用户自助 API（`/api/agent`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agent/status` | 我的容器状态（已有） |
| POST | `/api/agent/start` | 启动我的容器（已有） |
| POST | `/api/agent/stop` | 停止我的容器（已有） |
| GET | `/api/agent/logs` | 我的容器日志（已有） |
| GET | `/api/agent/metrics` | 我的容器资源使用 |
| POST | `/api/agent/snapshot` | 创建快照 |
| GET | `/api/agent/snapshots` | 快照列表 |
| POST | `/api/agent/restore` | 从快照恢复 |

---

## 5. 前端组件规划

### 新增组件

| 组件 | 位置 | 说明 |
|---|---|---|
| `AdminPanel.tsx` | `/admin` 路由 | 管理后台主页面 |
| `ContainerList.tsx` | AdminPanel 左栏 | 用户/容器列表 + 搜索 + 分页 |
| `ContainerDetail.tsx` | AdminPanel 右栏 | 容器详情 + 资源监控 + 操作按钮 |
| `BatchOps.tsx` | AdminPanel 顶部 | 批量操作下拉 + 进度条 |
| `MetricsBar.tsx` | ContainerDetail 内 | CPU/MEM/磁盘 条形图 |
| `AuditLog.tsx` | AdminPanel 标签页 | 操作审计时间线 |
| `AlertBanner.tsx` | 全局 | 告警通知弹窗 |
| `QuotaEditor.tsx` | AdminPanel 弹窗 | 配额编辑表单 |
| `MyContainerCard.tsx` | Chat.tsx 侧边栏 | 用户自助容器状态卡片 |
| `SnapshotManager.tsx` | Chat.tsx 弹窗 | 快照列表 + 恢复 |

### 修改组件

| 组件 | 变更 |
|---|---|
| `App.tsx` | 增加 `/admin` 路由守卫 |
| `Chat.tsx` | 侧边栏增加 `MyContainerCard` + 快照入口 |
| `api.ts` | 增加 admin API + metrics + snapshot 方法 |
| `chatStyles.ts` | 增加管理后台样式 |

---

## 6. 实现优先级

### P0 — 核心可用（先做）
1. R1.1 角色体系（User.role + 权限中间件）
2. R10 全量容器列表（分页+过滤）
3. R4.2 配额校验（创建前检查）
4. R8 + R8.1 容器销毁 + 卷备份
5. R13 管理后台三栏布局
6. R16.0 用户自助状态卡片

### P1 — 监控增强
7. R9 实时资源监控（docker stats 接入）
8. R11.2 操作审计日志
9. R14 批量操作

### P2 — 运维自动化
10. R12 告警通知
11. R7.1 配置热重载
12. R15 配额管理 + 镜像管理
13. R8.2 空闲销毁（7 天阈值）

### P3 — 用户体验
14. R16.1 快照/恢复
15. R11.1 容器日志实时流
16. R3.2 per-user 可配置资源配额
