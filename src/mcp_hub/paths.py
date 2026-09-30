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


_MAC_EXTRA_PATH = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin")


def augment_path() -> None:
    """A macOS app started from Finder or at login gets a minimal PATH
    (`/usr/bin:/bin:...`) without Homebrew, `uv`, `node` or `git` installed
    elsewhere. Add the usual places so servers and the Rizzo installer find
    them. No-op on other systems."""
    if sys.platform != "darwin":
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    extra = [os.path.expanduser(p) for p in _MAC_EXTRA_PATH]
    extra = [p for p in extra if os.path.isdir(p) and p not in current]
    if extra:
        os.environ["PATH"] = os.pathsep.join([*extra, *[c for c in current if c]])
