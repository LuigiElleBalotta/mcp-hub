"""hub-proxy-efficiency R6: per-server tool allow/deny lists and name prefix."""
import asyncio
import json
import sys
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config
from mcp_hub.filters import filter_tools_result, resolve_call, tool_visible
from mcp_hub.hub_app import _proxy, build_app
from mcp_hub.manager import HubManager, ManagedServer

from hub_harness import client, mcp_client, running_hub

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


# ------------------------------------------------------------------ pure functions

def test_visibility_empty_allow_means_everything_and_deny_wins():
    assert tool_visible("anything", [], [])
    assert tool_visible("read_file", ["read_*"], [])
    assert not tool_visible("write_file", ["read_*"], [])
    assert not tool_visible("read_secret", ["read_*"], ["*secret*"]), "deny beats allow"
    assert not tool_visible("drop_table", [], ["drop_*"])
    assert tool_visible("Read_File", ["read_*"], []) is False, "patterns are case-sensitive, like tool names"


def test_filter_result_hides_and_prefixes_without_touching_the_original():
    raw = {"tools": [{"name": "read_a", "description": "d"}, {"name": "write_b"}], "nextCursor": "p2"}
    out = filter_tools_result(raw, allow=["read_*"], deny=[], prefix="gl_")
    assert out == {"tools": [{"name": "gl_read_a", "description": "d"}], "nextCursor": "p2"}
    assert raw["tools"][0]["name"] == "read_a", "the cached raw list must stay raw"


def test_resolve_call_strips_the_prefix_and_refuses_hidden_tools():
    assert resolve_call("gl_read_a", ["read_*"], [], "gl_") == "read_a"
    assert resolve_call("read_a", ["read_*"], [], "gl_") is None, "unprefixed name does not exist for the client"
    assert resolve_call("gl_write_b", ["read_*"], [], "gl_") is None, "hidden tool"
    assert resolve_call("plain", [], [], "") == "plain"
    assert resolve_call(None, [], [], "") is None and resolve_call(5, [], [], "") is None


def test_config_property_and_round_trip(tmp_path):
    assert not ServerConfig(enabled=True, command="x", args=[]).has_tool_filter
    assert ServerConfig(enabled=True, command="x", args=[], toolPrefix="a_").has_tool_filter
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={
        "plain": ServerConfig(enabled=True, command="x", args=[]),
        "f": ServerConfig(enabled=True, command="x", args=[], toolsAllow=["a*"], toolsDeny=["ab"], toolPrefix="p_"),
    }), path)
    raw = json.loads(path.read_text(encoding="utf-8"))["servers"]
    assert not {"toolsAllow", "toolsDeny", "toolPrefix"} & set(raw["plain"])
    loaded = load_config(path).servers["f"]
    assert (loaded.toolsAllow, loaded.toolsDeny, loaded.toolPrefix) == (["a*"], ["ab"], "p_")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"toolsAllow": "read_*"}, {"toolsDeny": [1]}, {"toolPrefix": 5},
                                 {"toolPrefix": "has space"}, {"toolPrefix": "x" * 65}])
async def test_api_rejects_invalid_filter_settings(bad):
    app = build_app(HubManager(Config(hub=HubConfig(), servers={})))
    body = {"enabled": False, "command": "x", "args": [], **bad}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:37450") as c:
        assert (await c.post("/api/servers/a", json=body)).status_code == 400
        ok = await c.post("/api/servers/a", json={"enabled": False, "command": "x", "args": [],
                                                   "toolsAllow": ["a*"], "toolsDeny": [], "toolPrefix": "gl_"})
        assert ok.status_code == 200


# ------------------------------------------------------------------ through the proxy

def _msg(obj):
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class Conn:
    def __init__(self, managed):
        self._rs, self._rr = anyio.create_memory_object_stream(32)
        self._ws, self._wr = anyio.create_memory_object_stream(32)
        self.task = asyncio.create_task(_proxy(self._rr, self._ws, managed))

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


async def _server(**cfg):
    env = {"FAKE_TOOLS": "read_a,read_secret,write_b", **cfg.pop("env", {})}
    sc = ServerConfig(enabled=True, command=sys.executable, args=[FAKE_SERVER], env=env,
                      concurrency="parallel", restart="never", **cfg)
    server = ManagedServer("fake", sc)
    await server.start()
    return server


def _names(reply):
    return [t["name"] for t in reply["result"]["tools"]]


@pytest.mark.asyncio
async def test_without_filters_nothing_changes():
    server = await _server()
    c = Conn(server)
    try:
        assert _names(await c.call(1, "tools/list")) == ["read_a", "read_secret", "write_b"]
        assert (await c.call(2, "tools/call", {"name": "write_b"}))["result"]["called"] == "write_b"
        await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_allow_deny_and_prefix_shape_the_list_and_calls():
    server = await _server(toolsAllow=["read_*"], toolsDeny=["*secret*"], toolPrefix="gl_")
    c = Conn(server)
    try:
        first = await c.call(1, "tools/list")
        again = await c.call(2, "tools/list")  # served from the cache: must be filtered the same way
        assert _names(first) == _names(again) == ["gl_read_a"]
        assert [t["description"] for t in first["result"]["tools"]] == ["tool read_a"]
        # the cache holds the RAW upstream list; filtering happens on the way out
        assert [t["name"] for t in server.cache.get("tools/list")["tools"]] == ["read_a", "read_secret", "write_b"]

        ok = await c.call(3, "tools/call", {"name": "gl_read_a", "arguments": {}})
        assert ok["result"]["called"] == "read_a", "the upstream sees its own, unprefixed name"
        for rid, name in ((4, "gl_write_b"), (5, "gl_read_secret"), (6, "read_a"), (7, "nope")):
            bad = await c.call(rid, "tools/call", {"name": name})
            assert bad["error"]["code"] == -32602 and name in bad["error"]["message"], name
        assert (await c.call(8, "test/tool_calls"))["result"]["names"] == ["read_a"], \
            "refused calls never reached the upstream"
        await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_page_of_a_paginated_list_is_filtered_too():
    server = await _server(toolsAllow=["read_*"], env={"FAKE_PAGINATE": "1"})
    c = Conn(server)
    try:
        page1 = await c.call(1, "tools/list")
        assert _names(page1) == ["read_a", "read_secret"] and page1["result"]["nextCursor"] == "p2"
        page2 = await c.call(2, "tools/list", {"cursor": "p2"})
        assert _names(page2) == ["read_a", "read_secret"]
        await c.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_prefix_only_and_deny_only():
    server = await _server(toolPrefix="x.")
    c = Conn(server)
    try:
        assert _names(await c.call(1, "tools/list")) == ["x.read_a", "x.read_secret", "x.write_b"]
        assert (await c.call(2, "tools/call", {"name": "x.write_b"}))["result"]["called"] == "write_b"
        await c.close()
    finally:
        await server.stop()
    server = await _server(toolsDeny=["write_*"])
    c = Conn(server)
    try:
        assert _names(await c.call(1, "tools/list")) == ["read_a", "read_secret"]
        assert (await c.call(2, "tools/call", {"name": "write_b"}))["error"]["code"] == -32602
        await c.close()
    finally:
        await server.stop()


# ------------------------------------------------------------------ real SDK client

@pytest.mark.asyncio
@pytest.mark.parametrize("open_client", [client, mcp_client], ids=["sse", "streamable"])
async def test_real_client_sees_only_the_allowed_prefixed_tools(open_client):
    async with running_hub(toolsDeny=["die", "slow"], toolPrefix="t_") as (_manager, base):
        async with open_client(base) as session:
            names = sorted(t.name for t in (await session.list_tools()).tools)
            assert names == ["t_add", "t_whoami"]
            assert (await session.call_tool("t_add", {"a": 2, "b": 5})).content[0].text == "7"
            for hidden in ("die", "t_die", "add"):
                with pytest.raises(MCPError) as err:
                    await session.call_tool(hidden, {})
                assert err.value.code == -32602
