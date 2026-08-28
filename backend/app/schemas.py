"""Pydantic request/response schemas."""
from pydantic import BaseModel


class RegisterRequest(BaseModel):
    username: str
    password: str
    # Optional employee number (工号); auto-assigned when omitted.
    uid: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    role: str = "user"


class StartAgentRequest(BaseModel):
    workspace: str | None = None
    # True = block until the startup flow finishes (scripts / e2e tests).
    # False (default) = return immediately; the caller polls GET /agent/status
    # for the live phase (creating → starting → warming → running).
    wait: bool = False


class AgentStatusResponse(BaseModel):
    running: bool
    healthy: bool = False
    status: str = "absent"
    # Epoch seconds when the current background start flow began (only set
    # while a start is in flight) — lets the UI show an accurate total wait
    # even for a browser that attached mid-start (P1-4).
    phase_since: float | None = None
    container_name: str | None = None
    workspace: str | None = None
    message: str = ""
    error: str | None = None


class CreateSessionRequest(BaseModel):
    title: str = "New Session"


class PromptRequest(BaseModel):
    parts: list[dict]


class SessionInfo(BaseModel):
    id: str
    title: str = ""
