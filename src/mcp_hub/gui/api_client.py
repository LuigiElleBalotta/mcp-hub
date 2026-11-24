from __future__ import annotations

from typing import Callable

import httpx


_SLOW_TIMEOUT = 60.0

OLD_HUB_MESSAGE = (
    "L'hub in esecuzione e' una versione precedente e non gestisce Rizzo Flow. "
    "Riavvialo con la versione aggiornata (pulsante \"Riavvia hub\", oppure \"Esci\" dal tray e riapri mcp-hub-gui)."
)


class HubTooOldError(RuntimeError):
    """The hub answers, but without the route this GUI needs (older build)."""


def _rizzo_json(resp: httpx.Response) -> dict:
    # An older hub has no /api/rizzo: it answers 404 with a plain-text "Not Found",
    # which used to surface as the cryptic "Expecting value: line 1 column 1 (char 0)".
    if resp.status_code == 404 and "json" not in resp.headers.get("content-type", ""):
        raise HubTooOldError(OLD_HUB_MESSAGE)
    try:
        return resp.json()
    except ValueError as exc:
        raise HubTooOldError(OLD_HUB_MESSAGE) from exc


def _read_hub_config():
    from mcp_hub.config import HubConfig, load_config

    try:
        return load_config().hub
    except (OSError, ValueError, TypeError):
        return HubConfig()


class _BearerAuth(httpx.Auth):
    """Adds `Authorization: Bearer <token>` when a token is set. On a 401 it
    asks `reload` for a fresh token (the hub was restarted with another one)
    and retries the request once."""

    def __init__(self, token: str | None, reload: Callable[[], str | None] | None = None):
        self.token = token or None
        self._reload = reload

    def auth_flow(self, request: httpx.Request):
        if self.token:
            request.headers["Authorization"] = f"Bearer {self.token}"
        response = yield request
        if response.status_code == 401 and self._reload is not None:
            fresh = self._reload() or None
            if fresh != self.token:
                self.token = fresh
                if fresh:
                    request.headers["Authorization"] = f"Bearer {fresh}"
                else:
                    request.headers.pop("Authorization", None)
                yield request


class HubApiClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:37450",
        token: str | None = None,
        token_reloader: Callable[[], str | None] | None = None,
    ):
        self._auth = _BearerAuth(token, token_reloader)
        self._client = httpx.Client(base_url=base_url, timeout=5.0, auth=self._auth)

    @classmethod
    def from_config(cls) -> "HubApiClient":
        """Client for the hub described by config.json: its host, port and
        (when set) bearer token. Falls back to the defaults if the file is
        unreadable, so the GUI can still open and show the problem. The token
        is re-read from config.json when the hub answers 401, so a token
        changed in Settings is picked up after the hub restarts."""
        hub = _read_hub_config()
        host = "127.0.0.1" if hub.host in ("0.0.0.0", "::") else hub.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return cls(
            base_url=f"http://{host}:{hub.port}",
            token=hub.authToken,
            token_reloader=lambda: _read_hub_config().authToken,
        )

    def set_token(self, token: str | None) -> None:
        """Sets (or clears) the bearer token sent with every request."""
        self._auth.token = token or None

    def status(self) -> dict[str, dict[str, str]]:
        return self._client.get("/api/status").json()["servers"]

    def start(self, name: str) -> str:
        return self._client.post(f"/api/servers/{name}/start", timeout=_SLOW_TIMEOUT).json()["status"]

    def stop(self, name: str) -> str:
        # A service stop kills the process tree and then waits (up to ~10s)
        # for its port to be released, so the default 5s is not enough.
        return self._client.post(f"/api/servers/{name}/stop", timeout=_SLOW_TIMEOUT).json()["status"]

    def logs(self, name: str) -> list[str]:
        return self._client.get(f"/api/servers/{name}/logs").json()["lines"]

    def upsert(self, name: str, config: dict) -> str:
        return self._client.post(f"/api/servers/{name}", json=config).json()["status"]

    def remove(self, name: str) -> None:
        self._client.delete(f"/api/servers/{name}")

    def rizzo(self) -> dict:
        return _rizzo_json(self._client.get("/api/rizzo"))

    def set_rizzo(self, settings: dict) -> dict:
        resp = self._client.put("/api/rizzo", json=settings)
        if resp.status_code != 200:
            body = _rizzo_json(resp)
            raise RuntimeError(body.get("error", f"HTTP {resp.status_code}"))
        return _rizzo_json(resp)

    def reload(self) -> dict:
        return self._client.post("/api/reload").json()

    def settings(self) -> dict:
        return self._client.get("/api/settings").json()

    def update_settings(self, **fields) -> dict:
        return self._client.put("/api/settings", json=fields).json()

    def metrics(self, name: str) -> dict | None:
        """Dashboard detail of one server, or None when the hub cannot give it
        (an older hub has no such route and answers a plain-text 404)."""
        resp = self._client.get(f"/api/servers/{name}/metrics")
        if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
            return None
        return resp.json()

    def pid(self) -> int:
        return self._client.get("/api/pid").json()["pid"]

    def shutdown(self) -> None:
        try:
            self._client.post("/api/shutdown")
        except httpx.RemoteProtocolError:
            pass  # hub closed the connection as it shut down -- expected
