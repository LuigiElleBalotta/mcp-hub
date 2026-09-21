from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from mcp_hub.config import load_config
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager


async def serve_with_managed_shutdown(manager: HubManager, server: uvicorn.Server) -> None:
    """Starts every managed server, runs `server.serve()`, and guarantees
    every managed subprocess is stopped afterward.

    Round-2 review fix (Critical finding): `uvicorn.Server.serve()` returns
    normally on a graceful shutdown (e.g. Ctrl-C/SIGINT), not just on a
    crash -- that's the ONLY way the `serve` command currently exits, since
    it's the only subcommand this project ships. Without the `finally`
    below, every ordinary `serve` shutdown left every managed subprocess
    running (orphaned), since nothing ever called `stop_all()`. This runs on
    every exit path out of `serve()`: clean shutdown, signal, or an
    exception propagating out of `serve()` itself.

    Extracted to module level (rather than a closure inside `cmd_serve`) so
    it can be exercised directly in tests with a fake `server` whose
    `serve()` raises, without needing a live uvicorn server or a real CLI
    subprocess.

    `start_all()` is inside the try too: if it raises partway through (e.g. a
    bad `command` for one server raising `FileNotFoundError`), any servers it
    already spawned before the failure must still be stopped rather than
    orphaned -- `stop_all()`/`ManagedServer.stop()` are no-ops for servers
    that never started, so this is safe.
    """
    try:
        await manager.start_all()
        await server.serve()
    finally:
        await manager.stop_all()


def cmd_serve(args: argparse.Namespace) -> None:
    config = load_config()
    localhost_names = {"127.0.0.1", "localhost"}
    if config.hub.host not in localhost_names and not config.hub.authToken:
        sys.exit(
            f"Refusing to bind {config.hub.host}: set hub.authToken in config.json "
            "before binding anywhere other than 127.0.0.1/localhost."
        )
    manager = HubManager(config)
    app = build_app(manager)
    uvicorn_config = uvicorn.Config(app, host=config.hub.host, port=config.hub.port, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    asyncio.run(serve_with_managed_shutdown(manager, server))


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_hub")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
