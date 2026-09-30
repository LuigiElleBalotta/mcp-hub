from __future__ import annotations

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


class HubApiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:37450"):
        self._client = httpx.Client(base_url=base_url, timeout=5.0)

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

    def pid(self) -> int:
        return self._client.get("/api/pid").json()["pid"]

    def shutdown(self) -> None:
        try:
            self._client.post("/api/shutdown")
        except httpx.RemoteProtocolError:
            pass  # hub closed the connection as it shut down -- expected
