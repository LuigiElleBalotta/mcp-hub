"""hub-gui-and-clients R2: GET /api/servers/{name}/tools and POST .../call."""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")
BASE = "http://127.0.0.1:37450"


def _hub(env=None, mode="parallel", **cfg):
    env = {"FAKE_TOOLS": "read_a,read_secret,write_b", "FAKE_READONLY": "read_a,read_secret", **(env or {})}
    sc = ServerConfig(enabled=True, command=sys.executable, args=[FAKE_SERVER], env=env,
                      concurrency=mode, restart="never", **cfg)
    return HubManager(Config(hub=HubConfig(), servers={"fake": sc}))


def _api(hub):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(hub)), base_url=BASE)


@pytest.mark.asyncio
async def test_lists_the_raw_tools_live_then_from_the_cache():
    hub = _hub()
    await hub.start_all()
    try:
        async with _api(hub) as api:
            first = (await api.get("/api/servers/fake/tools")).json()
            assert first["source"] == "live" and first["filtered"] is False
            assert [t["name"] for t in first["tools"]] == ["read_a", "read_secret", "write_b"]
            a = first["tools"][0]
            assert a["description"] == "tool read_a" and a["readOnly"] is True and a["visible"] is True
            assert first["tools"][2]["readOnly"] is False
            second = (await api.get("/api/servers/fake/tools")).json()
            assert second["source"] == "cache"
            # one upstream tools/list in total: the live fetch filled the shared cache
            resp = await hub.get("fake").hub_request("test/list_calls", {}, timeout=3)
            assert resp["result"]["calls"]["tools/list"] == 1
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_shows_what_the_client_facing_filters_make_of_each_tool():
    hub = _hub(toolsAllow=["read_*"], toolsDeny=["*secret*"], toolPrefix="gl_")
    await hub.start_all()
    try:
        async with _api(hub) as api:
            body = (await api.get("/api/servers/fake/tools")).json()
        by_name = {t["name"]: t for t in body["tools"]}
        assert body["filtered"] is True
        assert by_name["read_a"]["visible"] is True and by_name["read_a"]["clientName"] == "gl_read_a"
        assert by_name["read_secret"]["visible"] is False and by_name["read_secret"]["lockedByPattern"] is True
        assert by_name["write_b"]["visible"] is False and by_name["write_b"]["lockedByPattern"] is False, \
            "hidden only by the allow-list: an exact-name edit can fix that"
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_an_exact_name_in_the_deny_list_is_not_locked_only_a_glob_is():
    hub = _hub(toolsDeny=["read_secret", "write_*"])
    await hub.start_all()
    try:
        async with _api(hub) as api:
            by_name = {t["name"]: t for t in (await api.get("/api/servers/fake/tools")).json()["tools"]}
        assert by_name["read_secret"]["visible"] is False and by_name["read_secret"]["lockedByPattern"] is False
        assert by_name["write_b"]["visible"] is False and by_name["write_b"]["lockedByPattern"] is True
        assert by_name["read_a"]["visible"] is True and by_name["read_a"]["lockedByPattern"] is False
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_collects_every_page_of_a_paginated_list():
    hub = _hub(env={"FAKE_PAGINATE": "1"})
    await hub.start_all()
    try:
        async with _api(hub) as api:
            body = (await api.get("/api/servers/fake/tools")).json()
        # the fake serves the same 3 tools on page 1 and page 2: both pages were followed
        assert len(body["tools"]) == 6
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_lazy_server_is_started_to_answer_and_unknown_server_is_404():
    hub = _hub(startMode="lazy")
    assert hub.get("fake").status == "idle"
    try:
        async with _api(hub) as api:
            assert (await api.get("/api/servers/fake/tools")).status_code == 200
            assert hub.get("fake").status == "running"
            assert (await api.get("/api/servers/nope/tools")).status_code == 404
            assert (await api.post("/api/servers/nope/call", json={"tool": "x"})).status_code == 404
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_an_isolated_server_has_no_shared_tool_list():
    hub = _hub(mode="isolated")
    async with _api(hub) as api:
        r = await api.get("/api/servers/fake/tools")
        assert r.status_code == 409 and "isolated" in r.json()["error"]
        assert (await api.post("/api/servers/fake/call", json={"tool": "read_a"})).status_code == 409


@pytest.mark.asyncio
async def test_a_dead_server_is_a_502_not_a_crash():
    hub = _hub()
    await hub.start_all()
    hub.get("fake").process.kill()
    for _ in range(100):
        if hub.get("fake").status == "crashed":
            break
        await asyncio.sleep(0.05)
    try:
        async with _api(hub) as api:
            r = await api.get("/api/servers/fake/tools")
            assert r.status_code == 502 and "subprocess" in r.json()["error"]
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_calls_a_read_only_tool_without_confirmation_and_counts_it():
    hub = _hub()
    await hub.start_all()
    try:
        async with _api(hub) as api:
            r = await api.post("/api/servers/fake/call", json={"tool": "read_a", "arguments": {"path": "x"}})
            body = r.json()
            assert r.status_code == 200 and body["readOnly"] is True and body["error"] is None
            assert body["result"] == {"called": "read_a", "arguments": {"path": "x"}} and body["ms"] >= 0
        recent = hub.get("fake").metrics.recent()
        assert any(c["method"] == "tools/call read_a" and c["ok"] for c in recent), "the tester shows in the dashboard"
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_a_tool_not_declared_read_only_needs_explicit_confirmation():
    hub = _hub()
    await hub.start_all()
    try:
        async with _api(hub) as api:
            refused = await api.post("/api/servers/fake/call", json={"tool": "write_b", "arguments": {}})
            assert refused.status_code == 409 and refused.json()["readOnly"] is False
            for not_true in ("yes", 1, "true", None):
                assert (await api.post("/api/servers/fake/call",
                                       json={"tool": "write_b", "confirm": not_true})).status_code == 409
            # nothing reached the upstream
            resp = await hub.get("fake").hub_request("test/tool_calls", {}, timeout=3)
            assert resp["result"]["names"] == []
            ok = await api.post("/api/servers/fake/call", json={"tool": "write_b", "confirm": True})
            assert ok.status_code == 200 and ok.json()["result"]["called"] == "write_b"
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"tool": ""}, {"tool": 5}, {"tool": "read_a", "arguments": [1]}, "text", [1]])
async def test_call_validates_its_body(body):
    hub = _hub()
    await hub.start_all()
    try:
        async with _api(hub) as api:
            assert (await api.post("/api/servers/fake/call", json=body)).status_code == 400
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_unknown_tool_is_404_and_hidden_tools_can_still_be_tried_by_the_admin():
    hub = _hub(toolsDeny=["read_secret"])
    await hub.start_all()
    try:
        async with _api(hub) as api:
            assert (await api.post("/api/servers/fake/call", json={"tool": "nope"})).status_code == 404
            # the tester is an admin tool: a tool hidden from CLIENTS can still be exercised here
            ok = await api.post("/api/servers/fake/call", json={"tool": "read_secret"})
            assert ok.status_code == 200 and ok.json()["result"]["called"] == "read_secret"
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
async def test_the_tester_respects_an_exclusive_servers_guard():
    hub = _hub(mode="exclusive", env={"FAKE_TOOLS": "read_a", "FAKE_READONLY": "read_a"})
    await hub.start_all()
    server = hub.get("fake")
    try:
        order = []
        original = server.guard.run

        async def watching(fn):
            order.append("guard")
            return await original(fn)

        server.guard.run = watching
        async with _api(hub) as api:
            assert (await api.post("/api/servers/fake/call", json={"tool": "read_a"})).status_code == 200
        assert order == ["guard"]
    finally:
        await hub.stop_all()


# ------------------------------------------------------------------ edit_filters (pure)

from mcp_hub.filters import edit_filters, tool_visible

NAMES = ["read_a", "read_secret", "write_b"]


def test_edit_filters_without_a_change_changes_nothing():
    assert edit_filters(NAMES, set(NAMES), [], []) == ([], [], [])
    # read_a and read_secret are what ["read_*"] allows, write_b is denied: asking for exactly that is no change
    assert edit_filters(NAMES, {"read_a", "read_secret"}, ["read_*"], ["write_b"]) == (["read_*"], ["write_b"], [])


def test_hiding_adds_the_exact_name_to_deny():
    allow, deny, locked = edit_filters(NAMES, {"read_a", "write_b"}, [], [])
    assert (allow, deny, locked) == ([], ["read_secret"], [])
    assert [tool_visible(n, allow, deny) for n in NAMES] == [True, False, True]


def test_showing_removes_an_exact_deny_and_extends_the_allow_list_when_it_was_that_hiding():
    allow, deny, locked = edit_filters(NAMES, set(NAMES), ["read_*"], ["read_secret"])
    assert deny == [] and allow == ["read_*", "write_b"] and locked == []
    assert all(tool_visible(n, allow, deny) for n in NAMES)


def test_a_tool_hidden_by_a_deny_glob_is_locked_not_silently_ignored():
    allow, deny, locked = edit_filters(NAMES, set(NAMES), [], ["*secret*"])
    assert locked == ["read_secret"] and deny == ["*secret*"], "the glob stays; the caller is told"
    assert tool_visible("read_a", allow, deny) and not tool_visible("read_secret", allow, deny)


def test_edit_filters_does_not_mutate_its_inputs():
    allow, deny = ["read_*"], []
    edit_filters(NAMES, {"read_a"}, allow, deny)
    assert allow == ["read_*"] and deny == []


# ------------------------------------------------------------------ PUT /filters

from mcp_hub.config import load_config


@pytest.mark.asyncio
async def test_filters_are_changed_in_place_without_restarting_and_clients_are_told():
    hub = _hub()
    await hub.start_all()
    server = hub.get("fake")
    pid = server.process.pid
    told = []

    async def subscriber(obj):
        told.append(obj.get("method"))

    server.subscribers.add(subscriber)
    try:
        async with _api(hub) as api:
            r = await api.put("/api/servers/fake/filters",
                              json={"toolsAllow": ["read_*"], "toolsDeny": ["read_secret"], "toolPrefix": "gl_"})
            assert r.status_code == 200 and r.json()["toolPrefix"] == "gl_"
            assert server.process.pid == pid and server.status == "running", "no restart: sessions are kept"
            assert told == ["notifications/tools/list_changed"]
            body = (await api.get("/api/servers/fake/tools")).json()
            assert {t["name"]: t["visible"] for t in body["tools"]} == {
                "read_a": True, "read_secret": False, "write_b": False}
            assert body["toolsAllow"] == ["read_*"] and body["toolPrefix"] == "gl_"
            # a partial body keeps what it does not mention
            await api.put("/api/servers/fake/filters", json={"toolPrefix": ""})
            assert server.config.toolsAllow == ["read_*"] and server.config.toolPrefix == ""
        assert load_config().servers["fake"].toolsDeny == ["read_secret"], "persisted to config.json"
    finally:
        await hub.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"toolsAllow": "read_*"}, {"toolsDeny": [""]}, {"toolPrefix": "has space"}, [1]])
async def test_set_filters_validates_and_leaves_the_config_alone(body):
    hub = _hub()
    async with _api(hub) as api:
        assert (await api.put("/api/servers/fake/filters", json=body)).status_code == 400
        assert (await api.put("/api/servers/nope/filters", json={})).status_code == 404
    assert hub.get("fake").config.toolsAllow == []


@pytest.mark.asyncio
async def test_set_filters_reaches_the_processes_of_isolated_sessions_too():
    hub = _hub(mode="isolated")
    parent = hub.get("fake")
    child = parent.session_upstream()
    try:
        async with _api(hub) as api:
            await api.put("/api/servers/fake/filters", json={"toolsDeny": ["write_b"]})
        assert child.config.toolsDeny == ["write_b"] and parent.config.toolsDeny == ["write_b"]
    finally:
        await parent.release_session_upstream(child)


@pytest.mark.asyncio
async def test_filters_are_refused_for_a_service():
    hub = HubManager(Config(hub=HubConfig(), servers={
        "svc": ServerConfig(enabled=False, command="x", args=[], type="service")}))
    async with _api(hub) as api:
        assert (await api.put("/api/servers/svc/filters", json={"toolsDeny": ["a"]})).status_code == 409
