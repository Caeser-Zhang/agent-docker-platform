"""One-shot migration: copy rows from the legacy SQLite file to PostgreSQL.

Run INSIDE the backend container (it has both drivers and the model code):

    docker exec -i agent-docker-demo-backend-1 python - < scripts/migrate-sqlite-to-pg.py

Preconditions:
  - AGENT_DATABASE_URL points at the (empty) PostgreSQL database
  - the backend has started once (init_db created the tables)
  - the old SQLite file is still mounted at /app/data/agent_demo.db

Idempotent: if the target users table is not empty it does nothing.
"""

import asyncio
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import text

from app.database import engine

SQLITE_PATH = "/app/data/agent_demo.db"


def aware(value):
    """SQLite stores naive UTC strings; PG timestamptz needs aware datetimes."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def load_tables(path: str):
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    tables = {}
    for name in ("users", "agent_containers", "user_llm_selection"):
        try:
            tables[name] = [dict(r) for r in src.execute(f"SELECT * FROM {name}")]
        except sqlite3.OperationalError:
            tables[name] = []  # table absent in this (older) SQLite file
    src.close()
    return tables


async def main():
    data = load_tables(SQLITE_PATH)
    print("source rows:", {k: len(v) for k, v in data.items()})

    async with engine.begin() as conn:
        existing = (await conn.execute(text("SELECT count(*) FROM users"))).scalar()
        if existing:
            print(f"target already has {existing} users — nothing to do")
            return

        for u in data["users"]:
            u["created_at"] = aware(u.get("created_at"))
            await conn.execute(
                text(
                    "INSERT INTO users (id, username, hashed_password, role, created_at) "
                    "VALUES (:id, :username, :hashed_password, :role, :created_at)"
                ),
                u,
            )

        for c in data["agent_containers"]:
            cols = [
                "id", "user_id", "container_name", "status", "password_enc", "image",
                "workspace_volume", "data_volume", "started_at", "last_activity",
                "restart_count", "last_error", "created_at", "updated_at",
            ]
            params = {k: c.get(k) for k in cols}
            for k in ("started_at", "last_activity", "created_at", "updated_at"):
                params[k] = aware(params[k])
            await conn.execute(
                text(
                    f"INSERT INTO agent_containers ({', '.join(cols)}) "
                    f"VALUES ({', '.join(':' + k for k in cols)})"
                ),
                params,
            )

        for s in data["user_llm_selection"]:
            await conn.execute(
                text(
                    "INSERT INTO user_llm_selection "
                    "(user_id, provider_id, model, base_url, api_key) "
                    "VALUES (:user_id, :provider_id, :model, :base_url, :api_key)"
                ),
                s,
            )

    print(
        f"migrated: {len(data['users'])} users, "
        f"{len(data['agent_containers'])} containers, "
        f"{len(data['user_llm_selection'])} llm selections"
    )


asyncio.run(main())
