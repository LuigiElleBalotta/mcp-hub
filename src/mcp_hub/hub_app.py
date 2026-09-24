from __future__ import annotations

import asyncio
import json
import time

from mcp.server.sse import SseServerTransport
from mcp.shared.message import SessionMessage
from mcp_types import jsonrpc_message_adapter
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from mcp_hub.management_api import management_routes
from mcp_hub.manager import HubManager, ManagedServer


def _mount_for(name: str, manager: HubManager) -> Mount:
    managed = manager.get(name)
    # Endpoint is relative to THIS mount's root_path (Starlette sets it from
    # the outer Mount(f"/{name}", ...) below) — do NOT repeat the server name
    # here. Confirmed in Task 2's spike (scripts/spike_multi_mount.py):
    # f"/{name}/messages" doubles the prefix into "/{name}/{name}/messages"
    # and breaks every client POST. Trailing slash matches Mount("/messages/", ...)
    # below and avoids an extra 307 redirect.
    transport = SseServerTransport("/messages/")

    async def handle_sse(request):
        assert managed.process is not None
        # NOTE: no `managed.guard.run(...)` around this whole SSE lifetime.
        # Task 6 review finding: an `exclusive` server's guard must gate one
        # JSON-RPC request's turn talking to the subprocess, not an entire
        # SSE session (sessions are opened once and kept alive for a whole
        # client run). The guard is applied per-request inside `_proxy`
        # instead, so two SSE clients can both connect to an `exclusive`
        # server at once; what serializes is each individual request.
        async with transport.connect_sse(request.scope, request.receive, request._send) as (read, write):
            await _proxy(read, write, managed)
        return Response()  # avoids a TypeError on client disconnect (Task 2 spike finding)

    return Mount(f"/{name}", routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=transport.handle_post_message),
    ])


async def _proxy(read, write, managed: ManagedServer) -> None:
    """Pump JSON-RPC frames between one SSE client (read/write) and the
    managed subprocess's shared stdio (managed.process.stdin/stdout).

    `read`/`write` are the streams `SseServerTransport.connect_sse` yields
    (installed mcp SDK, see mcp.server.sse). They do NOT carry raw JSON or
    plain `JSONRPCMessage` objects: `read` yields `SessionMessage | Exception`
    (an `Exception` when the client sent something that failed to parse —
    those are dropped here rather than forwarded to the subprocess), and
    `write` accepts `SessionMessage` sends.

    Concurrency (Task 6 review, finding 1): `managed.process.stdin`/`stdout`
    is a single pipe shared by every connection currently proxying to this
    server (there is one subprocess per server, not one per client). Reading
    stdout is centralized in `ManagedServer._read_stdout` (one task per
    subprocess, started lazily via `ensure_stdout_reader`) so there is never
    more than one consumer racing to read lines off the shared stream, no
    matter how many SSE connections are open concurrently. That reader
    correlates a JSON-RPC response back to whichever `_proxy` call sent the
    matching request, keyed by a hub-generated id (see `next_request_id` /
    `handle_message` below) rather than the client-supplied one, via
    `managed.pending`, and broadcasts anything else (notifications, or a
    response with no matching in-flight request) to every currently-connected
    client via `managed.subscribers`.

    Each incoming client message is handled by its own task (`handle_message`,
    spawned per message into this connection's task group) rather than
    awaited inline in the read loop, so a client that pipelines multiple
    requests within one SSE session isn't serialized against itself. The
    actual write-to-stdin-and-await-the-response step for each message is
    what goes through `managed.guard.run(...)`: for an `exclusive` server
    that's the one thing that must serialize — one request's turn on the
    subprocess at a time, across ALL connections — not this connection's
    whole lifetime. For a `parallel` server `guard.run` is a no-op passthrough
    (see ConcurrencyGuard), so this reduces to full concurrency as before.
    """
    import anyio

    async def deliver(obj: dict) -> None:
        message = jsonrpc_message_adapter.validate_python(obj)
        await write.send(SessionMessage(message))

    async def handle_message(session_message: SessionMessage) -> None:
        assert managed.process is not None and managed.process.stdin is not None
        payload_text = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
        obj = json.loads(payload_text)
        client_id = obj.get("id")
        # A JSON-RPC request (expects a response) has both an id and a
        # method; a notification has no id; a client's response to a
        # server-initiated request has an id but no method — none of the
        # latter two get a future registered, since nothing will resolve it.
        is_request = client_id is not None and "method" in obj

        # Round-2 review fix (Important finding): the client-supplied `id` is
        # only unique within that one client's own session, not across the
        # multiple SSE connections a `parallel` server allows to share this
        # one subprocess (and `managed.pending`) concurrently. Two
        # independent clients commonly both start numbering at 1, which would
        # otherwise let one client's registration in `managed.pending`
        # silently clobber another's in-flight request. Rewrite to a
        # hub-generated id (unique for this ManagedServer's whole lifetime,
        # see `next_request_id`) before writing to the subprocess, and
        # restore the client's own id on the response before delivering it
        # back — the client expects its own id echoed, per JSON-RPC, and has
        # no knowledge of the hub-generated one.
        hub_id = managed.next_request_id() if is_request else None
        if hub_id is not None:
            obj["id"] = hub_id
            payload_text = json.dumps(obj)

        async def write_and_maybe_wait() -> None:
            # Fail fast if the subprocess is already gone *before* registering
            # anything in `managed.pending`. Without this check there is a gap
            # the Critical-finding fix doesn't otherwise close: if the
            # subprocess died and its stdout reader already ran to completion
            # (rejecting whatever was pending at the time and exiting), a
            # request registered AFTER that point has no live reader left to
            # ever reject its future -- `ensure_stdout_reader()` would spawn a
            # new reader against the same dead process, which hits EOF
            # immediately and exits again having rejected nothing (pending was
            # still empty at that instant), and a write to a dead process's
            # stdin pipe does not reliably raise synchronously. That future
            # would then hang forever, deadlocking an `exclusive` guard again.
            # Checking `returncode` here closes that window: no future is ever
            # registered unless the process was observably still running at
            # registration time.
            if managed.process is None or managed.process.returncode is not None:
                raise ConnectionError(
                    f"managed server {managed.name!r} subprocess is not running"
                )
            # Round-3 review fix: `ensure_stdout_reader()` is otherwise only
            # called once per connection, at connection-open, before this
            # per-message loop even starts. If the subprocess dies mid
            # connection, that ONE shared reader observes EOF, rejects
            # whatever was pending at that instant, and exits -- no new
            # reader is spawned for this still-open connection. A second
            # message pipelined on the SAME connection after that point then
            # depended entirely on the `returncode` check above to avoid
            # registering an orphaned future, but `returncode` is only
            # updated once asyncio's subprocess transport notices the child
            # exited -- not guaranteed to have happened yet at the exact
            # instant stdout EOF was observed by the reader. Calling
            # `ensure_stdout_reader()` again here, immediately before
            # registering anything in `pending`, closes that window
            # regardless of `returncode` timing: it is idempotent (a no-op
            # while a reader is already running), so this is cheap on the
            # hot path, but if the previous reader already ran to completion
            # it spawns a fresh one *before* the future below exists. That
            # new reader, whether it finds the process still alive or
            # already dead (immediate EOF), always ends in `_reject_pending()`
            # once it exits -- and since spawning it and registering the
            # future are both synchronous statements with no `await` between
            # them, the new reader cannot get a turn to run (and reject
            # `pending`) until this coroutine's next `await`, by which time
            # the future is already registered. So the future is always
            # either resolved normally or rejected -- never orphaned.
            managed.ensure_stdout_reader()
            fut: asyncio.Future | None = None
            if hub_id is not None:
                fut = asyncio.get_running_loop().create_future()
                managed.pending[hub_id] = fut
                # Permanent hub-side dispatch timing (Task 13 review finding:
                # scripts/concurrency_check.py's client-observed completion
                # timestamps can appear near-simultaneous for two calls to an
                # `exclusive` server even when dispatch itself was cleanly
                # serialized -- that gap is real SSE response-delivery
                # latency downstream of this point, in the `mcp` SDK's
                # transport, not a guard failure). Logged here, which already
                # runs under `managed.guard`'s lock for an `exclusive` server
                # (`guard.run` wraps this whole coroutine -- see `_proxy`'s
                # docstring above), this line and its matching "done"/"error"
                # line below give a rerunnable, hub-side proof of
                # serialization that is immune to that downstream latency.
                # `id`/`method` let a reader (the script, or a human) pair a
                # start with its outcome and isolate the specific request(s)
                # it cares about from other traffic (initialize,
                # notifications, etc.) that also passes through this same
                # guarded path. `time.monotonic()`, not wall clock: the proof
                # this backs is entirely intra-hub (one window's end vs the
                # next window's start), so cross-process comparability is
                # irrelevant and monotonic is immune to clock steps.
                managed.append_log(
                    f"[hub-dispatch] phase=start id={hub_id} method={obj.get('method')} "
                    f"t={time.monotonic():.6f}"
                )
            # Tracks whether "done" was already logged, so the broad
            # `except Exception` below (covering the stdin write/drain AND
            # the awaited response, not just the latter) can't double-log an
            # "error" after a successful dispatch that only failed later,
            # during `deliver()`'s SSE send -- that's a delivery problem, not
            # a dispatch one, and this timing is specifically NOT about the
            # delivery leg (see comment above `deliver` below).
            dispatch_done_logged = False
            try:
                managed.process.stdin.write((payload_text + "\n").encode("utf-8"))
                await managed.process.stdin.drain()
                if fut is not None:
                    response_obj = await fut
                    # Logged BEFORE `deliver(...)`: `deliver` hands the
                    # response to the SSE transport, which is exactly the
                    # downstream leg this timing is meant to exclude from the
                    # measured dispatch window (see comment above).
                    managed.append_log(
                        f"[hub-dispatch] phase=done id={hub_id} method={obj.get('method')} "
                        f"t={time.monotonic():.6f}"
                    )
                    dispatch_done_logged = True
                    response_obj = {**response_obj, "id": client_id}
                    await deliver(response_obj)
            except Exception:
                # Log the "error" outcome too (not just the happy path): a
                # reader waiting to pair this id's start/outcome lines must
                # not hang or mis-parse if the stdin write/drain itself
                # raises, or the subprocess died, or the guard's wait was
                # otherwise rejected mid-flight (see `_reject_pending`) --
                # any of those leaves `fut` without a "done".
                if fut is not None and not dispatch_done_logged:
                    managed.append_log(
                        f"[hub-dispatch] phase=error id={hub_id} method={obj.get('method')} "
                        f"t={time.monotonic():.6f}"
                    )
                raise
            finally:
                if fut is not None:
                    managed.pending.pop(hub_id, None)

        await managed.guard.run(write_and_maybe_wait)

    managed.subscribers.add(deliver)
    managed.ensure_stdout_reader()
    try:
        async with anyio.create_task_group() as tg:
            async for session_message in read:
                if isinstance(session_message, Exception):
                    continue
                tg.start_soon(handle_message, session_message)
    finally:
        managed.subscribers.discard(deliver)


def build_app(manager: HubManager, shutdown_event: asyncio.Event | None = None) -> Starlette:
    routes = [_mount_for(name, manager) for name, sc in manager.config.servers.items() if sc.enabled]
    routes += management_routes(manager, shutdown_event)
    return Starlette(routes=routes)
