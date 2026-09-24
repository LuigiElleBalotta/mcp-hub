# GUI robustness, responsiveness, tray, and full settings editing — Tasks

## Tasks

- [x] 1. Fix R1: table no longer blanks on transient `client.status()` errors, adds a connection-lost banner
- [x] 2. Fix R2: responsive table columns + window minimum size + stretch factors between table/log panel
- [x] 3. Implement R3: system tray icon, minimize-to-tray, close-to-tray, "Apri"/"Esci" menu
- [x] 4. Implement R5 backend: `HubManager.remove`, `DELETE /api/servers/{name}`, `HubApiClient.remove`
- [x] 5. Implement R5 GUI: Edit/Remove buttons per row, reusing `ServerDialog`'s `existing` pre-fill
- [x] 6. Implement R4 backend: `PUT /api/settings`, `HubApiClient.update_settings`
- [x] 7. Implement R4 GUI: `SettingsDialog` (host/port/authToken/autostart/checkForUpdates/includeBetaUpdates), wire into `MainWindow`, remove the now-redundant standalone autostart checkbox
- [x] 8. Tests: `HubManager.remove`, `DELETE /api/servers/{name}`, `PUT /api/settings` — 67/67 passing
- [x] 9. Manual verification: ran the real hub + GUI exes against a live config (temp server entry, restored afterward). Confirmed live: responsive column stretch on resize, connection-lost banner, minimize-to-tray + restore from tray, Settings dialog fields, Edit dialog pre-fill, Remove confirm + row disappears.
- [ ] 10. `git flow feature finish` (or push for review, per user's usual flow) once 1–9 are done and green
