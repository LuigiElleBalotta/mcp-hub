# GUI and client integration: dashboard, tools, client sync, secrets, groups, registry

Depends on `hub-proxy-hardening` (metrics need the request table) and, for
R2, on `hub-proxy-efficiency` (list cache, tool filters).

## Background

Ideas taken from xjeway/mcp-manager (write endpoints into many client
configs with backup/rollback), samanhappy/mcphub and McpMux (status,
latency, activity; groups; registry install; keychain secrets), MCP Router
and MetaMCP (per-tool toggle, tool browser). Licence note: McpMux is GPL-3.0
and MCP Router has a non-standard licence, so only ideas are taken, never
code. mcp-hub already has the GUI shell (server list, logs, dialogs, tray,
wizard) and `import`/`apply` for `.claude.json`.

## Requirements

- R1. Dashboard. `ManagedServer` keeps metrics: pid, uptime, restart count,
  last error line, in-flight count, active sessions, total calls, total
  errors, calls per minute, and latency p50/p95 over the last 200 calls.
  `/api/status` adds these fields (additive); `/api/servers/{name}/metrics`
  returns the detail. The GUI list shows status, calls/min, p95 and sessions
  columns; a detail pane shows the rest and a small recent-activity list.
  Metrics hold method names and durations only, never arguments or results.
- R2. Tool browser and tester. `GET /api/servers/{name}/tools` returns the
  raw tool list (from the cache, or live) with each tool's allowed/hidden
  state. The GUI has a Tools tab: list with search, a checkbox per tool that
  edits `toolsAllow`/`toolsDeny`, and a "Test" panel that builds a form from
  the input schema (JSON text fallback), calls `POST /api/servers/{name}/call`
  and shows the result. The call route is management-authenticated and
  requires an explicit confirm flag from the GUI for tools whose annotations
  are not read-only.
- R3. Client config sync. A "Sync clients" action writes the hub endpoints
  into the config of each detected client: Claude Code (`~/.claude.json`),
  Cursor, VS Code (user `mcp.json`), others only if their path and format
  are confirmed in the task 1 spike. Every write shows a diff preview first,
  makes a timestamped backup next to the file, and `mcp-hub rollback
  <client>` / a GUI button restores the last backup. Only entries the hub
  owns (marked, as `apply` does today) are touched. CLI: `apply --client
  <name> --dry-run`.
- R4. Secrets. Env values whose key matches the secret pattern can be stored
  in the OS credential store (Windows Credential Manager / DPAPI, macOS
  Keychain, via `keyring`) and referenced in config.json as
  `${secret:<server>/<KEY>}`. The hub resolves references at spawn. The GUI
  edits them masked and offers "move to credential store" for existing
  plaintext secrets. Logs keep redacting. `import` and `apply` never write
  resolved secrets into a client config.
- R5. Groups. `config.groups: {name: [server, ...]}`. The GUI filters and
  bulk starts/stops by group; `apply --group <name>` writes only that group
  into a project `.mcp.json`. No aggregated endpoint and no namespacing (out
  of scope on purpose: it needs tool merging and collision handling).
- R6. Registry install. A Settings switch "Enable registry search" (off by
  default) lets the Add-server dialog search the official MCP Registry,
  cached 6 hours. Selecting an entry only pre-fills the form (command, args,
  env keys); the user reviews it and nothing runs until they save and enable.
  The endpoint and schema are confirmed in the task 1 spike.
- R7. Backward compatibility. Old config.json loads unchanged; all new keys
  are optional; the API stays additive; the GUI degrades gracefully (hides
  columns/tabs) against an older hub.
- R8. GUI cross-cutting. All new network calls run off the GUI thread (the
  pattern of `gui-improvements`); long lists stay responsive; strings are
  Italian/English like the rest; the settings dialog gains the new global
  switches (registry, metrics on/off, secret store status).
