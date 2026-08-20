"""FastAPI application entry point — the platform control layer.

Wires together all routers and services. On startup:
  1. Initialize the database
  2. Ensure the agent-net Docker network exists
  3. Start the idle reclaim background task
  4. Recover any running containers from a previous platform instance

Architecture (design section 2.1):
  Browser → [this FastAPI app] → Docker containers → Shared services

  Control plane: Agent Controller + Container Manager (lifecycle)
  Data plane:    Tunnel Relay + SSE Pump (request/event forwarding)
"""
import asyncio
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import init_db
from .routers import auth, agent, tunnel, config, workspace
from .services.agent_controller import agent_controller
from .services.container_manager import container_manager
from .services.sse_pump import sse_pump_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    # --- Startup ---
    logger.info("=== Agent Docker Platform starting ===")

    # 1. Initialize database
    await init_db()
    logger.info("Database initialized")

    # 2. Ensure Docker network exists
    try:
        container_manager.ensure_network()
        logger.info("Docker network ready: %s", settings.agent_network)
    except Exception as e:
        logger.warning("Docker network setup failed (Docker may not be available): %s", e)

    # 3. Recover running containers from DB
    try:
        await agent_controller.recover()
    except Exception as e:
        logger.warning("Container recovery failed: %s", e)

    # 4. Start idle reclaim background task
    reclaim_task = asyncio.create_task(agent_controller.idle_reclaim_loop())

    logger.info("=== Platform ready on %s:%d ===", settings.host, settings.port)

    yield

    # --- Shutdown ---
    logger.info("=== Shutting down ===")
    reclaim_task.cancel()
    try:
        await reclaim_task
    except asyncio.CancelledError:
        pass

    await sse_pump_manager.stop_all()
    logger.info("=== Shutdown complete ===")


app = FastAPI(
    title="Agent Docker Platform",
    description="Per-user agent container management — browser to container execution",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow the frontend to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth.router)
app.include_router(agent.router)
app.include_router(tunnel.router)
app.include_router(config.router)
app.include_router(workspace.router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Safely handle validation errors without encoding binary request bodies."""
    errors = exc.errors()
    # Strip body context from errors to avoid UnicodeDecodeError on binary data
    safe_errors = [
        {k: v for k, v in e.items() if k != "ctx"}
        for e in errors
    ]
    return JSONResponse(status_code=422, content={"detail": safe_errors})


@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "service": "Agent Docker Platform",
        "version": "1.0.0",
        "status": "running",
    }


@app.get("/api/health")
async def health():
    """Platform health check."""
    return {"status": "ok"}
