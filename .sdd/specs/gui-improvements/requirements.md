# GUI robustness, responsiveness, tray, and full settings editing

## Background

Reported bug: after a while, the server rows in the main window's table
disappear and nothing comes back. Root cause found by inspection:
`MainWindow.refresh()` (`src/mcp_hub/gui/main_window.py`) does
`self.table.setRowCount(0)` on **any** exception from `client.status()`,
including a single transient timeout/connection hiccup. The 2s poll timer
keeps calling `refresh()`, so the table should self-heal on the next
successful call — but every failed tick still blanks it first, with no
indication to the user that anything went wrong. This is the bug to fix.

While in this area, the user also asked for:
- A responsive window (table columns should use available width instead of
  staying at fixed/interactive sizes).
- Minimize-to-tray: minimizing the GUI should hide it to the Windows system
  tray instead of just minimizing to the taskbar.
- Full settings editability from the GUI — today only per-server "Add" and
  "Avvia con Windows" (autostart) exist; host/port/authToken/checkForUpdates/
  includeBetaUpdates are only editable by hand-editing config.json, and there
  is no Edit/Remove for an existing server.

## Requirements

### R1 — Table must not go blank on transient errors
- A single failed `client.status()` call (timeout, connection reset, hub
  briefly unreachable) must NOT clear an already-populated table.
- The table is only cleared to empty when there is genuinely no prior known
  state (first launch, before the hub has ever answered).
- A failed poll surfaces a visible, non-blocking indicator (e.g. a status/
  error label) so the user knows the hub is currently unreachable, distinct
  from "there are zero configured servers".
- Once `client.status()` succeeds again, the indicator clears and the table
  updates normally.

### R2 — Responsive layout
- The table's columns resize proportionally as the window is resized
  (Server/Status/Concurrency stretch, Actions column sized to fit its
  buttons) instead of using fixed/interactive-only column widths.
- The window has a sane minimum size so controls stay usable, but no fixed/
  maximum size that would prevent resizing.
- The log panel and table share vertical space sensibly on resize (table
  should not be starved to zero height, log panel should not be forced
  oversized).

### R3 — Minimize to system tray
- Minimizing the main window hides it and shows a tray icon instead of
  leaving a taskbar-minimized window.
- Clicking/double-clicking the tray icon restores and raises the window.
- The tray icon has a context menu: "Apri" (restore) and "Esci" (quit the
  GUI — does not stop the hub process itself, consistent with the GUI being
  a separate process from the hub per `app.py`/`hub_app.py`).
- Closing the window via the title bar's X also minimizes to tray rather
  than exiting the process outright, so the tray is actually reachable
  (otherwise this feature would be unreachable via the most common close
  action). Real quit only via the tray menu's "Esci".

### R4 — Full settings editable from GUI
- New "Settings" dialog/button exposes every `HubConfig` field for editing:
  `host`, `port`, `authToken`, `autostart`, `checkForUpdates`,
  `includeBetaUpdates`.
- `authToken` is masked (password echo) with a show/hide toggle, consistent
  with existing secret-masking pattern in `server_dialog.py`.
- Saving persists to `config.json` (via `mcp_hub.config.save_config`, same
  as the existing import/apply-to-claude direct-file-access pattern) and,
  for `checkForUpdates`/`includeBetaUpdates` (which the running hub reads
  live via `GET /api/settings`), also pushes the change to the running hub
  via a new settings-update API call so it takes effect without a hub
  restart.
- `host`/`port`/`authToken` changes cannot take effect on the already-bound
  listening socket; the dialog says so (a hub restart is required) rather
  than silently implying they applied immediately.
- `autostart` continues to drive the existing Task Scheduler script
  (`scripts/install_task.ps1`), now also reflecting the config's stored
  value when the dialog opens.

### R5 — Edit and Remove existing servers
- Each table row gets "Edit" and "Remove" actions alongside the existing
  Start/Stop.
- "Edit" reopens `ServerDialog` pre-filled with that server's current
  config (command/args/env/concurrency) and saves via the existing
  `client.upsert` path.
- "Remove" stops the server's process (if running) and deletes it from
  both the running hub's in-memory manager and `config.json`, after a
  confirmation prompt (destructive, mirrors the existing confirm pattern
  used by "Apply to Claude Code config").
- Requires a new hub API endpoint (`DELETE /api/servers/{name}`) since no
  removal path exists today (`HubManager` only has `upsert`, never a
  remove).

## Out of scope
- Changing the hub's listening socket live (still requires restart).
- Verifying/reflecting the *actual* OS Task Scheduler registration state in
  the autostart checkbox (existing behavior: it's a fire-and-forget toggle,
  not a synced read of `schtasks`); not touched beyond wiring it into the
  new Settings dialog.
- Any change to the wire protocol / MCP proxying behavior in `hub_app.py`.
