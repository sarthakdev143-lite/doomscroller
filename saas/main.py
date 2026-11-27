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


class _RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers must revalidate (ETag/Last-Modified make that a cheap 304).

    Page modules import siblings by bare relative path (`./api.js`, `../api.js`) without a `?v=`, so a
    browser caching JS freely can run a new page module against a stale import after a deploy.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


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
        app.mount(prefix, _RevalidatingStaticFiles(directory=str(directory)), name=prefix.strip("/"))

    from .admin.routes import router as admin_router
    from .auth.routes import router as auth_router
    from .billing.routes import router as billing_router
    from .jobs.routes import router as jobs_router
    from .tracking.routes import router as tracking_router
    from .waitlist.routes import router as waitlist_router
    from .web.editor_routes import router as editor_router
    from .web.routes import router as web_router

    for router in (auth_router, billing_router, jobs_router, tracking_router, waitlist_router, admin_router,
                   editor_router, web_router):
        app.include_router(router)

    from . import mcp as mcp_server
    from .web import errors as web_errors

    mcp_server.mount(app)
    web_errors.install(app)  # HTML 404/500 for pages; JSON paths keep JSON errors
    return app


app = create_app()
