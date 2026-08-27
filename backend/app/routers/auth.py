"""Auth routes — register and login."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import hash_password, verify_password, create_access_token
from ..config import settings
from ..database import async_session, _next_uid
from ..models import User
from ..schemas import RegisterRequest, LoginRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest):
    """Register a new user.

    Role assignment: the very first user bootstraps the platform as admin,
    and any username listed in AGENT_ADMIN_USERNAMES is always admin.
    """
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
async def login(req: LoginRequest):
    """Login and receive a JWT token."""
    async with async_session() as db:
        result = await db.execute(select(User).where(User.username == req.username))
        user = result.scalar_one_or_none()
        if not user or not verify_password(req.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Promote on login too, so AGENT_ADMIN_USERNAMES changes take effect
        # without a backend restart.
        if user.role != "admin" and req.username in settings.admin_username_set:
            user.role = "admin"
            await db.commit()
            await db.refresh(user)

    token = create_access_token(user.id, user.username, user.role)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        role=user.role,
    )
