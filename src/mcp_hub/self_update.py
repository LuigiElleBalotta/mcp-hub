from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mcp_hub.updater import UpdateInfo, download_asset

HUB_EXE_NAME = "mcp-hub.exe"
GUI_EXE_NAME = "mcp-hub-gui.exe"

# CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS -- launches the helper
# independent of this process's console/job, so it survives this process
# (and the hub process) exiting.
_DETACHED_FLAGS = 0x00000200 | 0x00000008

_HELPER_SCRIPT = """\
param(
    [int]$HubPid,
    [int]$GuiPid,
    [string]$NewHubExe,
    [string]$NewGuiExe,
    [string]$TargetHubExe,
    [string]$TargetGuiExe
)

foreach ($p in @($HubPid, $GuiPid)) {
    if ($p -gt 0) {
        try { Wait-Process -Id $p -Timeout 30 -ErrorAction SilentlyContinue } catch {}
    }
}

# The old exes may stay locked for a moment after their process exits
# (antivirus scan, handle release lag) -- retry instead of failing outright.
function Replace-WithRetry($source, $target) {
    for ($i = 0; $i -lt 10; $i++) {
        try {
            Copy-Item -Path $source -Destination $target -Force
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

Replace-WithRetry -source $NewHubExe -target $TargetHubExe | Out-Null
Replace-WithRetry -source $NewGuiExe -target $TargetGuiExe | Out-Null

Start-Process -FilePath $TargetHubExe -ArgumentList "serve"
Start-Process -FilePath $TargetGuiExe
"""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def gui_exe_path() -> Path:
    return Path(sys.executable)


def hub_exe_path() -> Path:
    """Assumes the CLI/hub exe is installed next to the GUI exe -- the
    layout the release workflow and README both document."""
    return gui_exe_path().parent / HUB_EXE_NAME


def apply_update(
    info: UpdateInfo,
    hub_pid: int,
    work_dir: Path,
    hub_exe: Path | None = None,
    gui_exe: Path | None = None,
) -> None:
    """Downloads the new exes, then hands off to a detached PowerShell
    helper that waits for this process and the hub process to exit, does
    the (locked-while-running) file replace, and relaunches both -- so the
    actual swap happens after nothing holds the old files open."""
    hub_exe = hub_exe or hub_exe_path()
    gui_exe = gui_exe or gui_exe_path()

    if HUB_EXE_NAME not in info.assets or GUI_EXE_NAME not in info.assets:
        raise ValueError(f"release {info.version} is missing one of {HUB_EXE_NAME}/{GUI_EXE_NAME}")

    work_dir.mkdir(parents=True, exist_ok=True)
    new_hub = work_dir / HUB_EXE_NAME
    new_gui = work_dir / GUI_EXE_NAME
    download_asset(info.assets[HUB_EXE_NAME], new_hub)
    download_asset(info.assets[GUI_EXE_NAME], new_gui)

    script_path = work_dir / "apply_update.ps1"
    script_path.write_text(_HELPER_SCRIPT, encoding="utf-8")

    subprocess.Popen(
        [
            "powershell", "-NoProfile", "-WindowStyle", "Hidden",
            "-File", str(script_path),
            "-HubPid", str(hub_pid),
            "-GuiPid", str(_current_pid()),
            "-NewHubExe", str(new_hub),
            "-NewGuiExe", str(new_gui),
            "-TargetHubExe", str(hub_exe),
            "-TargetGuiExe", str(gui_exe),
        ],
        creationflags=_DETACHED_FLAGS,
        close_fds=True,
    )


def _current_pid() -> int:
    import os
    return os.getpid()
