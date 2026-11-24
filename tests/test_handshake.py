"""hub-proxy-hardening R4: the hub performs the MCP handshake itself, once
per upstream spawn, and answers clients' `initialize` from the cache."""
import asyncio
import sys
from pathlib import Path

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub.config import ServerConfig
from mcp_hub.hub_app import _proxy
from mcp_hub.manager import ManagedServer

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


async def _start(env: dict | None = None, concurrency: str = "parallel") -> ManagedServer:
    cfg = ServerConfig(enabled=True, command=sys.executable, args=[FAKE_SERVER], env=env or {}, concurrency=concurrency, restart="never")
    server = ManagedServer("fake", cfg)
    await server.start()
    return server


def _msg(obj: dict) -> SessionMessage:
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class Conn:
    def __init__(self, managed: ManagedServer):
        self._rs, self._rr = anyio.create_memory_object_stream(32)
        self._ws, self._wr = anyio.create_memory_object_stream(32)
        self.task = asyncio.create_task(_proxy(self._rr, self._ws, managed))

    async def request(self, req_id, method: str, params: dict | None = None) -> None:
        await self._rs.send(_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}))

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._rs.send(_msg({"jsonrpc": "2.0", "method": method, "params": params or {}}))

    async def recv(self, timeout: float = 5.0) -> dict:
        with anyio.fail_after(timeout):
            m = await self._wr.receive()
        return m.message.model_dump(by_alias=True, exclude_unset=True)

    async def stats(self, req_id: int = 900) -> dict:
        await self.request(req_id, "test/handshake_stats")
        return (await self.recv())["result"]

    async def close(self) -> None:
        await self._rs.aclose()
        with anyio.fail_after(5.0):
            await self.task


CLIENT_INIT = {"protocolVersion": "2025-06-18", "capabilities": {"roots": {}},
               "clientInfo": {"name": "claude-code", "version": "1"}}


@pytest.mark.asyncio
async def test_client_initialize_is_answered_from_the_hub_handshake():
    server = await _start()
    try:
        a = Conn(server)
        await a.request(1, "initialize", CLIENT_INIT)
        reply = await a.recv()
        assert reply["id"] == 1
        assert reply["result"]["serverInfo"] == {"name": "fake", "version": "9.9"}
        assert reply["result"]["capabilities"] == {"tools": {}}
        # The upstream got ONE initialize, asked by the hub with the hub's own
        # version (not the client's), plus the hub's own `initialized`.
        stats = await a.stats()
        assert stats["initializes"] == 1 and stats["initializeds"] == 1
        assert stats["protocolVersion"] != "2025-06-18"
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_many_clients_share_one_handshake_and_their_initialized_is_swallowed():
    server = await _start()
    try:
        a, b, c = Conn(server), Conn(server), Conn(server)
        await asyncio.gather(*(x.request(1, "initialize", CLIENT_INIT) for x in (a, b, c)))  # concurrent
        replies = [await x.recv() for x in (a, b, c)]
        assert all(r["result"]["serverInfo"]["name"] == "fake" and r["id"] == 1 for r in replies)
        for x in (a, b, c):
            await x.notify("notifications/initialized")
        stats = await a.stats()
        assert stats["initializes"] == 1, "one handshake per spawn, not one per client"
        assert stats["initializeds"] == 1, "a client's notifications/initialized must not reach the upstream"
        for x in (a, b, c):
            await x.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_respawned_upstream_is_handshaken_again_for_the_same_connection():
    server = await _start()
    try:
        a = Conn(server)
        await a.request(1, "initialize", CLIENT_INIT)
        await a.recv()
        assert (await a.stats())["initializes"] == 1

        await server.stop()
        await server.start()  # a brand-new subprocess that has never been initialised
        await a.request(2, "test/echo", {"value": "after restart"})
        assert (await a.recv())["result"] == {"echo": "after restart"}
        stats = await a.stats(901)
        assert stats["initializes"] == 1 and stats["initializeds"] == 1, "the NEW process was initialised by the hub"
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_refused_handshake_answers_the_client_with_an_error_and_is_retried():
    server = await _start(env={"FAKE_FAIL_INIT": "1"})
    try:
        a = Conn(server)
        await a.request(1, "initialize", CLIENT_INIT)
        reply = await a.recv()
        assert reply["id"] == 1 and reply["error"]["code"] == -32000
        assert "initialize" in reply["error"]["message"]
        await a.request(2, "initialize", CLIENT_INIT)  # not stuck: tried again, still refused
        assert (await a.recv())["error"]["code"] == -32000
        await a.close()
    finally:
        await server.stop()
