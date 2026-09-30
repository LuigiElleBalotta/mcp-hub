# Rizzo Flow / Jev built into mcp-hub

## Background

Rizzo Flow (local Jev-compatible server) and the `fast-jev-compaction` plugin
fork were installed with `team-skills/scripts/setup-rizzo-compaction.ps1`
(command line) and, in 1.0.6, could only be started by hand-adding a generic
`service`. Design decision: Rizzo Flow + Jev are a **built-in** function of
mcp-hub: a dedicated GUI tab, no generic server row to fill in.

## Requirements

- R1. A first-class "Rizzo Flow / Jev" tab, visible by default next to "Server".
  It shows the state at a glance (Not installed / Installing with step and
  progress / Installed-stopped / Starting / Running / Error / Hub unreachable),
  GPU + VRAM, port, model, ctx, KV type and the last health check.
- R2. Detection comes first: `rizzo_setup.detect()` (no side effects) reports
  prerequisites, the install dir (hub settings, else the default
  `C:\repositories\mindicity\utils\claude-code\rizzo-compaction`), which steps are
  done, server health and whether Rizzo is activated in the hub. An existing
  install opens directly in the installed state with Start/Stop, no wizard.
- R3. Install steps (same layout/branches as the PowerShell script):
  prerequisites, clone rizzo-flow (fork, `custom`), `uv sync --locked`, download
  weights + runtime, clone fast-jev-compaction (fork, `custom`), `npm install`,
  activate in the hub, show the plugin config. Idempotent (done steps are
  skipped), run off the GUI thread, stop at the first failure (later steps stay
  pending), cancellable (process tree killed) and resumable. Disk >= 6 GB free is
  checked before the download; progress comes from the bytes on disk because
  the CLI is silent without a terminal.
- R4. Settings with sane defaults, simple fields only: install folder, port 8017,
  quantization, ctx 16384, KV type q8_0, autostart (off). Persisted under the
  `rizzo` key of `config.json` (older hubs ignore it). Nobody types
  command/args/cwd/healthUrl.
- R5. The hub builds the service from the settings (`RizzoSettings.to_service`,
  reusing `ServerConfig(type="service")` + `ManagedServer`). The reserved name
  `rizzo-flow` is never stored in `servers`, is hidden from the generic table,
  and generic upsert/remove are refused (API 409). A 1.0.6 hand-added
  `rizzo-flow` service is adopted into the settings on load.
- R6. "Use with Claude Code (Jev)": plugin userConfig (baseUrl, rizzo-latest,
  placeholder apiKey, 12000/14000/64) and the two `/plugin` commands with Copy
  buttons, plus a live connection test (one `noul` question, shows
  OK/latency/model/VRAM). `~/.claude*/settings.json` is never written; installing
  the plugin stays manual (see design).
- R7. The Repair action re-runs `uv sync`, `npm install` and the activation, never
  the download.
