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
5. (Round 3) `ensure_stdout_reader()` is only called once per connection, at
   connection-open, before the per-message loop -- not per message. A SECOND
   message pipelined on the SAME, already-open connection, sent after that
   connection's one shared reader has already exited (subprocess died), must
   not depend solely on `returncode` (updated asynchronously, not guaranteed
   to have flipped yet) to avoid registering an orphaned future.
"""
import asyncio
import contextlib
import importlib.util
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
CONCURRENCY_CHECK_SCRIPT = Path(__file__).parent.parent / "scripts" / "concurrency_check.py"


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


def _notification(method: str, params: dict) -> SessionMessage:
    # No "id" key at all (a real JSON-RPC notification): `write_and_maybe_wait`
    # never registers a future for one (`is_request` requires an id), so
    # sending this doesn't make `handle_message`'s task await anything -- it
    # can't itself raise/fail, which is what keeps a connection's task group
    # alive so a genuinely SECOND message can still reach it afterward.
    obj = {"jsonrpc": "2.0", "method": method, "params": params}
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

    async def send_notification(self, method: str, params: dict) -> None:
        await self._read_send.send(_notification(method, params))

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
async def test_second_message_on_same_open_connection_after_reader_exit_does_not_hang(monkeypatch):
    """Round 3 review finding: `ensure_stdout_reader()` was only called once
    per connection, at connection-open, before the per-message loop -- not
    per message. If the subprocess dies mid-connection, that one shared
    reader observes EOF, rejects whatever was pending at that instant, and
    exits; no new reader is spawned for this SAME, still-open connection. A
    SECOND message pipelined on that same connection then depended entirely
    on `write_and_maybe_wait()`'s `returncode`-based fail-fast check to avoid
    registering an orphaned future -- and `returncode` is only updated once
    asyncio's subprocess transport notices the child exited, asynchronously,
    not guaranteed to have happened yet at the exact instant stdout EOF was
    observed by the reader.

    This drives that exact scenario end-to-end on ONE `_Connection` (per the
    review's explicit instruction not to rely on a brand-new `_proxy()`/
    connection). Message 1 is a *notification* (`test/die`, no `id`), which
    kills the subprocess WITHOUT registering a future of its own --
    `write_and_maybe_wait` never awaits anything for a notification, so its
    task can't itself raise and tear down this connection's task group,
    which is what lets a genuinely second message still reach the SAME
    connection afterward (a request as message 1 would fail and cancel the
    whole group the moment the old reader rejected it, masking the very
    thing this test needs to observe).

    Once the original shared reader has fully exited (polled below), the
    test forces the exact race the finding describes -- reader already gone,
    `returncode` still observably `None` -- by monkeypatching this specific
    subprocess's `returncode` to stay `None`, rather than hoping real OS
    timing happens to land in that window (unreliable: `returncode` can
    already be set for real by the time polling detects the reader is done,
    which would let the test pass for a reason unrelated to the fix under
    test). Writing to the subprocess's stdin is ALSO neutralized (patched to
    a no-op): on this platform, a write to an already-dead stdin pipe was
    independently found (round 2 of this task's fix reports) to fail fast on
    its own too, which would otherwise let this test pass even without the
    round-3 fix, for that unrelated reason, rather than because
    `ensure_stdout_reader()` respawned a reader. With both of those
    short-circuits neutralized, the SECOND message's future can only ever be
    resolved by a live stdout reader -- so this test is a clean, isolated
    check of the one thing the round-3 fix actually changes.
    """
    server = await _start_managed("exclusive")
    try:
        await _drive_second_message_after_reader_exit(server, monkeypatch)
    finally:
        # Undo the `returncode`/stdin patches before cleanup: the subprocess
        # is already dead for real, and `stop()` reading the patched
        # (forced-`None`) `returncode` would try to `terminate()` a process
        # that no longer exists, raising `ProcessLookupError` -- unrelated to
        # this test. `monkeypatch.undo()` is safe to call even if pytest's
        # fixture teardown will also call it.
        monkeypatch.undo()
        await server.stop()


async def _drive_second_message_after_reader_exit(server, monkeypatch) -> None:
    conn = _Connection(server)
    conn.open()

    await conn.send_notification("test/die", {})

    # Wait for the ORIGINAL shared reader (spawned once at connection-open)
    # to fully observe the subprocess's death and exit -- the precondition
    # the finding describes ("no new reader will ever be spawned for this
    # same, still-open connection" until something asks again).
    for _ in range(50):
        if server._reader_task is not None and server._reader_task.done():
            break
        await asyncio.sleep(0.05)
    assert server._reader_task is not None and server._reader_task.done()

    # Force the narrow window deterministically: the reader is provably gone
    # (asserted above), but `returncode` still reads as `None`, exactly as
    # the finding says can happen. This isolates the fix under test (the
    # extra `ensure_stdout_reader()` call) from `write_and_maybe_wait()`'s
    # pre-existing `returncode` check, which must NOT be what saves this
    # request.
    # `returncode` is a read-only property (`self._transport.get_returncode()`)
    # with no setter, so it can't be assigned directly -- patch the
    # underlying transport method it reads instead. This reaches into a
    # CPython-private `asyncio.subprocess.Process._transport` attribute; if a
    # future Python version changes that internal, this specific patch (not
    # the fix under test) is the first thing to check.
    assert server.process is not None
    monkeypatch.setattr(server.process._transport, "get_returncode", lambda: None)

    # Also neutralize the stdin write/drain path (see docstring above): a
    # write to the already-dead subprocess's stdin must not be the thing
    # that saves the second message either -- only a live reader may.
    assert server.process.stdin is not None

    async def _noop_drain() -> None:
        return None

    monkeypatch.setattr(server.process.stdin, "write", lambda data: None)
    monkeypatch.setattr(server.process.stdin, "drain", _noop_drain)

    # Second message, pipelined on the SAME already-open connection -- not a
    # new `_Connection`/`_proxy()` call.
    await conn.send(2, "test/echo", {"value": "still open"})
    await conn._read_send.aclose()  # same as _Connection.close()'s first step

    # NOTE: deliberately NOT using `_Connection.close()`'s own
    # `anyio.fail_after(5.0)` here. Empirically (verified with a standalone
    # repro while writing this test), on this environment, cancelling the
    # coroutine that's doing `await conn.task` -- whether via
    # `anyio.fail_after` or a bare `asyncio.wait_for` without `shield` --
    # also cancels `conn.task` itself, because CPython's
    # `Task.cancel()` forwards the cancellation to whatever future/task it is
    # currently suspended on (`Task._fut_waiter`). That masks a genuine hang
    # as a clean-looking early return instead of a timeout, which is exactly
    # the ambiguity this test exists to rule out. `asyncio.shield(...)`
    # prevents that forwarding, so a real hang in `conn.task` reliably
    # surfaces here as `asyncio.TimeoutError`/`TimeoutError`, not silence.
    start = time.monotonic()
    caught: BaseException | None = None
    try:
        await asyncio.wait_for(asyncio.shield(conn.task), timeout=3.0)
    except BaseException as exc:  # noqa: BLE001 -- see other tests in this file
        caught = exc
    elapsed = time.monotonic() - start

    assert caught is not None, (
        "a second message pipelined on the same already-open connection, "
        "after its reader already exited, must fail fast -- got no "
        "exception at all"
    )
    assert not isinstance(caught, (asyncio.TimeoutError, TimeoutError)), (
        f"expected a fast failure well under the 3s wait, took {elapsed:.2f}s "
        "and only surfaced via the outer timeout -- consistent with the "
        "second message hanging forever (i.e. the original deadlock "
        "reproduced), not with anything actually rejecting it"
    )
    assert elapsed < 2.0, (
        f"expected a fast failure, took {elapsed:.2f}s"
    )
    # With stdin write/drain neutralized, the only remaining way for this to
    # fail fast is a live reader rejecting the freshly-registered future --
    # confirms it wasn't some other unrelated exception.
    assert any("subprocess exited" in m for m in _exception_messages(caught))
    assert conn.task.done()


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


def _load_concurrency_check_module():
    """`scripts/` isn't a package (no `__init__.py`, not installed), so this
    loads `concurrency_check.py` directly from its file path -- reusing its
    actual `_DISPATCH_RE` rather than a hand-copied duplicate, so THIS test
    fails the moment the two files' regex and log-line format drift apart,
    instead of two maintained-separately copies silently agreeing with each
    other while disagreeing with the real log line hub_app.py writes."""
    spec = importlib.util.spec_from_file_location("concurrency_check", CONCURRENCY_CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_hub_dispatch_log_lines_are_parseable_by_concurrency_check():
    """Task 13 follow-up (review finding): `scripts/concurrency_check.py`'s
    proof that an `exclusive` server's dispatch is genuinely serialized
    depends entirely on `write_and_maybe_wait()` (hub_app.py) writing
    `[hub-dispatch] phase=.../id=.../method=.../t=...` lines into
    `managed.logs` in a shape the script's `_DISPATCH_RE` can parse. Nothing
    else couples these two files together -- if hub_app.py's log line format
    ever drifts, the script would silently start reporting "hub-side
    dispatch windows OVERLAP" (or "found 0 windows"), i.e. it would look like
    the concurrency GUARD broke, which is a worse failure mode than the
    misleading-client-timestamps problem this whole fix exists to close.

    Drives one real request through `_proxy` against the fake subprocess and
    asserts `managed.logs` contains a start/done pair for it that the
    script's OWN regex parses out cleanly, with matching ids -- i.e. this
    fails if either side of the contract moves without the other.
    """
    concurrency_check = _load_concurrency_check_module()
    server = await _start_managed("exclusive")
    try:
        conn = _Connection(server)
        conn.open()
        await conn.send(1, "test/echo", {"value": "hello"})
        reply = await conn.recv(timeout=5.0)
        assert reply["result"] == {"echo": "hello"}
        await conn.close()
    finally:
        await server.stop()

    starts, dones = [], []
    for line in server.logs:
        m = concurrency_check._DISPATCH_RE.search(line)
        if not m or m.group("method") != "test/echo":
            continue
        (starts if m.group("phase") == "start" else dones).append(m.group("id"))

    assert starts, (
        "expected a '[hub-dispatch] phase=start ... method=test/echo' line "
        f"parseable by concurrency_check.py's own regex in managed.logs, got: {list(server.logs)}"
    )
    assert dones, (
        "expected a '[hub-dispatch] phase=done ... method=test/echo' line "
        f"parseable by concurrency_check.py's own regex in managed.logs, got: {list(server.logs)}"
    )
    assert len(starts) == 1 and starts == dones, (
        "expected exactly one start/done pair, both reporting the SAME "
        f"hub-generated id for this one request; got starts={starts} dones={dones}"
    )


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
