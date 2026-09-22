# MCP Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One shared, long-running Python process that hosts local MCP servers (mariadb, headroom, gitlab, figma-bridge, chrome-real, windows-mcp) and exposes each over HTTP/SSE, so N Claude Code sessions connect as remote clients instead of each spawning its own subprocess tree.

**Architecture:** A Starlette/FastAPI ASGI app built on the official `mcp` Python SDK mounts one SSE endpoint per configured backend; a process manager owns each backend's real subprocess (started once, eagerly, at hub startup) behind a per-server concurrency guard; a JSON config store outside the repo is the single source of truth; a PySide6 GUI talks only to the hub's local management API, never to the config file directly; `import`/`apply` CLI commands move server definitions to and from Claude Code's own `.claude.json`, with `apply --cleanup` able to reclaim memory from already-open sessions after one upfront confirmation.

**Tech Stack:** Python 3.11+, `mcp` (official SDK), Starlette, Uvicorn, PySide6, `httpx` (GUI→hub client), `pytest`, Windows Task Scheduler (`schtasks`) for autostart.

**Spec:** `docs/superpowers/specs/2026-09-21-mcp-hub-design.md`

## Global Constraints

- Config file lives at `%LOCALAPPDATA%\mcp-hub\config.json`, never inside the repo; repo ships only `config.example.json`. (spec: Config schema)
- Hub refuses to bind any host other than `127.0.0.1`/`localhost` unless `hub.authToken` is set in config — enforced at hub startup, not only in the GUI. (spec: Security)
- Every log line (hub process log, per-server ring buffers) redacts the value of any env/config key matching `TOKEN|SECRET|PASS|KEY|AUTH` (case-insensitive) before it is written or returned by the API. (spec: Security)
- `concurrency` is a per-server config field, `"exclusive"` (default) or `"parallel"` — never a hardcoded server-name list. (spec: Config schema)
- Any write to `.claude.json` (`apply`) makes a timestamped backup first and verifies the result parses as JSON before treating the write as successful. (spec: Claude Code integration)
- `apply --cleanup` always excludes the ancestor-process chain of the process running the command itself, and asks exactly one yes/no confirmation before killing anything — never a per-process checklist. (spec: Cleanup)
- GUI never writes `config.json` directly; all mutations go through the hub's management API. (spec: Architecture / GUI)

---

## File Structure

```
mcp-hub/
  pyproject.toml
  README.md
  .gitignore
  config.example.json
  src/mcp_hub/
    __init__.py
    __main__.py          # CLI entry: serve | import | apply subcommands
    config.py            # ConfigStore: load/save/atomic-write, dataclasses, redact()
    manager.py           # ManagedServer, HubManager: subprocess lifecycle, status, log ring buffer
    concurrency.py        # ConcurrencyGuard: per-server exclusive/parallel gating
    hub_app.py            # build_app(): ASGI app, mounts one MCP SSE route per enabled server
    management_api.py     # REST routes the GUI calls: status/start/stop/logs
    claude_config.py      # import_servers(), apply_servers(), backup_file()
    cleanup.py            # find_legacy_processes(), describe_plan(), execute_cleanup()
    gui/
      __init__.py
      app.py               # QApplication entry point (python -m mcp_hub.gui)
      api_client.py         # thin httpx wrapper around the management API
      main_window.py
      server_dialog.py
      log_panel.py
  tests/
    test_config.py
    test_manager.py
    test_concurrency.py
    test_claude_config.py
    test_cleanup.py
  scripts/
    install_task.ps1
    smoke_test.py
```

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `config.example.json`
- Create: `src/mcp_hub/__init__.py`
- Create: `README.md`

**Interfaces:**
- Produces: an installable package `mcp_hub` (editable install via `pip install -e .`), pytest discoverable under `tests/`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "mcp-hub"
version = "0.1.0"
description = "Shared local hub for MCP servers, one process instead of one per Claude Code session"
requires-python = ">=3.11"
dependencies = [
    "mcp>=1.0.0",
    "starlette>=0.37",
    "uvicorn>=0.29",
    "httpx>=0.27",
    "PySide6>=6.6",
    "psutil>=5.9",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `.gitignore`**

```
__pycache__/
*.pyc
.venv/
venv/
*.egg-info/
config.json
.pytest_cache/
```

- [ ] **Step 3: Write `config.example.json`**

```json
{
  "hub": {
    "host": "127.0.0.1",
    "port": 37450,
    "authToken": null,
    "autostart": false
  },
  "servers": {
    "example-server": {
      "enabled": false,
      "command": "npx",
      "args": ["-y", "some-mcp-server"],
      "env": {},
      "concurrency": "exclusive"
    }
  }
}
```

- [ ] **Step 4: Write `src/mcp_hub/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 5: Write `README.md`**

```markdown
# mcp-hub

Runs local MCP servers once, shares them across every Claude Code session via
HTTP/SSE, instead of each session spawning its own copy.

Design: `docs/superpowers/specs/2026-09-21-mcp-hub-design.md`
Plan: `docs/superpowers/plans/2026-09-21-mcp-hub-implementation.md`

## Setup

    python -m venv .venv
    .venv\Scripts\activate
    pip install -e ".[dev]"

Config lives at `%LOCALAPPDATA%\mcp-hub\config.json`, not in this repo.
Copy `config.example.json` there and edit it, or use `python -m mcp_hub import`.
```

- [ ] **Step 6: Create venv and install**

Run: `cd C:\altro\Personale\git\mcp-hub && python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"`
Expected: package installs without errors, `pytest` and `uvicorn` importable.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore config.example.json src/mcp_hub/__init__.py README.md
git commit -m "chore: project scaffolding"
```

---

### Task 2: Retire the SDK multi-mount risk (spike)

This is the open risk flagged in the spec — prove it before building anything
on top of the assumption.

**Files:**
- Create: `scripts/spike_multi_mount.py` (throwaway, deleted or kept as a
  reference at the end of the task depending on outcome — see Step 4)

**Interfaces:**
- Produces: a confirmed pattern for mounting 2+ independent MCP servers
  under one Starlette app, which Task 6 (`hub_app.py`) codifies for real.

- [ ] **Step 1: Write a two-server echo spike**

```python
# scripts/spike_multi_mount.py
"""Spike: can Starlette mount two independent MCP SSE servers under one app?
Run: python scripts/spike_multi_mount.py
Then: curl http://127.0.0.1:37450/echo-a/sse and /echo-b/sse in two terminals,
confirm each gets its own tool list and independent responses.
"""
import anyio
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Mount, Route
import uvicorn


def make_echo_server(name: str) -> Server:
    server = Server(name)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [Tool(name="echo", description="echo back input",
                      inputSchema={"type": "object", "properties": {"text": {"type": "string"}}})]

    @server.call_tool()
    async def call_tool(tool_name: str, arguments: dict) -> list[TextContent]:
        return [TextContent(type="text", text=f"[{name}] {arguments.get('text', '')}")]

    return server


def mount_for(name: str) -> Mount:
    server = make_echo_server(name)
    transport = SseServerTransport(f"/{name}/messages")

    async def handle_sse(request):
        async with transport.connect_sse(request.scope, request.receive, request._send) as (read, write):
            await server.run(read, write, server.create_initialization_options())

    return Mount(f"/{name}", routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages", app=transport.handle_post_message),
    ])


app = Starlette(routes=[mount_for("echo-a"), mount_for("echo-b")])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=37450)
```

- [ ] **Step 2: Run it and verify both mounts work independently**

Run: `.venv\Scripts\python scripts\spike_multi_mount.py` (leave running)

In a second terminal, use any MCP-capable client (or the official SDK's
`mcp.client.sse` helper in a throwaway script) to connect to
`http://127.0.0.1:37450/echo-a/sse` and `.../echo-b/sse` simultaneously, call
`echo` on each with different text, and confirm each mount answers with its
own server name embedded (`[echo-a] ...` / `[echo-b] ...`) and neither
interferes with the other's session state.

Expected: both mounts respond correctly and independently, in the same
process, at the same time.

- [ ] **Step 3: Record the outcome**

If Step 2 passes as expected, this confirms `Mount(name, routes=[Route(sse), Mount(messages)])`
repeated per server is the pattern Task 6 will use. If it does NOT pass
(session bleed between mounts, transport conflicts, etc.), stop and revise
the spec's Architecture section before continuing — this is a load-bearing
assumption for the whole project.

- [ ] **Step 4: Commit the spike as a reference**

```bash
git add scripts/spike_multi_mount.py
git commit -m "spike: confirm multi-mount SSE pattern for the mcp SDK"
```

---

### Task 3: Config store

**Files:**
- Create: `src/mcp_hub/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `CONFIG_PATH: Path` — `%LOCALAPPDATA%\mcp-hub\config.json`
  - `redact(env: dict[str, str]) -> dict[str, str]` — returns a copy with
    values masked (`"***"`) for keys matching
    `re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)`
  - `class ServerConfig` (dataclass): `enabled: bool`, `command: str`,
    `args: list[str]`, `env: dict[str, str]`,
    `concurrency: Literal["exclusive", "parallel"] = "exclusive"`
  - `class HubConfig` (dataclass): `host: str = "127.0.0.1"`, `port: int = 37450`,
    `authToken: str | None = None`, `autostart: bool = False`
  - `class Config` (dataclass): `hub: HubConfig`, `servers: dict[str, ServerConfig]`
  - `load_config(path: Path = CONFIG_PATH) -> Config` — returns a default
    empty `Config` if the file does not exist yet
  - `save_config(config: Config, path: Path = CONFIG_PATH) -> None` — atomic
    write (temp file + `os.replace`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
import json
from pathlib import Path
from mcp_hub.config import Config, HubConfig, ServerConfig, load_config, save_config, redact


def test_redact_masks_secret_like_keys():
    env = {"GITLAB_PERSONAL_ACCESS_TOKEN": "glpat-xxx", "MARIADB_HOST": "db.local", "MARIADB_PASS": "hunter2"}
    out = redact(env)
    assert out["GITLAB_PERSONAL_ACCESS_TOKEN"] == "***"
    assert out["MARIADB_PASS"] == "***"
    assert out["MARIADB_HOST"] == "db.local"


def test_load_config_missing_file_returns_default(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.json")
    assert cfg.hub.host == "127.0.0.1"
    assert cfg.servers == {}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(
        hub=HubConfig(host="127.0.0.1", port=37450, authToken=None, autostart=True),
        servers={"mariadb": ServerConfig(enabled=True, command="npx",
                                          args=["-y", "@oleander/mcp-server-mariadb"],
                                          env={"MARIADB_PASS": "x"}, concurrency="parallel")},
    )
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded == cfg


def test_save_is_atomic_leaves_no_temp_file_on_success(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(hub=HubConfig(), servers={}), path)
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_default_concurrency_is_exclusive():
    sc = ServerConfig(enabled=True, command="npx", args=[], env={})
    assert sc.concurrency == "exclusive"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\pytest tests\test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_hub.config'`

- [ ] **Step 3: Implement `src/mcp_hub/config.py`**

```python
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

CONFIG_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mcp-hub" / "config.json"

_SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)


def redact(env: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if _SECRET_KEY_RE.search(k) else v) for k, v in env.items()}


@dataclass
class ServerConfig:
    enabled: bool
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    concurrency: Literal["exclusive", "parallel"] = "exclusive"


@dataclass
class HubConfig:
    host: str = "127.0.0.1"
    port: int = 37450
    authToken: str | None = None
    autostart: bool = False


@dataclass
class Config:
    hub: HubConfig
    servers: dict[str, ServerConfig]


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        return Config(hub=HubConfig(), servers={})
    raw = json.loads(path.read_text(encoding="utf-8"))
    hub = HubConfig(**raw.get("hub", {}))
    servers = {name: ServerConfig(**sc) for name, sc in raw.get("servers", {}).items()}
    return Config(hub=hub, servers=servers)


def save_config(config: Config, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hub": asdict(config.hub),
        "servers": {name: asdict(sc) for name, sc in config.servers.items()},
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\pytest tests\test_config.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/config.py tests/test_config.py
git commit -m "feat: config store with atomic writes and secret redaction"
```

---

### Task 4: Concurrency guard

Built before the process manager because the manager depends on it directly
(every proxied call goes through a guard).

**Files:**
- Create: `src/mcp_hub/concurrency.py`
- Test: `tests/test_concurrency.py`

**Interfaces:**
- Consumes: `ServerConfig.concurrency` (from Task 3, `"exclusive" | "parallel"`)
- Produces:
  - `class ConcurrencyGuard` — `__init__(self, mode: Literal["exclusive", "parallel"])`
  - `async def run(self, coro_fn: Callable[[], Awaitable[T]]) -> T` — awaits
    `coro_fn()` while holding the guard's lock if `mode == "exclusive"`,
    otherwise awaits it directly with no serialization.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_concurrency.py
import asyncio
import pytest
from mcp_hub.concurrency import ConcurrencyGuard


@pytest.mark.asyncio
async def test_exclusive_serializes_calls():
    guard = ConcurrencyGuard("exclusive")
    order: list[str] = []

    async def slow(tag: str):
        order.append(f"{tag}-start")
        await asyncio.sleep(0.05)
        order.append(f"{tag}-end")
        return tag

    await asyncio.gather(guard.run(lambda: slow("a")), guard.run(lambda: slow("b")))
    # second call must not start until the first has ended
    assert order == ["a-start", "a-end", "b-start", "b-end"] or order == ["b-start", "b-end", "a-start", "a-end"]


@pytest.mark.asyncio
async def test_parallel_does_not_serialize():
    guard = ConcurrencyGuard("parallel")
    order: list[str] = []

    async def slow(tag: str):
        order.append(f"{tag}-start")
        await asyncio.sleep(0.05)
        order.append(f"{tag}-end")
        return tag

    await asyncio.gather(guard.run(lambda: slow("a")), guard.run(lambda: slow("b")))
    # both starts happen before either end, proving no serialization
    assert order[0].endswith("-start") and order[1].endswith("-start")


@pytest.mark.asyncio
async def test_run_returns_the_coroutine_result():
    guard = ConcurrencyGuard("exclusive")
    result = await guard.run(lambda: asyncio.sleep(0, result="value"))
    assert result == "value"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\pytest tests\test_concurrency.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_hub.concurrency'`

- [ ] **Step 3: Implement `src/mcp_hub/concurrency.py`**

```python
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Literal, TypeVar

T = TypeVar("T")


class ConcurrencyGuard:
    def __init__(self, mode: Literal["exclusive", "parallel"]):
        self.mode = mode
        self._lock = asyncio.Lock() if mode == "exclusive" else None

    async def run(self, coro_fn: Callable[[], Awaitable[T]]) -> T:
        if self._lock is None:
            return await coro_fn()
        async with self._lock:
            return await coro_fn()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\pytest tests\test_concurrency.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/concurrency.py tests/test_concurrency.py
git commit -m "feat: per-server concurrency guard (exclusive/parallel)"
```

---

### Task 5: Process manager

**Files:**
- Create: `src/mcp_hub/manager.py`
- Test: `tests/test_manager.py`

**Interfaces:**
- Consumes: `ServerConfig` (Task 3), `ConcurrencyGuard` (Task 4)
- Produces:
  - `class ManagedServer` — fields: `name: str`, `config: ServerConfig`,
    `status: Literal["stopped", "starting", "running", "crashed"]`,
    `process: asyncio.subprocess.Process | None`, `guard: ConcurrencyGuard`,
    `logs: collections.deque[str]` (maxlen 500)
    - `async def start(self) -> None` — spawns the subprocess, sets status
    - `async def stop(self) -> None` — terminates the subprocess, sets status to `"stopped"`
    - `def append_log(self, line: str) -> None` — appends a **redacted** line
      (reuses `mcp_hub.config.redact` logic applied to the line's key=value
      pairs where recognizable, otherwise stores as-is — see Step 3 for the
      exact rule) to `logs`
  - `class HubManager` — `__init__(self, config: Config)`
    - `async def start_all(self) -> None` — starts every `enabled` server
    - `async def stop_all(self) -> None`
    - `def get(self, name: str) -> ManagedServer`
    - `def status_snapshot(self) -> dict[str, str]` — `{name: status}` for every managed server
    - `def upsert(self, name: str, server_config: ServerConfig) -> ManagedServer` —
      creates a new `ManagedServer` (or replaces the existing one) for `name`,
      updates `self.config.servers[name]` to match, returns the
      `ManagedServer`. Used by the management API's create/edit-server route
      (Task 7) so the GUI's add/edit dialog (Task 10) has something real to
      call instead of only mutating local dialog state.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_manager.py
import asyncio
import sys
import pytest
from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.manager import HubManager


def _python_sleep_config(seconds: float = 0.2) -> ServerConfig:
    return ServerConfig(enabled=True, command=sys.executable,
                         args=["-c", f"import time; print('ready'); time.sleep({seconds})"],
                         env={}, concurrency="exclusive")


@pytest.mark.asyncio
async def test_start_all_marks_enabled_servers_running():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config()})
    manager = HubManager(cfg)
    await manager.start_all()
    await asyncio.sleep(0.05)
    assert manager.get("a").status == "running"
    await manager.stop_all()


@pytest.mark.asyncio
async def test_disabled_server_is_never_started():
    disabled = _python_sleep_config()
    disabled.enabled = False
    cfg = Config(hub=HubConfig(), servers={"a": disabled})
    manager = HubManager(cfg)
    await manager.start_all()
    assert manager.get("a").status == "stopped"


@pytest.mark.asyncio
async def test_stop_sets_status_stopped():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config(seconds=5)})
    manager = HubManager(cfg)
    await manager.start_all()
    await manager.get("a").stop()
    assert manager.get("a").status == "stopped"


@pytest.mark.asyncio
async def test_crash_is_detected():
    crashing = ServerConfig(enabled=True, command=sys.executable,
                             args=["-c", "import sys; sys.exit(1)"], env={})
    cfg = Config(hub=HubConfig(), servers={"a": crashing})
    manager = HubManager(cfg)
    await manager.start_all()
    await asyncio.sleep(0.3)
    assert manager.get("a").status == "crashed"


def test_status_snapshot_reflects_all_servers():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config(), "b": _python_sleep_config()})
    manager = HubManager(cfg)
    assert manager.status_snapshot() == {"a": "stopped", "b": "stopped"}


def test_append_log_redacts_secret_like_lines():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config()})
    manager = HubManager(cfg)
    server = manager.get("a")
    server.append_log("GITLAB_PERSONAL_ACCESS_TOKEN=glpat-realsecret")
    assert "glpat-realsecret" not in server.logs[-1]


def test_upsert_adds_a_new_server_to_both_config_and_manager():
    cfg = Config(hub=HubConfig(), servers={})
    manager = HubManager(cfg)
    new_server = _python_sleep_config()
    managed = manager.upsert("b", new_server)
    assert managed.name == "b"
    assert manager.get("b") is managed
    assert cfg.servers["b"] is new_server


def test_upsert_replaces_an_existing_server():
    cfg = Config(hub=HubConfig(), servers={"a": _python_sleep_config()})
    manager = HubManager(cfg)
    replacement = _python_sleep_config(seconds=1)
    manager.upsert("a", replacement)
    assert manager.get("a").config is replacement
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\pytest tests\test_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_hub.manager'`

- [ ] **Step 3: Implement `src/mcp_hub/manager.py`**

```python
from __future__ import annotations

import asyncio
import collections
import os
import re
import shutil
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
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        self.status = "running"
        asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        assert self.process is not None
        if self.process.stdout is not None:
            async for raw in self.process.stdout:
                self.append_log(raw.decode(errors="replace").rstrip())
        code = await self.process.wait()
        if self.status != "stopped":
            self.status = "crashed" if code != 0 else "stopped"

    async def stop(self) -> None:
        # NOTE (found by Task 13's live testing against windows-mcp, a uvx-
        # based server): on Windows, a shim command (npx.cmd/uvx.exe) can
        # exit while a grandchild it spawned (the real server process) keeps
        # running. self.process only ever refers to the shim -- terminate()
        # on it does not touch that grandchild, orphaning a real, potentially
        # desktop-controlling process. The actual implementation (evolved
        # past this sketch through several fix rounds already) must kill the
        # whole process tree, e.g. via psutil.Process(self.process.pid)
        # .children(recursive=True) plus the process itself, not just
        # self.process directly. Read the CURRENT manager.py before editing
        # -- this note describes the requirement, not the exact code to paste.
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\pytest tests\test_manager.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/manager.py tests/test_manager.py
git commit -m "feat: process manager for MCP server subprocesses"
```

---

### Task 6: Hub ASGI app — proven against one real stateless server

This closes rollout phase 1 from the spec: hub core proven against one
stateless server (mariadb), verified against the real stdio server it
replaces.

**Files:**
- Create: `src/mcp_hub/hub_app.py`
- Create: `scripts/smoke_test.py`

**Interfaces:**
- Consumes: `HubManager` (Task 5), `ConcurrencyGuard.run` (Task 4), the
  multi-mount pattern proven in Task 2
- Produces: `def build_app(manager: HubManager) -> Starlette` — one
  `Mount(f"/{name}", ...)` per **enabled** server in `manager.config.servers`,
  proxying each MCP request through `manager.get(name).guard.run(...)` before
  it reaches that server's real subprocess (via stdio, using the SDK's
  stdio client transport pointed at `manager.get(name).process`).

- [ ] **Step 1: Implement `src/mcp_hub/hub_app.py`**

```python
from __future__ import annotations

from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route

from mcp_hub.manager import HubManager


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
        async def guarded_run():
            assert managed.process is not None
            async with transport.connect_sse(request.scope, request.receive, request._send) as (read, write):
                await _proxy(read, write, managed)
        await managed.guard.run(guarded_run)
        return Response()  # avoids a TypeError on client disconnect (Task 2 spike finding)

    return Mount(f"/{name}", routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Mount("/messages/", app=transport.handle_post_message),
    ])


async def _proxy(read, write, managed) -> None:
    """Pump JSON-RPC frames between the SSE client (read/write) and the
    managed subprocess's stdio (managed.process.stdin/stdout)."""
    import anyio

    async def to_process():
        async for message in read:
            managed.process.stdin.write((message.model_dump_json() + "\n").encode())
            await managed.process.stdin.drain()

    async def from_process():
        async for raw in managed.process.stdout:
            await write.send(raw.decode())

    async with anyio.create_task_group() as tg:
        tg.start_soon(to_process)
        tg.start_soon(from_process)


def build_app(manager: HubManager) -> Starlette:
    routes = [_mount_for(name, manager) for name, sc in manager.config.servers.items() if sc.enabled]
    return Starlette(routes=routes)
```

**Resolved by Task 2's spike** (`scripts/spike_multi_mount.py`, commit
`557f99a`; installed SDK is `mcp==2.2.0`): the mounting/routing mechanics
above (`SseServerTransport("/messages/")`, the nested `Mount`, returning
`Response()` from the SSE handler) are exactly what the spike proved works,
copied verbatim from the working spike code — not a guess. The one thing
the spike did NOT exercise is `_proxy` itself: the spike ran an in-process
`mcp.server.lowlevel.Server(name, on_list_tools=..., on_call_tool=...)` and
called `server.run(read, write, ...)` against it, whereas `_proxy` here
bridges `read`/`write` to a **real external subprocess's stdio** instead of
an in-process `Server` object — there is no in-process `Server` in the hub's
real design, `_proxy` is a raw byte/frame pump. Treat `_proxy`'s body as the
one part of this task still needing implementation-time verification: the
`read`/`write` objects yielded by `transport.connect_sse(...)` are the
stream types `mcp.server.sse` provides for this pattern — write against
their actual iteration/`send` API as installed, matching the spike's
demonstrated usage of the same call (`async with transport.connect_sse(...)
as (read, write)`), and confirm with Step 2's smoke test before moving on.

- [ ] **Step 2: Write the manual smoke-test script**

```python
# scripts/smoke_test.py
"""Manual smoke test: compare a tool call through the hub against calling
the same MCP server directly over stdio.

Usage:
    1. Put a real, enabled "mariadb" (or "headroom") entry in
       %LOCALAPPDATA%\\mcp-hub\\config.json
    2. Run: .venv\\Scripts\\python -m mcp_hub serve   (leave running)
    3. In a second terminal: .venv\\Scripts\\python scripts\\smoke_test.py mariadb
"""
import asyncio
import sys
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession


async def call_via_hub(server_name: str, port: int = 37450):
    url = f"http://127.0.0.1:{port}/{server_name}/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"[hub:{server_name}] tools = {[t.name for t in tools.tools]}")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "mariadb"
    asyncio.run(call_via_hub(name))
```

- [ ] **Step 3: Run the smoke test and compare against a direct stdio call**

Run: `.venv\Scripts\python -m mcp_hub serve` (separate terminal, leave running)
Run: `.venv\Scripts\python scripts\smoke_test.py mariadb`
Expected: prints the same tool list `list_tools()` would return if you ran
the `mariadb` server's `command`/`args` directly via stdio (compare against
what the existing `.claude.json` entry currently exposes in a live Claude
Code session, e.g. via the `mcp_server_mariadb__mariadb_query` tool
already visible in this environment). Pick one simple, side-effect-free
tool call and confirm the result matches calling it directly today.

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/hub_app.py scripts/smoke_test.py
git commit -m "feat: hub ASGI app proxying to managed subprocesses via SSE"
```

---

### Task 7: Management API + `serve` CLI command

**Files:**
- Create: `src/mcp_hub/management_api.py`
- Create: `src/mcp_hub/__main__.py`

**Interfaces:**
- Consumes: `HubManager` (Task 5), `build_app` (Task 6), `Config`/`load_config` (Task 3)
- Produces:
  - `def management_routes(manager: HubManager) -> list[Route]` — mounted
    under `/api` by `build_app`:
    - `GET /api/status` → `{"servers": manager.status_snapshot()}`
    - `POST /api/servers/{name}/start` → starts that server, `{"status": "..."}`
    - `POST /api/servers/{name}/stop` → stops that server, `{"status": "stopped"}`
    - `GET /api/servers/{name}/logs` → `{"lines": list(manager.get(name).logs)}`
    - `POST /api/servers/{name}` (body: `enabled`/`command`/`args`/`env`/`concurrency`)
      → `manager.upsert(name, ServerConfig(**body))`, persists via
      `save_config(manager.config)`, starts (or restarts) the server if
      `enabled` is true, returns `{"status": "..."}`
  - `def build_app(manager: HubManager) -> Starlette` (Task 6, **modified**
    here to also mount `management_routes`)
  - `python -m mcp_hub serve` — loads config, refuses to start if
    `hub.host` is not `127.0.0.1`/`localhost` and `hub.authToken` is falsy
    (raises `SystemExit` with a clear message), otherwise starts
    `HubManager`, calls `start_all()`, runs `uvicorn` on `hub.host:hub.port`.

- [ ] **Step 1: Implement `src/mcp_hub/management_api.py`**

```python
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_hub.config import ServerConfig, save_config
from mcp_hub.manager import HubManager


def management_routes(manager: HubManager) -> list[Route]:
    async def status(request: Request) -> JSONResponse:
        return JSONResponse({"servers": manager.status_snapshot()})

    async def start(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        await manager.get(name).start()
        return JSONResponse({"status": manager.get(name).status})

    async def stop(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        await manager.get(name).stop()
        return JSONResponse({"status": manager.get(name).status})

    async def logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        return JSONResponse({"lines": list(manager.get(name).logs)})

    async def upsert(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        body = await request.json()
        server_config = ServerConfig(**body)
        managed = manager.upsert(name, server_config)
        save_config(manager.config)
        if server_config.enabled:
            if managed.process is not None and managed.process.returncode is None:
                await managed.stop()
            await managed.start()
        return JSONResponse({"status": managed.status})

    return [
        Route("/api/status", status, methods=["GET"]),
        Route("/api/servers/{name}/start", start, methods=["POST"]),
        Route("/api/servers/{name}/stop", stop, methods=["POST"]),
        Route("/api/servers/{name}/logs", logs, methods=["GET"]),
        Route("/api/servers/{name}", upsert, methods=["POST"]),
    ]
```

- [ ] **Step 2: Modify `src/mcp_hub/hub_app.py`'s `build_app` to include them**

```python
# in src/mcp_hub/hub_app.py, replace the build_app function:
from mcp_hub.management_api import management_routes


def build_app(manager: HubManager) -> Starlette:
    routes = [_mount_for(name, manager) for name, sc in manager.config.servers.items() if sc.enabled]
    routes += management_routes(manager)
    return Starlette(routes=routes)
```

- [ ] **Step 3: Implement `src/mcp_hub/__main__.py`**

```python
from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from mcp_hub.config import load_config
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager


def cmd_serve(args: argparse.Namespace) -> None:
    config = load_config()
    localhost_names = {"127.0.0.1", "localhost"}
    if config.hub.host not in localhost_names and not config.hub.authToken:
        sys.exit(
            f"Refusing to bind {config.hub.host}: set hub.authToken in config.json "
            "before binding anywhere other than 127.0.0.1/localhost."
        )
    manager = HubManager(config)
    app = build_app(manager)

    async def _run():
        await manager.start_all()
        uvicorn_config = uvicorn.Config(app, host=config.hub.host, port=config.hub.port, log_level="info")
        server = uvicorn.Server(uvicorn_config)
        await server.serve()

    asyncio.run(_run())


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_hub")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Manual verification**

Run: `.venv\Scripts\python -m mcp_hub serve`
Expected: starts without error with a `127.0.0.1` config; `curl http://127.0.0.1:37450/api/status` returns JSON with the configured servers' statuses.

Run with a config edited to `"host": "0.0.0.0"` and `"authToken": null`:
Expected: exits immediately with the refusal message, does not bind.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/management_api.py src/mcp_hub/hub_app.py src/mcp_hub/__main__.py
git commit -m "feat: management API and serve CLI, enforce localhost-or-token bind rule"
```

---

## Phase 2: GUI

### Task 8: GUI API client

**Files:**
- Create: `src/mcp_hub/gui/__init__.py`
- Create: `src/mcp_hub/gui/api_client.py`

**Interfaces:**
- Consumes: the management API routes from Task 7 (`/api/status`,
  `/api/servers/{name}/start|stop|logs`)
- Produces: `class HubApiClient` — `__init__(self, base_url: str)`
  - `def status(self) -> dict[str, str]`
  - `def start(self, name: str) -> str` (returns new status)
  - `def stop(self, name: str) -> str`
  - `def logs(self, name: str) -> list[str]`
  - `def upsert(self, name: str, config: dict) -> str` — posts to
    `POST /api/servers/{name}` (Task 7), returns the resulting status

  All methods synchronous (httpx sync client) — PySide6 event loop
  integration is simpler without asyncio in the GUI process for v1.

- [ ] **Step 1: Write `src/mcp_hub/gui/__init__.py`** (empty file)

- [ ] **Step 2: Implement `src/mcp_hub/gui/api_client.py`**

```python
from __future__ import annotations

import httpx


class HubApiClient:
    def __init__(self, base_url: str = "http://127.0.0.1:37450"):
        self._client = httpx.Client(base_url=base_url, timeout=5.0)

    def status(self) -> dict[str, str]:
        return self._client.get("/api/status").json()["servers"]

    def start(self, name: str) -> str:
        return self._client.post(f"/api/servers/{name}/start").json()["status"]

    def stop(self, name: str) -> str:
        return self._client.post(f"/api/servers/{name}/stop").json()["status"]

    def logs(self, name: str) -> list[str]:
        return self._client.get(f"/api/servers/{name}/logs").json()["lines"]

    def upsert(self, name: str, config: dict) -> str:
        return self._client.post(f"/api/servers/{name}", json=config).json()["status"]
```

- [ ] **Step 3: Manual verification**

Run (with hub already running from Task 7): open a Python REPL in the venv,
`from mcp_hub.gui.api_client import HubApiClient; c = HubApiClient(); print(c.status())`
Expected: prints the same status dict `/api/status` returns via curl.

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/gui/__init__.py src/mcp_hub/gui/api_client.py
git commit -m "feat: GUI HTTP client for the hub management API"
```

---

### Task 9: GUI main window — server list and status

**Files:**
- Create: `src/mcp_hub/gui/main_window.py`
- Create: `src/mcp_hub/gui/app.py`

**Interfaces:**
- Consumes: `HubApiClient` (Task 8)
- Produces: `class MainWindow(QMainWindow)` with a `QTableWidget` server list
  (columns: name, status, concurrency, actions), refreshed on a `QTimer`
  (2s interval) via `HubApiClient.status()`; `def main() -> None` in
  `app.py` as the `python -m mcp_hub.gui` entry point.

- [ ] **Step 1: Implement `src/mcp_hub/gui/main_window.py`**

```python
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QHBoxLayout,
)

from mcp_hub.gui.api_client import HubApiClient

_STATUS_COLOR = {"running": "#2e7d32", "stopped": "#757575", "crashed": "#c62828", "starting": "#f9a825"}


class MainWindow(QMainWindow):
    def __init__(self, client: HubApiClient | None = None):
        super().__init__()
        self.setWindowTitle("mcp-hub")
        self.client = client or HubApiClient()

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Server", "Status", "Concurrency", "Actions"])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.table)
        self.setCentralWidget(central)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def refresh(self) -> None:
        statuses = self.client.status()
        self.table.setRowCount(len(statuses))
        for row, (name, status) in enumerate(sorted(statuses.items())):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            status_item = QTableWidgetItem(status)
            status_item.setForeground(_STATUS_COLOR.get(status, "#000000"))
            self.table.setItem(row, 1, status_item)

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            start_btn = QPushButton("Start")
            stop_btn = QPushButton("Stop")
            start_btn.clicked.connect(lambda _, n=name: self._start(n))
            stop_btn.clicked.connect(lambda _, n=name: self._stop(n))
            actions_layout.addWidget(start_btn)
            actions_layout.addWidget(stop_btn)
            self.table.setCellWidget(row, 3, actions)

    def _start(self, name: str) -> None:
        self.client.start(name)
        self.refresh()

    def _stop(self, name: str) -> None:
        self.client.stop(name)
        self.refresh()
```

- [ ] **Step 2: Implement `src/mcp_hub/gui/app.py`**

```python
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from mcp_hub.gui.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(700, 400)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Wire `python -m mcp_hub.gui`**

Add `src/mcp_hub/gui/__main__.py`:

```python
from mcp_hub.gui.app import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Manual verification**

Run (hub already running): `.venv\Scripts\python -m mcp_hub.gui`
Expected: window opens, table shows configured servers with live status,
Start/Stop buttons change status within 2 seconds.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/gui/main_window.py src/mcp_hub/gui/app.py src/mcp_hub/gui/__main__.py
git commit -m "feat: GUI main window with live server status table"
```

---

### Task 10: GUI add/edit server dialog with credential masking

**Files:**
- Create: `src/mcp_hub/gui/server_dialog.py`
- Modify: `src/mcp_hub/gui/main_window.py` — add an "Add server" button
  opening this dialog

**Interfaces:**
- Consumes: `ServerConfig` shape (Task 3), `_SECRET_KEY_RE` pattern
  (re-declared locally to avoid a GUI→config import of private internals —
  see Step 1)
- Consumes also: `HubApiClient.upsert` (Task 8, `POST /api/servers/{name}`,
  already implemented — the dialog's result is sent there, not left unwired)
- Produces: `class ServerDialog(QDialog)` — `__init__(self, name: str = "", existing: dict | None = None)`,
  `def result_name(self) -> str`, `def result_config(self) -> dict`
  (command/args/env/concurrency/enabled), env rows whose key matches the
  secret pattern render with `QLineEdit.Password` echo mode and a "show"
  checkbox per row.

- [ ] **Step 1: Implement `src/mcp_hub/gui/server_dialog.py`**

```python
from __future__ import annotations

import re

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QComboBox, QPushButton, QVBoxLayout,
    QHBoxLayout, QTableWidget, QTableWidgetItem, QCheckBox, QWidget,
)

_SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)


class ServerDialog(QDialog):
    def __init__(self, name: str = "", existing: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Server")
        existing = existing or {}

        self.name_edit = QLineEdit(name)
        self.command_edit = QLineEdit(existing.get("command", ""))
        self.args_edit = QLineEdit(" ".join(existing.get("args", [])))
        self.concurrency_combo = QComboBox()
        self.concurrency_combo.addItems(["exclusive", "parallel"])
        self.concurrency_combo.setCurrentText(existing.get("concurrency", "exclusive"))

        self.env_table = QTableWidget(0, 3)
        self.env_table.setHorizontalHeaderLabels(["Key", "Value", "Show"])
        for key, value in existing.get("env", {}).items():
            self._add_env_row(key, value)

        add_env_btn = QPushButton("Add env var")
        add_env_btn.clicked.connect(lambda: self._add_env_row("", ""))

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Command", self.command_edit)
        form.addRow("Args (space-separated)", self.args_edit)
        form.addRow("Concurrency", self.concurrency_combo)

        buttons = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(ok_btn)
        buttons.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.env_table)
        layout.addWidget(add_env_btn)
        layout.addLayout(buttons)

    def _add_env_row(self, key: str, value: str) -> None:
        row = self.env_table.rowCount()
        self.env_table.insertRow(row)
        self.env_table.setItem(row, 0, QTableWidgetItem(key))
        value_edit = QLineEdit(value)
        if _SECRET_KEY_RE.search(key):
            value_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.env_table.setCellWidget(row, 1, value_edit)

        show_checkbox = QCheckBox()

        def _toggle(checked: bool, edit=value_edit):
            edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)

        show_checkbox.toggled.connect(_toggle)
        self.env_table.setCellWidget(row, 2, show_checkbox)

    def result_name(self) -> str:
        return self.name_edit.text().strip()

    def result_config(self) -> dict:
        env = {}
        for row in range(self.env_table.rowCount()):
            key = self.env_table.item(row, 0).text()
            value_widget = self.env_table.cellWidget(row, 1)
            if key:
                env[key] = value_widget.text()
        return {
            "command": self.command_edit.text(),
            "args": self.args_edit.text().split(),
            "env": env,
            "concurrency": self.concurrency_combo.currentText(),
            "enabled": True,
        }
```

- [ ] **Step 2: Wire an "Add server" button into `MainWindow`**

```python
# in src/mcp_hub/gui/main_window.py MainWindow.__init__, after self.table setup:
        add_btn = QPushButton("Add server")
        add_btn.clicked.connect(self._add_server)
        layout.addWidget(add_btn)
```

```python
# new method on MainWindow:
    def _add_server(self) -> None:
        from mcp_hub.gui.server_dialog import ServerDialog
        from PySide6.QtWidgets import QMessageBox
        dialog = ServerDialog(parent=self)
        if dialog.exec():
            name = dialog.result_name()
            if not name:
                QMessageBox.warning(self, "Add server", "Name cannot be empty.")
                return
            self.client.upsert(name, dialog.result_config())
            self.refresh()
```

- [ ] **Step 3: Manual verification**

Run: `.venv\Scripts\python -m mcp_hub.gui`
Expected: "Add server" opens the dialog; entering a key matching
`TOKEN`/`PASS`/etc. in the env table masks its value field immediately;
the "Show" checkbox reveals/hides it. Filling in a name and a trivial
command (e.g. `cmd` / `/c echo hi`) and clicking OK makes a new row appear
in the main table within the next refresh, with status moving to
`running` (confirms the `POST /api/servers/{name}` round-trip from Task 7
actually starts it).

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/gui/server_dialog.py src/mcp_hub/gui/main_window.py
git commit -m "feat: add/edit server dialog with credential masking"
```

---

### Task 11: GUI log panel

**Files:**
- Create: `src/mcp_hub/gui/log_panel.py`
- Modify: `src/mcp_hub/gui/main_window.py` — selecting a table row shows its logs

**Interfaces:**
- Consumes: `HubApiClient.logs` (Task 8)
- Produces: `class LogPanel(QWidget)` — `def show_logs(self, name: str, lines: list[str]) -> None`

- [ ] **Step 1: Implement `src/mcp_hub/gui/log_panel.py`**

```python
from __future__ import annotations

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPlainTextEdit


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.label = QLabel("No server selected")
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.text)

    def show_logs(self, name: str, lines: list[str]) -> None:
        self.label.setText(f"Logs: {name}")
        self.text.setPlainText("\n".join(lines))
```

- [ ] **Step 2: Wire it into `MainWindow`**

```python
# in src/mcp_hub/gui/main_window.py, __init__, add near the end (after self.setCentralWidget(central)):
        from mcp_hub.gui.log_panel import LogPanel
        self.log_panel = LogPanel()
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        layout.addWidget(self.log_panel)
```

```python
    def _on_row_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        name = self.table.item(row, 0).text()
        self.log_panel.show_logs(name, self.client.logs(name))
```

- [ ] **Step 3: Manual verification**

Run: `.venv\Scripts\python -m mcp_hub.gui`
Expected: clicking a server row shows its recent log lines in the panel below.

- [ ] **Step 4: Commit**

```bash
git add src/mcp_hub/gui/log_panel.py src/mcp_hub/gui/main_window.py
git commit -m "feat: GUI log panel for the selected server"
```

---

## Phase 3: remaining stateless servers

### Task 12: Add gitlab and figma-bridge

No new code — `hub_app.build_app` (Task 6/7) already mounts every enabled
server generically. This task is config + verification only, per the spec's
rollout order (each server proven individually before moving on).

**Files:**
- Modify: local `%LOCALAPPDATA%\mcp-hub\config.json` only (not committed)

- [ ] **Step 1: Add `gitlab` and `figma-bridge` entries**

Copy the `command`/`args`/`env` from the existing global `.claude.json`
`mcpServers.gitlab` and `mcpServers.figma-bridge` entries into
`%LOCALAPPDATA%\mcp-hub\config.json`, each with `"enabled": true` and
`"concurrency": "parallel"` (both are stateless API wrappers, safe to allow
concurrent calls per the spec's classification).

- [ ] **Step 2: Restart the hub and smoke-test each**

Run: `.venv\Scripts\python -m mcp_hub serve` (restart to pick up new config)
Run: `.venv\Scripts\python scripts\smoke_test.py gitlab`
Run: `.venv\Scripts\python scripts\smoke_test.py figma-bridge`
Expected: each prints a tool list matching what that server exposes when
Claude Code spawns it directly today.

- [ ] **Step 3: No commit** — this task only changes the local, gitignored
config file. Note the verification result in the plan's execution log (if
using subagent-driven-development, this is the task's completion report).

---

## Phase 4: stateful servers with verified concurrency

### Task 13: Add chrome-real and windows-mcp, verify exclusive locking under real concurrent load

**Files:**
- Create: `scripts/concurrency_check.py`
- Modify: local `%LOCALAPPDATA%\mcp-hub\config.json` (not committed)

**Interfaces:**
- Consumes: `ConcurrencyGuard` (Task 4, already unit-tested with synthetic
  coroutines) — this task proves the same guarantee holds for a **real**
  managed subprocess under the hub, not just the abstract guard.

- [ ] **Step 1: Add `chrome-real` and `windows-mcp` entries**

Copy their `command`/`args`/`env` from the global `.claude.json` into the
local hub config, `"enabled": true`, `"concurrency": "exclusive"` (both
control a single shared resource — a browser, the desktop — per the spec).

- [ ] **Step 2: Write a concurrency verification script**

```python
# scripts/concurrency_check.py
"""Issue two overlapping calls to the same exclusive server through the hub
and confirm the second only starts after the first finishes.
Usage: .venv\\Scripts\\python scripts\\concurrency_check.py windows-mcp
"""
import asyncio
import sys
import time
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession


async def timed_call(server_name: str, tag: str, port: int = 37450):
    url = f"http://127.0.0.1:{port}/{server_name}/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            start = time.monotonic()
            await session.list_tools()  # any cheap call is enough to observe ordering
            print(f"{tag}: start={start:.3f} end={time.monotonic():.3f}")


async def main(server_name: str):
    await asyncio.gather(timed_call(server_name, "A"), timed_call(server_name, "B"))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "windows-mcp"))
```

- [ ] **Step 3: Run it and inspect the timing**

Run: `.venv\Scripts\python -m mcp_hub serve` (restart, separate terminal)
Run: `.venv\Scripts\python scripts\concurrency_check.py windows-mcp`
Expected: the printed `start`/`end` intervals for A and B do not overlap —
B's `start` is at or after A's `end` (allowing for network overhead), proving
the `exclusive` guard serializes real hub traffic, not just the synthetic
unit test from Task 4.

- [ ] **Step 4: Commit**

```bash
git add scripts/concurrency_check.py
git commit -m "test: manual concurrency verification script for exclusive servers"
```

---

## Phase 5: autostart

### Task 14: Windows Task Scheduler autostart

**Files:**
- Create: `scripts/install_task.ps1`
- Modify: `src/mcp_hub/gui/main_window.py` — autostart checkbox

**Interfaces:**
- Produces: `scripts/install_task.ps1 -Enable` / `-Disable` (PowerShell
  script, same `schtasks` pattern already used for `ProcessWatcher` on this
  machine — no elevation required); a checkbox in the GUI that shells out to
  this script.

- [ ] **Step 1: Implement `scripts/install_task.ps1`**

```powershell
param(
    [switch]$Enable,
    [switch]$Disable
)

$TaskName = "McpHub"
$PythonExe = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$Action = "$PythonExe -m mcp_hub serve"

if ($Disable) {
    schtasks /Delete /TN $TaskName /F
    Write-Host "Autostart disabled."
    return
}

if ($Enable) {
    schtasks /Create /TN $TaskName /TR "`"$PythonExe`" -m mcp_hub serve" /SC ONLOGON /F
    Write-Host "Autostart enabled: $TaskName will run at logon."
    return
}

Write-Host "Usage: install_task.ps1 -Enable | -Disable"
```

- [ ] **Step 2: Manual verification**

Run: `powershell -File scripts\install_task.ps1 -Enable`
Run: `schtasks /Query /TN McpHub`
Expected: task listed, "Pronta"/Ready, trigger "Ogni volta che l'utente accede" (ONLOGON).
Run: `powershell -File scripts\install_task.ps1 -Disable`
Expected: `schtasks /Query /TN McpHub` now reports the task does not exist.

- [ ] **Step 3: Wire the GUI checkbox**

```python
# in src/mcp_hub/gui/main_window.py, __init__, after add_btn:
        from PySide6.QtWidgets import QCheckBox
        self.autostart_checkbox = QCheckBox("Avvia con Windows")
        self.autostart_checkbox.toggled.connect(self._toggle_autostart)
        layout.addWidget(self.autostart_checkbox)
```

```python
    def _toggle_autostart(self, checked: bool) -> None:
        import subprocess
        from pathlib import Path
        script = Path(__file__).resolve().parents[3] / "scripts" / "install_task.ps1"
        flag = "-Enable" if checked else "-Disable"
        subprocess.run(["powershell", "-File", str(script), flag], check=False)
```

- [ ] **Step 4: Manual verification**

Run: `.venv\Scripts\python -m mcp_hub.gui`
Expected: toggling the "Avvia con Windows" checkbox creates/removes the
`McpHub` scheduled task, confirmed via `schtasks /Query /TN McpHub`.

- [ ] **Step 5: Commit**

```bash
git add scripts/install_task.ps1 src/mcp_hub/gui/main_window.py
git commit -m "feat: Windows Task Scheduler autostart, wired to GUI checkbox"
```

---

## Phase 6: Claude Code integration — import, apply, cleanup

### Task 15: `claude_config.py` — import and apply, with mandatory backup

**Files:**
- Create: `src/mcp_hub/claude_config.py`
- Test: `tests/test_claude_config.py`

**Interfaces:**
- Consumes: `Config`, `ServerConfig`, `HubConfig`, `save_config`, `load_config` (Task 3)
- Produces:
  - `def backup_file(path: Path) -> Path` — copies `path` to
    `path.with_name(f"{path.name}.bak-{timestamp}")`, returns the backup path
  - `def import_servers(claude_config_path: Path, hub_config: Config, project_scope: str | None = None) -> list[str]` —
    reads `mcpServers` from the target file (top-level, or
    `projects[project_scope].mcpServers` when `project_scope` given), adds
    any name not already in `hub_config.servers` as a new `ServerConfig`
    with `enabled=False`, `concurrency="exclusive"`; mutates `hub_config` in
    place; returns the list of newly imported names. **Does not** call
    `save_config` itself — caller decides when to persist.
  - `def apply_servers(claude_config_path: Path, hub_config: Config, only: list[str] | None = None, project_scope: str | None = None) -> list[str]` —
    for every server in `hub_config.servers` that is `enabled` (filtered to
    `only` if given), rewrites its entry in the target file's `mcpServers`
    (or `projects[project_scope].mcpServers`) to
    `{"type": "http", "url": f"http://{hub_config.hub.host}:{hub_config.hub.port}/{name}/sse"}`,
    adding `{"headers": {"Authorization": f"Bearer {hub_config.hub.authToken}"}}`
    when `authToken` is set. Calls `backup_file` first. Writes atomically
    (temp file + `os.replace`). Re-reads and `json.loads`s the written file
    to confirm validity; raises `ValueError` and leaves the backup in place
    if that check fails. Returns the list of migrated names.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_config.py
import json
from pathlib import Path
import pytest
from mcp_hub.config import Config, HubConfig, ServerConfig
from mcp_hub.claude_config import backup_file, import_servers, apply_servers


def _write_claude_json(path: Path, mcp_servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": mcp_servers}), encoding="utf-8")


def test_backup_file_creates_timestamped_copy(tmp_path):
    original = tmp_path / ".claude.json"
    original.write_text('{"a": 1}', encoding="utf-8")
    backup = backup_file(original)
    assert backup.exists()
    assert backup.read_text() == '{"a": 1}'
    assert backup.name.startswith(".claude.json.bak-")


def test_import_adds_new_servers_disabled_by_default(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {
        "mariadb": {"command": "npx", "args": ["-y", "@oleander/mcp-server-mariadb"], "env": {"MARIADB_PASS": "x"}}
    })
    hub_config = Config(hub=HubConfig(), servers={})
    imported = import_servers(claude_json, hub_config)
    assert imported == ["mariadb"]
    assert hub_config.servers["mariadb"].enabled is False
    assert hub_config.servers["mariadb"].concurrency == "exclusive"
    assert hub_config.servers["mariadb"].command == "npx"


def test_import_skips_servers_already_present(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {"mariadb": {"command": "npx", "args": [], "env": {}}})
    hub_config = Config(hub=HubConfig(), servers={
        "mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})
    })
    imported = import_servers(claude_json, hub_config)
    assert imported == []


def test_apply_rewrites_enabled_servers_to_remote_type(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {
        "mariadb": {"command": "npx", "args": ["-y", "@oleander/mcp-server-mariadb"], "env": {}}
    })
    hub_config = Config(
        hub=HubConfig(host="127.0.0.1", port=37450, authToken=None),
        servers={"mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})},
    )
    migrated = apply_servers(claude_json, hub_config)
    assert migrated == ["mariadb"]
    written = json.loads(claude_json.read_text())
    assert written["mcpServers"]["mariadb"] == {
        "type": "http", "url": "http://127.0.0.1:37450/mariadb/sse"
    }


def test_apply_adds_auth_header_when_token_set(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {"mariadb": {"command": "npx", "args": [], "env": {}}})
    hub_config = Config(
        hub=HubConfig(host="0.0.0.0", port=37450, authToken="secret-token"),
        servers={"mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})},
    )
    apply_servers(claude_json, hub_config)
    written = json.loads(claude_json.read_text())
    assert written["mcpServers"]["mariadb"]["headers"]["Authorization"] == "Bearer secret-token"


def test_apply_creates_a_backup_before_writing(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {"mariadb": {"command": "npx", "args": [], "env": {}}})
    hub_config = Config(hub=HubConfig(), servers={"mariadb": ServerConfig(enabled=True, command="npx", args=[], env={})})
    apply_servers(claude_json, hub_config)
    backups = list(tmp_path.glob(".claude.json.bak-*"))
    assert len(backups) == 1


def test_apply_only_touches_servers_in_the_only_list(tmp_path):
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, {
        "mariadb": {"command": "npx", "args": [], "env": {}},
        "headroom": {"command": "headroom.exe", "args": [], "env": {}},
    })
    hub_config = Config(hub=HubConfig(), servers={
        "mariadb": ServerConfig(enabled=True, command="npx", args=[], env={}),
        "headroom": ServerConfig(enabled=True, command="headroom.exe", args=[], env={}),
    })
    migrated = apply_servers(claude_json, hub_config, only=["mariadb"])
    assert migrated == ["mariadb"]
    written = json.loads(claude_json.read_text())
    assert written["mcpServers"]["headroom"]["command"] == "headroom.exe"  # untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\pytest tests\test_claude_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_hub.claude_config'`

- [ ] **Step 3: Implement `src/mcp_hub/claude_config.py`**

```python
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from mcp_hub.config import Config, ServerConfig


def backup_file(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{timestamp}")
    shutil.copy2(path, backup)
    return backup


def _mcp_servers_ref(data: dict, project_scope: str | None) -> dict:
    if project_scope is None:
        return data.setdefault("mcpServers", {})
    return data.setdefault("projects", {}).setdefault(project_scope, {}).setdefault("mcpServers", {})


def import_servers(claude_config_path: Path, hub_config: Config, project_scope: str | None = None) -> list[str]:
    data = json.loads(claude_config_path.read_text(encoding="utf-8"))
    mcp_servers = _mcp_servers_ref(data, project_scope)
    imported = []
    for name, entry in mcp_servers.items():
        if name in hub_config.servers:
            continue
        hub_config.servers[name] = ServerConfig(
            enabled=False,
            command=entry.get("command", ""),
            args=entry.get("args", []),
            env=entry.get("env", {}),
            concurrency="exclusive",
        )
        imported.append(name)
    return imported


def apply_servers(
    claude_config_path: Path,
    hub_config: Config,
    only: list[str] | None = None,
    project_scope: str | None = None,
) -> list[str]:
    backup_file(claude_config_path)
    data = json.loads(claude_config_path.read_text(encoding="utf-8"))
    mcp_servers = _mcp_servers_ref(data, project_scope)

    migrated = []
    for name, server in hub_config.servers.items():
        if not server.enabled:
            continue
        if only is not None and name not in only:
            continue
        remote_entry = {
            "type": "http",
            "url": f"http://{hub_config.hub.host}:{hub_config.hub.port}/{name}/sse",
        }
        if hub_config.hub.authToken:
            remote_entry["headers"] = {"Authorization": f"Bearer {hub_config.hub.authToken}"}
        mcp_servers[name] = remote_entry
        migrated.append(name)

    tmp_path = claude_config_path.with_suffix(claude_config_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, claude_config_path)

    # verify the write is valid JSON before declaring success
    json.loads(claude_config_path.read_text(encoding="utf-8"))
    return migrated
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\pytest tests\test_claude_config.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/claude_config.py tests/test_claude_config.py
git commit -m "feat: import/apply between hub config and Claude Code's config, with backup"
```

---

### Task 16: `cleanup.py` — legacy process reclamation with self-protection

**Files:**
- Create: `src/mcp_hub/cleanup.py`
- Test: `tests/test_cleanup.py`

**Interfaces:**
- Produces:
  - `@dataclass class ProcessInfo`: `pid: int`, `ppid: int`, `name: str`, `command_line: str`
  - `def list_processes() -> list[ProcessInfo]` — real implementation via
    `psutil.process_iter(["pid", "ppid", "name", "cmdline"])`
  - `def ancestor_pids(pid: int, processes: list[ProcessInfo]) -> set[int]` —
    walks the parent chain of `pid` (the calling process) up to 20 hops,
    same bound and logic as `ProcessWatcher.ps1`'s self-protection
  - `def find_legacy_processes(processes: list[ProcessInfo], migrated: dict[str, ServerConfig], self_pid: int) -> list[ProcessInfo]` —
    candidates are any process whose `command_line` contains the `command`
    and every arg of a migrated server's config (substring match per arg),
    **excluding** any process whose pid is in `ancestor_pids(self_pid, processes)`
    or whose ancestor chain reaches a pid in that set
  - `def describe_plan(matches: list[ProcessInfo]) -> str` — one line per
    match: `"{name} (pid {pid}): {command_line}"`, plus a total count, for
    the single upfront confirmation prompt
  - `def execute_cleanup(matches: list[ProcessInfo], kill_fn: Callable[[int], None] | None = None) -> int` —
    calls `kill_fn` (default: `psutil.Process(pid).terminate()`, wrapped to
    swallow `NoSuchProcess`) for every match, returns count actually killed

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cleanup.py
from mcp_hub.config import ServerConfig
from mcp_hub.cleanup import ProcessInfo, ancestor_pids, find_legacy_processes, describe_plan, execute_cleanup


def _p(pid, ppid, name, cmd):
    return ProcessInfo(pid=pid, ppid=ppid, name=name, command_line=cmd)


def test_ancestor_pids_walks_parent_chain():
    processes = [_p(3, 2, "cmd.exe", "cmd"), _p(2, 1, "claude.exe", "claude"), _p(1, 0, "explorer.exe", "explorer")]
    assert ancestor_pids(3, processes) == {2, 1}


def test_find_legacy_processes_matches_by_command_and_args():
    processes = [
        _p(10, 1, "claude.exe", "claude.exe"),
        _p(11, 10, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),
        _p(12, 10, "node.exe", "node unrelated-tool.js"),
    ]
    migrated = {"mariadb": ServerConfig(enabled=True, command="npx", args=["-y", "@oleander/mcp-server-mariadb"], env={})}
    matches = find_legacy_processes(processes, migrated, self_pid=999)
    assert [m.pid for m in matches] == [11]


def test_find_legacy_processes_excludes_self_ancestor_tree():
    processes = [
        _p(20, 1, "claude.exe", "claude.exe"),          # self's own session root
        _p(21, 20, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),  # self's own MCP child - must be excluded
        _p(30, 1, "claude.exe", "claude.exe"),          # another, unrelated session
        _p(31, 30, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),  # must be included
    ]
    migrated = {"mariadb": ServerConfig(enabled=True, command="npx", args=["-y", "@oleander/mcp-server-mariadb"], env={})}
    matches = find_legacy_processes(processes, migrated, self_pid=21)
    assert [m.pid for m in matches] == [31]


def test_describe_plan_lists_each_match():
    matches = [_p(11, 10, "npx.cmd", "npx -y @oleander/mcp-server-mariadb")]
    text = describe_plan(matches)
    assert "11" in text and "mariadb" in text


def test_execute_cleanup_calls_kill_fn_for_each_match_and_counts():
    matches = [_p(11, 10, "npx.cmd", "cmd-a"), _p(12, 10, "npx.cmd", "cmd-b")]
    killed = []
    count = execute_cleanup(matches, kill_fn=killed.append)
    assert killed == [11, 12]
    assert count == 2


def test_find_legacy_processes_does_not_over_protect_via_deep_shared_system_ancestor():
    """Two independent sessions that both eventually trace back to the same
    high-level system ancestor (explorer.exe here) must not cause one
    session's legacy process to protect the other's -- self-protection must
    anchor at the nearest claude.exe, not walk to a shared system root."""
    processes = [
        _p(1, 0, "explorer.exe", "explorer"),
        _p(2, 1, "WindowsTerminal.exe", "wt"),
        _p(3, 2, "cmd.exe", "cmd"),
        _p(40, 3, "claude.exe", "claude"),          # self's session root
        _p(41, 40, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),  # self, must be excluded
        _p(4, 1, "WindowsTerminal.exe", "wt"),
        _p(5, 4, "cmd.exe", "cmd"),
        _p(50, 5, "claude.exe", "claude"),          # another, unrelated session
        _p(51, 50, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),  # must be included
    ]
    migrated = {"mariadb": ServerConfig(enabled=True, command="npx", args=["-y", "@oleander/mcp-server-mariadb"], env={})}
    matches = find_legacy_processes(processes, migrated, self_pid=41)
    assert [m.pid for m in matches] == [51]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\pytest tests\test_cleanup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_hub.cleanup'`

- [ ] **Step 3: Implement `src/mcp_hub/cleanup.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import psutil

from mcp_hub.config import ServerConfig


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    name: str
    command_line: str


def list_processes() -> list[ProcessInfo]:
    result = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        info = proc.info
        cmdline = " ".join(info.get("cmdline") or [])
        result.append(ProcessInfo(pid=info["pid"], ppid=info["ppid"] or 0, name=info["name"] or "", command_line=cmdline))
    return result


def ancestor_pids(pid: int, processes: list[ProcessInfo], max_depth: int = 20) -> set[int]:
    by_pid = {p.pid: p for p in processes}
    result: set[int] = set()
    current = pid
    for _ in range(max_depth):
        proc = by_pid.get(current)
        if proc is None or proc.ppid in (0, current):
            break
        result.add(proc.ppid)
        current = proc.ppid
    return result


def _matches_server(command_line: str, server: ServerConfig) -> bool:
    return server.command in command_line and all(arg in command_line for arg in server.args)


def _descendant_pids(root_pid: int, processes: list[ProcessInfo]) -> set[int]:
    by_parent: dict[int, list[int]] = {}
    for p in processes:
        by_parent.setdefault(p.ppid, []).append(p.pid)
    result: set[int] = set()
    queue = [root_pid]
    while queue:
        cur = queue.pop()
        for child in by_parent.get(cur, []):
            if child not in result:
                result.add(child)
                queue.append(child)
    return result


def find_legacy_processes(
    processes: list[ProcessInfo], migrated: dict[str, ServerConfig], self_pid: int
) -> list[ProcessInfo]:
    by_pid = {p.pid: p for p in processes}
    self_ancestors = ancestor_pids(self_pid, processes)

    # Anchor self-protection at the nearest claude.exe ancestor (self's own
    # session root) instead of walking all the way up self's full ancestor
    # chain. Two independent sessions commonly share a high-level ancestor
    # (explorer.exe, a services host, or an unresolved/off-list pid) well
    # within a 20-hop bound -- protecting based on ANY shared ancestor, at
    # any depth (the original design here, and originally caught by live
    # testing), made every other session's legacy processes look
    # "protected" too. Stopping at the nearest claude.exe keeps the
    # protected set scoped to this specific session.
    session_root = None
    for candidate_pid in (self_pid, *self_ancestors):
        proc = by_pid.get(candidate_pid)
        if proc is not None and proc.name == "claude.exe":
            session_root = candidate_pid
            break

    protected = {self_pid} | self_ancestors
    if session_root is not None:
        protected |= _descendant_pids(session_root, processes)
    # else: no resolvable claude.exe boundary found in self's ancestor
    # chain -- fall back to protecting only self's own direct ancestor
    # chain. This under-protects self's sibling processes in that edge
    # case rather than risk over-protecting an unrelated session.

    matches = []
    for proc in processes:
        if proc.pid in protected:
            continue
        for server in migrated.values():
            if _matches_server(proc.command_line, server):
                matches.append(proc)
                break
    return matches


def describe_plan(matches: list[ProcessInfo]) -> str:
    lines = [f"pid {m.pid} ({m.name}): {m.command_line}" for m in matches]
    lines.append(f"Total: {len(matches)} process(es)")
    return "\n".join(lines)


def execute_cleanup(matches: list[ProcessInfo], kill_fn: Callable[[int], None] | None = None) -> int:
    if kill_fn is None:
        def kill_fn(pid: int) -> None:
            try:
                psutil.Process(pid).terminate()
            except psutil.NoSuchProcess:
                pass
    count = 0
    for match in matches:
        kill_fn(match.pid)
        count += 1
    return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\pytest tests\test_cleanup.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/cleanup.py tests/test_cleanup.py
git commit -m "feat: legacy MCP process cleanup with self-protection"
```

---

### Task 17: Wire `import`/`apply`/`apply --cleanup` into the CLI and GUI

**Files:**
- Modify: `src/mcp_hub/__main__.py` — add `import` and `apply` subcommands
- Modify: `src/mcp_hub/gui/main_window.py` — add Import/Apply buttons with a diff preview dialog

**Interfaces:**
- Consumes: `import_servers`, `apply_servers` (Task 15), `find_legacy_processes`,
  `describe_plan`, `execute_cleanup`, `list_processes` (Task 16)
- Produces:
  - `mcp_hub import --from <path> [--project <scope>]`
  - `mcp_hub apply --to <path> [--project <scope>] [--cleanup] [--yes]`

- [ ] **Step 1: Add the subcommands to `src/mcp_hub/__main__.py`**

```python
# add near the top, alongside existing imports:
import os
from pathlib import Path

from mcp_hub.config import save_config
from mcp_hub.claude_config import import_servers, apply_servers
from mcp_hub.cleanup import list_processes, find_legacy_processes, describe_plan, execute_cleanup


def cmd_import(args: argparse.Namespace) -> None:
    config = load_config()
    imported = import_servers(Path(args.from_path), config, project_scope=args.project)
    save_config(config)
    if imported:
        print(f"Imported {len(imported)} server(s), disabled by default: {', '.join(imported)}")
        print("Enable them in config.json (or the GUI) before starting the hub.")
    else:
        print("Nothing new to import.")


def cmd_apply(args: argparse.Namespace) -> None:
    config = load_config()
    migrated = apply_servers(Path(args.to_path), config, project_scope=args.project)
    print(f"Applied {len(migrated)} server(s) to {args.to_path}: {', '.join(migrated) or '(none)'}")

    if args.cleanup and migrated:
        migrated_configs = {name: config.servers[name] for name in migrated}
        matches = find_legacy_processes(list_processes(), migrated_configs, self_pid=os.getpid())
        if not matches:
            print("No legacy processes found to clean up.")
            return
        print(describe_plan(matches))
        if not args.yes:
            answer = input("Close these processes now? [y/N] ")
            if answer.strip().lower() != "y":
                print("Cleanup skipped.")
                return
        count = execute_cleanup(matches)
        print(f"Closed {count} process(es).")


# in main(), alongside the existing "serve" subparser:
    import_parser = sub.add_parser("import")
    import_parser.add_argument("--from", dest="from_path", required=True)
    import_parser.add_argument("--project", default=None)
    import_parser.set_defaults(func=cmd_import)

    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--to", dest="to_path", required=True)
    apply_parser.add_argument("--project", default=None)
    apply_parser.add_argument("--cleanup", action="store_true")
    apply_parser.add_argument("--yes", action="store_true")
    apply_parser.set_defaults(func=cmd_apply)
```

- [ ] **Step 2: Manual verification of the CLI, against throwaway files**

Run: `copy %USERPROFILE%\.claude.json %TEMP%\claude-test.json` (never touch
the real file in this step)
Run: `.venv\Scripts\python -m mcp_hub import --from %TEMP%\claude-test.json`
Expected: reports newly imported server names, all added `enabled: false`
in `%LOCALAPPDATA%\mcp-hub\config.json`.
Run: `.venv\Scripts\python -m mcp_hub apply --to %TEMP%\claude-test.json`
(after enabling at least one imported server in config.json)
Expected: reports migrated names; `%TEMP%\claude-test.json` now has a
`type: http` entry for that server; a `.bak-<timestamp>` copy exists next to it.

- [ ] **Step 3: Add Import/Apply buttons to the GUI with a diff preview**

```python
# in src/mcp_hub/gui/main_window.py, add near the other buttons in __init__:
        import_btn = QPushButton("Import from Claude Code config")
        apply_btn = QPushButton("Apply to Claude Code config")
        import_btn.clicked.connect(self._import_from_claude)
        apply_btn.clicked.connect(self._apply_to_claude)
        layout.addWidget(import_btn)
        layout.addWidget(apply_btn)
```

```python
    def _import_from_claude(self) -> None:
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(self, "Select .claude.json", filter="*.json")
        if not path:
            return
        from mcp_hub.config import load_config, save_config
        from mcp_hub.claude_config import import_servers
        config = load_config()
        imported = import_servers(Path(path), config)
        save_config(config)
        QMessageBox.information(self, "Import", f"Imported (disabled): {', '.join(imported) or '(none)'}")
        self.refresh()

    def _apply_to_claude(self) -> None:
        from pathlib import Path
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, _ = QFileDialog.getOpenFileName(self, "Select .claude.json", filter="*.json")
        if not path:
            return
        from mcp_hub.config import load_config
        from mcp_hub.claude_config import apply_servers
        config = load_config()
        enabled_names = [n for n, s in config.servers.items() if s.enabled]
        confirm = QMessageBox.question(
            self, "Apply",
            f"This will back up {path} and rewrite these servers to point at the hub:\n"
            + "\n".join(enabled_names)
            + "\n\nContinue?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        migrated = apply_servers(Path(path), config)
        QMessageBox.information(self, "Apply", f"Migrated: {', '.join(migrated) or '(none)'}")
```

Note: the diff preview here lists server names about to be migrated before
the write, satisfying the spec's "diff preview before confirming a write"
requirement at the level of which servers change; it does not render a
line-by-line textual diff of the JSON — add that only if the name list
proves insufficient in practice (YAGNI).

- [ ] **Step 4: Manual verification**

Run: `.venv\Scripts\python -m mcp_hub.gui`
Expected: Import button reads a chosen file, reports imported names, table
does not change (imports are disabled by default so they don't start).
Apply button shows the confirmation dialog listing enabled servers before
writing anything.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_hub/__main__.py src/mcp_hub/gui/main_window.py
git commit -m "feat: import/apply CLI subcommands and GUI wiring with cleanup"
```

---

### Task 18: Go live — migrate the real `.claude.json`

This is the spec's final rollout step: only after every earlier task has
been manually verified working. Not automated — a deliberate, one-time,
human-supervised cutover.

**Files:**
- Modify: `C:\Users\l.balotta\.claude.json` (the real file — outside this repo)

- [ ] **Step 1: Confirm every server intended for migration is enabled and verified**

Check `%LOCALAPPDATA%\mcp-hub\config.json`: `mariadb`, `headroom`, `gitlab`,
`figma-bridge`, `chrome-real`, `windows-mcp` all `enabled: true`, each
already individually smoke-tested in Tasks 6, 12, 13.

- [ ] **Step 2: Start the hub for real, register autostart**

Run: `.venv\Scripts\python -m mcp_hub serve` (or rely on the Task Scheduler
entry from Task 14 if already enabled)

- [ ] **Step 3: Close any currently-open Claude Code sessions you can safely close**

Reduces how many legacy processes Step 4's cleanup needs to touch. Sessions
you can't close right now are handled safely by cleanup's self/ancestor
protection regardless.

- [ ] **Step 4: Run `apply --cleanup` against the real config**

Run: `.venv\Scripts\python -m mcp_hub apply --to C:\Users\l.balotta\.claude.json --cleanup`
Expected: reports migrated server names, a `.claude.json.bak-<timestamp>`
appears next to the real file, `describe_plan` output is reviewed before
confirming, legacy processes matching the migrated servers close (except
the calling session's own).

- [ ] **Step 5: Start a fresh Claude Code session and verify**

Confirm the migrated servers' tools (e.g. a `mariadb` query) still work
identically from the new session, now via the hub, and that
`Task Manager`/`Get-Process` shows one `mariadb` subprocess total instead of
one per session.

- [ ] **Step 6: No repo commit** — this task changes only the real,
un-tracked `.claude.json` and its backup. Record the outcome (which servers
migrated, RAM observed before/after) wherever this plan's execution is being
tracked.
