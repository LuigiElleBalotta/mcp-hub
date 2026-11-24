"""Request guard for the hub's HTTP surface (hub-proxy-hardening R1).

The hub listens on localhost and can start arbitrary commands through its
management API, so it must not be drivable by a web page (CSRF / DNS
rebinding) or, when a token is configured, by anyone without it.

Pure ASGI middleware rather than `BaseHTTPMiddleware` so the long-lived SSE
streams are passed through untouched, not buffered.

Order of checks: Host -> Origin -> bearer token -> (management routes only)
Content-Type of a request that carries a body.
"""
from __future__ import annotations

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_hub.config import HubConfig

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_WILDCARD_BINDS = ("0.0.0.0", "::")
_BODY_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _host_forms(hub: HubConfig) -> set[str]:
    """Host header values that name this hub (with and without the port)."""
    names = {*_LOCAL_HOSTS, hub.host.lower()}
    for extra in hub.allowedHosts:
        names.add(extra.lower())
    forms: set[str] = set()
    for name in names:
        forms.add(name)
        if ":" not in name.removeprefix("[").removesuffix("]") or name.endswith("]"):
            forms.add(f"{name}:{hub.port}")
    return forms


def allowed_origins(hub: HubConfig) -> set[str]:
    origins = {f"http://{form}" for form in _host_forms(hub)}
    origins.update(o.lower().rstrip("/") for o in hub.allowedOrigins)
    return origins


def _host_check_skipped(hub: HubConfig) -> bool:
    # A hub bound to every interface is reached by arbitrary names/addresses;
    # the mandatory token (see cmd_serve) is its protection. "*" opts out too.
    return hub.host in _WILDCARD_BINDS or "*" in hub.allowedHosts


def _media_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


class HubSecurityMiddleware:
    def __init__(self, app: ASGIApp, hub: HubConfig) -> None:
        self.app = app
        # The live HubConfig object, read per request (cheap): a settings
        # change is picked up without rebuilding the middleware stack.
        self.hub = hub

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rejection = self._check(scope)
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _check(self, scope: Scope) -> JSONResponse | None:
        hub = self.hub
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}

        host = headers.get("host")
        if host is not None and not _host_check_skipped(hub) and host.lower() not in _host_forms(hub):
            return JSONResponse({"error": "host not allowed"}, status_code=403)

        origin = headers.get("origin")
        if origin is not None and origin.lower().rstrip("/") not in allowed_origins(hub):
            return JSONResponse({"error": "origin not allowed"}, status_code=403)

        if hub.authToken:
            scheme, _, supplied = headers.get("authorization", "").partition(" ")
            valid = scheme.lower() == "bearer" and hmac.compare_digest(
                supplied.strip().encode("utf-8"), hub.authToken.encode("utf-8")
            )
            if not valid:
                return JSONResponse(
                    {"error": "missing or invalid bearer token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )

        if scope["path"].startswith("/api/") and scope["method"] in _BODY_METHODS:
            has_body = headers.get("content-length", "0") not in ("", "0") or "transfer-encoding" in headers
            if has_body and _media_type(headers.get("content-type")) != "application/json":
                return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        return None
