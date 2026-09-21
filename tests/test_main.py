# tests/test_main.py
"""Regression tests for the round-2 review Critical finding: every normal
`serve` shutdown must stop every managed subprocess, not just a crash path.

`serve_with_managed_shutdown` (src/mcp_hub/__main__.py) wraps
`uvicorn.Server.serve()` in try/finally so `manager.stop_all()` always runs.
These tests exercise that try/finally shape directly with a fake
`uvicorn.Server` (clean exit and an exception out of `serve()`), without
needing a live uvicorn server or a real CLI subprocess.
"""
import pytest

from mcp_hub.config import Config, HubConfig
from mcp_hub.manager import HubManager
from mcp_hub.__main__ import serve_with_managed_shutdown


class _FakeServerCleanExit:
    """Stands in for uvicorn.Server: serve() returns normally, the way the
    real uvicorn.Server.serve() does on a graceful Ctrl-C/SIGINT shutdown."""

    def __init__(self) -> None:
        self.served = False

    async def serve(self) -> None:
        self.served = True


class _FakeServerRaises:
    """Stands in for uvicorn.Server: serve() exits via an exception, to
    confirm the finally still runs on this path too."""

    async def serve(self) -> None:
        raise RuntimeError("boom")


class _RecordingHubManager(HubManager):
    """A HubManager that records whether start_all/stop_all were actually
    invoked, on top of the real behavior (so real ManagedServer bookkeeping
    still happens)."""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.start_all_called = False
        self.stop_all_called = False

    async def start_all(self) -> None:
        self.start_all_called = True
        await super().start_all()

    async def stop_all(self) -> None:
        self.stop_all_called = True
        await super().stop_all()


async def test_serve_with_managed_shutdown_calls_stop_all_on_clean_exit():
    manager = _RecordingHubManager(Config(hub=HubConfig(), servers={}))
    server = _FakeServerCleanExit()

    await serve_with_managed_shutdown(manager, server)

    assert manager.start_all_called
    assert server.served
    assert manager.stop_all_called


async def test_serve_with_managed_shutdown_calls_stop_all_when_serve_raises():
    manager = _RecordingHubManager(Config(hub=HubConfig(), servers={}))
    server = _FakeServerRaises()

    with pytest.raises(RuntimeError, match="boom"):
        await serve_with_managed_shutdown(manager, server)

    assert manager.start_all_called
    assert manager.stop_all_called


class _FailingStartManager(_RecordingHubManager):
    """start_all() itself raises partway through -- e.g. a bad `command` for
    one server raising FileNotFoundError out of create_subprocess_exec."""

    async def start_all(self) -> None:
        self.start_all_called = True
        raise RuntimeError("bad command")


async def test_serve_with_managed_shutdown_calls_stop_all_when_start_all_raises():
    manager = _FailingStartManager(Config(hub=HubConfig(), servers={}))
    server = _FakeServerCleanExit()

    with pytest.raises(RuntimeError, match="bad command"):
        await serve_with_managed_shutdown(manager, server)

    assert manager.start_all_called
    assert not server.served  # never reached serve() at all
    assert manager.stop_all_called
