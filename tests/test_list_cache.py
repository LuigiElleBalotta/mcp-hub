"""hub-proxy-efficiency R1: tools/list & co answered from a cache."""
import asyncio
import sys
from pathlib import Path

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub.cache import CACHEABLE_LISTS, DiskCache, ListCache, cache_key, has_cursor
from mcp_hub.config import ServerConfig
from mcp_hub.hub_app import _proxy
from mcp_hub.manager import ManagedServer

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


# ----------------------------------------------------------------- unit: ListCache

def test_put_get_and_pages_are_never_cached():
    cache = ListCache()
    gen = cache.generation("tools/list")
    assert cache.put("tools/list", {"tools": [1]}, gen) is True
    assert cache.get("tools/list") == {"tools": [1]}
    assert cache.put("prompts/list", {"prompts": [], "nextCursor": "p2"}, cache.generation("prompts/list")) is False
    assert cache.get("prompts/list") is None
    assert cache.put("tools/call", {"x": 1}, 0) is False  # not a list method


def test_an_answer_that_raced_with_list_changed_is_not_cached():
    cache = ListCache()
    gen = cache.generation("tools/list")  # request sent...
    cache.invalidate_for_notification("notifications/tools/list_changed")  # ...upstream says it changed...
    assert cache.put("tools/list", {"tools": ["stale"]}, gen) is False  # ...stale answer arrives
    assert cache.get("tools/list") is None


def test_resources_list_changed_drops_resources_and_templates_only():
    cache = ListCache()
    for m in CACHEABLE_LISTS:
        cache.put(m, {"k": m}, cache.generation(m))
    assert cache.invalidate_for_notification("notifications/resources/list_changed") is True
    assert cache.get("resources/list") is None and cache.get("resources/templates/list") is None
    assert cache.get("tools/list") is not None and cache.get("prompts/list") is not None
    assert cache.invalidate_for_notification("notifications/message") is False


def test_clear_drops_everything_and_bumps_generations():
    cache = ListCache()
    gen = cache.generation("tools/list")
    cache.put("tools/list", {"tools": []}, gen)
    cache.clear()
    assert cache.get("tools/list") is None and cache.generation("tools/list") == gen + 1


def test_has_cursor():
    assert has_cursor({"cursor": "abc"}) and not has_cursor({}) and not has_cursor(None)
    assert not has_cursor({"cursor": None})


# ---------------------------------------------------------------- unit: DiskCache

def test_disk_cache_round_trip_and_key_mismatch(tmp_path):
    key = cache_key("npx", ["-y", "srv"], ["TOKEN"])
    disk = DiskCache(tmp_path, "my/server", key)
    assert disk.load() is None
    disk.save({"serverInfo": {"name": "x"}}, {"tools/list": {"tools": []}})
    assert disk.load() == {"initialize": {"serverInfo": {"name": "x"}}, "lists": {"tools/list": {"tools": []}}}
    assert (tmp_path / "my_server.json").exists(), "unsafe characters in a name never escape the directory"
    other = DiskCache(tmp_path, "my/server", cache_key("npx", ["-y", "srv", "--new-flag"], ["TOKEN"]))
    assert other.load() is None, "another command line is another server"


def test_cache_key_ignores_env_values_and_order():
    assert cache_key("a", ["x"], ["B", "A"]) == cache_key("a", ["x"], ["A", "B"])
    assert cache_key("a", ["x"], []) != cache_key("a", ["y"], [])


def test_disk_cache_survives_corruption_and_unwritable_paths(tmp_path):
    disk = DiskCache(tmp_path, "s", "k")
    (tmp_path / "s.json").write_text("{not json", encoding="utf-8")
    assert disk.load() is None
    (tmp_path / "s.json").write_text('{"key": "k", "initialize": "oops"}', encoding="utf-8")
    assert disk.load() is None
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    DiskCache(blocked / "sub", "s", "k").save({"a": 1}, {})  # must not raise


# ------------------------------------------------------------- through the proxy

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
            if m.get("id") == req_id:  # skip broadcast notifications
                return m

    async def close(self):
        await self._rs.aclose()
        with anyio.fail_after(5.0):
            await self.task


async def _start(**env):
    cfg = ServerConfig(enabled=True, command=sys.executable, args=[FAKE_SERVER], env=env,
                       concurrency="parallel", restart="never")
    server = ManagedServer("fake", cfg)
    await server.start()
    return server


async def _calls(conn, rid=900):
    return (await conn.call(rid, "test/list_calls"))["result"]["calls"]


@pytest.mark.asyncio
async def test_second_list_is_served_from_the_cache_for_any_session():
    server = await _start()
    try:
        a, b = Conn(server), Conn(server)
        first = await a.call(1, "tools/list")
        second = await b.call(1, "tools/list")  # a DIFFERENT session
        third = await a.call(2, "tools/list")
        assert first["result"] == second["result"] == third["result"] == {"tools": [
            {"name": "t0", "description": "tool t0", "inputSchema": {"type": "object"}}]}
        assert second["id"] == 1 and third["id"] == 2, "each answer carries its own client's id"
        assert (await _calls(a))["tools/list"] == 1, "the upstream was asked exactly once"
        await a.close()
        await b.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["prompts/list", "resources/list", "resources/templates/list"])
async def test_the_other_list_methods_are_cached_too(method):
    server = await _start()
    try:
        a = Conn(server)
        await a.call(1, method)
        await a.call(2, method)
        assert (await _calls(a))[method] == 1
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_list_changed_invalidates_and_the_next_list_is_fresh():
    server = await _start()
    try:
        a = Conn(server)
        assert (await a.call(1, "tools/list"))["result"]["tools"][0]["name"] == "t0"
        await a.call(2, "test/emit_list_changed")  # upstream: "my tools changed"
        await asyncio.sleep(0.1)
        fresh = await a.call(3, "tools/list")
        assert fresh["result"]["tools"][0]["name"] == "t1"
        assert (await _calls(a))["tools/list"] == 2
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_paginated_lists_are_forwarded_and_never_cached():
    server = await _start(FAKE_PAGINATE="1")
    try:
        a = Conn(server)
        page1 = await a.call(1, "tools/list")
        assert page1["result"]["nextCursor"] == "p2"
        await a.call(2, "tools/list")  # first page again: not cached (it has a nextCursor)
        await a.call(3, "tools/list", {"cursor": "p2"})  # a page request: always forwarded
        await a.call(4, "tools/list", {"cursor": "p2"})
        assert (await _calls(a))["tools/list"] == 4
        await a.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_respawn_clears_the_cache_and_a_down_server_still_serves_cached_lists():
    server = await _start()
    try:
        a = Conn(server)
        await a.call(1, "tools/list")
        assert server.cache.get("tools/list") is not None
        server.process.kill()  # restart="never": it just stays down
        for _ in range(100):
            if server.status == "crashed":
                break
            await asyncio.sleep(0.05)
        # upstream is dead, yet the list is still answered...
        assert (await a.call(2, "tools/list"))["result"]["tools"][0]["name"] == "t0"
        # ...while a real call fails fast, as before
        assert (await a.call(3, "test/echo", {"value": 1}))["error"]["code"] == -32000
        await server.start()  # a new process: its lists are unknown until asked
        assert server.cache.get("tools/list") is None
        await a.close()
    finally:
        await server.stop()
