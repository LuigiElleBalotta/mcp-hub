import sys
from pathlib import Path

import pytest

from mcp_hub import paths, self_update
from mcp_hub.updater import UpdateInfo


def test_localappdata_wins_everywhere(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.data_dir() == tmp_path / "mcp-hub"


def test_macos_uses_application_support(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.data_dir() == Path.home() / "Library" / "Application Support" / "mcp-hub"


def test_other_systems_use_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.data_dir() == tmp_path / "mcp-hub"


def test_self_update_is_windows_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert self_update.is_supported() is False
    info = UpdateInfo(version="9.9.9", url="u", prerelease=False, assets={})
    with pytest.raises(RuntimeError, match="solo su Windows"):
        self_update.apply_update(info, hub_pid=1, work_dir=Path("."))


def test_hub_command_per_platform(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Apps/mcp-hub-gui.app/Contents/MacOS/mcp-hub-gui")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert self_update.hub_command() == ["/Apps/mcp-hub-gui.app/Contents/MacOS/mcp-hub-gui", "serve"]
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert self_update.hub_command()[1:] == ["-m", "mcp_hub", "serve"]
