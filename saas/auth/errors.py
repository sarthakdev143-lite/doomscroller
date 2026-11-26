"""The JSON error envelope every core route returns: {"error": "<code>", "message": "<sentence>"}.

FastAPI's own errors are {"detail": ...}. Core routers use `ApiRoute` as their route class, which
turns ApiError, HTTPException and request-validation failures into the envelope before the app ever
sees them, so the shape holds even for errors raised inside dependencies. `install(app)` also
registers a handler for ApiError raised from routers that do not use ApiRoute (e.g. a page that
depends on require_user).
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    402: "payment_required",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_error",
    503: "unavailable",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, headers: dict | None = None, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers
        self.extra = extra

    def response(self) -> JSONResponse:
        body = {"error": self.code, "message": self.message, **self.extra}
        return JSONResponse(body, status_code=self.status, headers=self.headers)


def not_found(what: str = "Not found.") -> ApiError:
    return ApiError(404, "not_found", what)


def _http_exception_response(exc: HTTPException) -> Response:
    headers = getattr(exc, "headers", None)
    if exc.status_code in (204, 304) or 300 <= exc.status_code < 400:
        return Response(status_code=exc.status_code, headers=headers)
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        body = {"message": detail.get("message") or str(detail["error"]).replace("_", " ").capitalize() + ".",
                **detail}
    else:
        body = {"error": _STATUS_CODES.get(exc.status_code, "error"),
                "message": str(detail) if detail else "Request failed."}
    return JSONResponse(body, status_code=exc.status_code, headers=headers)


def _validation_response(exc: RequestValidationError) -> JSONResponse:
    fields = []
    for err in exc.errors()[:20]:
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        fields.append({"field": ".".join(loc), "message": str(err.get("msg", "invalid"))[:200]})
    first = fields[0] if fields else {"field": "", "message": "invalid"}
    message = f"{first['field'] or 'request'}: {first['message']}"
    return JSONResponse({"error": "invalid_request", "message": message, "fields": fields}, status_code=422)


class ApiRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except ApiError as exc:
                return exc.response()
            except RequestValidationError as exc:
                return _validation_response(exc)
            except HTTPException as exc:
                return _http_exception_response(exc)

        return handler


async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return exc.response()


def install(app: FastAPI) -> None:
    """Register the ApiError handler app-wide (idempotent)."""
    if ApiError not in app.exception_handlers:
        app.add_exception_handler(ApiError, _api_error_handler)
