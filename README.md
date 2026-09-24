# mcp-hub

One shared, long-running process that hosts local MCP servers (mariadb,
headroom, gitlab, figma-bridge, chrome-real, windows-mcp, ...) and exposes
each one over HTTP/SSE, so N Claude Code sessions connect to the same
backend instead of each spawning and paying for its own subprocess tree.

- **Design spec:** `docs/superpowers/specs/2026-09-21-mcp-hub-design.md`
- **Implementation plan:** `docs/superpowers/plans/2026-09-21-mcp-hub-implementation.md`

## Why

Every Claude Code session that lists an MCP server in `.claude.json`
spawns its own copy of that server's process. With several sessions open at
once (several terminals, several projects) you end up with N processes per
server — N Node/`npx` processes for gitlab, N for figma-bridge, N Python
processes for mariadb, etc. mcp-hub starts each server **once**, keeps it
running, and every session talks to that single instance over HTTP/SSE
instead of spawning its own.

## Architecture

```
Claude Code session 1 ─┐
Claude Code session 2 ─┼──HTTP/SSE──▶  mcp-hub (uvicorn/Starlette)
Claude Code session N ─┘                 │
                                          ├─ ManagedServer "gitlab"      (npx, real subprocess, started once)
                                          ├─ ManagedServer "mariadb"     (npx, real subprocess, started once)
                                          ├─ ManagedServer "figma-bridge"(npx, real subprocess, started once)
                                          └─ ...
```

- A **Starlette/uvicorn ASGI app** (`hub_app.py`) mounts one MCP SSE
  endpoint per configured, enabled server: `http://127.0.0.1:37450/<name>/sse`.
- A **process manager** (`manager.py`) owns each backend's real subprocess,
  started once at hub startup, with a redacted rolling log buffer and
  crash detection.
- A **per-server concurrency guard** (`concurrency.py`) serializes calls to
  servers that can't handle concurrent requests (`"exclusive"`, the
  default) or lets them run unserialized (`"parallel"`).
- A **JSON config store** (`config.py`) outside the repo is the single
  source of truth for which servers exist and how to start them.
- A **PySide6 GUI** talks only to the hub's local management HTTP API
  (`management_api.py`) — it never touches `config.json` directly.
- **`import`/`apply` CLI commands** (`claude_config.py`) move server
  definitions between the hub's config and Claude Code's own
  `.claude.json`, so you don't hand-edit both files.
- **`apply --cleanup`** (`cleanup.py`) can reclaim memory from
  already-running legacy per-session server processes after one upfront
  confirmation, protecting the calling process's own ancestor chain.

## Requirements

- Windows (PySide6 GUI, Task Scheduler autostart, Windows-specific process
  handling in `cleanup.py`/`manager.py`)
- Python 3.11+

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

Run the tests to confirm the install is sane:

```
.venv\Scripts\pytest -q
```

## Configuration

Config lives at **`%LOCALAPPDATA%\mcp-hub\config.json`** — never inside
this repo (the repo only ships `config.example.json` as a template). Copy
it there and edit it by hand, or populate it from an existing
`.claude.json` with `mcp_hub import` (see below).

```json
{
  "hub": {
    "host": "127.0.0.1",
    "port": 37450,
    "authToken": null,
    "autostart": false,
    "checkForUpdates": true,
    "includeBetaUpdates": false
  },
  "servers": {
    "example-server": {
      "enabled": false,
      "command": "npx",
      "args": ["-y", "some-mcp-server"],
      "env": {},
      "concurrency": "exclusive"
    }
  }
}
```

**`hub` fields:**

| Field                | Default       | Meaning |
|----------------------|---------------|---------|
| `host`               | `127.0.0.1`   | Bind address. Anything other than `127.0.0.1`/`localhost` is refused at startup unless `authToken` is set. |
| `port`               | `37450`       | Bind port. |
| `authToken`          | `null`        | Required if `host` is not localhost. Not yet enforced on individual requests — binding restriction only. |
| `autostart`          | `false`       | Informational flag mirrored by the GUI's "Avvia con Windows" checkbox, which registers/removes a Windows Task Scheduler entry (see below). |
| `checkForUpdates`    | `true`        | GUI checks GitHub Releases for a newer version on startup and shows a banner if one exists. Never downloads or installs anything automatically. |
| `includeBetaUpdates` | `false`       | If `true`, the update check also considers beta (prerelease) tags, not just stable releases. |

**Per-server fields (`servers.<name>`):**

| Field         | Meaning |
|---------------|---------|
| `enabled`     | Whether the hub starts this server at all. Disabled servers are never spawned and get no HTTP mount. |
| `command`     | Executable to run (resolved via `PATH`/`PATHEXT`, so a bare `npx`/`uvx` works on Windows). |
| `args`        | Argument list passed to `command`. |
| `env`         | Extra environment variables, merged on top of the hub process's own environment (so `PATH` etc. are preserved). Any key matching `TOKEN\|SECRET\|PASS\|KEY\|AUTH` (case-insensitive) is redacted (`***`) wherever it's logged or returned by the API — never redacted in the config file itself, since the hub needs the real values to launch the process. |
| `concurrency` | `"exclusive"` (default, serializes every request to this server behind an `asyncio.Lock`) or `"parallel"` (no serialization). Use `"exclusive"` for anything that can't handle overlapping calls (e.g. a stateful browser session); `"parallel"` for stateless/read-only tools. |

## Running the hub

```
.venv\Scripts\python -m mcp_hub serve
```

- Starts every `enabled` server, then serves the ASGI app on
  `hub.host:hub.port`.
- Refuses to start (exits with a clear message) if `host` isn't
  `127.0.0.1`/`localhost` and no `authToken` is set.
- On shutdown (Ctrl-C or otherwise), every managed subprocess is stopped —
  nothing is left orphaned.
- Check it's alive: `curl http://127.0.0.1:37450/api/status`

## Running the GUI

```
.venv\Scripts\python -m mcp_hub.gui
```

The GUI is a thin client over the hub's management API — it never reads or
writes `config.json` directly.

- **Server table**: live status (`stopped`/`starting`/`running`/`crashed`)
  for every configured server, refreshed every 2 seconds, with Start/Stop
  buttons per row.
- **Add server**: opens a dialog to define a new server (command, args,
  concurrency, env vars). Env var fields whose key looks like a secret
  (`TOKEN`/`SECRET`/`PASS`/`KEY`/`AUTH`) render masked by default, with a
  per-row "show" checkbox.
- **Log panel**: select a row to see that server's last 500 (redacted) log
  lines.
- **Avvia con Windows**: registers/removes autostart via Windows Task
  Scheduler (see below).
- **Import from Claude Code config / Apply to Claude Code config**: GUI
  wrappers around the `import`/`apply` CLI commands below, with a
  confirmation dialog before any write.
- **Update banner**: if a newer version is published on GitHub Releases
  (and `hub.checkForUpdates` is true), a banner appears with a link to
  download it. This is a manual-download notification only — it does not
  download or replace the running exe/install for you.

## Moving servers to/from Claude Code's config

These commands read/write Claude Code's own `.claude.json` (or a project's
scoped config with `--project`), never the hub's `config.json` directly
except to record what was imported.

**Import** existing server definitions from `.claude.json` into the hub,
disabled by default (so nothing starts until you review and enable them):

```
mcp_hub import --from C:\Users\<you>\.claude.json [--project <scope>]
```

**Apply** enabled hub servers into `.claude.json`, pointing Claude Code at
the hub instead of a locally-spawned process:

```
mcp_hub apply --to C:\Users\<you>\.claude.json [--project <scope>] [--cleanup] [--yes]
```

- Always makes a timestamped backup (`.claude.json.bak-<timestamp>`) next
  to the target file before writing, and verifies the result parses as
  valid JSON before treating the write as successful.
- Rewrites each migrated server's entry to `{"type": "http", "url": "http://<hub host:port>/<name>/sse"}`.
- `--cleanup`: after applying, looks for already-running legacy processes
  matching the migrated servers' `command`/`args` and offers to close them
  (single yes/no confirmation, not a per-process checklist). **Always
  excludes the ancestor-process chain of the process running the command
  itself** — but does *not* know about other terminals/sessions that also
  have their own copies of those servers running; closing those is exactly
  the point of `--cleanup`; run it only when you're fine with other
  sessions' matching processes being closed too (they'll get fresh
  hub-backed connections next time they need that tool).
- `--yes`: skips the interactive confirmation (useful for scripting; still
  prints the plan first).

Typical one-time cutover for an existing `.claude.json`:

```
mcp_hub import --from C:\Users\<you>\.claude.json
:: review %LOCALAPPDATA%\mcp-hub\config.json, set "enabled": true on what you want migrated
mcp_hub serve                                    :: (leave running, or use Task Scheduler autostart)
mcp_hub apply --to C:\Users\<you>\.claude.json --cleanup
:: start a fresh Claude Code session and confirm the migrated tools still work
```

## Autostart (Windows Task Scheduler)

```
scripts\install_task.ps1 -Enable    # registers a logon task ("McpHub") that runs `mcp_hub serve`
scripts\install_task.ps1 -Disable   # removes it
```

Also available as the "Avvia con Windows" checkbox in the GUI. Requires
permission to create scheduled tasks for the current user — on a
locked-down/managed machine this can fail with "Accesso negato"; if so,
ask IT to grant Task Scheduler creation rights, or start the hub manually
each session.

## Releases and versioning (git flow)

This repo follows **git flow**: `master` holds released history,
`develop` is the integration branch for ongoing work.

Pushing a version tag triggers `.github/workflows/release.yml`, which
builds two Windows executables with PyInstaller (`mcp-hub.exe` — the CLI,
`serve`/`import`/`apply`; `mcp-hub-gui.exe` — the GUI) and publishes them
as assets on a GitHub Release for that tag:

| Tag pattern         | Example      | Result |
|----------------------|--------------|--------|
| `x.y.z` (`v` prefix optional) | `1.2.0`, `v1.2.0` | Stable release build, published as a normal (non-prerelease) GitHub Release. |
| `x.y.z-n`            | `1.2.0-1`     | Beta build, published as a **prerelease** GitHub Release. |

Any other tag shape is ignored by the workflow.

The GUI's update checker (`hub.checkForUpdates`) polls this same GitHub
Releases list; set `hub.includeBetaUpdates: true` to also be notified about
`x.y.z-n` prereleases, not just stable tags.

## Security notes

- The hub refuses to bind anywhere other than `127.0.0.1`/`localhost`
  unless `hub.authToken` is set — enforced at startup, not just suggested
  in the GUI.
- Every log line (hub log, per-server ring buffers, the management API's
  `/api/servers/{name}/logs`) redacts the value of any env/config key
  matching `TOKEN|SECRET|PASS|KEY|AUTH` (case-insensitive) before it's
  written or returned — the raw secret only ever exists in
  `%LOCALAPPDATA%\mcp-hub\config.json` and the spawned process's actual
  environment.
- `apply` never overwrites `.claude.json` without a timestamped backup and
  a post-write JSON-validity check.

## Development

```
.venv\Scripts\pytest -q                 # run the full test suite
.venv\Scripts\python scripts\smoke_test.py <server-name>   # compare a hub-proxied tool call against a direct stdio call
.venv\Scripts\python scripts\concurrency_check.py           # verify exclusive-mode serialization under real concurrent load
```

Project layout, task-by-task history, and the exact interfaces each module
exposes are in the implementation plan linked at the top of this file.
