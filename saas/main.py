"""SaaS app factory. Integrator-owned: agents add routers inside their own packages, not here.

Run: .venv-saas/Scripts/python.exe -m uvicorn saas.main:app --port 8700
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .templating import STATIC_DIRS

log = logging.getLogger("saas")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.brand_name} API",
        lifespan=lifespan,
        # Interactive docs leak the whole surface to scanners; only the public v1 API is documented,
        # at /developers (saas.web). Turn these back on locally with SAAS_ENV=dev.
        docs_url="/_docs" if not settings.is_prod else None,
        redoc_url=None,
        openapi_url="/_openapi.json" if not settings.is_prod else None,
    )

    # Order matters: the security layer wraps everything, so bans and limits run before any route.
    from .security import middleware as security_middleware

    security_middleware.install(app)

    for prefix, directory in STATIC_DIRS.items():
        directory.mkdir(parents=True, exist_ok=True)
        app.mount(prefix, StaticFiles(directory=str(directory)), name=prefix.strip("/"))

    from .admin.routes import router as admin_router
    from .auth.routes import router as auth_router
    from .billing.routes import router as billing_router
    from .jobs.routes import router as jobs_router
    from .tracking.routes import router as tracking_router
    from .waitlist.routes import router as waitlist_router
    from .web.routes import router as web_router

    for router in (auth_router, billing_router, jobs_router, tracking_router, waitlist_router, admin_router,
                   web_router):
        app.include_router(router)

    from . import mcp as mcp_server

    mcp_server.mount(app)
    return app


app = create_app()
