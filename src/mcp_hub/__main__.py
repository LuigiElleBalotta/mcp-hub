from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from mcp_hub.config import load_config
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager


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

    async def _run():
        await manager.start_all()
        uvicorn_config = uvicorn.Config(app, host=config.hub.host, port=config.hub.port, log_level="info")
        server = uvicorn.Server(uvicorn_config)
        await server.serve()

    asyncio.run(_run())


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_hub")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
