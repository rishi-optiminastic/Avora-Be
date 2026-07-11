"""FastAPI application factory.

Wires settings, logging, middleware, exception handlers, and the v1 router.
The factory is the only place global app concerns are assembled.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.deps import configure_rate_limiter
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.migrate import upgrade_to_head
from app.mcp.server import mcp, mcp_app

logger = get_logger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    if settings.should_auto_migrate:
        # Bring the DB to head BEFORE serving so a model edit + `--reload` never
        # races ahead of its migration. Run off-thread (Alembic spins its own
        # loop). Non-fatal: a transient DB hiccup at boot shouldn't crash dev.
        try:
            await asyncio.to_thread(upgrade_to_head)
            logger.info("migrations_at_head")
        except Exception:
            logger.exception("auto_migrate_failed")
    logger.info("startup", extra={"environment": settings.environment})
    # The mounted MCP endpoint (/mcp) needs its Streamable-HTTP session manager
    # running for the life of the app.
    async with mcp.session_manager.run():
        yield
    logger.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.assert_production_safe()

    configure_logging()
    configure_rate_limiter(settings)

    app = FastAPI(
        title="PMS Backend",
        version="0.1.0",
        # Hide schema docs in production — they map the attack surface.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Middleware (outermost first).
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,  # cookies must cross origins to the dashboard
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Signature", "X-HR-Signature"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")

    # Claude Code / MCP integration. The Avora MCP server is a Streamable-HTTP
    # ASGI app mounted here; it authenticates a Personal Access Token and reuses
    # the same task/employee services the REST API uses.
    app.mount("/mcp", mcp_app)

    return app


app = create_app()
