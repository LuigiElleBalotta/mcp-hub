# Service servers (non-MCP processes the hub starts/stops)

## Background

The hub manages MCP servers over stdio. The team also needs long-running
non-MCP processes, first of all Rizzo Flow (local HTTP server used as a
Jev-compatible backend for Claude Code compaction), started and stopped from
the same place and the same GUI instead of a hand-run terminal.

## Requirements

- R1. `ServerConfig.type` is `"mcp"` (default, backward compatible) or
  `"service"`; service-only fields: `cwd`, `healthUrl`, `port`,
  `healthTimeout`, `autostart`. MCP entries keep their on-disk shape.
- R2. A service is spawned with the existing process manager (redacted log
  buffer, crash detection) but without stdio proxying; no `/<name>/sse`
  route; never exported by `apply`.
- R3. Start is manual by default; `enabled` + `autostart` starts it with the
  hub. Status is `starting` until `healthUrl` answers 2xx, then `running`;
  timeout stops it and marks it `crashed`.
- R4. Start is refused if the port is already in use. Stop kills the whole
  process tree and verifies the port is released.
- R5. Management API: existing start/stop/logs/upsert routes work for
  services; `/api/status` exposes `type` (and `port` for services); bad
  `type`/missing command -> 400.
- R6. GUI: Type column, service shows its port instead of concurrency,
  Start/Stop enabled by status, start/stop run off the GUI thread, selected
  server's log refreshes each tick without losing scroll, Add/Edit dialog
  switches fields by type, Actions column never overlaps other columns.
