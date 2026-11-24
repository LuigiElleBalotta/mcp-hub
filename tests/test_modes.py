"""hub-proxy-efficiency R3: session-aware and isolated concurrency modes."""
import asyncio
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub.config import CONCURRENCY_MODES, Config, HubConfig, ServerConfig
from mcp_hub.hub_app import _proxy, build_app
from mcp_hub.manager import HubManager, ManagedServer
from mcp_hub.session import SESSION_META_KEY

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


def _cfg(mode, **kw) -> ServerConfig:
    base = dict(enabled=True, command=sys.executable, args=[FAKE_SERVER], env={}, concurrency=mode, restart="never")
    base.update(kw)
    return ServerConfig(**base)


def _msg(obj):
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class Conn:
    """A client session. Like `handle_sse` it asks the server for its upstream."""

    def __init__(self, server: ManagedServer):
        self.server = server
        self.upstream = server.session_upstream()
        self._rs, self._rr = anyio.create_memory_object_stream(32)
        self._ws, self._wr = anyio.create_memory_object_stream(32)
        self.task = asyncio.create_task(_proxy(self._rr, self._ws, self.upstream))

    async def call(self, req_id, method, params=None):
        await self._rs.send(_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}))
        while True:
            with anyio.fail_after(5.0):
                m = (await self._wr.receive()).message.model_dump(by_alias=True, exclude_unset=True)
            if m.get("id") == req_id:
                return m

    async def close(self):
        await self._rs.aclose()
        with anyio.fail_after(5.0):
            await self.task
        await self.server.release_session_upstream(self.upstream)


def test_all_four_modes_are_known_and_only_isolated_has_no_shared_process():
    assert CONCURRENCY_MODES == ("exclusive", "parallel", "session-aware", "isolated")
    assert _cfg("session-aware").starts_with_hub is True
    assert _cfg("isolated").starts_with_hub is False


@pytest.mark.asyncio
async def test_session_aware_shares_one_process_and_tells_it_who_is_asking():
    server = ManagedServer("fake", _cfg("session-aware"))
    await server.start()
    a, b = Conn(server), Conn(server)
    try:
        pid_a = (await a.call(1, "test/pid"))["result"]["pid"]
        pid_b = (await b.call(1, "test/pid"))["result"]["pid"]
        assert pid_a == pid_b, "still ONE shared upstream"

        meta_a1 = (await a.call(2, "test/echo_meta"))["result"]["meta"]
        meta_a2 = (await a.call(3, "test/echo_meta"))["result"]["meta"]
        meta_b = (await b.call(2, "test/echo_meta"))["result"]["meta"]
        assert meta_a1[SESSION_META_KEY] == meta_a2[SESSION_META_KEY], "stable for the whole connection"
        assert meta_a1[SESSION_META_KEY] != meta_b[SESSION_META_KEY], "different sessions, different ids"
        assert len(meta_a1[SESSION_META_KEY]) >= 8
        await a.close()
        await b.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_session_aware_keeps_the_clients_own_meta_and_progress_token_working():
    server = ManagedServer("fake", _cfg("session-aware"))
    await server.start()
    a = Conn(server)
    try:
        reply = await a.call(1, "test/echo_meta", {"_meta": {"progressToken": "tok", "custom": 7}})
        meta = reply["result"]["meta"]
        assert meta["custom"] == 7, "the client's own _meta is preserved"
        assert str(meta["progressToken"]).startswith("hubp:"), "progress token still remapped"
        assert SESSION_META_KEY in meta
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exclusive", "parallel"])
async def test_the_other_modes_do_not_add_anything_to_meta(mode):
    server = ManagedServer("fake", _cfg(mode))
    await server.start()
    a = Conn(server)
    try:
        assert (await a.call(1, "test/echo_meta"))["result"]["meta"] is None
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_isolated_gives_every_session_its_own_process_and_ends_it_with_the_session():
    server = ManagedServer("fake", _cfg("isolated"))
    await server.start()  # a no-op: nothing shared to start
    assert server.process is None and server.status == "idle"
    a, b = Conn(server), Conn(server)
    try:
        assert len(server.children) == 2
        pid_a = (await a.call(1, "test/pid"))["result"]["pid"]
        pid_b = (await b.call(1, "test/pid"))["result"]["pid"]
        assert pid_a != pid_b, "two sessions, two processes"
        assert (await a.call(2, "test/pid"))["result"]["pid"] == pid_a, "a session keeps ITS process"

        child_a = a.upstream
        process_a = child_a.process
        await a.close()  # session A ends -> its process is stopped
        assert process_a.returncode is not None and child_a not in server.children
        assert len(server.children) == 1
        assert (await b.call(2, "test/pid"))["result"]["pid"] == pid_b, "B is untouched"
        await b.close()
        assert server.children == set()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_isolated_sessions_do_not_share_the_list_cache_or_the_handshake():
    server = ManagedServer("fake", _cfg("isolated"))
    a, b = Conn(server), Conn(server)
    try:
        await a.call(1, "tools/list")
        await b.call(1, "tools/list")
        # each private process was asked once: nothing came from the other's cache
        assert (await a.call(2, "test/list_calls"))["result"]["calls"]["tools/list"] == 1
        assert (await b.call(2, "test/list_calls"))["result"]["calls"]["tools/list"] == 1
        assert (await a.call(3, "test/handshake_stats"))["result"]["initializes"] == 1
        assert (await b.call(3, "test/handshake_stats"))["result"]["initializes"] == 1
        await a.close()
        await b.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_isolated_status_logs_and_stop():
    hub = HubManager(Config(hub=HubConfig(), servers={"fake": _cfg("isolated")}))
    parent = hub.get("fake")
    assert hub.status_snapshot()["fake"]["status"] == "idle"
    a = Conn(parent)
    try:
        await a.call(1, "test/pid")
        snap = hub.status_snapshot()["fake"]
        assert snap["status"] == "running" and snap["sessions"] == 1 and snap["concurrency"] == "isolated"
        assert any("fake#1" in line for line in parent.logs), "the session's process logs show on the server"

        await parent.stop()  # Stop in the GUI: every session's process goes away
        assert parent.children == set()
        assert hub.status_snapshot()["fake"]["status"] == "idle"
        assert a.upstream.process.returncode is not None
    finally:
        await a._rs.aclose()
        try:
            await a.task
        except BaseException:  # the session lost its upstream; how it ends is not what this test is about
            pass
        await parent.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["sometimes", "", None])
async def test_api_rejects_an_unknown_concurrency_mode(bad):
    app = build_app(HubManager(Config(hub=HubConfig(), servers={})))
    body = {"enabled": False, "command": "x", "args": [], "concurrency": bad}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:37450") as c:
        assert (await c.post("/api/servers/a", json=body)).status_code == 400
        for mode in CONCURRENCY_MODES:
            ok = await c.post("/api/servers/a", json={"enabled": False, "command": "x", "args": [], "concurrency": mode})
            assert ok.status_code == 200, mode
