from __future__ import annotations

import asyncio
import collections
import itertools
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

import psutil

from mcp_hub.config import Config, ServerConfig
from mcp_hub.concurrency import ConcurrencyGuard

_KV_SECRET_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>\S+)")
_SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)

Status = Literal["stopped", "starting", "running", "crashed"]

# Default asyncio.StreamReader line-length limit is 64 KiB (65536), which
# raises ValueError out of readline()/`async for` iteration on any single
# line (one JSON-RPC frame on the subprocess's stdout) longer than that. Real
# MCP responses (e.g. a mariadb query result set) can exceed 64 KiB even
# though the small stats payloads used in earlier manual testing never did.
# 10 MiB is comfortably above any response this hub is expected to proxy.
_STDOUT_LIMIT = 10 * 1024 * 1024


def _redact_line(line: str) -> str:
    def _sub(m: re.Match) -> str:
        key, val = m.group("key"), m.group("val")
        return f"{key}=***" if _SECRET_KEY_RE.search(key) else f"{key}={val}"
    return _KV_SECRET_RE.sub(_sub, line)


@dataclass
class ManagedServer:
    name: str
    config: ServerConfig
    status: Status = "stopped"
    process: asyncio.subprocess.Process | None = None
    guard: ConcurrencyGuard = field(init=False)
    logs: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500))
    # `pending`/`subscribers`/`_reader_task` back the single shared stdout
    # reader below (see `ensure_stdout_reader`/`_read_stdout`): exactly one
    # task consumes `process.stdout` per subprocess, no matter how many SSE
    # connections (hub_app._proxy calls) are proxying requests to it
    # concurrently. `pending` correlates an outstanding request's JSON-RPC
    # `id` to the asyncio.Future its caller is awaiting; anything read off
    # stdout that isn't a response to a pending id (a notification, or a
    # server-initiated request) is fanned out to every `subscribers` entry.
    pending: dict[Any, asyncio.Future] = field(default_factory=dict, init=False, repr=False)
    subscribers: set[Callable[[dict], Awaitable[None]]] = field(default_factory=set, init=False, repr=False)
    _reader_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    # Backs `next_request_id()` (round-2 review fix, see below): a hub-owned,
    # monotonically increasing counter scoped to this ManagedServer instance.
    # Never reset (including across a stop()/start() restart reusing the same
    # object), so a value it has ever handed out is never handed out again --
    # that is what rules out a NEW id collision being introduced by the
    # rewriting scheme itself.
    _id_counter: itertools.count = field(default_factory=lambda: itertools.count(1), init=False, repr=False)

    def __post_init__(self):
        self.guard = ConcurrencyGuard(self.config.concurrency)

    def append_log(self, line: str) -> None:
        self.logs.append(_redact_line(line))

    def next_request_id(self) -> str:
        """Returns a hub-generated JSON-RPC request id, unique for the
        lifetime of this ManagedServer, to use in place of a client-supplied
        id when writing a request to the subprocess's shared stdin.

        Round-2 review fix (Important finding): client-supplied JSON-RPC ids
        are only unique *within* one client's own session, not across the
        multiple concurrently-connected clients a `parallel` server allows.
        Two independent MCP ClientSessions commonly both start numbering at
        1 (e.g. `initialize`), so trusting the raw client id as the key into
        the single shared `pending` dict lets one client's registration
        silently overwrite another's, misrouting or hanging a request. The
        hub-generated id here is prefixed with this server's name and drawn
        from a private, ever-incrementing counter, so it is unique both
        across concurrently in-flight requests (the property this fix is
        for) and across the ManagedServer's entire lifetime (a stronger
        guarantee than required, but it is what rules out the rewriting
        scheme introducing a *new* collision between two different real
        subprocess requests: a value this counter has ever produced is never
        produced again, full stop).
        """
        return f"hub:{self.name}:{next(self._id_counter)}"

    def ensure_stdout_reader(self) -> None:
        """Starts the single background task that owns reading this
        subprocess's stdout, if it isn't already running.

        Must be called with the event loop running. Idempotent: safe to call
        once per proxied connection (hub_app._proxy does), since only the
        first call actually starts anything.
        """
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._read_stdout())

    async def _read_stdout(self) -> None:
        """Reads newline-delimited JSON-RPC frames off `process.stdout` and
        either resolves the matching pending request's future (a response)
        or fans the message out to every subscriber (a notification, a
        server-initiated request, or a response with no matching pending
        entry, e.g. arrived after its waiter gave up).

        A JSON-RPC response is distinguished from a request/notification by
        shape, not by tracking directions: it carries an `id` and either
        `result` or `error`, and never a `method`.

        Round-2 review fix (Critical finding): when this loop ends -- for
        ANY reason: the subprocess crashed, `stop()` deliberately terminated
        it, or anything else that closes stdout -- every future still in
        `self.pending` is rejected with an exception in the `finally` below,
        instead of being left to hang forever. `write_and_maybe_wait()` in
        hub_app.py awaits exactly one of these futures directly inside
        `async with self._lock:` (via `ConcurrencyGuard.run`), so making the
        future raise, by itself, also releases an `exclusive` server's guard
        for the next caller: `async with` releases its lock on any exception
        propagating out of the body, not only on clean return -- no separate
        lock-release logic is needed. `stop()` goes through this same path
        for free: `terminate()`/`kill()` ends the subprocess, which closes
        its stdout, which ends this loop, which lands here -- there is no
        separate "deliberate stop" rejection path to keep in sync.
        """
        assert self.process is not None and self.process.stdout is not None
        try:
            async for raw in self.process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_id = obj.get("id")
                is_response = msg_id is not None and "method" not in obj and ("result" in obj or "error" in obj)
                fut = self.pending.get(msg_id) if is_response else None
                if fut is not None and not fut.done():
                    fut.set_result(obj)
                    continue
                if is_response and isinstance(msg_id, str) and msg_id.startswith("hub:"):
                    # A response shaped for a hub-generated id (see
                    # `next_request_id`) that has no matching `pending` entry
                    # is, by construction, a response to a request whose
                    # waiter already gave up (e.g. its connection disconnected
                    # before the reply arrived) -- every id this hub ever
                    # writes to the subprocess is hub-generated, so this
                    # cannot be a legitimate unsolicited message. It must be
                    # dropped, not broadcast: `subscribers` fan-out is for
                    # every OTHER currently-connected client too, and
                    # forwarding this would leak the internal hub id onto an
                    # unrelated client's connection and misattribute someone
                    # else's abandoned response to it.
                    continue
                for sub in list(self.subscribers):
                    try:
                        await sub(obj)
                    except Exception:
                        # One dead/misbehaving subscriber (e.g. a connection
                        # that's mid-teardown) must not stop delivery to the
                        # others, nor kill this shared reader task.
                        pass
        finally:
            self._reject_pending()

    def _reject_pending(self) -> None:
        """Rejects every future still waiting in `self.pending` (subprocess
        gone, so nothing will ever resolve them) and clears the dict. Safe to
        call even if `self.pending` is empty. See `_read_stdout`'s docstring
        for why this is sufficient to also unblock an `exclusive` guard."""
        pending, self.pending = self.pending, {}
        if not pending:
            return
        exc = ConnectionError(
            f"managed server {self.name!r} subprocess exited while this request was in flight"
        )
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    async def start(self) -> None:
        self.status = "starting"
        # Merge with the parent's environment rather than replacing it: several
        # real servers (mariadb, gitlab, ...) run via `npx`/`uvx`, which need
        # PATH to resolve at all. A bare `self.config.env` would silently drop
        # PATH the moment any server sets custom env vars.
        env = {**os.environ, **self.config.env}
        # stdin must be piped: the hub proxies MCP requests to this process
        # over stdio (see hub_app._proxy), so there must be a write channel.
        # stderr must be its own pipe, NOT merged into stdout (stderr=STDOUT):
        # the MCP stdio protocol requires stdout to carry ONLY newline-delimited
        # JSON-RPC frames -- any stderr line merged in would corrupt framing.
        # Diagnostic/log output belongs on stderr, which is what a well-behaved
        # MCP stdio server uses for it; that's what _watch() below now reads.
        # limit=: asyncio.StreamReader's default is 65536 (64 KiB) and raises
        # ValueError out of readline()/iteration for any single line past it.
        # One JSON-RPC frame is one line on this wire format, and a real MCP
        # response (e.g. a mariadb result set) can exceed 64 KiB even though
        # small stats payloads used in earlier manual testing never did.
        # Resolve via PATH (and, on Windows, PATHEXT: .cmd/.bat/.exe) ourselves.
        # asyncio.create_subprocess_exec goes straight to CreateProcess on
        # Windows, which does NOT do PATHEXT probing the way cmd.exe does --
        # a bare "npx" (the real shim is "npx.cmd") raises FileNotFoundError,
        # blocking start_all() -- and thus the whole hub -- for every server
        # except the one (headroom) that happens to ship a real .exe. Falls
        # back to the original string if not found, so a genuinely bad
        # command still fails the same way it did before.
        resolved_command = shutil.which(self.config.command) or self.config.command
        self.process = await asyncio.create_subprocess_exec(
            resolved_command, *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_STDOUT_LIMIT,
        )
        self.status = "running"
        asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        assert self.process is not None
        if self.process.stderr is not None:
            async for raw in self.process.stderr:
                self.append_log(raw.decode(errors="replace").rstrip())
        code = await self.process.wait()
        if self.status != "stopped":
            self.status = "crashed" if code != 0 else "stopped"

    async def stop(self) -> None:
        """Stops this server's subprocess AND every descendant it has spawned.

        Task 13 live-verification fix (Critical finding): on Windows, some
        commands are shims -- `npx.cmd`/`uvx.exe` -- that spawn a real
        grandchild process (the actual MCP server) and stay alive themselves
        as the parent. Terminating only `self.process` (the shim) does NOT
        cascade to that grandchild -- Windows has no implicit process-group
        kill the way POSIX SIGTERM-to-a-group can provide. Confirmed live:
        stopping `windows-mcp` (launched via `uvx`) through the hub left a
        real, orphaned `windows-mcp.exe` running every time, across 3
        separate test runs. `gitlab`/`figma-bridge` (npx-based) happened not
        to show this because their node.exe children self-terminate cleanly
        on stdin EOF -- incidental, not a property of `stop()` that should be
        relied on for any other shim-spawning command.

        Fix: walk `self.process`'s full descendant tree via psutil BEFORE
        terminating the shim (a process that's already dead enumerates no
        children), terminate every descendant, terminate the shim itself,
        then confirm the descendants are actually gone and `.kill()` any
        survivor that ignored terminate().

        This only covers descendants that are still alive when `stop()`
        runs and rooted at a `self.process` that is still running -- a shim
        that has already exited before `stop()` is called cannot be walked
        (a dead process has no enumerable children) and is out of scope for
        this fix; closing that gap would need a Windows Job Object with
        `KILL_ON_JOB_CLOSE` assigned at spawn time, which is a bigger change
        than this bug warrants.

        psutil calls are wrapped broadly (`psutil.Error`, not just
        `NoSuchProcess`): `AccessDenied` is also possible on Windows and must
        not propagate out of `stop()` -- `stop_all()` calls this in a loop
        and one uncaught exception here would abort shutdown for every
        OTHER managed server too (the same bug class Task 7's serve()
        try/finally already exists to prevent).

        `psutil.wait_procs` blocks synchronously for up to its timeout; it's
        offloaded via `asyncio.to_thread` so it doesn't stall the event loop
        (and, transitively, this server's `_read_stdout` reader task/
        `_reject_pending` finally, and every other managed server's own
        stop() in a sequential `stop_all()`).
        """
        if self.process is not None and self.process.returncode is None:
            children: list[psutil.Process] = []
            try:
                children = psutil.Process(self.process.pid).children(recursive=True)
            except psutil.Error:
                children = []
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            if children:
                _gone, alive = await asyncio.to_thread(psutil.wait_procs, children, 5)
                for child in alive:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
        self.status = "stopped"


class HubManager:
    def __init__(self, config: Config):
        self.config = config
        self._servers = {name: ManagedServer(name, sc) for name, sc in config.servers.items()}

    def get(self, name: str) -> ManagedServer:
        return self._servers[name]

    async def start_all(self) -> None:
        for server in self._servers.values():
            if server.config.enabled:
                await server.start()

    async def stop_all(self) -> None:
        for server in self._servers.values():
            await server.stop()

    def status_snapshot(self) -> dict[str, str]:
        return {name: s.status for name, s in self._servers.items()}

    async def upsert(self, name: str, server_config: ServerConfig) -> ManagedServer:
        """Replaces (or creates) the `ManagedServer` entry for `name`.

        Round-2 review fix (Important finding): if a `ManagedServer` already
        exists for this name, its process -- if one is running -- is stopped
        BEFORE the entry is replaced. Previously the old `ManagedServer` (and
        any live subprocess it owned) was simply dropped from `_servers` with
        nothing ever calling `stop()` on it, orphaning the subprocess every
        time an already-running server was re-upserted. `ManagedServer.stop()`
        is a no-op when there's nothing running, so this is safe to call
        unconditionally for any pre-existing entry.
        """
        existing = self._servers.get(name)
        if existing is not None:
            await existing.stop()
        self.config.servers[name] = server_config
        self._servers[name] = ManagedServer(name, server_config)
        return self._servers[name]
