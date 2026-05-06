"""Login page and Better Auth proxy routes."""

from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import validate_session
from app.config import get_settings

router = APIRouter(tags=["auth"])

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    """Render the dark login page."""
    next_url = request.query_params.get("next") or "/dashboard/"
    if await validate_session(request):
        return RedirectResponse(url=next_url, status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"next_url": next_url},
    )


@router.post("/logout")
async def logout(request: Request) -> Response:
    """Sign out through Better Auth and return to the login page."""
    response = await proxy_auth("sign-out", request)
    redirect = RedirectResponse(url="/login", status_code=303)
    for key, value in response.raw_headers:
        if key.lower() == b"set-cookie":
            redirect.raw_headers.append((key, value))
    return redirect


@router.api_route(
    "/api/auth/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_auth(path: str, request: Request) -> Response:
    """Proxy Better Auth requests while preserving cookies."""
    settings = get_settings()
    base_url = settings.auth_service_url.rstrip("/")
    query = f"?{request.url.query}" if request.url.query else ""
    upstream_url = f"{base_url}/api/auth/{quote(path)}{query}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    if request.headers.get("host"):
        headers["host"] = request.headers["host"]
        headers["x-forwarded-host"] = request.headers["host"]
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and "origin" not in headers:
            headers["origin"] = f"{request.url.scheme}://{request.headers['host']}"
    headers["x-forwarded-proto"] = request.url.scheme

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        upstream = await client.request(
            request.method,
            upstream_url,
            headers=headers,
            content=await request.body(),
        )

    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
    for key, value in upstream.headers.multi_items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS or lower_key == "content-type":
            continue
        if lower_key == "set-cookie":
            response.raw_headers.append((key.encode("latin-1"), value.encode("latin-1")))
        else:
            response.headers[key] = value

    return response
