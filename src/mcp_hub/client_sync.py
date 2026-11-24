"""Writing the hub's endpoints into AI clients' MCP config files, with a
preview, a backup and a rollback (hub-gui-and-clients R3).

Adapters exist only for clients whose file path and format were CONFIRMED (see
the spike in .sdd/specs/hub-gui-and-clients/tasks.md): Claude Code, Cursor and
VS Code. Each write is planned first (`plan_sync`, nothing touched), shown as a
diff, then applied atomically after a timestamped backup. A file that does not
parse as plain JSON is never written: better to say so than to guess.

Only entries named like a hub server are written; every other entry in the
file is left exactly as it is.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mcp_hub.claude_config import backup_file
from mcp_hub.config import Config, HubConfig

_SECRET_VALUE_RE = re.compile(r'("[^"\n]*(?:TOKEN|SECRET|PASS|KEY|AUTH)[^"\n]*"\s*:\s*)"[^"\n]*"', re.IGNORECASE)


def mask_secrets(text: str) -> str:
    """`"Authorization": "Bearer abc"` -> `"Authorization": "***"` (and the same for any
    key naming a token, secret, password, key or auth). For DISPLAY only: the diff
    of a file shows its neighbours' env values too, and the hub's own token."""
    return _SECRET_VALUE_RE.sub(r'\1"***"', text)


def hub_base_url(hub: HubConfig) -> str:
    host = "127.0.0.1" if hub.host in ("0.0.0.0", "::") else hub.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{hub.port}"


def _url(hub: HubConfig, name: str) -> str:
    return f"{hub_base_url(hub)}/{name}/{'mcp' if hub.preferredTransport == 'http' else 'sse'}"


def _headers(hub: HubConfig) -> dict:
    return {"headers": {"Authorization": f"Bearer {hub.authToken}"}} if hub.authToken else {}


def _claude_entry(hub: HubConfig, name: str) -> dict:
    # What `apply` has always written: type "http" (clients fall back to SSE on /sse).
    return {"type": "http", "url": _url(hub, name), **_headers(hub)}


def _cursor_entry(hub: HubConfig, name: str) -> dict:
    return {"url": _url(hub, name), **_headers(hub)}


def _vscode_entry(hub: HubConfig, name: str) -> dict:
    return {"type": "http" if hub.preferredTransport == "http" else "sse", "url": _url(hub, name), **_headers(hub)}


@dataclass(frozen=True)
class ClientAdapter:
    id: str
    label: str
    key: str  # the top-level object holding the servers
    entry: Callable[[HubConfig, str], dict]
    user_path: Callable[[], Path | None]
    project_path: Callable[[Path], Path]
    # a directory whose presence says the client is installed even before it has a config file
    marker: Callable[[], Path | None] = lambda: None


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _vscode_user_path() -> Path | None:
    # Confirmed on Windows only (the file exists there); other systems are not shipped
    # until their path is confirmed too.
    if sys.platform != "win32" or not os.environ.get("APPDATA"):
        return None
    return Path(os.environ["APPDATA"]) / "Code" / "User" / "mcp.json"


ADAPTERS: dict[str, ClientAdapter] = {
    "claude": ClientAdapter(
        "claude", "Claude Code", "mcpServers", _claude_entry,
        user_path=lambda: _home() / ".claude.json",
        project_path=lambda d: d / ".mcp.json",
    ),
    "cursor": ClientAdapter(
        "cursor", "Cursor", "mcpServers", _cursor_entry,
        user_path=lambda: _home() / ".cursor" / "mcp.json",
        project_path=lambda d: d / ".cursor" / "mcp.json",
        marker=lambda: _home() / ".cursor",
    ),
    "vscode": ClientAdapter(
        "vscode", "VS Code", "servers", _vscode_entry,
        user_path=_vscode_user_path,
        project_path=lambda d: d / ".vscode" / "mcp.json",
        marker=lambda: (_vscode_user_path() or Path("/nonexistent")).parent,
    ),
}


@dataclass
class Detected:
    adapter: ClientAdapter
    path: Path
    exists: bool
    installed: bool


def detect_clients() -> list[Detected]:
    """User-level config files of the supported clients that are present here.
    A client counts as installed if its file exists, or its settings folder does."""
    found = []
    for adapter in ADAPTERS.values():
        path = adapter.user_path()
        if path is None:
            continue
        marker = adapter.marker()
        installed = path.exists() or (marker is not None and marker.exists())
        if installed:
            found.append(Detected(adapter, path, path.exists(), True))
    return found


@dataclass
class Plan:
    adapter: ClientAdapter
    path: Path
    exists: bool
    before: str = ""
    after: str = ""
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)  # a hub entry brought up to date
    replaced_foreign: list[str] = field(default_factory=list)  # a same-named NON-hub entry replaced
    unchanged: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.error is None and self.before != self.after

    def diff(self) -> str:
        """Unified diff of the file, with secrets masked (display only)."""
        lines = difflib.unified_diff(
            mask_secrets(self.before).splitlines(), mask_secrets(self.after).splitlines(),
            fromfile=str(self.path), tofile=f"{self.path.name} (dopo)", lineterm="",
        )
        return "\n".join(lines)

    def summary(self) -> str:
        if self.error:
            return f"{self.adapter.label}: {self.error}"
        parts = [f"{len(v)} {label}" for label, v in (
            ("nuovi", self.added), ("aggiornati", self.updated), ("gia' a posto", self.unchanged)) if v]
        if self.replaced_foreign:
            parts.append(f"{len(self.replaced_foreign)} voci non-hub sostituite")
        return f"{self.adapter.label}: " + (", ".join(parts) or "nessun server da scrivere")


def detect_indent(text: str) -> str | int:
    """The indentation a JSON file already uses (a tab, or N spaces), so rewriting it
    changes only the lines that really change. A real VS Code mcp.json is tab-indented:
    re-serialising it with 2 spaces would make every line of the diff differ."""
    for line in text.splitlines():
        stripped = line.lstrip(" 	")
        if stripped and stripped != line:
            lead = line[: len(line) - len(stripped)]
            return "	" if lead.startswith("	") else len(lead)
    return 2


def _is_hub_entry(entry: object, hub: HubConfig) -> bool:
    return isinstance(entry, dict) and str(entry.get("url", "")).startswith(hub_base_url(hub) + "/")


def plan_sync(adapter: ClientAdapter, path: Path, hub_config: Config, names: list[str]) -> Plan:
    """What writing `names` into `path` would do. Reads only; never writes."""
    hub = hub_config.hub
    plan = Plan(adapter, path, path.exists())
    if plan.exists:
        plan.before = path.read_text(encoding="utf-8")
    try:
        data = json.loads(plan.before) if plan.before.strip() else {}
    except ValueError as exc:
        plan.error = (f"il file non e' JSON valido ({exc}); se contiene commenti o virgole finali "
                      "modificalo a mano: non lo tocco")
        return plan
    if not isinstance(data, dict):
        plan.error = "il file non contiene un oggetto JSON: non lo tocco"
        return plan
    container = data.setdefault(adapter.key, {})
    if not isinstance(container, dict):
        plan.error = f"'{adapter.key}' non e' un oggetto: non lo tocco"
        return plan
    for name in names:
        new = adapter.entry(hub, name)
        old = container.get(name)
        if old is None:
            plan.added.append(name)
        elif old == new:
            plan.unchanged.append(name)
            continue
        elif _is_hub_entry(old, hub):
            plan.updated.append(name)
        else:
            plan.updated.append(name)
            plan.replaced_foreign.append(name)
        container[name] = new
    indent_text = json.dumps(data, indent=detect_indent(plan.before), ensure_ascii=False)
    plan.after = indent_text + ("\n" if plan.before.endswith("\n") else "")
    if not (plan.added or plan.updated):
        plan.after = plan.before  # nothing to write: keep the file byte for byte
    return plan


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        json.loads(tmp.read_text(encoding="utf-8"))  # never replace a good file with a bad one
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"refusing to update {path}: the staged file failed validation ({exc})") from exc
    os.replace(tmp, path)


def apply_plan(plan: Plan) -> Path | None:
    """Writes a plan: backup of the current file first, then an atomic replace.
    Returns the backup's path (None when there was no file or nothing to write)."""
    if plan.error:
        raise ValueError(plan.error)
    if not plan.changed:
        return None
    backup = backup_file(plan.path) if plan.exists else None
    _atomic_write(plan.path, plan.after)
    return backup


def backups(path: Path) -> list[Path]:
    """Backups next to `path` made by `backup_file`, newest first."""
    found = list(path.parent.glob(f"{path.name}.bak-*"))

    def stamp(p: Path) -> tuple[str, int]:
        # name: <file>.bak-YYYYmmdd-HHMMSS[-n]. NOT the mtime: `backup_file` copies with
        # copy2, which keeps the SOURCE file's modification time.
        parts = p.name.rsplit(".bak-", 1)[1].split("-")
        return parts[0] + parts[1] if len(parts) > 1 else parts[0], int(parts[2]) if len(parts) > 2 else 0

    return sorted(found, key=stamp, reverse=True)


def rollback(path: Path) -> Path:
    """Restores the newest backup over `path`. The file as it is now is backed up
    first, so a rollback can itself be undone. Returns the backup restored."""
    available = backups(path)
    if not available:
        raise FileNotFoundError(f"no backup of {path} to restore")
    chosen = available[0]
    content = chosen.read_text(encoding="utf-8")
    json.loads(content)  # a backup that is not valid JSON is not restored over a file
    if path.exists():
        backup_file(path)
    _atomic_write(path, content)
    return chosen


def sync_names(hub_config: Config, only: list[str] | None = None, group: str | None = None) -> list[str]:
    """The servers worth writing into a client: enabled MCP servers (a service is not
    an MCP server), optionally narrowed to `only` and to the members of `group`.
    An unknown group is an error, not "nothing": a typo must not write an empty set."""
    names = [n for n, s in hub_config.servers.items() if s.enabled and not s.is_service]
    if group is not None:
        if group not in hub_config.groups:
            raise ValueError(f"unknown group: {group}")
        names = [n for n in names if n in hub_config.groups[group]]
    return [n for n in names if only is None or n in only]
