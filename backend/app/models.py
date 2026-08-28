"""SQLAlchemy ORM models — maps to the agent_containers schema in the design doc."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Text, Integer, DateTime, Boolean, ForeignKey, Index, UniqueConstraint
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
