# GUI and client integration — Tasks

## Tasks

- [x] 1. Spike: confirm config paths and formats for Cursor and VS Code, and the MCP Registry endpoint and schema; decide which client adapters ship. Record the result here.
  - Claude Code: `.claude.json` / `.mcp.json`, key `mcpServers`, remote entry `{type: "http", url, headers}` (already what `apply` writes). Project `.mcp.json` VERIFIED in the docs.
  - Cursor: `~/.cursor/mcp.json` (user) and `.cursor/mcp.json` (project), key `mcpServers`, remote entry `{url, headers}` (no `type` in the docs' remote examples; SSE and Streamable HTTP both supported, how it tells them apart is not documented). VERIFIED in the docs (cursor.com/docs/context/mcp). Cursor is NOT installed on this machine, so no real file was read.
  - VS Code: the user file `%APPDATA%/Code/User/mcp.json` on Windows is CONFIRMED by the file existing on this machine (structure read, values not): top-level `servers` + `inputs`, entries carry `type`. Workspace `.vscode/mcp.json` VERIFIED in the docs: key `servers`, remote entry `{type: "http"|"sse", url, headers}`, optional `inputs` for prompted secrets. The macOS/Linux user paths (`~/Library/Application Support/Code/User/mcp.json`, `~/.config/Code/User/mcp.json`) are from memory: NOT shipped until confirmed.
  - Registry: `https://registry.modelcontextprotocol.io/v0.1/servers?search=<3+ chars>&limit=<=100>&version=latest&cursor=`; reads need no auth; response `{servers:[{server:{name,description,version,packages[],remotes[]},_meta}],metadata:{count,nextCursor}}`; a package has `registryType, identifier, transport, version?, runtimeHint?, environmentVariables[], packageArguments[], runtimeArguments[]`; a remote has `type` (streamable-http|sse), `url`, `headers[]`. Rate limits are not documented. The full OpenAPI spec was saved locally for task 8 (the inner fields of environmentVariables/packageArguments are still to be read there).
  - DECISION - adapters that ship: Claude Code, Cursor (user + project), VS Code (Windows user file + any workspace `.vscode/mcp.json`). Not shipped: the VS Code user file on macOS/Linux.
- [x] 2. Metrics collection and `/metrics` route; status snapshot additions
  - `metrics.py` (`ServerMetrics`: counters, nearest-rank p50/p95 over the last 200 calls, calls/min, last 50 calls as method+ms+ok), `ManagedServer.record_call/metrics_detail/session_count`, `GET /api/servers/{name}/metrics`, additive fields on `/api/status` (pid, uptime, sessions, inFlight, calls, errors, callsPerMin, p50, p95), `hub.metrics` switch (default on, omitted while default). Privacy proven by a test: a secret argument never appears in any metric, only the tool NAME is kept. An isolated server's numbers aggregate its sessions' processes.
- [ ] 3. GUI dashboard: metric columns and detail pane
- [ ] 4. Tools API (`/tools`, `/call`) and the Tools tab with filter checkboxes and tester
- [ ] 5. Client sync: adapter framework, Claude Code adapter, diff preview, backup, rollback, CLI flags, then the confirmed extra adapters
- [ ] 6. Secrets: `keyring` wrapper, `${secret:...}` resolution, GUI masked editing and "move to credential store", packaging
- [ ] 7. Groups: config, GUI filter and bulk actions, `apply --group`
- [ ] 8. Registry search and prefill in the Add-server dialog, opt-in switch
- [ ] 9. Settings dialog and old-hub degradation, i18n strings
- [ ] 10. Docs (README EN+IT), manual verification, review/merge, release notes
