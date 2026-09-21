# tests/test_hub_app_proxy.py
"""Regression tests for the Task 6 review findings (round 1 and round 2):

1. The concurrency guard must gate one JSON-RPC request's turn on the
   subprocess, not an entire SSE connection's lifetime. Proven by driving
   `hub_app._proxy` directly against a real subprocess (bypassing the SSE
   transport, which `_proxy` doesn't touch) using anyio memory object streams
   in place of the SSE `read`/`write` streams -- exactly the shape
   `SseServerTransport.connect_sse` yields.
2. A JSON-RPC response line longer than asyncio.StreamReader's default 64 KiB
   limit must not crash the reader. Proven with a synthetic subprocess
   (tests/fixtures/fake_stdio_server.py) that echoes back an oversized line.
3. (Round 2, Critical) A request in flight when the subprocess dies must be
   rejected with an exception, not hang forever -- whether the subprocess
   crashed, was deliberately `stop()`ped, or died between requests with no
   restart -- and for an `exclusive` server, a subsequent request (e.g.
   after a restart) must not be stuck behind a permanently-deadlocked guard.
4. (Round 2, Important) Two concurrently-connected clients on a `parallel`
   server that happen to use the same JSON-RPC id must each get their OWN
   response, not misroute onto each other or hang, even though the hub
   shares one `pending` dict per subprocess across all connections -- and a
   response to an abandoned hub-generated request must never leak to some
   other, unrelated client.
"""
import asyncio
import contextlib
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


def _exception_messages(exc: BaseException) -> list[str]:
    """Flattens an exception (possibly a nested exception group, which is
    how anyio task groups surface a child task's error) into every message
    string it or its sub-exceptions carry, so a test can assert on the
    substance of the failure regardless of how deeply anyio wraps it."""
    msgs = [str(exc)]
    for sub in getattr(exc, "exceptions", ()):
        msgs.extend(_exception_messages(sub))
    return msgs


@pytest.mark.asyncio
async def test_subprocess_death_rejects_pending_request_and_unblocks_exclusive_guard():
    """Round 2, Critical finding: a request in flight when the managed
    subprocess dies (crash, or any other cause of stdout EOF) must be
    rejected with an exception -- not left to hang forever with `await fut`
    inside `write_and_maybe_wait()`. For an `exclusive` server this is also
    what must release the guard's lock (`write_and_maybe_wait` runs directly
    inside `async with self._lock:`), so a SUBSEQUENT request against a
    fresh/restarted server on the same ManagedServer isn't stuck behind a
    permanently-deadlocked guard.
    """
    server = await _start_managed("exclusive")

    conn = _Connection(server)
    conn.open()
    await conn.send(1, "test/die", {})
    # The fake subprocess exits immediately without ever writing a response
    # to this request. The waiting connection must see an exception
    # propagate out of `_proxy`, not hang. `conn.close()` has its own
    # internal 5s `anyio.fail_after` around awaiting the proxy task -- relied
    # on here as the sole timeout (nesting a second `fail_after` at the same
    # deadline around it is unreliable: the two cancel scopes can race, and
    # the outer one can end up swallowing the inner's exception instead of
    # observing it, which is exactly what happened when this test was first
    # written -- caught by manually reverting the fix and finding this
    # version reported a false "test passed" via `DID NOT RAISE`).
    caught: BaseException | None = None
    try:
        await conn.close()
    except BaseException as exc:  # noqa: BLE001 -- intentionally broad, see below
        caught = exc
    assert caught is not None, (
        "expected _proxy to raise once the subprocess died with this "
        "request in flight -- got no exception at all (hang or silent drop)"
    )
    assert any("subprocess exited" in m for m in _exception_messages(caught))

    # Simulate a restart: a fresh subprocess on the SAME ManagedServer, so
    # the SAME ConcurrencyGuard/lock is reused. If the prior request's
    # exception hadn't released the exclusive lock, this would hang forever
    # instead of completing within the timeout below.
    await server.start()
    try:
        conn2 = _Connection(server)
        conn2.open()
        await conn2.send(2, "test/echo", {"value": "still alive"})
        reply = await conn2.recv(timeout=5.0)
        assert reply["result"] == {"echo": "still alive"}
        await conn2.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_request_after_dead_reader_fails_fast_without_a_restart():
    """Round 2, Critical finding, gap closed after advisor review: rejecting
    futures already in `pending` when the reader exits is not enough on its
    own. If a NEW request registers a future in `pending` AFTER the shared
    reader has already run to completion (subprocess dead, no restart), no
    reader is left alive to ever reject it -- `ensure_stdout_reader()` would
    spawn a fresh reader against the same dead process, which hits EOF
    immediately and exits having rejected nothing (because `pending` was
    still empty at that instant). Without the fail-fast check in
    `write_and_maybe_wait()` (before registering anything in `pending`), this
    second request would hang forever -- the exact permanent-deadlock failure
    mode the Critical finding describes, reachable even without ever calling
    `server.start()` again.
    """
    server = await _start_managed("exclusive")
    try:
        conn = _Connection(server)
        conn.open()
        await conn.send(1, "test/die", {})
        try:
            await conn.close()
        except BaseException:
            pass  # already covered by the other test; just get past it here

        # Give the shared reader time to actually observe EOF and finish
        # (it rejects an already-empty `pending` and exits) before the next
        # request tries to register anything -- this is the exact ordering
        # the gap needed: reader fully done, no restart, pending currently
        # empty.
        for _ in range(50):
            if server._reader_task is not None and server._reader_task.done():
                break
            await asyncio.sleep(0.05)
        assert server._reader_task is not None and server._reader_task.done()

        conn2 = _Connection(server)
        conn2.open()  # deliberately NOT server.start() -- same dead process
        await conn2.send(2, "test/echo", {"value": "x"})
        caught: BaseException | None = None
        try:
            await conn2.close()
        except BaseException as exc:
            caught = exc
        assert caught is not None, (
            "a request registered after the reader already exited, with no "
            "restart, must fail fast -- got no exception at all (hang)"
        )
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stop_rejects_a_request_in_flight():
    """Round 2, Critical finding: the review explicitly asked to verify --
    not assume -- that `stop()`'s deliberate termination path rejects a
    request in flight the same way a crash does. `test/sleep` keeps the fake
    subprocess busy (and its response line unwritten) long enough for
    `server.stop()` to terminate it while the request is still outstanding.
    """
    server = await _start_managed("exclusive")
    conn = _Connection(server)
    conn.open()
    await conn.send(1, "test/sleep", {"seconds": 5.0})
    await asyncio.sleep(0.1)  # let the request actually reach the subprocess

    stop_task = asyncio.create_task(server.stop())
    caught: BaseException | None = None
    try:
        await conn.close()
    except BaseException as exc:
        caught = exc
    await stop_task

    assert caught is not None, (
        "expected the in-flight request to be rejected when stop() "
        "terminates the subprocess, got no exception at all (hang)"
    )
    assert any("subprocess exited" in m for m in _exception_messages(caught))


@pytest.mark.asyncio
async def test_colliding_client_ids_on_parallel_server_do_not_misroute():
    """Round 2, Important finding: two independently-connected clients on a
    `parallel` server can legitimately pick the same JSON-RPC id (e.g. both
    starting their own numbering at 1 for `initialize`) while both have a
    request genuinely in flight at once. Each must receive its OWN correct
    response -- not the other's, and not hang -- even though both requests
    are, at the hub-protocol level, id=1 sharing one subprocess's single
    `pending` dict.

    Connection A's request is deliberately slower (`test/sleep`) than B's
    (`test/echo`) so that both registrations are genuinely concurrent in
    `managed.pending` -- widening the window in which the pre-fix code (keyed
    by the raw client id) would have let B's registration overwrite A's,
    per the review's exact described failure mode.
    """
    server = await _start_managed("parallel")
    try:
        conn_a = _Connection(server)
        conn_a.open()
        conn_b = _Connection(server)
        conn_b.open()

        await conn_a.send(1, "test/sleep", {"seconds": 0.3})
        await conn_b.send(1, "test/echo", {"value": "B"})

        reply_a = await conn_a.recv(timeout=5.0)
        reply_b = await conn_b.recv(timeout=5.0)

        # Each client must see ITS OWN client-supplied id (1) echoed back --
        # the hub-generated id used internally must never leak to a client --
        # and ITS OWN result, not the other connection's.
        assert reply_a["id"] == 1
        assert reply_b["id"] == 1
        assert reply_a["result"] == {"slept": 0.3}
        assert reply_b["result"] == {"echo": "B"}

        await conn_a.close()
        await conn_b.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_orphaned_hub_id_response_is_not_broadcast_to_other_clients():
    """Self-review fix (gap an advisor review caught after the two named
    findings were addressed): a response to a hub-generated id whose waiter
    already gave up (its connection was abruptly cancelled/disconnected
    before the response arrived) must be dropped, not broadcast to every
    OTHER currently-connected client via `subscribers`. Broadcasting it would
    leak the internal hub id onto an unrelated client's connection and
    misattribute someone else's abandoned response to it -- which is
    reachable precisely because every id this hub writes to the subprocess
    is now hub-generated (the Important-finding fix), so an unmatched
    response's id is never a legitimate notification/server-request.
    """
    server = await _start_managed("parallel")
    try:
        conn_a = _Connection(server)
        conn_a.open()
        conn_b = _Connection(server)
        conn_b.open()  # bystander: must never receive A's orphaned response

        await conn_a.send(1, "test/sleep", {"seconds": 0.4})
        await asyncio.sleep(0.1)  # let A's request actually register+write
        assert conn_a.task is not None
        conn_a.task.cancel()  # simulate an abrupt disconnect/give-up
        with contextlib.suppress(BaseException):
            await conn_a.task

        # A's real response is still coming from the subprocess (~0.3s left
        # to arrive). B, who never asked for anything, must never see it.
        with pytest.raises(TimeoutError):
            await conn_b.recv(timeout=1.0)

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
