"""Pydantic request/response schemas."""
from pydantic import BaseModel


class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str


class StartAgentRequest(BaseModel):
    workspace: str | None = None


class AgentStatusResponse(BaseModel):
    running: bool
    healthy: bool = False
    status: str = "absent"
    container_name: str | None = None
    workspace: str | None = None
    message: str = ""


class CreateSessionRequest(BaseModel):
    title: str = "New Session"


class PromptRequest(BaseModel):
    parts: list[dict]


class SessionInfo(BaseModel):
    id: str
    title: str = ""
