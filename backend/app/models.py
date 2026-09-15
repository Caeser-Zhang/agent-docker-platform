"""SQLAlchemy ORM models — maps to the agent_containers schema in the design doc."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Text, Integer, Float, DateTime, Boolean, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # Employee number (工号) — unique, auto-assigned at registration when absent.
    uid: Mapped[str | None] = mapped_column(String(50), unique=True, index=True, nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(200))
    # "user" | "admin" — admins get the Docker management panel.
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Active LLM selection — a lightweight pointer to one of this user's
    # ``user_llm_providers`` rows (by ``provider_id`` slug) plus an optional
    # model id. Replaces the legacy ``user_llm_selection`` table, which stored
    # plaintext credentials and was never consumed. No secrets here: keys live
    # in the provider row's encrypted columns.
    active_llm_provider_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    active_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    containers: Mapped[list["AgentContainer"]] = relationship(back_populates="user")
    mcp_servers: Mapped[list["UserMcpServer"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    builtin_mcp_toggles: Mapped[list["UserBuiltinMcpToggle"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    llm_providers: Mapped[list["UserLLMProvider"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AgentContainer(Base):
    """Per-user agent container record — replaces the in-memory dict.

    Schema matches Appendix C of the design document.
    """

    __tablename__ = "agent_containers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    container_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="absent")
    # States: absent / creating / starting / running / idle / stopped / failed / destroyed

    password_enc: Mapped[str] = mapped_column(Text, nullable=False)
    image: Mapped[str] = mapped_column(String(200), nullable=False)
    workspace_volume: Mapped[str] = mapped_column(String(100), nullable=False)
    data_volume: Mapped[str] = mapped_column(String(100), nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship(back_populates="containers")

    __table_args__ = (
        Index("idx_agent_containers_user", "user_id"),
        Index("idx_agent_containers_status", "status"),
    )


class UserMcpServer(Base):
    """A single user's custom MCP server, isolated by ``user_id``.

    Non-sensitive metadata (name/type/enabled) is stored as plain columns so
    listings and toggling never require decryption. Everything else — command,
    url, headers, environment, cwd, timeout — is stored as one encrypted JSON
    blob in ``config_enc`` (see :mod:`app.crypto`). ``name`` is unique per
    user, so two users may each define an MCP server with the same name.
    """

    __tablename__ = "user_mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # "local" | "remote"
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_enc: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship(back_populates="mcp_servers")

    __table_args__ = (
        Index("idx_user_mcp_servers_user", "user_id"),
        UniqueConstraint("user_id", "name", name="uq_user_mcp_server_name"),
    )


class UserBuiltinMcpToggle(Base):
    """One user's enable/disable choice for one platform built-in MCP server.

    Built-in MCP servers are defined image-side (read-only manifests), so
    unlike :class:`UserMcpServer` there is no connection config to own here —
    only a visibility preference keyed by ``(user_id, name)``. No row means
    "inherit the platform default" (on), which keeps the table empty until a
    user actually diverges from the platform.

    Enforcement reuses the visibility mechanism already in place for
    platform-wide hides: a disabled name becomes a
    ``permission["<sanitized>_*"] = "deny"`` rule in *this user's* rendered
    opencode.json plus an exclusion in their plugin config preset lists. An
    admin's platform-wide hide always wins — a user row can only narrow
    visibility, never widen it (see
    :func:`opencode_config.hidden_mcp_servers`).
    """

    __tablename__ = "user_builtin_mcp_toggles"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship(back_populates="builtin_mcp_toggles")

    __table_args__ = (
        Index("idx_user_builtin_mcp_toggles_user", "user_id"),
    )


class UserLLMProvider(Base):
    """A single user's custom LLM provider, isolated by ``user_id``.

    ``provider_id`` is a user-chosen slug unique per user. Credentials
    (base_url, api_key) and the optional model list are encrypted at rest.
    """

    __tablename__ = "user_llm_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    npm: Mapped[str] = mapped_column(String(100), default="@ai-sdk/openai-compatible")
    base_url_enc: Mapped[str] = mapped_column(Text, default="")
    api_key_enc: Mapped[str] = mapped_column(Text, default="")
    models_enc: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    user: Mapped["User"] = relationship(back_populates="llm_providers")

    __table_args__ = (
        Index("idx_user_llm_providers_user", "user_id"),
        UniqueConstraint("user_id", "provider_id", name="uq_user_llm_provider"),
    )


class Project(Base):
    """A user-created project space — a directory inside the workspace volume.

    The platform stores only the project roster; session membership is NOT
    recorded here. opencode derives each session's project from its
    ``location.directory``, so the frontend groups sessions by matching
    ``session.location.directory`` against ``Project.directory``.

    ``origin`` records how the project came to be:
      - "created": platform made a fresh directory under /workspace/projects/
      - "bound":   user picked an existing workspace directory

    Deleting a project removes this row and the sessions under its directory
    (via opencode's API); the directory itself is never touched, so files
    remain visible in the workspace browser.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Container-absolute path, e.g. /workspace/projects/官网改版 — its last
    # segment always equals the project name, and it must match opencode's
    # session.location.directory exactly for grouping to work.
    directory: Mapped[str] = mapped_column(String(500), nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False, default="created")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("idx_projects_user", "user_id"),
        UniqueConstraint("user_id", "name", name="uq_projects_user_name"),
        UniqueConstraint("user_id", "directory", name="uq_projects_user_dir"),
    )


class AuditEvent(Base):
    """P1-6: lifecycle audit trail — one row per significant platform action
    (start / stop / restart / destroy).

    ``user_id`` is a plain indexed string with no FK on purpose: audit rows
    must survive the referenced user's deletion — destroying the subject
    must never erase the evidence. ``detail`` holds a JSON blob (e.g. the
    destroy-time backup metadata).
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # "container.start" | "container.stop" | "container.restart" | "container.destroy"
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="")  # JSON

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class KbKey(Base):
    """A knowledge base's API credential — the minimal copy the proxy needs.

    The fastk server owns the authoritative key↔database mapping; the platform
    only stores what it must inject when forwarding a read request on a user's
    behalf. ``kb_name`` is the server's PHYSICAL database name (the CLI's
    logical→physical mapping is a container-side concern, see FASTK_DB_MAP).
    The key itself is Fernet-encrypted (see :mod:`app.crypto`) and is never
    returned by any API.
    """

    __tablename__ = "kb_keys"

    kb_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class KbGrant(Base):
    """Whitelist entry: one user may read one knowledge base.

    The unit of authorisation is the DATABASE, not the key — that matches both
    the server's per-database key scoping and how admins think about access.
    A credential can never be deleted while leaving grants behind: the admin
    route removes them in the same transaction (the FK's ON DELETE CASCADE only
    fires on PostgreSQL — SQLite needs foreign_keys=ON, which this project does
    not set).

    ``user_id`` has a real FK (unlike AuditEvent/RequestLog): grants are live
    access control, not evidence, so they must disappear with the user.
    """

    __tablename__ = "kb_grants"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kb_name: Mapped[str] = mapped_column(
        String(100), ForeignKey("kb_keys.kb_name", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Soft delete: revoking stamps this instead of dropping the row, so the
    # (user_id, kb_name) history survives and re-granting is an UPDATE that
    # clears it — the composite PK forbids a second INSERT for the same pair.
    # Every enforcement query filters on ``revoked_at IS NULL``.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    __table_args__ = (
        Index("idx_kb_grants_kb", "kb_name"),
    )


class RequestLog(Base):
    """Platform-side access log for tunnel-proxied requests — one row per call.

    opencode 1.x emits no HTTP request logs of its own (its server disables
    the HTTP logger), so the tunnel proxy is the only vantage point from
    which the platform can observe request/response traffic between the
    browser and each user's container. Bodies are deliberately NOT stored:
    prompts and LLM responses can be huge; method/path/status/duration is
    what an access log needs.

    ``user_id`` is a plain indexed string with no FK, mirroring AuditEvent:
    log rows must survive the referenced user's deletion.
    """

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    # opencode path incl. query string, e.g. "/api/session?limit=20".
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    __table_args__ = (
        Index("idx_request_logs_user_time", "user_id", "created_at"),
    )


class MessageFeedback(Base):
    """用户体验采集：一条 assistant 回复的点赞/点踩（任务一）。

    点击后不可取消；重提同一 (user_id, message_id) 幂等返回 already=true。
    ``context`` 存本轮完整上下文快照（上一 user 提问 → 本 assistant 全部
    输出），JSON 序列化后落 Text 列（沿用 AuditEvent.detail 约定，双方言安全）。
    点踩时 ``reason_codes`` 存白名单原因码数组、``reason_text`` 存「其他」文本。

    ``user_id`` 无 FK（同 AuditEvent/RequestLog）：反馈是证据，须在用户删除后存活。
    """

    __tablename__ = "message_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # 被打分的 assistant 消息 id（自然键的一半）。
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 本轮对应的 user 提问消息 id（上下文快照的起点）。
    user_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)  # "up" | "down"
    # 原因码白名单 JSON 数组：misunderstood / wrong_answer / tool_failure /
    # too_verbose / ignored_constraints / interrupted / other。
    reason_codes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # ≤500
    turn_errored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # JSON 快照
    context_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "message_id", name="uq_feedback_user_message"),
        Index("idx_feedback_session", "session_id"),
        Index("idx_feedback_verdict_time", "verdict", "created_at"),
    )


class AgentRoundMetrics(Base):
    """一个回合（user prompt → session.idle）的结算指标（任务二 L1/L2/L3）。

    明细行，看板查询时聚合（分位数无法由预聚合均值二次推导）。由服务端 SSE
    tap 实时结算，或回补时经同一 RoundAggregator 离线重放写入。``source``
    区分二者。自然键 (user_id, session_id, round_seq) 保证回补幂等。

    ``is_task``：该回合是否出现过 todo.updated（双轨统计里的「任务」轨）；
    ``task_success``：is_task 时 todos 是否全部完成。``tokens`` 原样存整个
    dict（防 cache.read/write 等未列字段丢失），``total_tokens`` 为冗余求和列。

    ``user_id`` 无 FK：度量证据须在用户删除后存活。
    """

    __tablename__ = "agent_round_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    round_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 幂等自然键的一半：assistant 消息 id（opencode 内全局唯一、tap 与回补一致）；
    # 无 assistant 消息的回合用 "user:{user_message_id}" 合成兜底，保证非空且确定。
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    is_task: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # --- L1 结果层 ---
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    task_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    errored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 错误分类（A1）：opencode 错误联合的判别名（ProviderAuthError/APIError/
    # ContextOverflowError/MessageAbortedError/ContentFilterError…），供错误率下钻。
    error_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # APIError 的上游 HTTP 状态码（429/5xx/401…）；非 APIError 时为 None。
    error_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # --- L2 效率与性能 ---
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON dict 原样
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Token 拆分（A3）：冗余列供 SQL 聚合（缓存命中率 / reasoning 占比 / 成本归因）。
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    # --- L3 过程与轨迹 ---
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # --- 维度 ---
    model_provider: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    agent: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="tap")  # tap | backfill

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("user_id", "session_id", "message_id", name="uq_round_natural"),
        Index("idx_round_user_time", "user_id", "created_at"),
        Index("idx_round_model", "model_provider", "model_id"),
        Index("idx_round_session_seq", "session_id", "round_seq"),
    )


class ToolCallMetrics(Base):
    """单次工具调用（任务二 L3 工具调用准确率）。

    独立成表以支持 GROUP BY tool_name 的高频聚合（塞进 round 的 JSON 列会
    无法走索引）。``is_error`` 对应 tool part state.status == "error"。
    """

    __tablename__ = "tool_call_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    round_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # completed|error|pending|running
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 单次工具耗时（A2）：state.time.end - state.time.start。仅实时 tap 可得
    # （REST 回补的 SessionMessageToolState* 无 time 字段），故回补行为 None。
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="tap")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    __table_args__ = (
        Index("idx_tool_user_time", "user_id", "created_at"),
        Index("idx_tool_name_error", "tool_name", "is_error"),
    )


class LLMProxyMetrics(Base):
    """一次经 ``/llm-proxy`` 转发到上游 provider 的调用指标（B1 状态码 + B2 延迟/TTFT）。

    LLM 代理是所有容器到上游的必经之路，是平台侧唯一能直接观测上游健康度的位置。
    ``status_code`` 为上游返回码（连接失败/代理错误时为 502，``upstream_error=True``）；
    ``ttft_ms`` 为发起请求到收到上游**首个响应字节**的耗时（流式即首 chunk），
    ``duration_ms`` 为到响应体完全消费/关闭为止的总耗时。二者之差≈上游出流时长。

    ``user_id`` 仅用户级代理路由（``/_user/{user_id}/...``）可得，宿主级路由为 None。
    与 RequestLog 一致：无 FK，度量证据须在用户删除后存活；body 一律不存。
    """

    __tablename__ = "llm_proxy_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="POST")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_sse: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    upstream_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    __table_args__ = (
        Index("idx_llm_user_time", "user_id", "created_at"),
        Index("idx_llm_provider_time", "provider_id", "created_at"),
    )
