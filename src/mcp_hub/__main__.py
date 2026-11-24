from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import uvicorn

import mcp_hub
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
    print(f"mcp-hub v{mcp_hub.__version__}")
    config = load_config()
    localhost_names = {"127.0.0.1", "localhost"}
    if config.hub.host not in localhost_names and not config.hub.authToken:
        sys.exit(
            f"Refusing to bind {config.hub.host}: set hub.authToken in config.json "
            "before binding anywhere other than 127.0.0.1/localhost."
        )
    # Servers started from here on die with the hub even if it is killed.
    from mcp_hub.jobobject import bind_children_to_this_process
    bind_children_to_this_process()
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


def _client_path(adapter, args: argparse.Namespace) -> Path:
    """The file to sync: --to wins, else the client's user-level file or (with
    --scope project) the one inside --project-dir."""
    if args.to_path:
        return Path(args.to_path)
    if args.scope == "project":
        return adapter.project_path(Path(args.project_dir or "."))
    path = adapter.user_path()
    if path is None:
        sys.exit(f"{adapter.label}: the user-level config path is not confirmed on this system; pass --to <file>.")
    return path


def cmd_apply_client(args: argparse.Namespace, config) -> None:
    """`apply --client X`: plan, show the diff, then (unless --dry-run) back up and write."""
    from mcp_hub.client_sync import ADAPTERS, apply_plan, plan_sync, sync_names

    adapter = ADAPTERS[args.client or "claude"]
    if args.project:
        sys.exit("--project (Claude Code's per-project section) belongs to the legacy form `apply --to`; "
                 "use --scope project --project-dir <dir> for a project file.")
    only = [n for n in args.only.split(",") if n] if args.only else None
    path = _client_path(adapter, args)
    plan = plan_sync(adapter, path, config, sync_names(config, only))
    print(plan.summary())
    print(f"File: {path}")
    if plan.error:
        sys.exit(1)
    if not plan.changed:
        print("Nothing to change.")
        return
    print(plan.diff())
    if args.dry_run:
        print("(dry run: nothing was written)")
        return
    if not args.yes and input("Write these changes? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return
    backup = apply_plan(plan)
    print(f"Written. Backup: {backup}" if backup else "Written (new file).")


def cmd_rollback(args: argparse.Namespace) -> None:
    from mcp_hub.client_sync import ADAPTERS, rollback

    adapter = ADAPTERS[args.client]
    path = _client_path(adapter, args)
    try:
        restored = rollback(path)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(str(exc))
    print(f"Restored {path} from {restored}")


def cmd_apply(args: argparse.Namespace) -> None:
    config = load_config()
    if args.client or args.dry_run:
        cmd_apply_client(args, config)
        return
    if not args.to_path:
        sys.exit("apply: pass --to <file> (Claude Code) or --client claude|cursor|vscode.")
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
    from mcp_hub.paths import augment_path

    augment_path()
    parser = argparse.ArgumentParser(prog="mcp_hub")
    parser.add_argument("--version", action="version", version=f"mcp-hub {mcp_hub.__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve").set_defaults(func=cmd_serve)

    import_parser = sub.add_parser("import")
    import_parser.add_argument("--from", dest="from_path", required=True)
    import_parser.add_argument("--project", default=None)
    import_parser.set_defaults(func=cmd_import)

    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--to", dest="to_path", default=None)
    apply_parser.add_argument("--project", default=None)
    apply_parser.add_argument("--client", choices=["claude", "cursor", "vscode"], default=None,
                              help="write the hub's endpoints into this client's config (with a diff preview)")
    apply_parser.add_argument("--scope", choices=["user", "project"], default="user")
    apply_parser.add_argument("--project-dir", default=None, help="folder of the project file (--scope project)")
    apply_parser.add_argument("--only", default=None, help="comma-separated server names")
    apply_parser.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    apply_parser.add_argument("--cleanup", action="store_true")
    apply_parser.add_argument("--yes", action="store_true")
    apply_parser.set_defaults(func=cmd_apply)

    rollback_parser = sub.add_parser("rollback", help="restore the newest backup of a client's config")
    rollback_parser.add_argument("--client", choices=["claude", "cursor", "vscode"], required=True)
    rollback_parser.add_argument("--scope", choices=["user", "project"], default="user")
    rollback_parser.add_argument("--project-dir", default=None)
    rollback_parser.add_argument("--to", dest="to_path", default=None)
    rollback_parser.set_defaults(func=cmd_rollback)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
