# Service servers — Design

- `config.py`: new fields + `is_service` / `starts_with_hub` /
  `effective_port`; `server_to_dict` drops service-only keys for MCP entries.
- `manager.py`: `ManagedServer._start_service/_watch_service/_wait_healthy`;
  `stop()` shared (psutil tree kill) plus a port-released check for services;
  `_stopping` flag so a deliberate stop is never reported as a crash.
  `start_all`/`upsert`/`reload_from_disk` use `starts_with_hub`.
- `hub_app.py`: mounts only enabled non-service servers. `claude_config.py`:
  `apply_servers` skips services.
- `management_api.py`: validation (400) on upsert; no new routes.
- GUI: see R6; Actions column width is applied explicitly
  (`_fit_actions_column`) because Qt's ResizeToContents ignores cell widgets
  and Fixed ignores the width set.
