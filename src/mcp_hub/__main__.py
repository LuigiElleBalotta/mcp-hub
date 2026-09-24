from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import uvicorn

from mcp_hub.config import load_config, save_config
from mcp_hub.hub_app import build_app
from mcp_hub.manager import HubManager
from mcp_hub.claude_config import import_servers, apply_servers
from mcp_hub.cleanup import list_processes, find_legacy_processes, describe_plan, execute_cleanup


async def serve_with_managed_shutdown(
    manager: HubManager, server: uvicorn.Server, shutdown_event: asyncio.Event | None = None
) -> None:
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
        watcher = None
        if shutdown_event is not None:
            watcher = asyncio.create_task(_watch_shutdown(shutdown_event, server))
        await server.serve()
        if watcher is not None:
            watcher.cancel()
    finally:
        await manager.stop_all()


async def _watch_shutdown(event: asyncio.Event, server: uvicorn.Server) -> None:
    """Lets `/api/shutdown` (self-update flow) trigger the same graceful
    exit path as Ctrl-C, instead of requiring a caller to kill the process
    and skip `manager.stop_all()`."""
    await event.wait()
    server.should_exit = True


def cmd_serve(args: argparse.Namespace) -> None:
    config = load_config()
    localhost_names = {"127.0.0.1", "localhost"}
    if config.hub.host not in localhost_names and not config.hub.authToken:
        sys.exit(
            f"Refusing to bind {config.hub.host}: set hub.authToken in config.json "
            "before binding anywhere other than 127.0.0.1/localhost."
        )
    manager = HubManager(config)
    shutdown_event = asyncio.Event()
    app = build_app(manager, shutdown_event)
    uvicorn_config = uvicorn.Config(app, host=config.hub.host, port=config.hub.port, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    asyncio.run(serve_with_managed_shutdown(manager, server, shutdown_event))


def cmd_import(args: argparse.Namespace) -> None:
    config = load_config()
    imported = import_servers(Path(args.from_path), config, project_scope=args.project)
    save_config(config)
    if imported:
        print(f"Imported {len(imported)} server(s), disabled by default: {', '.join(imported)}")
        print("Enable them in config.json (or the GUI) before starting the hub.")
    else:
        print("Nothing new to import.")


def cmd_apply(args: argparse.Namespace) -> None:
    config = load_config()
    migrated = apply_servers(Path(args.to_path), config, project_scope=args.project)
    print(f"Applied {len(migrated)} server(s) to {args.to_path}: {', '.join(migrated) or '(none)'}")

    if args.cleanup and migrated:
        migrated_configs = {name: config.servers[name] for name in migrated}
        matches = find_legacy_processes(list_processes(), migrated_configs, self_pid=os.getpid())
        if not matches:
            print("No legacy processes found to clean up.")
            return
        print(describe_plan(matches))
        if not args.yes:
            answer = input("Close these processes now? [y/N] ")
            if answer.strip().lower() != "y":
                print("Cleanup skipped.")
                return
        count = execute_cleanup(matches)
        print(f"Closed {count} process(es).")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_hub")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").set_defaults(func=cmd_serve)

    import_parser = sub.add_parser("import")
    import_parser.add_argument("--from", dest="from_path", required=True)
    import_parser.add_argument("--project", default=None)
    import_parser.set_defaults(func=cmd_import)

    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--to", dest="to_path", required=True)
    apply_parser.add_argument("--project", default=None)
    apply_parser.add_argument("--cleanup", action="store_true")
    apply_parser.add_argument("--yes", action="store_true")
    apply_parser.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
