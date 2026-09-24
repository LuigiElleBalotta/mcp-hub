from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mcp_hub.updater import UpdateInfo, download_asset

HUB_EXE_NAME = "mcp-hub.exe"
GUI_EXE_NAME = "mcp-hub-gui.exe"

# CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB --
# launches the helper independent of this process's console AND its Job
# Object, so it survives this process (and the hub process) exiting.
#
# Found live (Important finding): CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
# alone detaches from the console and process group, but NOT from a Windows
# Job Object -- if this GUI process happens to be a descendant of a job with
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE set (common: many terminal/IDE hosts
# assign one to their child process tree for cleanup), the helper inherits
# that job membership regardless of these flags, and dies WITH the rest of
# the tree the moment it's torn down -- mid-script, before it ever reaches
# Replace-WithRetry or the relaunch. Reproduced live: "Installa e riavvia"
# showed "Download in corso...", both processes exited (the intended part),
# but neither exe got replaced and nothing came back -- the detached helper
# was gone too, with no trace, no error, nothing. CREATE_BREAKAWAY_FROM_JOB
# on process creation is what actually escapes an existing job (subject to
# the job allowing it, which is the default unless a job explicitly sets
# JOB_OBJECT_LIMIT_BREAKAWAY_OK to false).
_DETACHED_FLAGS = 0x00000200 | 0x00000008 | 0x01000000

_HELPER_SCRIPT = """\
param(
    [int]$HubPid,
    [int]$GuiPid,
    [string]$NewHubExe,
    [string]$NewGuiExe,
    [string]$TargetHubExe,
    [string]$TargetGuiExe
)

# Logged to a fixed path next to the downloaded exes (not stdout/stderr --
# this process is fully detached and hidden, nothing would ever see them)
# so a failed update leaves a trace instead of the previous silent "nothing
# happens" -- found live with zero diagnostic information to go on.
$logPath = Join-Path (Split-Path -Parent $NewHubExe) "apply_update.log"
function Log($msg) {
    "$(Get-Date -Format o)  $msg" | Out-File -FilePath $logPath -Append -Encoding utf8
}

Log "start: HubPid=$HubPid GuiPid=$GuiPid"

foreach ($p in @($HubPid, $GuiPid)) {
    if ($p -gt 0) {
        try { Wait-Process -Id $p -Timeout 30 -ErrorAction SilentlyContinue } catch {}
    }
}
Log "both processes exited (or timed out waiting)"

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

$hubReplaced = Replace-WithRetry -source $NewHubExe -target $TargetHubExe
$guiReplaced = Replace-WithRetry -source $NewGuiExe -target $TargetGuiExe
Log "replace: hub=$hubReplaced gui=$guiReplaced"

try {
    Start-Process -FilePath $TargetHubExe -ArgumentList "serve"
    Log "started hub"
} catch {
    Log "FAILED to start hub: $_"
}
try {
    Start-Process -FilePath $TargetGuiExe
    Log "started gui"
} catch {
    Log "FAILED to start gui: $_"
}
"""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def gui_exe_path() -> Path:
    return Path(sys.executable)


def hub_exe_path() -> Path:
    """Assumes the CLI/hub exe is installed next to the GUI exe -- the
    layout the release workflow and README both document."""
    return gui_exe_path().parent / HUB_EXE_NAME


def launch_detached(args: list[str]) -> subprocess.Popen:
    """Starts `args` independent of this process's console/job, so it keeps
    running (or, for the update helper, keeps waiting) after this process
    exits. Shared by `apply_update`'s helper launch and the GUI's first-run
    wizard, which needs to start the hub the same detached way after
    writing a fresh config.json."""
    return subprocess.Popen(args, creationflags=_DETACHED_FLAGS, close_fds=True)


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

    launch_detached([
        "powershell", "-NoProfile", "-WindowStyle", "Hidden",
        "-File", str(script_path),
        "-HubPid", str(hub_pid),
        "-GuiPid", str(_current_pid()),
        "-NewHubExe", str(new_hub),
        "-NewGuiExe", str(new_gui),
        "-TargetHubExe", str(hub_exe),
        "-TargetGuiExe", str(gui_exe),
    ])


def _current_pid() -> int:
    import os
    return os.getpid()
