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
        "servers": {name: asdict(sc) for name, sc in config.servers.items()},
    }
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
