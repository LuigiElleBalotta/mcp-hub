# tests/test_hub_app_proxy.py
"""Regression tests for the two Task 6 review findings:

1. The concurrency guard must gate one JSON-RPC request's turn on the
   subprocess, not an entire SSE connection's lifetime. Proven by driving
   `hub_app._proxy` directly against a real subprocess (bypassing the SSE
   transport, which `_proxy` doesn't touch) using anyio memory object streams
   in place of the SSE `read`/`write` streams -- exactly the shape
   `SseServerTransport.connect_sse` yields.
2. A JSON-RPC response line longer than asyncio.StreamReader's default 64 KiB
   limit must not crash the reader. Proven with a synthetic subprocess
   (tests/fixtures/fake_stdio_server.py) that echoes back an oversized line.
"""
import asyncio
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter

from mcp_hub.config import ServerConfig
from mcp_hub.hub_app import _proxy
from mcp_hub.manager import ManagedServer

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_stdio_server.py")


def _server_config(concurrency: str) -> ServerConfig:
    return ServerConfig(
        enabled=True,
        command=sys.executable,
        args=[FAKE_SERVER],
        env={},
        concurrency=concurrency,
    )


async def _start_managed(concurrency: str) -> ManagedServer:
    server = ManagedServer("fake", _server_config(concurrency))
    await server.start()
    return server


def _request(req_id, method: str, params: dict) -> SessionMessage:
    obj = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    return SessionMessage(jsonrpc_message_adapter.validate_python(obj))


class _Connection:
    """One simulated SSE client: owns its own read/write memory streams and
    drives `_proxy` against a shared ManagedServer, exactly like one
    `handle_sse` call would."""

    def __init__(self, managed: ManagedServer):
        self.managed = managed
        self._read_send, self._read_recv = anyio.create_memory_object_stream(16)
        self._write_send, self._write_recv = anyio.create_memory_object_stream(16)
        self.task: asyncio.Task | None = None

    def open(self) -> None:
        self.task = asyncio.create_task(_proxy(self._read_recv, self._write_send, self.managed))

    async def send(self, req_id, method: str, params: dict) -> None:
        await self._read_send.send(_request(req_id, method, params))

    async def recv(self, timeout: float = 5.0) -> dict:
        with anyio.fail_after(timeout):
            session_message = await self._write_recv.receive()
        return session_message.message.model_dump(by_alias=True, exclude_unset=True)

    async def close(self) -> None:
        await self._read_send.aclose()
        if self.task is not None:
            with anyio.fail_after(5.0):
                await self.task


@pytest.mark.asyncio
async def test_second_connection_is_not_blocked_by_first_idle_connection():
    """Finding 1: an open-but-idle SSE connection to an `exclusive` server
    must not hold the guard for its whole lifetime. Under the old code
    (`managed.guard.run(guarded_run)` wrapping the entire `connect_sse`/
    `_proxy` call), connection A opening and never sending anything would
    hold the exclusive lock forever, and connection B's request would hang.
    """
    server = await _start_managed("exclusive")
    try:
        conn_a = _Connection(server)
        conn_a.open()  # opens and stays idle -- sends nothing

        conn_b = _Connection(server)
        conn_b.open()
        await conn_b.send(1, "test/echo", {"value": "hello"})
        # Must complete promptly even though A's connection is still open.
        reply = await conn_b.recv(timeout=3.0)
        assert reply["result"] == {"echo": "hello"}

        await conn_a.close()
        await conn_b.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_exclusive_still_serializes_individual_requests():
    """The fix narrows the guard to per-request scope, but exclusive-mode
    servers must still serialize actual request turns: two requests that
    are both in flight at once take roughly as long as the sum of their
    durations, not the max (which would indicate they ran concurrently on
    the one subprocess)."""
    server = await _start_managed("exclusive")
    try:
        conn_a = _Connection(server)
        conn_a.open()
        conn_b = _Connection(server)
        conn_b.open()

        start = time.monotonic()
        await conn_a.send(1, "test/sleep", {"seconds": 0.3})
        await conn_b.send(2, "test/sleep", {"seconds": 0.3})
        reply_a = await conn_a.recv(timeout=5.0)
        reply_b = await conn_b.recv(timeout=5.0)
        elapsed = time.monotonic() - start

        assert reply_a["result"] == {"slept": 0.3}
        assert reply_b["result"] == {"slept": 0.3}
        # Serialized: >= ~0.6s. Generous floor to avoid flakiness while still
        # clearly ruling out concurrent (~0.3s) execution.
        assert elapsed >= 0.5, f"expected serialized (~0.6s), got {elapsed:.2f}s -- requests ran concurrently"

        await conn_a.close()
        await conn_b.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_large_response_does_not_crash_the_reader():
    """Finding 2: asyncio.StreamReader's default line limit is 64 KiB
    (65536 bytes); a JSON-RPC response line past that must not raise
    ValueError out of the subprocess's stdout reader and kill the session."""
    server = await _start_managed("parallel")
    try:
        conn = _Connection(server)
        conn.open()

        big_size = 200_000  # > 65536
        await conn.send(1, "test/big", {"size": big_size})
        reply = await conn.recv(timeout=5.0)
        assert len(reply["result"]["blob"]) == big_size

        await conn.close()
    finally:
        await server.stop()
