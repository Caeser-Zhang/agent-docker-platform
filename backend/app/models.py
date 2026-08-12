"""SQLAlchemy ORM models — maps to the agent_containers schema in the design doc."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    containers: Mapped[list["AgentContainer"]] = relationship(back_populates="user")


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


class UserLLMSelection(Base):
    """Per-user LLM selection for the "切换 LLM" config bar.

    Stores which provider/model the user wants, plus optional custom
    base_url/api_key overrides. The API key lives only here (server-side).
    """

    __tablename__ = "user_llm_selection"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), primary_key=True
    )
    provider_id: Mapped[str] = mapped_column(String(100), default="custom")
    model: Mapped[str] = mapped_column(String(100), default="")
    base_url: Mapped[str] = mapped_column(Text, default="")
    api_key: Mapped[str] = mapped_column(Text, default="")

    user: Mapped["User"] = relationship("User")
