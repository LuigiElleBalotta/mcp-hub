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
    # `type == "service"`: a plain long-running process (e.g. an HTTP server
    # such as Rizzo Flow) that the hub only starts/stops/monitors. It is not
    # an MCP server: stdio is not proxied, no `/<name>/sse` route is mounted
    # and it is never written into Claude Code's config by `apply`. The
    # fields below apply to services only.
    type: Literal["mcp", "service"] = "mcp"
    cwd: str | None = None
    # URL polled after start; the service is "starting" until it answers 2xx.
    healthUrl: str | None = None
    # TCP port the service listens on (derived from healthUrl when unset);
    # used to refuse a start while the port is taken and to verify it is
    # released after stop.
    port: int | None = None
    healthTimeout: float = 120.0
    # With `enabled`, start the service when the hub starts (default: manual).
    autostart: bool = False

    @property
    def is_service(self) -> bool:
        return self.type == "service"

    @property
    def starts_with_hub(self) -> bool:
        """Whether the hub starts this server at its own startup / on upsert.

        MCP servers: whenever `enabled`. Services: only `enabled` AND
        `autostart`, so a heavy local service (GPU model) is never started
        behind the user's back."""
        return self.enabled and (self.autostart if self.is_service else True)

    @property
    def effective_port(self) -> int | None:
        if self.port is not None:
            return self.port
        if self.healthUrl:
            from urllib.parse import urlparse
            return urlparse(self.healthUrl).port
        return None


_SERVICE_ONLY_FIELDS = ("cwd", "healthUrl", "port", "healthTimeout", "autostart")


def server_to_dict(sc: ServerConfig) -> dict:
    """`asdict`, but MCP servers omit the service-only fields so existing
    entries in config.json keep their original shape."""
    data = asdict(sc)
    if not sc.is_service:
        data.pop("type")
        for key in _SERVICE_ONLY_FIELDS:
            data.pop(key, None)
    return data


@dataclass
class HubConfig:
    host: str = "127.0.0.1"
    port: int = 37450
    authToken: str | None = None
    autostart: bool = False
    checkForUpdates: bool = True
    includeBetaUpdates: bool = False


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
        "servers": {name: server_to_dict(sc) for name, sc in config.servers.items()},
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
