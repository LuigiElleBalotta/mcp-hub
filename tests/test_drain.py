"""hub-proxy-efficiency R5: graceful drain before the hub stops."""
import asyncio
import json
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub.config import Config, HubConfig, ServerConfig, hub_to_dict, load_config, save_config
from mcp_hub.hub_app import _proxy, build_app
from mcp_hub.manager import HubManager

from hub_harness import client, mcp_client, running_hub

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")
BASE = "http://127.0.0.1:37450"


def _hub(mode="parallel", **hub_kw):
    cfg = ServerConfig(enabled=True, command=sys.executable, args=[FAKE_SERVER], env={},
                       concurrency=mode, restart="never")
    return HubManager(Config(hub=HubConfig(**hub_kw), servers={"fake": cfg}))


def _msg(obj):
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class Conn:
    def __init__(self, server):
        self.server = server
        self.upstream = server.session_upstream()
        self._rs, self._rr = anyio.create_memory_object_stream(32)
        self._ws, self._wr = anyio.create_memory_object_stream(32)
        self.task = asyncio.create_task(_proxy(self._rr, self._ws, self.upstream))

    async def send(self, req_id, method, params=None):
        await self._rs.send(_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}))

    async def recv(self, timeout=5.0):
        with anyio.fail_after(timeout):
            return (await self._wr.receive()).message.model_dump(by_alias=True, exclude_unset=True)

    async def close(self):
        await self._rs.aclose()
        with anyio.fail_after(5.0):
            await self.task
        await self.server.release_session_upstream(self.upstream)


async def _wait_in_flight(hub, n=1):
    for _ in range(100):
        if hub.in_flight() >= n:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("request never reached the upstream")


@pytest.mark.asyncio
async def test_drain_waits_for_the_request_in_flight_and_refuses_new_traffic():
    hub = _hub()
    await hub.start_all()
    app = build_app(hub)
    conn = Conn(hub.get("fake"))
    try:
        await conn.send(1, "test/sleep", {"seconds": 0.8})
        await _wait_in_flight(hub)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as api:
            drain = asyncio.create_task(api.post("/api/drain", json={"timeout": 5}))
            await asyncio.sleep(0.2)
            assert hub.draining and not drain.done(), "still waiting: a request is in flight"

            # new MCP traffic is refused while the API stays open
            refused = await api.get("/fake/sse")
            assert refused.status_code == 503 and refused.headers["retry-after"] == "5"
            assert (await api.post("/fake/messages/?session_id=x", json={})).status_code == 503
            status = (await api.get("/api/status")).json()
            assert status["draining"] is True and "fake" in status["servers"]

            result = (await drain).json()
            assert result == {"drained": True, "inFlight": 0}
            assert (await conn.recv())["result"] == {"slept": 0.8}, "the in-flight request was NOT cut off"
    finally:
        await conn.close()
        await hub.stop_all()


@pytest.mark.asyncio
async def test_drain_gives_up_at_the_timeout_and_says_so():
    hub = _hub()
    await hub.start_all()
    app = build_app(hub)
    conn = Conn(hub.get("fake"))
    try:
        await conn.send(1, "test/sleep", {"seconds": 2.0})
        await _wait_in_flight(hub)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as api:
            result = (await api.post("/api/drain", json={"timeout": 0.3})).json()
            assert result == {"drained": False, "inFlight": 1}
    finally:
        await hub.stop_all()  # cuts the sleeper off
        try:
            await conn.close()
        except BaseException:
            pass


@pytest.mark.asyncio
async def test_delete_resumes_a_drained_hub():
    hub = _hub()
    app = build_app(hub)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as api:
        assert (await api.post("/api/drain", json={"timeout": 0})).json()["drained"] is True
        assert (await api.get("/fake/sse")).status_code == 503
        assert (await api.delete("/api/drain")).json() == {"draining": False}
        assert hub.draining is False
        assert "draining" not in (await api.get("/api/status")).json()
        # (not GET /sse: that opens a stream that never ends) any answer but 503 means "let through"
        assert (await api.post("/fake/messages/?session_id=x", json={})).status_code != 503


@pytest.mark.asyncio
async def test_a_streamable_client_can_still_end_its_session_while_draining():
    hub = _hub()
    app = build_app(hub)
    hub.draining = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as api:
        assert (await api.post("/fake/mcp", json={})).status_code == 503
        assert (await api.delete("/fake/mcp", headers={"Mcp-Session-Id": "x"})).status_code == 404  # reached the endpoint


@pytest.mark.asyncio
async def test_shutdown_replies_at_once_then_exits_after_the_request_finishes():
    hub = _hub()
    await hub.start_all()
    event = asyncio.Event()
    app = build_app(hub, event)
    conn = Conn(hub.get("fake"))
    try:
        await conn.send(1, "test/sleep", {"seconds": 0.8})
        await _wait_in_flight(hub)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as api:
            r = await api.post("/api/shutdown", json={"drainTimeout": 5})
            assert r.json() == {"status": "shutting down", "drainTimeout": 5.0}
            await asyncio.sleep(0.2)
            assert not event.is_set(), "a request is still running: the hub must not exit yet"
            assert (await conn.recv())["result"] == {"slept": 0.8}
            await asyncio.wait_for(event.wait(), timeout=3.0)
    finally:
        await conn.close()
        await hub.stop_all()


@pytest.mark.asyncio
async def test_shutdown_with_zero_drain_exits_immediately_and_default_comes_from_config():
    hub = _hub()
    event = asyncio.Event()
    app = build_app(hub, event)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as api:
        assert (await api.post("/api/shutdown", json={"drainTimeout": 0})).status_code == 200
        await asyncio.wait_for(event.wait(), timeout=2.0)

    hub2 = _hub(drainTimeout=7.0)
    app2 = build_app(hub2, asyncio.Event())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url=BASE) as api:
        assert (await api.post("/api/shutdown")).json()["drainTimeout"] == 7.0  # no body: the config default


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [("/api/drain", {"timeout": -1}), ("/api/drain", {"timeout": "soon"}),
                                       ("/api/drain", {"timeout": True}), ("/api/drain", {"timeout": 99999}),
                                       ("/api/shutdown", {"drainTimeout": -5}), ("/api/shutdown", [1, 2])])
async def test_bad_timeouts_are_400_and_do_not_start_draining(path, body):
    hub = _hub()
    app = build_app(hub, asyncio.Event())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as api:
        assert (await api.post(path, json=body)).status_code == 400
    assert hub.draining is False


@pytest.mark.asyncio
async def test_in_flight_counts_the_isolated_sessions_processes_too():
    hub = _hub("isolated")
    conn = Conn(hub.get("fake"))
    try:
        await conn.send(1, "test/sleep", {"seconds": 0.5})
        await _wait_in_flight(hub)
        assert hub.in_flight() == 1
        assert (await conn.recv())["result"] == {"slept": 0.5}
        assert hub.in_flight() == 0
    finally:
        await conn.close()
        await hub.stop_all()


def test_drain_timeout_keeps_the_old_config_shape(tmp_path):
    assert "drainTimeout" not in hub_to_dict(HubConfig())
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(drainTimeout=5.0), servers={}), path)
    assert json.loads(path.read_text(encoding="utf-8"))["hub"]["drainTimeout"] == 5.0
    assert load_config(path).hub.drainTimeout == 5.0


@pytest.mark.asyncio
async def test_real_clients_in_flight_call_finishes_during_a_drain():
    """End to end: a real SDK client's slow call completes while the hub drains,
    and a NEW real connection is refused with 503 until the hub is resumed."""
    async with running_hub() as (manager, base):
        async with client(base) as session:
            await session.list_tools()  # warm the SDK's tool cache: it would otherwise ask for it after
            #                             the call, a NEW request that a draining hub rightly refuses
            progress = []

            async def on_progress(p, total, message):
                progress.append(p)

            call = asyncio.create_task(session.call_tool("slow", {"steps": 6}, progress_callback=on_progress))
            for _ in range(100):
                if manager.in_flight() > 0:
                    break
                await asyncio.sleep(0.02)
            async with httpx.AsyncClient() as http:
                drain = await http.post(f"{base}/api/drain", json={"timeout": 5}, timeout=10)
                assert drain.json() == {"drained": True, "inFlight": 0}
                assert (await http.get(f"{base}/real/sse")).status_code == 503
            result = await call
            assert result.content[0].text == "done" and progress[-1] == 6
        async with httpx.AsyncClient() as http:
            assert (await http.delete(f"{base}/api/drain")).status_code == 200
        async with mcp_client(base) as fresh:  # resumed: new connections work again
            assert (await fresh.call_tool("add", {"a": 1, "b": 1})).content[0].text == "2"
