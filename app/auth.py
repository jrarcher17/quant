"""Authentication helpers for protecting dashboard and API routes."""

from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from loguru import logger

from app.config import get_settings


PUBLIC_PREFIXES = (
    "/health",
    "/login",
    "/api/auth",
    "/favicon.ico",
)

PROTECTED_PREFIXES = (
    "/",
    "/dashboard",
    "/chart",
    "/candles",
    "/status",
    "/debug",
)


def is_public_path(path: str) -> bool:
    """Return whether a request path can be reached without a session."""
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in PUBLIC_PREFIXES)


def is_protected_path(path: str) -> bool:
    """Return whether a request path requires a Better Auth session."""
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in PROTECTED_PREFIXES)


def wants_html(request: Request) -> bool:
    """Infer whether an unauthenticated request should redirect or return JSON."""
    accept = request.headers.get("accept", "")
    return "text/html" in accept or request.url.path in {"/", "/dashboard", "/dashboard/"}


async def validate_session(request: Request) -> bool:
    """Validate the incoming Better Auth cookie against the local auth service."""
    cookie = request.headers.get("cookie")
    if not cookie:
        return False

    settings = get_settings()
    headers = {
        "cookie": cookie,
        "host": request.headers.get("host", ""),
        "x-forwarded-proto": request.url.scheme,
        "x-forwarded-host": request.headers.get("host", ""),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.auth_service_url.rstrip('/')}/api/auth-session",
                headers={key: value for key, value in headers.items() if value},
            )
    except httpx.HTTPError as exc:
        logger.warning("Auth session validation failed: {}", exc)
        return False

    return response.status_code == 200


async def auth_middleware(request: Request, call_next) -> Response:
    """Require Better Auth sessions for browser and data routes."""
    path = request.url.path
    if is_public_path(path) or not is_protected_path(path):
        return await call_next(request)

    if await validate_session(request):
        return await call_next(request)

    if wants_html(request):
        next_url = quote(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)

    return JSONResponse({"detail": "Authentication required"}, status_code=401)
