"""Auth routes — register and login.

P1-5: both endpoints are rate-limited per client IP (login additionally per
username) via an in-memory sliding window, and the login path performs a
dummy bcrypt verification when the username is unknown so response timing
does not reveal whether an account exists.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import hash_password, verify_password, create_access_token
from ..config import settings
from ..database import async_session, _next_uid
from ..models import User
from ..schemas import RegisterRequest, LoginRequest, TokenResponse
from ..services.rate_limit import login_limiter, register_limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])

# login: 5 failures per (IP, username) per 15 min; register: 10 per IP per hour.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW = 15 * 60
REGISTER_MAX = 10
REGISTER_WINDOW = 60 * 60

# Constant bcrypt hash used to equalise timing when the username is unknown.
_DUMMY_HASH = hash_password("timing-equaliser")


def _client_ip(request: Request) -> str:
    # Behind the nginx reverse proxy X-Forwarded-For carries the real client;
    # fall back to the socket peer (container IP) when absent.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest, request: Request):
    """Register a new user.

    Role assignment: the very first user bootstraps the platform as admin,
    and any username listed in AGENT_ADMIN_USERNAMES is always admin.
    """
    ip = _client_ip(request)
    retry_after = register_limiter.hit(f"register:{ip}", REGISTER_MAX, REGISTER_WINDOW)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many registrations from this address, retry in {int(retry_after)}s",
        )

    async with async_session() as db:
        existing = await db.execute(select(User).where(User.username == req.username))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Username already taken")

        user_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        role = "admin" if (user_count == 0 or req.username in settings.admin_username_set) else "user"

        # 工号: optional at registration, validated for uniqueness and
        # auto-assigned sequentially (10001+) when omitted.
        uid = (req.uid or "").strip() or None
        if uid:
            clash = await db.execute(select(User).where(User.uid == uid))
            if clash.scalar_one_or_none():
                raise HTTPException(status_code=400, detail="Employee number (uid) already taken")
        else:
            taken = list((await db.execute(select(User.uid))).scalars())
            uid = _next_uid(taken)

        user = User(
            username=req.username,
            uid=uid,
            hashed_password=hash_password(req.password),
            role=role,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    token = create_access_token(user.id, user.username, user.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        role=user.role,
    )


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, request: Request):
    """Login and receive a JWT token."""
    ip = _client_ip(request)
    limiter_key = f"login:{ip}:{req.username}"
    retry_after = login_limiter.hit(limiter_key, LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts, retry in {int(retry_after)}s",
        )

    async with async_session() as db:
        result = await db.execute(select(User).where(User.username == req.username))
        user = result.scalar_one_or_none()
        # Constant-time check regardless of account existence.
        if not user or not verify_password(req.password, user.hashed_password):
            verify_password(req.password, _DUMMY_HASH)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Promote on login too, so AGENT_ADMIN_USERNAMES changes take effect
        # without a backend restart.
        if user.role != "admin" and req.username in settings.admin_username_set:
            user.role = "admin"
            await db.commit()
            await db.refresh(user)

    login_limiter.reset(limiter_key)
    token = create_access_token(user.id, user.username, user.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        role=user.role,
    )
