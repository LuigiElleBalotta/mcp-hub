from __future__ import annotations

import psutil


def _snapshot(pid: int) -> list[psutil.Process]:
    """The hub process plus every descendant (managed MCP servers, Rizzo,
    their own children), taken BEFORE asking the hub to stop: once the hub
    exits its children are re-parented and can no longer be found by
    walking the tree."""
    try:
        proc = psutil.Process(pid)
        return [proc, *proc.children(recursive=True)]
    except psutil.Error:
        return []


def shutdown_hub(client, wait: float = 30.0) -> bool:
    """Stops the background hub and everything it started, for real.

    1. Graceful: `POST /api/shutdown` lets the hub stop each managed server
       itself (a service stop also waits for its port to be released).
    2. Fallback: whatever is still alive after `wait` seconds -- the hub
       itself, or a server it failed to reap -- is killed outright.

    Returns True when nothing of the hub's process tree is left. A hub that
    is not reachable is treated as already stopped (nothing to do).
    """
    try:
        pid = client.pid()
    except Exception:
        return True
    tree = _snapshot(pid)
    try:
        client.shutdown()
    except Exception:
        pass  # the hub may close the connection mid-response; we verify below
    _, alive = psutil.wait_procs(tree, timeout=wait)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    return not alive
