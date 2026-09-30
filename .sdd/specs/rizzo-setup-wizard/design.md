# Rizzo Flow / Jev built-in - Design

- `config.py`: `RizzoSettings` (+ `to_service()`, `rizzo_settings_from_dict`
  validation), `Config.rizzo`, reserved `BUILTIN_RIZZO`. Saved under `rizzo` only
  when non-default; `load_config` adopts a legacy hand-added `rizzo-flow` service.
- `manager.py`: `_sync_builtin()` / `apply_rizzo()` keep a `ManagedServer` for
  the built-in next to the user's servers but never in `config.servers`;
  `status_snapshot` marks it `builtin: true`; `upsert`/`remove` refuse the
  reserved name; `reload_from_disk` applies changed settings.
- `management_api.py`: `GET/PUT /api/rizzo` (validation -> 400); generic
  upsert/remove of the reserved name -> 409. `hub_app` mounts nothing for it.
- `rizzo_setup.py` (no Qt): `detect()`, `SetupRunner` (injectable `SetupEnv`),
  `derive_phase()`, `check_connection()`, plugin config text.
- `gui/rizzo_panel.py`: `RizzoPanel` (scrollable), workers for setup/detect/test,
  Start/Stop through the main window's `_ActionWorker` -> `client.start/stop`;
  settings saved via `client.set_rizzo`, falling back to writing config.json
  only when the hub is unreachable (a 400 is surfaced, not swallowed).
- `main_window.py`: `QTabWidget` (Server | Rizzo Flow / Jev); the builtin status
  is forwarded to the panel and filtered out of the table. Hidden-from-table was
  preferred over a read-only row: the panel already owns everything about it.
- Plugin install: NOT automated. `claude plugin install` writes Claude Code's own
  settings in a config dir the hub cannot know (two are in use on the dev
  machine) and the plugin's userConfig cannot be set from the CLI as far as
  verified, so the tab shows the commands/config with Copy buttons.
- Polling: the tab re-detects every 3 s only while visible (nvidia-smi, disk and
  health are not run in the background) plus once at startup.
