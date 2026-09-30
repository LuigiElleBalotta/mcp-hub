import os
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


def test_augment_path_adds_homebrew_on_macos(monkeypatch, tmp_path):
    brew = tmp_path / "brew-bin"
    brew.mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(paths, "_MAC_EXTRA_PATH", (str(brew), str(tmp_path / "missing")))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    paths.augment_path()
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[0] == str(brew) and "/usr/bin" in parts and str(tmp_path / "missing") not in parts


def test_augment_path_is_a_noop_elsewhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PATH", "x")
    paths.augment_path()
    assert os.environ["PATH"] == "x"


class _Done:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_brew_cask_detection(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(self_update.shutil, "which", lambda n: "/opt/homebrew/bin/brew")
    calls = []
    monkeypatch.setattr(self_update.subprocess, "run", lambda args, **kw: calls.append(args) or _Done(0))
    assert self_update.brew_cask_installed() is True
    assert calls == [["/opt/homebrew/bin/brew", "list", "--cask", "mcp-hub"]]
    monkeypatch.setattr(self_update.subprocess, "run", lambda args, **kw: _Done(1))
    assert self_update.brew_cask_installed() is False
    monkeypatch.setattr(self_update.shutil, "which", lambda n: None)
    assert self_update.brew_cask_installed() is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert self_update.brew_cask_installed() is False


def test_brew_upgrade_reports_failures(monkeypatch):
    monkeypatch.setattr(self_update.shutil, "which", lambda n: "/usr/local/bin/brew")
    monkeypatch.setattr(self_update.subprocess, "run", lambda args, **kw: _Done(0))
    self_update.brew_upgrade()
    monkeypatch.setattr(self_update.subprocess, "run", lambda args, **kw: _Done(1, err="a\nb\nError: cask not installed"))
    with pytest.raises(RuntimeError, match="cask not installed"):
        self_update.brew_upgrade()
    monkeypatch.setattr(self_update.shutil, "which", lambda n: None)
    with pytest.raises(RuntimeError, match="Homebrew"):
        self_update.brew_upgrade()
