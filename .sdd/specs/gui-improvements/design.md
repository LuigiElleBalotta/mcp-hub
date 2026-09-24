# GUI robustness, responsiveness, tray, and full settings editing — Design

## Overview

All changes are additive to the existing hub-API / Qt-GUI split
(`management_api.py` + `manager.py` on the hub side, `gui/*.py` on the
client side). No change to MCP proxying (`hub_app.py::_proxy`/`_mount_for`).

## R1 — Table must not go blank on transient errors

`main_window.py::refresh()`:
- Add `self._last_status_ok: bool = True` (or similar) and a small
  `QLabel self.connection_banner` near the top (reuse the existing
  `update_row` `QHBoxLayout` pattern, styled like `update_banner` but red/
  amber) that starts hidden.
- On `client.status()` exception:
  - If the table already has rows (`self.table.rowCount() > 0`) or this
    isn't the very first call, **do not** call `setRowCount(0)`. Leave the
    last-known rows in place.
  - Show `connection_banner` with e.g. "Hub non raggiungibile — nuovo
    tentativo tra 2s...".
  - Only clear to 0 rows when there has never been a successful status yet
    (covers the genuine first-run "hub still starting" case already
    commented in the code).
- On success: hide `connection_banner` if visible, then repopulate the
  table as today.

## R2 — Responsive layout

`main_window.py.__init__`:
- `self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)`
  for columns 0–2 (Server/Status/Concurrency), and
  `setSectionResizeMode(3, QHeaderView.ResizeToContents)` for Actions (needs
  `from PySide6.QtWidgets import QHeaderView`).
- `self.setMinimumSize(480, 320)` on the window (or on `central`), no
  `setFixedSize`/`setMaximumSize` anywhere (confirmed none exist today).
- `layout.setStretchFactor` (via `QVBoxLayout::addWidget(w, stretch)`)
  giving the table a stretch of e.g. 2 and the log panel 1, so both grow on
  resize but the table gets priority — table only, log panel stays
  reasonably sized. `app.py`'s `window.resize(700, 400)` stays as the
  initial size (unrelated to responsiveness, just the default).

## R3 — Minimize to system tray

New in `main_window.py`:
- `self.tray_icon = QSystemTrayIcon(self.style().standardIcon(QStyle.SP_ComputerIcon), self)`
  built in `__init__`. No new binary asset — reuses a Qt standard icon
  (consistent with not having any existing `.ico`/`.png` asset in the repo
  today).
- Context menu (`QMenu`) with "Apri" (`self.show(); self.raise_(); self.activateWindow()`)
  and "Esci" (`self._quit()` → `self.tray_icon.hide(); QApplication.quit()`).
  `tray_icon.activated` signal (on `Trigger`/double-click) also restores.
- Override `changeEvent`: when `event.type() == QEvent.WindowStateChange`
  and `self.isMinimized()`, hide the window and show a
  `tray_icon.showMessage(...)` balloon once, instead of leaving it
  taskbar-minimized.
- Override `closeEvent`: if the tray icon is visible, `event.ignore()` +
  hide-to-tray instead of closing (matches R3's requirement that the X
  button also goes to tray, with real quit only from the tray menu).
- `tray_icon.show()` called once at the end of `__init__`.
- This only changes GUI-process lifecycle; the hub process
  (`mcp_hub serve`) is untouched — "Esci" only ends the GUI, same as today
  closing the window would (`_launch_hub()`/autostart keep the hub
  independently alive already).

## R4 — Full settings editable from GUI

**New hub API** (`management_api.py`):
- `PUT /api/settings` — body `{"checkForUpdates": bool, "includeBetaUpdates": bool}`,
  updates `manager.config.hub.checkForUpdates`/`.includeBetaUpdates` in
  place and calls `save_config(manager.config)`, returns the updated
  settings dict (same shape as the existing `GET`). These two fields are
  the only ones the *running* hub process actually reads live
  (`_check_for_updates`), so they're the only ones round-tripped through
  the hub; see below for the rest.

**`api_client.py`**: add
```python
def update_settings(self, **fields) -> dict:
    return self._client.put("/api/settings", json=fields).json()
```

**New `gui/settings_dialog.py`** (`SettingsDialog(QDialog)`), modeled on
`server_dialog.py`:
- Loads current values via `mcp_hub.config.load_config()` directly (same
  direct-file-access pattern already used by `_import_from_claude`), not
  through the hub API, since `host`/`port`/`authToken`/`autostart` aren't
  hub-API-exposed and don't need to be — the GUI and hub share the same
  `config.json` on the same machine.
- Fields: `host` (QLineEdit), `port` (QLineEdit, int-validated), `authToken`
  (QLineEdit, password echo + a "Show" QCheckBox exactly like
  `server_dialog.py`'s env-var masking), `autostart` (QCheckBox),
  `checkForUpdates` (QCheckBox), `includeBetaUpdates` (QCheckBox).
- A static `QLabel` warns: "Host/porta/token: serve riavviare l'hub per
  applicare."
- On accept (`MainWindow._open_settings`):
  1. `config = load_config(); config.hub.host = ...; ...; save_config(config)`
     — persists everything, including host/port/authToken, directly to
     `config.json`.
  2. `self.client.update_settings(checkForUpdates=..., includeBetaUpdates=...)`
     — best-effort (wrapped in `try/except`, same tolerance as
     `_check_for_updates`) so the *running* hub also picks up these two
     live, without needing a restart.
  3. If the `autostart` checkbox value changed versus what it was when the
     dialog opened, call the existing `_toggle_autostart`-style
     `install_task.ps1 -Enable/-Disable` (moves that call out of the
     always-visible checkbox in `MainWindow` into the dialog's accept
     handler; the standalone `self.autostart_checkbox` in `MainWindow` is
     removed in favor of this dialog to avoid two places editing the same
     field).
- New "Settings" `QPushButton` added to `MainWindow`'s button row, opening
  `SettingsDialog(parent=self)`.

## R5 — Edit and Remove existing servers

**`manager.py`** — add to `HubManager`:
```python
async def remove(self, name: str) -> None:
    existing = self._servers.pop(name, None)
    if existing is not None:
        await existing.stop()
    self.config.servers.pop(name, None)
```
(mirrors `upsert`'s existing stop-before-drop safety; `pop` with default is
a no-op if the name is already gone, so this is safe to call idempotently.)

**`management_api.py`** — add:
```python
async def remove(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    await manager.remove(name)
    save_config(manager.config)
    return JSONResponse({"status": "removed"})
```
registered as `Route("/api/servers/{name}", remove, methods=["DELETE"])`
(same path as the existing POST upsert route, different method — Starlette
dispatches by method, so this is safe to add alongside it).

**`api_client.py`**: add
```python
def remove(self, name: str) -> None:
    self._client.delete(f"/api/servers/{name}")
```

**`main_window.py::refresh()`**: in the per-row `actions` `QHBoxLayout`,
add `edit_btn`/`remove_btn` next to the existing `start_btn`/`stop_btn`.
- `edit_btn` → `self._edit(name)`:
  ```python
  def _edit(self, name: str) -> None:
      from mcp_hub.config import load_config
      config = load_config()
      existing_sc = config.servers.get(name)
      dialog = ServerDialog(name, existing=asdict(existing_sc) if existing_sc else None, parent=self)
      if dialog.exec():
          self.client.upsert(dialog.result_name(), dialog.result_config())
          self.refresh()
  ```
  (reuses `ServerDialog`'s already-existing `existing` pre-fill parameter,
  currently unused by any caller in `main_window.py` — `_add_server` always
  passes `existing=None`.)
- `remove_btn` → `self._remove(name)`: `QMessageBox.question` confirm
  ("Rimuovere '{name}'? Il processo verrà fermato."), then
  `self.client.remove(name); self.refresh()` on Yes.

## Testing

- `tests/test_manager.py`: add a test for `HubManager.remove` (stops a
  running managed server, drops it from both `_servers` and
  `config.servers`; no-op on an unknown name).
- `tests/test_main.py` or a new `tests/test_management_api.py` (check which
  exists — management_api currently untested directly per the file list,
  `test_hub_app_proxy.py` covers proxying not the management routes): add
  coverage for `DELETE /api/servers/{name}` and `PUT /api/settings`.
- GUI code (`gui/*.py`) has no existing test coverage in `tests/` (Qt
  widgets); consistent with that, no new GUI unit tests are added — verified
  manually instead (see tasks.md).
