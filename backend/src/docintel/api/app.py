"""FastAPI application factory.

Run with:  uvicorn docintel.api.app:create_app --factory
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from docintel import __version__
from docintel.api.middleware import (
    BodySizeLimitMiddleware,
    HTTPMetricsMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from docintel.api.problems import register_exception_handlers
from docintel.api.routers import (
    admin,
    analysis,
    auth,
    comparisons,
    dashboard,
    documents,
    evaluations,
    health,
    knowledge,
    metrics,
    reports,
    reviews,
    rules,
    search,
    vendors,
    workflows,
)
from docintel.core.config import Settings, get_settings
from docintel.core.logging import configure_logging, get_logger
from docintel.db.session import create_engine, create_sessionmaker
from docintel.knowledge.rag import build_rag_engines
from docintel.storage import build_storage

API_V1_PREFIX = "/api/v1"
# Multipart framing (boundaries, part headers, form fields) on top of the file itself.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, log_format=settings.effective_log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.storage = build_storage(settings)
        app.state.rag = build_rag_engines(settings, app.state.sessionmaker)
        logger.info("app.started", env=settings.app_env.value, version=__version__)
        try:
            yield
        finally:
            await app.state.rag.aclose()
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

    v1_routers = [
        auth.router,
        dashboard.router,
        documents.router,
        vendors.router,
        comparisons.router,
        rules.router,
        reviews.router,
        knowledge.router,
        search.router,
        analysis.router,
        workflows.router,
        reports.router,
        admin.router,
        evaluations.router,
    ]
    root_routers = [health.router, *([metrics.router] if settings.metrics_enabled else [])]
    api_v1 = APIRouter(prefix=API_V1_PREFIX)
    for router in v1_routers:
        api_v1.include_router(router)
    for router in root_routers:
        app.include_router(router)
    app.include_router(api_v1)
    # Full route templates for the metrics labels (nested routers keep relative paths).
    templates = {
        id(route): API_V1_PREFIX + getattr(route, "path", "")
        for r in v1_routers
        for route in r.routes
    }
    templates |= {id(route): getattr(route, "path", "") for r in root_routers for route in r.routes}

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
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=settings.api_max_body_bytes,
        overrides={
            ("POST", f"{API_V1_PREFIX}/documents"): settings.upload_max_bytes
            + MULTIPART_OVERHEAD_BYTES
        },
        pattern_overrides=[
            (
                "POST",
                re.compile(rf"{API_V1_PREFIX}/documents/[0-9a-fA-F-]{{36}}/versions"),
                settings.upload_max_bytes + MULTIPART_OVERHEAD_BYTES,
            )
        ],
    )
    app.add_middleware(RequestContextMiddleware)
    if settings.metrics_enabled:
        app.add_middleware(HTTPMetricsMiddleware, templates=templates)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.hsts_enabled)
    return app
