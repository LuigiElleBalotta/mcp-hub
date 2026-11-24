# GUI and client integration — Design

## Components

- `metrics.py` (new): `ServerMetrics` (bounded deque of
  (method, duration, ok), counters, restart count, last error). Fed from
  `SessionPump` at the single point where a request completes.
  `status_snapshot()` reads it; no lock needed (single event loop).
- `management_api.py`: `/api/servers/{name}/metrics`, `/tools`, `/call`,
  `/api/groups`, `/api/registry/search`, `/api/secrets/*` (set/delete/exists,
  never returns a value). All behind the security middleware of
  `hub-proxy-hardening`.
- `client_sync.py` (new, extends `claude_config.py`): a `ClientAdapter`
  per client (`detect()`, `read()`, `plan(entries) -> diff`, `write()`,
  `backup()`, `rollback()`), with Claude Code first; new adapters only once
  their path/format is confirmed. Atomic write via temp file + replace.
  Hub-owned entries are identified by a marker key so user entries are never
  modified.
- `secrets.py` (new): thin wrapper over `keyring` with the reference syntax
  and the resolver called by `ManagedServer.start()`. Missing secret ->
  clear start error, never an empty value. PyInstaller spec gets the
  keyring backend hidden imports; macOS/Windows both tested in CI build.
- `registry.py` (new): client for the official MCP Registry with a 6h disk
  cache and a hard timeout; returns normalised draft `ServerConfig` objects.
- `config.py`: `groups`, `hub.metrics` (bool), `hub.registrySearch` (bool).
- GUI (`gui/`): `main_window.py` gets the metric columns and a detail pane
  (`detail_pane.py`, new); `tools_panel.py` (new tab with tool list, filter
  checkboxes and tester); `client_sync_dialog.py` (new, diff preview +
  rollback); `server_dialog.py` gets secret handling and the group field;
  `settings_dialog.py` gets the global switches. `api_client.py` gains the
  new calls, all invoked via the existing worker helper so the GUI thread
  never blocks.

## Decisions and risks

- Metrics never record arguments or results (privacy and log-secret risk).
- The tester can run destructive tools, so it asks for confirmation unless
  the tool declares `readOnlyHint`, and shows the exact call first.
- Registry results are untrusted input: pre-fill only, show the full command
  line, no auto-enable, no auto-run.
- `keyring` adds a dependency and a backend per platform; if it is not
  available the feature is hidden and plaintext env keeps working.
- Groups instead of an aggregated endpoint keeps the proxy simple; revisit
  only if a real need appears.
- Client file formats change over time: adapters are isolated, each with a
  fixture-based test, and a failed parse aborts without writing.

## Test strategy

`test_metrics.py`, `test_tools_api.py`, `test_client_sync.py` (fixtures for
each client, backup/rollback round trip), `test_secrets.py` (fake keyring),
`test_registry.py` (recorded responses), GUI tests following
`tests/test_gui_service.py` and `tests/test_gui_rizzo_panel.py`.
