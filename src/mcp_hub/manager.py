from __future__ import annotations

import asyncio
import collections
import itertools
import json
import os
import random
import re
import shutil
import socket
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

import psutil

from pathlib import Path

from mcp_hub.config import BUILTIN_RIZZO, Config, RizzoSettings, ServerConfig, load_config
from mcp_hub.concurrency import ConcurrencyGuard
from mcp_hub.session import RequestTable

_KV_SECRET_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>\S+)")
_SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)

Status = Literal["stopped", "starting", "running", "restarting", "crashed"]

# Default asyncio.StreamReader line-length limit is 64 KiB (65536), which
# raises ValueError out of readline()/`async for` iteration on any single
# line (one JSON-RPC frame on the subprocess's stdout) longer than that. Real
# MCP responses (e.g. a mariadb query result set) can exceed 64 KiB even
# though the small stats payloads used in earlier manual testing never did.
# 10 MiB is comfortably above any response this hub is expected to proxy.
_STDOUT_LIMIT = 10 * 1024 * 1024

# How long the hub waits for an upstream's answer to its own `initialize`.
# Generous on purpose: `npx`/`uvx` servers may download packages on first run.
HANDSHAKE_TIMEOUT = 120.0

# Automatic restart backoff (hub-proxy-hardening R5): the n-th consecutive
# attempt waits BASE * 2**n seconds, capped, with +/-20% jitter so several
# servers that died together do not all respawn in the same instant.
RESTART_BASE_DELAY = 1.0

# How long a hub ping may stay unanswered before it counts as a failure.
HEALTH_PING_TIMEOUT = 10.0
RESTART_MAX_DELAY = 60.0


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - operator-configured local URL
            return 200 <= resp.status < 300
    except Exception:
        return False


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
    # Set by `stop()` so the watcher tasks can tell a deliberate stop (which
    # ends the process with a non-zero exit code on Windows) from a crash.
    _stopping: bool = field(default=False, init=False, repr=False)
    _health_task: asyncio.Task | None = field(default=None, init=False, repr=False)

    # Which session owns which in-flight request / progress token (see
    # session.py): routes progress and server-initiated requests to the right
    # client instead of broadcasting them to every connection.
    table: RequestTable = field(init=False, repr=False)
    # Hub-owned MCP handshake (hub-proxy-hardening R4): the upstream is
    # initialised once per spawn by the hub itself and the result is cached;
    # clients' own `initialize` is answered from it, never forwarded.
    # `_ready_process` is the subprocess object the handshake completed for, so
    # a respawn (a new object) is detected without any reset bookkeeping.
    initialize_result: dict | None = field(default=None, init=False, repr=False)
    _ready_process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _handshake_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _handshake_for: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    # Auto-restart supervision (see `_schedule_restart`): monotonic times of
    # the restarts inside the current window, the pending backoff task, and
    # the running total shown by the status snapshot.
    _restart_times: collections.deque = field(default_factory=collections.deque, init=False, repr=False)
    _restart_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    restart_count: int = field(default=0, init=False)
    _ping_task: asyncio.Task | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self.guard = ConcurrencyGuard(self.config.concurrency)
        self.table = RequestTable(self.name)

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
                method = obj.get("method")
                if method == "notifications/progress":
                    await self._route_progress(obj)
                    continue
                if method is not None and msg_id is not None:
                    await self._route_server_request(obj)
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

    async def hub_request(self, method: str, params: dict, timeout: float) -> dict:
        """A request the HUB makes of the upstream (handshake, health ping),
        not on behalf of any client: own hub id, no session, no guard.
        Returns the JSON-RPC response object (`result` or `error`); raises
        ConnectionError if the process is gone and TimeoutError on no answer."""
        if self.process is None or self.process.stdin is None or self.process.returncode is not None:
            raise ConnectionError(f"managed server {self.name!r} subprocess is not running")
        self.ensure_stdout_reader()
        hub_id = self.next_request_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[hub_id] = fut
        try:
            try:
                self.process.stdin.write(
                    (json.dumps({"jsonrpc": "2.0", "id": hub_id, "method": method, "params": params}) + "\n").encode("utf-8")
                )
                await self.process.stdin.drain()
            except (ConnectionError, OSError) as exc:
                raise ConnectionError(
                    f"managed server {self.name!r} subprocess is not accepting requests ({exc})"
                ) from exc
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(hub_id, None)
            if fut.done() and not fut.cancelled():
                fut.exception()  # mark retrieved (see hub_app's same note)

    async def ensure_ready(self) -> None:
        """Makes sure the CURRENT subprocess has been initialised by the hub.

        The handshake runs once per spawn, shared by every caller (and
        shielded, so a client that disconnects mid-handshake cannot abort it
        for the others). Raises ConnectionError if the process is not running
        or refuses to initialise; the next call tries again."""
        process = self.process
        if process is None or process.returncode is not None:
            raise ConnectionError(f"managed server {self.name!r} subprocess is not running")
        if self._ready_process is process:
            return
        task = self._handshake_task
        if task is None or task.done() or self._handshake_for is not process:
            self._handshake_for = process
            task = self._handshake_task = asyncio.create_task(self._do_handshake(process))
            # Nobody may be left awaiting it (all callers cancelled): retrieve
            # the exception so asyncio does not log it as never retrieved.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        await asyncio.shield(task)

    async def _do_handshake(self, process: asyncio.subprocess.Process) -> None:
        from mcp_types.version import LATEST_HANDSHAKE_VERSION

        import mcp_hub

        try:
            response = await self.hub_request(
                "initialize",
                {
                    "protocolVersion": LATEST_HANDSHAKE_VERSION,
                    # The hub declares no client capabilities: roots/sampling/
                    # elicitation belong to individual sessions, not to the
                    # one shared connection.
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-hub", "version": mcp_hub.__version__},
                },
                timeout=HANDSHAKE_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise ConnectionError(
                f"managed server {self.name!r} did not answer initialize within {HANDSHAKE_TIMEOUT:g}s"
            ) from exc
        if "result" not in response:
            message = (response.get("error") or {}).get("message", "unknown error")
            raise ConnectionError(f"managed server {self.name!r} refused initialize: {message}")
        if process is not self.process:
            return  # respawned meanwhile: this handshake belongs to a dead process
        await self._write_to_upstream({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialize_result = response["result"]
        self._ready_process = process
        self._start_ping_loop(process)
        self.append_log(f"[hub] initialized {self.name} (protocol {response['result'].get('protocolVersion')})")

    async def _write_to_upstream(self, obj: dict) -> None:
        """Best-effort single-line write to the subprocess's stdin."""
        if self.process is None or self.process.stdin is None or self.process.returncode is not None:
            return
        try:
            self.process.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
        except (ConnectionError, OSError):
            pass  # the process is going away; its reader handles the rest

    async def _route_progress(self, obj: dict) -> None:
        """`notifications/progress` goes only to the session that asked for it,
        with that client's own token restored. A token with no owner belongs to
        a request that already finished: dropped, never broadcast."""
        params = obj.get("params") or {}
        owner = self.table.progress_owner(params.get("progressToken"))
        if owner is None:
            return
        session, original_token = owner
        try:
            await session.deliver({**obj, "params": {**params, "progressToken": original_token}})
        except Exception:
            pass  # that session is mid-teardown; nobody else may see its progress

    async def _route_server_request(self, obj: dict) -> None:
        """A request the upstream makes of its client (roots/list, sampling,
        elicitation, ping). Several sessions share this upstream, so it must
        not be broadcast: the first answer would win and the rest would be
        stray responses. `ping` is answered by the hub itself; anything else
        goes to the session owning the oldest in-flight request, or is refused
        with a JSON-RPC error when no session could plausibly answer."""
        req_id = obj["id"]
        if obj.get("method") == "ping":
            await self._write_to_upstream({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return
        session = self.table.oldest_session()
        if session is not None:
            try:
                await session.deliver(obj)
                return
            except Exception:
                pass  # fall through: refuse it rather than leave the server waiting
        await self._write_to_upstream({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": "no client session available to handle this server request"},
        })

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
        if self.config.is_service:
            await self._start_service()
            return
        # A manual start overrides any pending automatic restart and re-arms
        # the circuit breaker (the operator is deliberately trying again).
        self._cancel_restart()
        self._restart_times.clear()
        self._stopping = False
        await self._spawn()

    async def _spawn(self) -> None:
        """Spawns the MCP subprocess: a manual start or an automatic restart."""
        self.initialize_result = None
        self._ready_process = None
        self._cancel_ping_loop()
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
        try:
            self.process = await asyncio.create_subprocess_exec(
                resolved_command, *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=_STDOUT_LIMIT,
            )
        except OSError as exc:
            self.status = "crashed"
            self.append_log(f"[hub] failed to start {self.name}: {exc}")
            raise
        self.status = "running"
        asyncio.create_task(self._watch(self.process))

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is not None:
            async for raw in process.stderr:
                self.append_log(raw.decode(errors="replace").rstrip())
        code = await process.wait()
        if process is not self.process or self._stopping:
            return  # superseded by a newer spawn, or a deliberate stop
        self.append_log(f"[hub] {self.name} exited with code {code}")
        if self.config.restart == "on-failure":
            self._schedule_restart()
        elif self.status != "stopped":
            self.status = "crashed" if code != 0 else "stopped"

    def _restart_delay(self, attempt: int) -> float:
        """Backoff before the `attempt`-th (0-based) restart in the window."""
        return min(RESTART_MAX_DELAY, RESTART_BASE_DELAY * (2 ** attempt)) * random.uniform(0.8, 1.2)

    def _schedule_restart(self) -> None:
        """An upstream exited without being asked to: respawn it after a
        backoff, unless it has already been restarted `restartMaxAttempts`
        times inside `restartWindow` (circuit open: stays `crashed` until a
        manual start). Attempts older than the window are forgotten, so a
        server that then ran stably gets a fresh budget."""
        now = time.monotonic()
        while self._restart_times and now - self._restart_times[0] > self.config.restartWindow:
            self._restart_times.popleft()
        max_attempts = self.config.restartMaxAttempts
        if len(self._restart_times) >= max_attempts:
            self.status = "crashed"
            self.append_log(
                f"[hub] {self.name} keeps failing ({max_attempts} restarts in "
                f"{self.config.restartWindow:g}s): giving up until started manually"
            )
            return
        attempt = len(self._restart_times)
        delay = self._restart_delay(attempt)
        self._restart_times.append(now)
        self.restart_count += 1
        self.status = "restarting"
        self.append_log(f"[hub] restarting {self.name} in {delay:.1f}s (attempt {attempt + 1}/{max_attempts})")
        self._restart_task = asyncio.create_task(self._restart_after(delay))

    async def _restart_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopping:
            return
        try:
            await self._spawn()
        except Exception as exc:  # noqa: BLE001 - a failed respawn counts as one more failed attempt
            self.append_log(f"[hub] restart of {self.name} failed: {exc}")
            self._schedule_restart()

    def _cancel_restart(self) -> None:
        task, self._restart_task = self._restart_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    # -- services (non-MCP long-running processes) -------------------------

    async def _start_service(self) -> None:
        """Spawns a plain service process (no stdio proxying) and watches it.

        Returns as soon as the process is spawned, with status "starting";
        a background task flips it to "running" once `healthUrl` answers
        (immediately when there is none). Idempotent while already alive.
        Refuses to spawn when the configured port is already taken, so a
        foreign process (or an orphan of a previous run) is never mistaken
        for this service nor killed by a later `stop()`."""
        if self.process is not None and self.process.returncode is None:
            return
        self._stopping = False
        port = self.config.effective_port
        if port is not None and await asyncio.to_thread(_port_in_use, port):
            self.append_log(f"[hub] port {port} is already in use; not starting {self.name}")
            self.status = "crashed"
            return
        self.status = "starting"
        env = {**os.environ, **self.config.env}
        resolved_command = shutil.which(self.config.command) or self.config.command
        try:
            self.process = await asyncio.create_subprocess_exec(
                resolved_command, *self.config.args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.config.cwd or None,
                env=env,
            )
        except OSError as exc:
            self.append_log(f"[hub] failed to start {self.name}: {exc}")
            self.status = "crashed"
            return
        self.append_log(f"[hub] started {self.name} (pid {self.process.pid})")
        asyncio.create_task(self._watch_service(self.process))
        if self.config.healthUrl:
            self._health_task = asyncio.create_task(self._wait_healthy(self.process))
        else:
            self.status = "running"

    async def _watch_service(self, process: asyncio.subprocess.Process) -> None:
        # stdout and stderr are merged into one pipe (log-only, not protocol).
        assert process.stdout is not None
        try:
            async for raw in process.stdout:
                self.append_log(raw.decode(errors="replace").rstrip())
        except ValueError:
            pass  # over-long line; keep waiting for exit
        code = await process.wait()
        if process is self.process and not self._stopping:
            self.append_log(f"[hub] {self.name} exited with code {code}")
            self.status = "crashed" if code != 0 else "stopped"

    async def _wait_healthy(self, process: asyncio.subprocess.Process) -> None:
        url = self.config.healthUrl
        assert url is not None
        deadline = asyncio.get_running_loop().time() + self.config.healthTimeout
        while process.returncode is None and not self._stopping:
            if await asyncio.to_thread(_http_ok, url):
                if process.returncode is None and not self._stopping:
                    self.status = "running"
                    self.append_log(f"[hub] {self.name} is healthy ({url})")
                return
            if asyncio.get_running_loop().time() >= deadline:
                self.append_log(f"[hub] {self.name} not healthy after {self.config.healthTimeout:g}s; stopping it")
                self._health_task = None  # stop() must not cancel this very task
                await self.stop()
                self.status = "crashed"
                return
            await asyncio.sleep(0.5)

    async def _terminate_process_tree(self) -> None:
        """Terminates `self.process` and every descendant (see `stop()` for why
        the whole tree), killing survivors. No status or restart bookkeeping:
        callers decide what the exit means (a deliberate stop, or an upstream
        the hub gave up on because it stopped answering)."""
        if self.process is None or self.process.returncode is not None:
            return
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

    # -- health ping (hub-proxy-hardening R6) ------------------------------

    def _start_ping_loop(self, process: asyncio.subprocess.Process) -> None:
        self._cancel_ping_loop()
        if self.config.healthInterval > 0:
            self._ping_task = asyncio.create_task(self._ping_loop(process))
            self._ping_task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _cancel_ping_loop(self) -> None:
        task, self._ping_task = self._ping_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _ping_loop(self, process: asyncio.subprocess.Process) -> None:
        """Pings the upstream through the hub's own channel (no guard, so a long
        tool call never blocks it). `healthFailures` unanswered pings in a row
        mean the process is alive but not serving: it is terminated, and the
        normal exit handling applies the restart policy.

        A ping is neither sent nor counted while client requests are in flight:
        a busy single-threaded server answers late, which is not evidence that
        it is hung."""
        failures = 0
        while process is self.process and process.returncode is None and not self._stopping:
            await asyncio.sleep(self.config.healthInterval)
            if process is not self.process or process.returncode is not None or self._stopping:
                return
            if len(self.table) > 0:
                continue
            try:
                response = await self.hub_request("ping", {}, timeout=HEALTH_PING_TIMEOUT)
                answered = "result" in response
            except (asyncio.TimeoutError, ConnectionError):
                answered = False
            if answered:
                failures = 0
                continue
            if len(self.table) > 0 or process is not self.process or self._stopping:
                continue  # it became busy (or went away) meanwhile: not a verdict
            failures += 1
            self.append_log(f"[hub] {self.name} did not answer ping ({failures}/{self.config.healthFailures})")
            if failures >= self.config.healthFailures:
                self.append_log(f"[hub] {self.name} is unresponsive: terminating it")
                await self._terminate_process_tree()
                return

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
        self._stopping = True
        self._cancel_restart()
        self._cancel_ping_loop()
        if self._health_task is not None:
            self._health_task.cancel()
            self._health_task = None
        await self._terminate_process_tree()
        self.status = "stopped"
        port = self.config.effective_port
        if self.config.is_service and port is not None:
            # Verify the port is really released (a surviving child would
            # keep it); report instead of silently claiming "stopped".
            for _ in range(20):
                if not await asyncio.to_thread(_port_in_use, port):
                    break
                await asyncio.sleep(0.5)
            else:
                self.append_log(f"[hub] warning: port {port} still in use after stop")


class HubManager:
    def __init__(self, config: Config):
        self.config = config
        self._servers = {
            name: ManagedServer(name, sc) for name, sc in config.servers.items() if name != BUILTIN_RIZZO
        }
        self._sync_builtin()

    def _sync_builtin(self) -> None:
        """(Re)creates the built-in Rizzo Flow service from `config.rizzo`;
        it lives next to the user's servers at runtime but never in
        `config.servers`, so it is never persisted there, exported by `apply`
        or editable through the generic upsert/remove routes."""
        service = self.config.rizzo.to_service()
        if service is None:
            self._servers.pop(BUILTIN_RIZZO, None)
        else:
            self._servers[BUILTIN_RIZZO] = ManagedServer(BUILTIN_RIZZO, service)

    async def apply_rizzo(self, settings: RizzoSettings) -> ManagedServer | None:
        """Applies new built-in Rizzo settings: a running server is stopped
        first (its command line may have changed) and the entry is rebuilt.
        Does not start it; the caller does when `starts_with_hub`."""
        existing = self._servers.get(BUILTIN_RIZZO)
        if existing is not None:
            await existing.stop()
        self.config.rizzo = settings
        self._sync_builtin()
        return self._servers.get(BUILTIN_RIZZO)

    def get(self, name: str) -> ManagedServer:
        return self._servers[name]

    async def start_all(self) -> None:
        for server in self._servers.values():
            if server.config.starts_with_hub:
                await server.start()

    async def stop_all(self) -> None:
        for server in self._servers.values():
            await server.stop()

    def status_snapshot(self) -> dict[str, dict[str, str]]:
        # `concurrency` is included alongside `status` (not a separate
        # endpoint) so the GUI's 2s status poll is the one place both are
        # kept current -- a server's concurrency mode can change via
        # `upsert` (Edit dialog) same as its running status can.
        snapshot: dict[str, dict] = {}
        for name, s in self._servers.items():
            entry: dict = {"status": s.status, "concurrency": s.config.concurrency, "type": s.config.type}
            if name == BUILTIN_RIZZO:
                entry["builtin"] = True  # owned by the GUI's Rizzo Flow tab
            if s.config.is_service and s.config.effective_port is not None:
                entry["port"] = s.config.effective_port
            if s.restart_count:
                entry["restarts"] = s.restart_count
            snapshot[name] = entry
        return snapshot

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
        if name == BUILTIN_RIZZO:
            raise ValueError(f"'{name}' is managed by the Rizzo Flow panel")
        existing = self._servers.get(name)
        if existing is not None:
            await existing.stop()
        self.config.servers[name] = server_config
        self._servers[name] = ManagedServer(name, server_config)
        return self._servers[name]

    async def remove(self, name: str) -> None:
        """Stops (if running) and drops the `ManagedServer` entry for `name`
        from both the live manager and `self.config.servers`. A no-op if
        `name` is unknown -- safe to call idempotently, mirroring `upsert`'s
        existing stop-before-drop safety for a pre-existing entry."""
        if name == BUILTIN_RIZZO:
            raise ValueError(f"'{name}' is managed by the Rizzo Flow panel")
        existing = self._servers.pop(name, None)
        if existing is not None:
            await existing.stop()
        self.config.servers.pop(name, None)

    async def reload_from_disk(self, path: Path) -> tuple[list[str], list[str], list[str]]:
        """Re-reads `config.json` and applies the difference against the
        live manager, for the case config.json was changed by something
        other than this hub's own API (hand-edited, or written by
        `mcp_hub apply`/`import`, which both go straight to the file) --
        the exact gap that left a manually-`enabled`-then-saved server
        404ing over HTTP until the whole hub process was restarted, because
        nothing ever told the running hub anything had changed.

        Added/changed servers go through `upsert` (which stops any old
        process for that name, but -- unlike the `POST /api/servers/{name}`
        route -- does NOT itself start the replacement; this method starts
        it afterward if `enabled`, mirroring what that route does). Servers
        identical to their current live config are deliberately
        left untouched -- `upsert` unconditionally stops+replaces even when
        nothing differs, which would restart every OTHER already-running
        server's subprocess for no reason on every reload. Servers no
        longer present in the file go through `remove`.

        Returns `(added, updated, removed)` name lists (server names caught
        by each case), so the API layer can report what actually changed.
        """
        new_config = load_config(path)
        added: list[str] = []
        updated: list[str] = []
        removed: list[str] = []
        for name, server_config in new_config.servers.items():
            existing = self.config.servers.get(name)
            if existing is None:
                added.append(name)
                await self.upsert(name, server_config)
                if server_config.starts_with_hub:
                    await self.get(name).start()
            elif existing != server_config:
                updated.append(name)
                await self.upsert(name, server_config)
                if server_config.starts_with_hub:
                    await self.get(name).start()
        for name in list(self.config.servers):
            if name not in new_config.servers:
                removed.append(name)
                await self.remove(name)
        if new_config.rizzo != self.config.rizzo:
            updated.append(BUILTIN_RIZZO)
            managed = await self.apply_rizzo(new_config.rizzo)
            if managed is not None and managed.config.starts_with_hub:
                await managed.start()
        return added, updated, removed
