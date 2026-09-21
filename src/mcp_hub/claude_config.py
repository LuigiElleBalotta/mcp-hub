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
    if backup.exists():
        counter = 1
        while True:
            candidate = path.with_name(f"{path.name}.bak-{timestamp}-{counter}")
            if not candidate.exists():
                backup = candidate
                break
            counter += 1
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
    try:
        # Write to a staging file and validate ITS content before the live
        # file is ever touched. If either the write or the validation fails
        # (disk full, truncated write, interrupted process, etc.), the
        # temp file is discarded and os.replace is never reached -- the
        # live claude_config_path is left completely untouched (the backup
        # taken above is also still there, unaffected).
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        json.loads(tmp_path.read_text(encoding="utf-8"))
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(
            f"refusing to update {claude_config_path}: staged write failed validation ({exc})"
        ) from exc

    # Only now, after the staged content has been proven to be valid JSON,
    # does the atomic replace make the new content live.
    os.replace(tmp_path, claude_config_path)
    return migrated
