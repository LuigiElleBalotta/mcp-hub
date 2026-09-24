import pytest

from mcp_hub import self_update
from mcp_hub.updater import UpdateInfo


def _info(**assets: str) -> UpdateInfo:
    return UpdateInfo(version="1.3.0", url="u", prerelease=False, assets=assets)


def test_apply_update_rejects_release_missing_an_exe_asset(tmp_path):
    info = _info(**{"mcp-hub.exe": "https://dl/mcp-hub.exe"})  # gui exe missing
    with pytest.raises(ValueError):
        self_update.apply_update(info, hub_pid=123, work_dir=tmp_path)


def test_apply_update_downloads_writes_script_and_launches_helper(tmp_path, monkeypatch):
    info = _info(**{
        "mcp-hub.exe": "https://dl/mcp-hub.exe",
        "mcp-hub-gui.exe": "https://dl/mcp-hub-gui.exe",
    })
    downloaded = []

    def fake_download(url, dest):
        downloaded.append((url, dest))
        dest.write_bytes(b"fake-exe")

    launched = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            launched["args"] = args
            launched["kwargs"] = kwargs

    monkeypatch.setattr(self_update, "download_asset", fake_download)
    monkeypatch.setattr(self_update.subprocess, "Popen", _FakePopen)

    hub_exe = tmp_path / "install" / "mcp-hub.exe"
    gui_exe = tmp_path / "install" / "mcp-hub-gui.exe"
    work_dir = tmp_path / "update-staging"

    self_update.apply_update(info, hub_pid=4242, work_dir=work_dir, hub_exe=hub_exe, gui_exe=gui_exe)

    assert {u for u, _ in downloaded} == {"https://dl/mcp-hub.exe", "https://dl/mcp-hub-gui.exe"}
    assert (work_dir / "apply_update.ps1").exists()

    args = launched["args"]
    assert "-HubPid" in args and "4242" in args
    assert str(hub_exe) in args
    assert str(gui_exe) in args
