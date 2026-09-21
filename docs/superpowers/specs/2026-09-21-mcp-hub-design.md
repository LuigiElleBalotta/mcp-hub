# MCP Hub — Design Spec

Date: 2026-09-21
Status: Approved for implementation planning

## Problem

Claude Code spawns a fresh OS process for every configured MCP server, per
session, for any server using the stdio transport. On a machine with a rich
MCP configuration (gitlab, figma-bridge, mariadb, headroom, chrome-real,
windows-mcp, etc.), a single Claude Code session can bring up 40-50 child
processes. Two concurrent sessions duplicate that entirely — observed on this
machine: ~94 processes, ~1.5 GB RSS, just for MCP servers across two sessions,
contributing directly to system-wide memory pressure severe enough to cause
out-of-memory conditions with 16 GB installed.

MCP already supports a remote transport (HTTP/SSE) for servers that run once
and serve many clients — this machine's `youtrack` entry already uses it via
`mcp-remote`. Local stdio servers have no equivalent: each one is defined with
`command`/`args` and Claude Code owns the subprocess directly.

## Goals

- One real OS process per MCP server backend, shared across all Claude Code
  sessions on the machine, regardless of how many sessions are open.
- Sessions connect as lightweight remote (HTTP/SSE) MCP clients instead of
  spawning anything.
- A desktop GUI to manage the hub and its servers without touching a
  terminal: start/stop, live status, live logs, add/edit servers (including
  credentials), autostart toggle.
- The tool can create and own a real, working configuration on disk from day
  one — including importing what already exists in Claude Code's own config
  — not just a template the user hand-edits.
- Safe-by-default concurrency handling for servers that wrap a single stateful
  resource (a browser, the desktop), generalized so any MCP server — known
  today or added later by any user of this project — gets protected
  automatically without being named in code.
- Suitable to be published as a standalone open-source project: no
  machine-specific assumptions baked into the code, secrets never committed,
  Windows-first but not gratuitously Windows-only in the core.

## Non-goals (v1)

- No cross-machine/network MCP sharing beyond "reachable from other devices
  on the LAN if explicitly configured" — no cloud relay, no multi-host
  clustering.
- No general-purpose auth system — a single shared bearer token is enough for
  the LAN-exposure case.
- No automatic detection of which servers are stateful — the user (or a
  sensible default) declares it per server.
- No macOS/Linux autostart integration in v1 (Task Scheduler only); the hub
  server and GUI themselves are not deliberately made Windows-only, but
  cross-platform packaging is future work.

## Architecture

Four components:

1. **Hub server** — a single long-running Python process. Built on
   Starlette/FastAPI plus the official `mcp` Python SDK's server-side
   HTTP/SSE support. For each configured, enabled backend it exposes one
   mount point: `http://<host>:<port>/<server-name>/sse` (path derived from
   the server's config key, matching how Claude Code already names servers
   today).
2. **Process manager** — owns the lifecycle of each backend's real subprocess
   (the same `command`/`args`/`env` that would otherwise go directly into
   Claude Code's config). One subprocess per enabled server, started eagerly
   when the hub starts (not lazily on first request), so the first Claude
   Code session to connect never pays cold-start latency. Tracks status
   (`starting` / `running` / `crashed` / `stopped`), captures stdout/stderr
   into a bounded per-server ring buffer for the GUI's log view, and exposes
   start/stop/restart per server.
3. **Config store** — a single JSON file that is the source of truth for
   which servers exist, how to launch them, and their per-server settings
   (see schema below). The hub can read it, write it, and mutate it
   atomically (write to a temp file, then rename) so a crash mid-write never
   corrupts it.
4. **GUI** — a PySide6 desktop application that talks to the hub server's
   management API (a small local-only REST surface alongside the MCP
   mounts) to reflect and change state. Not a separate source of truth: the
   GUI never writes the config file directly, it always goes through the hub
   process, so hub and GUI can never disagree about state.

A CLI entry point (`python -m mcp_hub`) starts the hub headless — this is
what Task Scheduler launches at login. The GUI is a separate optional
process that connects to an already-running hub (or offers to start one if
none is found on the configured port).

## Config schema

Stored at `%LOCALAPPDATA%\mcp-hub\config.json` on Windows (XDG-style path on
other OSes) — deliberately **outside** the git repository. The repo ships
only `config.example.json`. Real secrets never get committed.

```jsonc
{
  "hub": {
    "host": "127.0.0.1",       // "0.0.0.0" or a LAN IP requires "authToken" to be set
    "port": 37450,
    "authToken": null,          // required (and enforced) when host != 127.0.0.1/localhost
    "autostart": true           // whether the installer should register the Task Scheduler entry
  },
  "servers": {
    "mariadb": {
      "enabled": true,
      "command": "npx",
      "args": ["-y", "@oleander/mcp-server-mariadb"],
      "env": { "MARIADB_HOST": "...", "MARIADB_PASS": "..." },
      "concurrency": "exclusive"   // "exclusive" | "parallel", default "exclusive"
    },
    "chrome-real": {
      "enabled": true,
      "command": "npx",
      "args": ["-y", "chrome-devtools-mcp@latest", "--userDataDir=..."],
      "env": {},
      "concurrency": "exclusive"
    }
  }
}
```

`concurrency` is the generalized answer to the "chrome-real / windows-mcp
fight over one resource" problem: every server carries this field, defaulting
to `exclusive` for anything newly added (safe by default, since the hub
cannot know in advance whether a server someone plugs in is stateful).
Internally this maps to an `asyncio.Lock` (`exclusive`) or no lock
(`parallel`) guarding the hub's proxying of requests to that server's
subprocess. The schema deliberately stores it as a per-server string rather
than a hardcoded server-name list, so it works unchanged for any MCP server a
user of this project adds later.

## Claude Code integration: import and apply

Two explicit, user-triggered operations, both available from the GUI and as
CLI commands:

- **Import** (`mcp-hub import --from <path-to-.claude.json>`): reads the
  target's `mcpServers` (global or a specific project scope), and for each
  entry not already present in the hub's own config, adds it with
  `concurrency: "exclusive"` by default and `enabled: false` (so nothing
  starts running without an explicit opt-in after review — importing must
  never silently start executing arbitrary commands found in a config file).
- **Apply** (`mcp-hub apply --to <path-to-.claude.json> [--project <scope>]`):
  for every *enabled* hub server, rewrites the corresponding entry in the
  target file from a `command`/`args` stdio definition to a remote
  definition (`type: "http"`, `url: "http://<hub host>:<hub port>/<name>/sse"`,
  plus an `Authorization` header if `authToken` is set) — same shape already
  used for the existing `youtrack` entry. **Always makes a timestamped backup
  of the target file before writing**, and validates the result is
  well-formed JSON before considering the write successful (mirrors the
  manual backup-then-edit procedure already used once on this machine).

Apply is deliberately not automatic/continuous — the user reviews hub status
first, then applies. This keeps a broken hub from ever silently taking over a
working Claude Code config.

## Cleanup of already-running legacy MCP processes

`apply` only affects *future* Claude Code sessions — a session already open
when `apply` runs keeps its own stdio-spawned MCP child processes, because it
read `.claude.json` at startup and cannot hot-swap a live tool connection
from stdio to remote mid-session. Killing one of those child processes while
its owning session is still open breaks that specific tool for that session
until it is restarted.

To let the user reclaim that memory immediately instead of waiting for a
natural restart, `apply` accepts an optional `--cleanup` flag (mirrored as a
GUI checkbox, checked by scanning before the apply confirmation is even
shown — i.e. the user is asked up front, before anything runs, not after):

1. Scan running processes for child trees, rooted at any `claude.exe`
   process, matching the `command`/`args` of a server being migrated.
2. **Always exclude** the process tree that is an ancestor of the
   `mcp-hub`/GUI process itself (self-protection — same ancestor-walk check
   already implemented and proven today in `ProcessWatcher.ps1`, ported to
   Python here).
3. Present one summary upfront — which sessions, which servers, how much
   memory — and ask a **single yes/no confirmation before starting anything**
   (not a per-process checklist). A "no" aborts cleanup entirely; `apply`
   itself (the config rewrite) still proceeds independently unless the user
   also declines that.
4. On "yes", kill exactly the matched MCP child processes (not the owning
   `claude.exe` session itself, not unrelated children) for every matched
   session except the caller's own.

CLI: `mcp-hub apply --cleanup` prompts the same single upfront confirmation;
`mcp-hub apply --cleanup --yes` skips the prompt for scripted/unattended use.

## GUI

PySide6, single main window. (PyQt6 was considered — functionally
equivalent Qt bindings — but PySide6 is LGPL, which keeps licensing simple
if this project is published on GitHub; PyQt6 requires GPL or a commercial
license for closed distribution. Sticking with PySide6 unless a concrete
reason to switch comes up.)

- Server list: name, status dot (green=running, red=crashed, grey=stopped),
  concurrency mode, enabled toggle, per-row start/stop/restart.
- Add/Edit server dialog: command, args (list editor), env (key/value table;
  values whose key matches `TOKEN|SECRET|PASS|KEY|AUTH` render masked with a
  reveal toggle), concurrency radio button.
- Log panel: tails the selected server's ring buffer live.
- Hub panel: hub-level start/stop, host/port fields, autostart checkbox
  (wires to Task Scheduler registration, same mechanism already used for
  `ProcessWatcher`), "Import from Claude Code config" and "Apply to Claude
  Code config" actions with a diff preview before confirming a write.

## Security

- Default bind is `127.0.0.1`; the hub refuses to bind elsewhere unless
  `authToken` is set (checked at startup, not just in the GUI, so a hand-edited
  config can't accidentally expose an unauthenticated hub).
- All log output (hub process log and per-server ring buffers shown in the
  GUI) redacts values of any env/config key matching
  `TOKEN|SECRET|PASS|KEY|AUTH` (case-insensitive) before it is written or
  displayed.
- Config file relies on standard per-user NTFS permissions under
  `%LOCALAPPDATA%`; no additional encryption in v1 (matches how
  `.claude.json` itself already stores these same secrets in plaintext).

## Persistence / autostart

A PowerShell registration script (`scripts/install_task.ps1`), same pattern
as `ProcessWatcher`: `schtasks /Create` running `python -m mcp_hub` hidden, at
logon. Driven by the `hub.autostart` config flag and the GUI's autostart
checkbox — both call the same script, so there is one code path for turning
autostart on/off.

## Rollout / migration order

Implementation and adoption proceed in verified steps, not big-bang:

1. Hub core (config store, process manager, FastAPI/SSE mounts, concurrency
   lock) — proven against **one stateless server** (`mariadb` or `headroom`)
   with manual verification that tool calls through the hub match calling
   the stdio server directly.
2. GUI.
3. Remaining stateless servers (`gitlab`, `figma-bridge`) added.
4. Stateful servers (`chrome-real`, `windows-mcp`) added with
   `concurrency: exclusive`, verified under two concurrent callers.
5. Task Scheduler autostart.
6. Only after all of the above are verified working end-to-end: `apply` run
   against the real `.claude.json`, with backup, switching the migrated
   servers from stdio to remote for real Claude Code sessions. Optionally
   run with `--cleanup` to also reclaim memory from already-open sessions'
   legacy MCP child processes, after the single upfront confirmation.

## Testing strategy

- Unit tests: config load/save/atomic-write, import/apply transforms,
  redaction logic — all pure functions, no live processes needed.
- Manual smoke-test checklist (documented in the repo, run before each
  rollout step above): start hub, connect a throwaway MCP client (or `curl`
  the SSE endpoint) to one server, confirm tool listing and one real tool
  call match the equivalent direct stdio invocation.
- Concurrency check for stateful servers: two simultaneous requests against
  the same `exclusive` server must serialize (second completes only after the
  first finishes), verified with a small script issuing overlapping calls.
- No automated integration tests against real external services (GitLab,
  MariaDB) in v1 — those require live credentials and are verified manually.

## Open risks

- The exact server-side API surface of the official `mcp` Python SDK for
  mounting multiple independent SSE servers under one ASGI app needs a short
  implementation-time spike; the design assumes this is supported (it mirrors
  how the SDK's own examples mount a single server), but the multi-mount
  wiring hasn't been prototyped yet.
- Crash/restart policy for a backend subprocess is currently "mark as
  crashed, surface in GUI, manual restart" — no automatic restart-with-backoff
  in v1, to avoid masking a persistently broken server behind silent retries
  (the same failure mode that led to disabling `caveman-shrink`,
  `mcp-unreal`, `sonarqube`, `tokensave`, `unrealMCP` today). Revisit if this
  proves annoying in practice.
