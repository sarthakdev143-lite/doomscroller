"""HTML 404 and 500 pages for browsers; machine paths keep their JSON.

The integrator calls install(app) from saas.main.create_app. Only status 404 and 500 are handled, so
every other agent's 401/403/409/429 responses pass through untouched, and on API-style paths even a
404 is answered by FastAPI's own handler, exactly as before.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..templating import templates

log = logging.getLogger("saas.web")

_MACHINE_PREFIXES = ("/api", "/auth/", "/admin", "/webhooks", "/mcp", "/files/", "/static/", "/ts/",
                     "/admin-static/", "/_")


def _wants_html(request: Request) -> bool:
    path = request.url.path
    if path.startswith(_MACHINE_PREFIXES):
        return False
    return "text/html" in request.headers.get("accept", "text/html")


def _page(request: Request, template: str, status_code: int):
    from .routes import _base_context  # late: routes imports the db layer

    return templates.TemplateResponse(request, template, _base_context(request), status_code=status_code)


async def not_found(request: Request, exc: StarletteHTTPException):
    if not _wants_html(request):
        return await http_exception_handler(request, exc)
    return _page(request, "errors/404.html", 404)


async def server_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    if not _wants_html(request):
        return JSONResponse({"error": "server_error", "message": "Something went wrong on our side."},
                            status_code=500)
    try:
        return _page(request, "errors/500.html", 500)
    except Exception:  # the page itself failed to render: fall back to text, never a second traceback
        return JSONResponse({"error": "server_error", "message": "Something went wrong on our side."},
                            status_code=500)


def install(app: FastAPI) -> None:
    app.add_exception_handler(404, not_found)
    app.add_exception_handler(500, server_error)
