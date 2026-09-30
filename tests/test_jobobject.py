# tests/test_jobobject.py
"""A hub killed outright must not leave its servers (Rizzo Flow) running."""
import os
import subprocess
import sys
import time

import psutil
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")

# Stands in for the hub: binds itself to a job, starts a child, reports the child's pid.
_PARENT = (
    "import subprocess,sys,time;"
    "from mcp_hub.jobobject import bind_children_to_this_process as b;"
    "assert b();"
    "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
    "print(c.pid,flush=True);time.sleep(120)"
)


def test_killing_the_hub_kills_its_children():
    parent = subprocess.Popen([sys.executable, "-c", _PARENT], stdout=subprocess.PIPE, text=True,
                              env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    try:
        child_pid = int(parent.stdout.readline())
        assert psutil.pid_exists(child_pid)
        psutil.Process(parent.pid).kill()  # hard kill: no graceful shutdown runs
        deadline = time.time() + 10
        while time.time() < deadline and psutil.pid_exists(child_pid):
            time.sleep(0.1)
        assert not psutil.pid_exists(child_pid)
    finally:
        parent.kill()
        try:
            psutil.Process(child_pid).kill()
        except Exception:
            pass
