"""Where mcp-hub keeps its data, per platform.

`%LOCALAPPDATA%` wins when set (Windows, and the test suite's sandbox on any
OS); otherwise macOS uses `~/Library/Application Support` and other systems
`$XDG_DATA_HOME` or `~/.local/share`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "mcp-hub"


def data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def data_dir() -> Path:
    return data_root() / APP_DIR_NAME
