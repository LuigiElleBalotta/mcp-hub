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
