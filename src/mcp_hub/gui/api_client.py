from __future__ import annotations

import httpx


class HubApiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:37450"):
        self._client = httpx.Client(base_url=base_url, timeout=5.0)

    def status(self) -> dict[str, str]:
        return self._client.get("/api/status").json()["servers"]

    def start(self, name: str) -> str:
        return self._client.post(f"/api/servers/{name}/start").json()["status"]

    def stop(self, name: str) -> str:
        return self._client.post(f"/api/servers/{name}/stop").json()["status"]

    def logs(self, name: str) -> list[str]:
        return self._client.get(f"/api/servers/{name}/logs").json()["lines"]

    def upsert(self, name: str, config: dict) -> str:
        return self._client.post(f"/api/servers/{name}", json=config).json()["status"]

    def remove(self, name: str) -> None:
        self._client.delete(f"/api/servers/{name}")

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
