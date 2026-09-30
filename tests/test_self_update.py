import sys
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


def test_gui_exe_path_outside_staging_is_the_running_exe(tmp_path, monkeypatch):
    exe = tmp_path / "Programs" / "mcp-hub" / "mcp-hub-gui.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    monkeypatch.setattr(self_update.sys, "executable", str(exe))
    assert self_update.gui_exe_path() == exe
    assert self_update.hub_exe_path() == exe.parent / "mcp-hub.exe"


def test_gui_exe_path_running_from_staging_targets_the_default_install_dir(tmp_path, monkeypatch):
    # Regression: GUI launched from %LOCALAPPDATA%\mcp-hub\update-staging used to treat the
    # staging copy as the install and fail with "Accesso negato" overwriting itself.
    staged = tmp_path / "mcp-hub" / "update-staging" / "mcp-hub-gui.exe"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"x")
    monkeypatch.setattr(self_update.sys, "executable", str(staged))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert self_update.gui_exe_path() == tmp_path / "Programs" / "mcp-hub" / "mcp-hub-gui.exe"
    assert self_update.hub_exe_path() == tmp_path / "Programs" / "mcp-hub" / "mcp-hub.exe"


def test_fresh_staging_dir_is_new_each_time_and_removes_old_attempts(tmp_path):
    root = tmp_path / "update-staging"
    root.mkdir()
    (root / "mcp-hub.exe.part").write_bytes(b"old")
    old = self_update.fresh_staging_dir(root, "1.0.7")
    (old / "mcp-hub.exe").write_bytes(b"old")
    new = self_update.fresh_staging_dir(root, "1.0.8")
    assert new != old and new.is_dir() and list(new.iterdir()) == []
    assert not old.exists()
    assert not (root / "mcp-hub.exe.part").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
def test_download_into_fresh_dir_succeeds_while_an_old_exe_is_locked(tmp_path):
    import ctypes

    from mcp_hub.updater import download_asset

    class Resp:
        content = b"new exe"

        def raise_for_status(self):
            pass

    root = tmp_path / "update-staging"
    root.mkdir()
    running = root / "mcp-hub-gui.exe"
    running.write_bytes(b"running")
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    handle = k32.CreateFileW(str(running), 0x80000000, 0, None, 3, 0, None)  # read, no sharing
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        with pytest.raises(PermissionError):  # the old behaviour: same dir
            download_asset("u", running, fetch=lambda url: Resp())
        work = self_update.fresh_staging_dir(root, "1.0.8")
        download_asset("u", work / "mcp-hub-gui.exe", fetch=lambda url: Resp())
        assert (work / "mcp-hub-gui.exe").read_bytes() == b"new exe"
        assert running.exists()  # the locked one is left alone
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))
