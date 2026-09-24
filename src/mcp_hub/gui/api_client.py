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

    def settings(self) -> dict:
        return self._client.get("/api/settings").json()
