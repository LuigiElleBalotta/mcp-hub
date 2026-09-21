from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_hub.config import ServerConfig, save_config
from mcp_hub.manager import HubManager


def management_routes(manager: HubManager) -> list[Route]:
    async def status(request: Request) -> JSONResponse:
        return JSONResponse({"servers": manager.status_snapshot()})

    async def start(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        await manager.get(name).start()
        return JSONResponse({"status": manager.get(name).status})

    async def stop(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        await manager.get(name).stop()
        return JSONResponse({"status": manager.get(name).status})

    async def logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        return JSONResponse({"lines": list(manager.get(name).logs)})

    async def upsert(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        body = await request.json()
        server_config = ServerConfig(**body)
        # HubManager.upsert stops any previously-running process for `name`
        # itself before replacing the entry (round-2 review fix), so the
        # freshly-returned `managed` here is a brand-new ManagedServer with
        # no old process to worry about -- just start it if enabled.
        managed = await manager.upsert(name, server_config)
        save_config(manager.config)
        if server_config.enabled:
            await managed.start()
        return JSONResponse({"status": managed.status})

    return [
        Route("/api/status", status, methods=["GET"]),
        Route("/api/servers/{name}/start", start, methods=["POST"]),
        Route("/api/servers/{name}/stop", stop, methods=["POST"]),
        Route("/api/servers/{name}/logs", logs, methods=["GET"]),
        Route("/api/servers/{name}", upsert, methods=["POST"]),
    ]
