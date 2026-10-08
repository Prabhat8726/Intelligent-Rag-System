"""FastAPI application factory.

Run with:  uvicorn docintel.api.app:create_app --factory
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from docintel import __version__
from docintel.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from docintel.api.problems import register_exception_handlers
from docintel.api.routers import auth, health
from docintel.core.config import Settings, get_settings
from docintel.core.logging import configure_logging, get_logger
from docintel.db.session import create_engine, create_sessionmaker

API_V1_PREFIX = "/api/v1"

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, log_format=settings.effective_log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        logger.info("app.started", env=settings.app_env.value, version=__version__)
        try:
            yield
        finally:
            await engine.dispose()
            logger.info("app.stopped")

    docs_enabled = settings.effective_api_docs_enabled
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Multimodal document understanding, verification, RAG, agentic reasoning and "
            "human-in-the-loop workflow automation."
        ),
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = settings

    register_exception_handlers(app)

    api_v1 = APIRouter(prefix=API_V1_PREFIX)
    api_v1.include_router(auth.router)
    app.include_router(health.router)
    app.include_router(api_v1)

    # add_middleware wraps: the last added is outermost.
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.hsts_enabled)
    return app
