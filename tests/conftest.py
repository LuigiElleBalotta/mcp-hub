"""Keeps the test suite away from the user's real hub configuration.

`CONFIG_PATH` is computed from %LOCALAPPDATA% when `mcp_hub.config` is first
imported, and several code paths (`DELETE /api/servers/<name>`, the GUI's
first-run wizard, `save_settings`) write to it. Without this sandbox, running
the suite overwrote the real %LOCALAPPDATA%\mcp-hub\config.json with an empty
server list. conftest.py is imported before any test module, so the variable is
set before `mcp_hub` is.
"""
import os
import tempfile
from pathlib import Path

_SANDBOX = tempfile.mkdtemp(prefix="mcp-hub-tests-")
os.environ["LOCALAPPDATA"] = _SANDBOX

import pytest  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _config_path_is_sandboxed():
    from mcp_hub import config

    path = Path(config.CONFIG_PATH).resolve()
    assert Path(_SANDBOX).resolve() in path.parents, (
        f"tests would touch the real config: {path}"
    )
    yield
