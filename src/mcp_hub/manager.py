from __future__ import annotations

import asyncio
import collections
import os
import re
from dataclasses import dataclass, field
from typing import Literal

from mcp_hub.config import Config, ServerConfig
from mcp_hub.concurrency import ConcurrencyGuard

_KV_SECRET_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>\S+)")
_SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)

Status = Literal["stopped", "starting", "running", "crashed"]


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

    def __post_init__(self):
        self.guard = ConcurrencyGuard(self.config.concurrency)

    def append_log(self, line: str) -> None:
        self.logs.append(_redact_line(line))

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
        self.process = await asyncio.create_subprocess_exec(
            self.config.command, *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
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

    def upsert(self, name: str, server_config: ServerConfig) -> ManagedServer:
        self.config.servers[name] = server_config
        self._servers[name] = ManagedServer(name, server_config)
        return self._servers[name]
