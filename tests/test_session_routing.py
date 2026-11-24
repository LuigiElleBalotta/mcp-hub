"""hub-proxy-hardening R3: session routing on a shared upstream.

Two (or more) client sessions share one subprocess. Progress notifications
must reach only the session that asked for them, a cancellation must be
translated to the hub's own request id, and a server-initiated request must
not be broadcast to every session.
"""
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


async def _start(concurrency: str = "parallel") -> ManagedServer:
    cfg = ServerConfig(enabled=True, command=sys.executable, args=[FAKE_SERVER], env={}, concurrency=concurrency, restart="never")
    server = ManagedServer("fake", cfg)
    await server.start()
    return server


def _msg(obj: dict) -> SessionMessage:
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class Conn:
    """One simulated client session (same shape as test_hub_app_proxy._Connection)."""

    def __init__(self, managed: ManagedServer):
        self._rs, self._rr = anyio.create_memory_object_stream(32)
        self._ws, self._wr = anyio.create_memory_object_stream(32)
        self.task = asyncio.create_task(_proxy(self._rr, self._ws, managed))

    async def request(self, req_id, method: str, params: dict | None = None) -> None:
        await self._rs.send(_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}))

    async def notify(self, method: str, params: dict) -> None:
        await self._rs.send(_msg({"jsonrpc": "2.0", "method": method, "params": params}))

    async def respond(self, req_id, result: dict) -> None:
        await self._rs.send(_msg({"jsonrpc": "2.0", "id": req_id, "result": result}))

    async def recv(self, timeout: float = 5.0) -> dict:
        with anyio.fail_after(timeout):
            m = await self._wr.receive()
        return m.message.model_dump(by_alias=True, exclude_unset=True)

    async def drain(self, window: float = 0.4) -> list[dict]:
        """Everything that arrives within `window` seconds."""
        out: list[dict] = []
        while True:
            try:
                out.append(await self.recv(timeout=window))
            except TimeoutError:
                return out

    async def close(self) -> None:
        await self._rs.aclose()
        with anyio.fail_after(5.0):
            await self.task


@pytest.mark.asyncio
async def test_progress_token_is_remapped_and_routed_only_to_its_owner():
    server = await _start()
    try:
        a, b = Conn(server), Conn(server)
        # Both clients independently pick the SAME token, as real clients do.
        await a.request(1, "test/progress", {"seconds": 0.3, "_meta": {"progressToken": "tok"}})
        await b.request(1, "test/progress", {"seconds": 0.1, "_meta": {"progressToken": "tok"}})

        got_a = await a.drain(window=1.0)
        got_b = await b.drain(window=1.0)
        for got in (got_a, got_b):
            progress = [m for m in got if m.get("method") == "notifications/progress"]
            results = [m for m in got if "result" in m]
            assert len(progress) == 1, f"each session must see exactly its own progress, got {got}"
            assert progress[0]["params"]["progressToken"] == "tok"  # original token restored
            assert len(results) == 1
            # The upstream saw a hub token, never the client's own.
            assert results[0]["result"]["sawToken"] != "tok"
            assert str(results[0]["result"]["sawToken"]).startswith("hubp:")
        await a.close()
        await b.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_progress_token_zero_and_integer_tokens_round_trip():
    server = await _start()
    try:
        a = Conn(server)
        await a.request(5, "test/progress", {"_meta": {"progressToken": 0}})
        got = await a.drain(window=1.0)
        progress = [m for m in got if m.get("method") == "notifications/progress"]
        assert len(progress) == 1 and progress[0]["params"]["progressToken"] == 0
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_cancelled_request_id_is_translated_to_the_hub_id():
    server = await _start()
    try:
        a, b = Conn(server), Conn(server)
        await a.request(7, "test/sleep", {"seconds": 3})
        await asyncio.sleep(0.3)  # in flight upstream
        await a.notify("notifications/cancelled", {"requestId": 7, "reason": "user"})
        await asyncio.sleep(0.2)

        await b.request(1, "test/last_cancel")
        cancel = (await b.recv())["result"]["cancel"]
        assert cancel is not None, "an in-flight request's cancellation must reach the upstream"
        assert cancel["requestId"] != 7
        assert str(cancel["requestId"]).startswith("hub:")
        assert cancel["reason"] == "user"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_cancel_of_an_unknown_or_foreign_id_is_not_forwarded():
    server = await _start()
    try:
        a, b = Conn(server), Conn(server)
        await b.request(3, "test/sleep", {"seconds": 3})  # B's request, id 3
        await asyncio.sleep(0.3)
        # A has nothing in flight: neither an unknown id nor B's id may cancel anything.
        await a.notify("notifications/cancelled", {"requestId": 999})
        await a.notify("notifications/cancelled", {"requestId": 3})
        await asyncio.sleep(0.2)

        await a.request(1, "test/last_cancel")
        assert (await a.recv())["result"]["cancel"] is None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_request_goes_only_to_the_session_with_a_request_in_flight():
    server = await _start()
    try:
        a, b = Conn(server), Conn(server)  # b is connected but idle
        await a.request(1, "test/ask_roots")

        ask = await a.recv(timeout=3.0)
        assert ask["method"] == "roots/list" and ask["id"] == "srv-1"
        assert await b.drain(window=0.5) == [], "an idle session must not receive the server's request"

        await a.respond("srv-1", {"roots": []})
        final = await a.recv(timeout=3.0)
        assert final["id"] == 1 and final["result"]["asked"] == {"roots": []}
        await a.close()
        await b.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_server_request_with_nobody_in_flight_is_answered_with_an_error():
    server = await _start()
    try:
        a = Conn(server)
        await a.request(1, "test/ask_later")
        assert (await a.recv())["result"] == {}
        await asyncio.sleep(0.9)  # the upstream now asks; no request is in flight

        assert await a.drain(window=0.3) == [], "nobody should be asked: nothing is in flight"
        await a.request(2, "test/last_srv_reply")
        reply = (await a.recv())["result"]["reply"]
        assert reply["id"] == "srv-later"
        assert reply["error"]["code"] == -32601
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exclusive_server_request_answer_does_not_deadlock_behind_the_guard():
    """On an `exclusive` server the request that triggered the server's own
    question holds the guard; the client's ANSWER is not a request and must
    not queue behind it."""
    server = await _start("exclusive")
    try:
        a = Conn(server)
        await a.request(1, "test/ask_roots")
        ask = await a.recv(timeout=3.0)
        assert ask["method"] == "roots/list"
        await a.respond("srv-1", {"roots": []})
        final = await a.recv(timeout=3.0)  # would hang forever before the fix
        assert final["result"]["asked"] == {"roots": []}
        await a.close()
    finally:
        await server.stop()
