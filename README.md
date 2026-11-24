# mcp-hub

🇮🇹 [Leggi in italiano — README.it.md](README.it.md)

One shared, long-running process that hosts local MCP servers (mariadb,
headroom, gitlab, figma-bridge, chrome-real, windows-mcp, ...) and exposes
each one over HTTP/SSE, so N Claude Code sessions connect to the same
backend instead of each spawning and paying for its own subprocess tree.

- **Design spec:** `docs/superpowers/specs/2026-09-21-mcp-hub-design.md`
- **Implementation plan:** `docs/superpowers/plans/2026-09-21-mcp-hub-implementation.md`

## Why

Every Claude Code session that lists an MCP server in `.claude.json`
spawns its own copy of that server's process. With several sessions open at
once (several terminals, several projects) you end up with N processes per
server — N Node/`npx` processes for gitlab, N for figma-bridge, N Python
processes for mariadb, etc. mcp-hub starts each server **once**, keeps it
running, and every session talks to that single instance over HTTP/SSE
instead of spawning its own.

## Architecture

```
Claude Code session 1 ─┐
Claude Code session 2 ─┼──HTTP/SSE──▶  mcp-hub (uvicorn/Starlette)
Claude Code session N ─┘                 │
                                          ├─ ManagedServer "gitlab"      (npx, real subprocess, started once)
                                          ├─ ManagedServer "mariadb"     (npx, real subprocess, started once)
                                          ├─ ManagedServer "figma-bridge"(npx, real subprocess, started once)
                                          └─ ...
```

- A **Starlette/uvicorn ASGI app** (`hub_app.py`) mounts one MCP SSE
  endpoint per configured, enabled server: `http://127.0.0.1:37450/<name>/sse`.
- A **process manager** (`manager.py`) owns each backend's real subprocess,
  started once at hub startup, with a redacted rolling log buffer and
  crash detection.
- A **per-server concurrency guard** (`concurrency.py`) serializes calls to
  servers that can't handle concurrent requests (`"exclusive"`, the
  default) or lets them run unserialized (`"parallel"`).
- A **JSON config store** (`config.py`) outside the repo is the single
  source of truth for which servers exist and how to start them.
- A **PySide6 GUI** talks only to the hub's local management HTTP API
  (`management_api.py`) — it never touches `config.json` directly.
- **`import`/`apply` CLI commands** (`claude_config.py`) move server
  definitions between the hub's config and Claude Code's own
  `.claude.json`, so you don't hand-edit both files.
- **`apply --cleanup`** (`cleanup.py`) can reclaim memory from
  already-running legacy per-session server processes after one upfront
  confirmation, protecting the calling process's own ancestor chain.

## Requirements

- Windows (full support: PySide6 GUI, Task Scheduler autostart, Rizzo Flow / Jev, self-update)
  or macOS (experimental, see [macOS](#macos-experimental))
- Python 3.11+

## Installing the exe (end users)

**The easy way: one installer.** Download `mcp-hub-setup.exe` from the repo's
[Releases page](https://github.com/LuigiElleBalotta/mcp-hub/releases) and run
it. It installs per user (no administrator rights) into
`%LOCALAPPDATA%\Programs\mcp-hub\`, adds a Start menu entry and an uninstaller
(Settings → Apps), and offers two options: a desktop icon, and adding `mcp-hub`
to your PATH so the command line works from any terminal (`mcp-hub apply
--client cursor --dry-run`, `mcp-hub secrets list`, …). Your data in
`%LOCALAPPDATA%\mcp-hub\` (config, backups, cache) is never touched by install,
update or uninstall, and uninstalling stops only the programs running from the
install folder. Installing a newer `mcp-hub-setup.exe` over an old one updates it
in place; the GUI's own **Installa e riavvia** button keeps working too.

**The manual way**: no installer, no admin rights, two standalone files:

1. Go to the repo's [Releases page](https://github.com/LuigiElleBalotta/mcp-hub/releases)
   and download `mcp-hub.exe` and `mcp-hub-gui.exe` from whichever version
   you want (a plain `x.y.z` tag is stable; `x.y.z-n` is beta). They stay
   published next to the installer: the GUI's self-update downloads them by
   name, and the CLI needs a console that the windowed GUI exe does not have,
   which is why they are not merged into one file.
2. Put **both files in the same folder** (e.g.
   `C:\Users\<you>\AppData\Local\Programs\mcp-hub\`) — the GUI's self-update
   assumes `mcp-hub.exe` sits right next to it, and this is also the layout
   the auto-updater writes back to.
3. Run `mcp-hub-gui.exe`. With no `config.json` yet, it opens a **first-run
   setup wizard** instead of an empty window: pick "Importa da un
   .claude.json esistente" and choose which imported servers to enable, or
   skip it to start from scratch (an empty config gets written either way,
   editable later by hand or from the GUI). The wizard also starts the hub
   for you, detached, so the GUI has something to connect to right away.
   You can still copy `config.example.json` to
   `%LOCALAPPDATA%\mcp-hub\config.json` yourself instead — the wizard only
   appears when that file doesn't exist yet (see
   [Configuration](#configuration)).
4. From here on, start `mcp-hub-gui.exe` any time to manage servers, or
   `mcp-hub.exe serve` directly if you just want the hub without the GUI.

New versions install with the GUI's **Installa e riavvia** button — no more
manual downloads.

## macOS (experimental)

Releases also ship macOS builds for **Apple Silicon** (`arm64`) and **Intel** (`x86_64`). They are
**not signed or notarized**.

**Status:** on an **Intel** Mac, installing with Homebrew and starting the app are verified (no errors). **Apple Silicon is not verified yet**, and neither is Rizzo Flow / Jev on any Mac. Please report problems.

### Install with Homebrew (recommended)

```
brew tap LuigiElleBalotta/tap
brew trust LuigiElleBalotta/tap
brew install --cask mcp-hub
```

Since Homebrew 6.0 a third-party tap must be trusted before its code runs: `brew trust` is that
one-time consent (without it `brew install` refuses). You can check it with `brew tap-info LuigiElleBalotta/tap`.

This puts `mcp-hub-gui.app` in `/Applications` (the right architecture is picked for you), removes the
download quarantine flag (no "damaged app" warning) and adds the `mcp-hub` command
(`mcp-hub serve | import | apply`). Update later with `brew upgrade --cask mcp-hub`, or with the
**Installa e riavvia** button in the app (it runs that command and reopens the app). Uninstall with
`brew uninstall --cask mcp-hub`.

### Install by hand

1. From the [Releases page](https://github.com/LuigiElleBalotta/mcp-hub/releases) download
   `mcp-hub-gui-macos-<arch>.zip`, where `<arch>` is `arm64` (M1/M2/M3/M4) or `x86_64` (Intel).
   Not sure which Mac you have? Run `uname -m` in Terminal.
2. Unzip it and move `mcp-hub-gui.app` to `/Applications`.
3. Remove the quarantine flag the browser added, otherwise macOS says the app is damaged:

   ```
   xattr -dr com.apple.quarantine /Applications/mcp-hub-gui.app
   ```

   (Or right-click the app, choose **Open**, then confirm.)

The GUI is all you need: it contains the hub too. The command-line tool
(`mcp-hub-macos-<arch>.tar.gz`, then `tar -xzf ... -C ~/.local/bin` and
`xattr -d com.apple.quarantine ~/.local/bin/mcp-hub`) is optional. Updates are then manual.

### Start it

- **Open `mcp-hub-gui.app`.** The GUI starts the hub in the background by itself (on first run, after
  the setup wizard; afterwards whenever it cannot reach the hub), so you do not run anything else.
  The hub keeps running when you close the window; **Esci** in the menu-bar icon stops it, and every
  server it manages, too.
- **Start at login:** System Settings > General > Login Items > **+** > `mcp-hub-gui.app`. The GUI
  then brings the hub up at every login.
- **Without the GUI:** `mcp-hub serve` (CLI), or `/Applications/mcp-hub-gui.app/Contents/MacOS/mcp-hub-gui serve`.
- **Check:** `curl http://127.0.0.1:37450/api/status`.
- Data lives in `~/Library/Application Support/mcp-hub/` (`config.json`, `backups/`). Claude Code's own
  config is `~/.claude.json`; import from and apply to it as described below.
- Apps started from Finder or at login have a minimal `PATH`; mcp-hub adds `/opt/homebrew/bin`,
  `/usr/local/bin`, `~/.local/bin` and `~/.cargo/bin` so `npx`, `uvx`, `uv`, `node` and `git` are found.

### Rizzo Flow / Jev on macOS (experimental)

The **Rizzo Flow / Jev** tab works on macOS too: Rizzo Flow runs on **Apple Silicon** through Metal
(unified memory, about 4.1 GB for the model) and on **Intel Macs** on the CPU only (slow). Install the
prerequisites first, for example `brew install git uv node`, then open the tab and follow the steps. The
default install folder is `~/rizzo-compaction`; the device (`metal` or `cpu`) is chosen for your Mac and
stored as `rizzo.device` in `config.json` (`auto`, `cuda`, `metal` or `cpu`). Upstream reports Metal
working on an M3 Pro but the maintainers have not reproduced it. Not verified through mcp-hub on any Mac yet
(Apple Silicon is the case worth trying; on Intel it would only run on the CPU).

### Not available on macOS yet

The "start at login" switch in Settings (use Login Items as above) and the in-app self-update for
manual installs (use Homebrew, or download the new release). Servers, the management API, the CLI
(`serve`, `import`, `apply`) and the GUI work as on Windows.

## Setup (development)

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

Run the tests to confirm the install is sane:

```
.venv\Scripts\pytest -q
```

`python -m mcp_hub serve` and `python -m mcp_hub.gui` run the same code the
built exes run, straight from source — see
[Running the hub](#running-the-hub) / [Running the GUI](#running-the-gui).

## Configuration

Config lives at **`%LOCALAPPDATA%\mcp-hub\config.json`** — never inside
this repo (the repo only ships `config.example.json` as a template). Copy
it there and edit it by hand, or populate it from an existing
`.claude.json` with `mcp_hub import` (see below).

**Automatic backups:** every time the hub or the GUI saves `config.json`, the
previous file is first copied to `%LOCALAPPDATA%\mcp-hub\backups\config.<YYYYMMDD-HHMMSS>.json`
(identical copies are skipped, the newest 30 are kept). To restore, close the
hub and copy a backup over `config.json`. Backups contain the same secrets as
the config: treat the folder the same way.

```json
{
  "hub": {
    "host": "127.0.0.1",
    "port": 37450,
    "authToken": null,
    "autostart": false,
    "checkForUpdates": true,
    "includeBetaUpdates": false
  },
  "servers": {
    "example-server": {
      "enabled": false,
      "command": "npx",
      "args": ["-y", "some-mcp-server"],
      "env": {},
      "concurrency": "exclusive"
    }
  }
}
```

**`hub` fields:**

| Field                | Default       | Meaning |
|----------------------|---------------|---------|
| `host`               | `127.0.0.1`   | Bind address. Anything other than `127.0.0.1`/`localhost` is refused at startup unless `authToken` is set. |
| `port`               | `37450`       | Bind port. |
| `authToken`          | `null`        | Bearer token. Required if `host` is not localhost. When set, **every** route (MCP and management API) answers `401` without `Authorization: Bearer <token>`. `apply` writes it into the client config; the GUI sends it too (and re-reads it after the hub restarts with a new one). The Settings dialog has a "Genera" button for a random one. |
| `allowedHosts`       | `[]`          | Extra `Host` header values accepted besides `127.0.0.1`, `localhost`, `[::1]` and `host` (each with the port). A request with another `Host` gets `403` (DNS-rebinding protection). A hub bound to `0.0.0.0`/`::`, or `["*"]`, skips this check. Omitted from `config.json` while empty. |
| `preferredTransport` | `"sse"`       | Which endpoint `apply` writes into client configs: `"sse"` → `/<server>/sse` (exactly what it always wrote) or `"http"` → `/<server>/mcp` (Streamable HTTP, the transport that replaces SSE in the MCP spec). Omitted from `config.json` while default. |
| `drainTimeout`       | `20`          | Seconds `/api/shutdown` (so the GUI's quit and self-update) waits for requests already in flight before the hub exits. `0` = don't wait. |
| `metrics`            | `true`        | Collect the per-server dashboard metrics (counters, latency, recent calls; never arguments or results). Needs a hub restart. |
| `registrySearch`     | `false`       | Offer *Search the MCP Registry* in the Add server dialog. Off by default: a search sends its text to registry.modelcontextprotocol.io. |
| `allowedOrigins`     | `[]`          | Extra `Origin` values accepted. A request carrying an `Origin` that is not the hub's own gets `403`, so a web page cannot drive the hub; clients without an `Origin` (Claude Code, the GUI, `curl`) are unaffected. |
| `autostart`          | `false`       | Informational flag mirrored by the GUI's "Avvia con Windows" checkbox, which registers/removes a Windows Task Scheduler entry (see below). |
| `checkForUpdates`    | `true`        | GUI checks GitHub Releases for a newer version on startup and shows a banner if one exists. Never downloads or installs anything without an explicit click on "Installa e riavvia" (see [Auto-update](#auto-update)). |
| `includeBetaUpdates` | `false`       | If `true`, the update check also considers beta (prerelease) tags, not just stable releases. |

**Per-server fields (`servers.<name>`):**

| Field         | Meaning |
|---------------|---------|
| `enabled`     | Whether the hub starts this server at all. Disabled servers are never spawned and get no HTTP mount. |
| `command`     | Executable to run (resolved via `PATH`/`PATHEXT`, so a bare `npx`/`uvx` works on Windows). |
| `args`        | Argument list passed to `command`. |
| `env`         | Extra environment variables, merged on top of the hub process's own environment (so `PATH` etc. are preserved). Any key matching `TOKEN\|SECRET\|PASS\|KEY\|AUTH` (case-insensitive) is redacted (`***`) wherever it's logged or returned by the API — never redacted in the config file itself, since the hub needs the real values to launch the process. |
| `concurrency` | `"exclusive"` (default, serializes every request to this server behind an `asyncio.Lock`) or `"parallel"` (no serialization). Use `"exclusive"` for anything that can't handle overlapping calls (e.g. a stateful browser session); `"parallel"` for stateless/read-only tools. |
| `restart`     | `"on-failure"` (default) or `"never"`. What to do when the process exits without the hub asking it to (see [Reliability](#reliability)). |
| `restartMaxAttempts` | Default `5`. Restarts allowed inside `restartWindow` before the server stays `crashed` until started by hand. |
| `restartWindow` | Default `300` (seconds). Restarts older than this are forgotten, so a server that then ran stably gets a fresh budget. |
| `healthInterval` | Default `30` (seconds, `0` = off). How often the hub pings the server. |
| `healthFailures` | Default `3`. Unanswered pings in a row before the server is considered unresponsive and terminated. |
| `startMode`   | `"eager"` (default, started with the hub) or `"lazy"` (started by the first request the cache cannot answer). A lazy or idle-stopped server shows status `idle`. |
| `idleTimeout` | Default `0` (never). Seconds without a client request after which the server is stopped; it restarts by itself on the next request. Never cuts a request in flight. |
| `persistent`  | Default `false`. `true` = never stop it for idleness (it keeps state in memory). |
| `toolsAllow` / `toolsDeny` | Glob patterns on the server's own tool names (`read_*`, `*delete*`). Empty allow = all; deny wins. See [Efficiency](#efficiency). |
| `toolPrefix`  | Prefix put in front of every tool name the client sees (≤ 64 chars of letters, digits, `_`, `-`, `.`), stripped again on `tools/call`. |

The `restart*`/`health*` fields are omitted from `config.json` while they have
their default value, so an untouched entry keeps its original shape.

### Reliability

- **The hub initialises each server itself.** On the first client request after
  a spawn the hub performs the MCP `initialize` handshake once, caches the
  result, and answers every client's `initialize` from the cache (a client's
  `notifications/initialized` is not forwarded). This is what lets a crashed
  server be respawned without any client noticing it was never initialised.
  Consequence: the upstream sees no per-client capabilities (roots, sampling,
  elicitation); the 2026-07-28 spec deprecates roots and sampling anyway.
- **Failures are answered, not dropped.** If a server dies while a request is
  in flight, that request gets a JSON-RPC error (`-32000`) with the client's own
  id and the connection stays open; a request that arrives while the server is
  down fails fast the same way. Requests are never replayed automatically:
  a tool call may have had side effects.
- **Automatic restart.** An unexpected exit is followed by a restart after
  1 s, 2 s, 4 s … (cap 60 s, ±20 % jitter). The status shows `restarting`.
  After `restartMaxAttempts` restarts inside `restartWindow` the server stays
  `crashed` (circuit open) until you press Start. Start and Stop override a
  pending restart; a deliberate stop is never restarted.
- **Health ping.** Every `healthInterval` seconds the hub sends a `ping`. After
  `healthFailures` unanswered pings in a row the server is terminated and the
  restart policy applies. Pings are skipped while client requests are in
  flight, since a busy single-threaded server answers late without being hung.
- **Per-session routing.** `progressToken` and `notifications/cancelled` are
  remapped, progress goes only to the session that asked for it, and a
  server-initiated request (roots/sampling) goes to the session owning the
  oldest in-flight request, or is refused with an error when none could answer.
  A server `ping` is answered by the hub itself.

### Efficiency

- **`concurrency` has four modes.** `exclusive` (default) serialises requests;
  `parallel` shares one process with no serialisation; `session-aware` is
  `parallel` plus `params._meta["io.github.mcp-hub/sessionId"]` on every request, so a
  server written to read it can keep per-client state (the key is this project's, not
  part of MCP, so other servers just ignore it); `isolated` gives every client session
  its own process, started on its first request and stopped when the session ends
  (nothing is shared: not the cache, not the handshake; the table shows `isolated (n)`).
  There is no automatic classification: you choose the mode.
- **List cache.** `tools/list`, `prompts/list`, `resources/list` and
  `resources/templates/list` are answered from memory once the server has answered
  once, for every client, even while it restarts. Dropped on `*/list_changed` and when
  the process is respawned; paginated requests (`cursor`) are never cached.
- **Lazy start.** A `lazy` server answers `initialize` and its lists from
  `%LOCALAPPDATA%\mcp-hub\cache\<name>.json` (keyed by command line and env *names*)
  without being spawned; it starts when a request really needs it. A deliberate Stop,
  or a crash with `restart: "never"`, is not undone by a request.
- **Two transports.** Every server is served at `/<name>/sse` (legacy) and
  `/<name>/mcp` (Streamable HTTP). The token and Origin/Host checks apply to both.
  Streamable sessions live in memory: after a hub restart an old session id gets `404`
  and the client initialises a new one. Claude Code 2.1.331 was verified against
  `/mcp` with a bearer header, against `/sse` as `type: "http"` (what `apply` has always
  written) and as `type: "sse"`.
- **Tool filters.** `toolsAllow`/`toolsDeny`/`toolPrefix` shape what a client sees and
  may call. Hidden tools vanish from `tools/list` (every page) and calling one is
  refused with `-32602` without reaching the server. The cache keeps the raw list, so
  changing a filter needs no refetch. Only tools are filtered, not prompts or resources.
- **Draining.** `POST /api/drain {"timeout": s}` refuses new MCP traffic (`503` +
  `Retry-After`), waits for requests in flight and reports `{drained, inFlight}`;
  `DELETE /api/drain` resumes. `POST /api/shutdown` replies at once and does the same
  for `drainTimeout` seconds before the hub exits. A client's follow-up request on an
  existing session during a drain also gets `503`, so some clients wait until the hub
  exits and they reconnect.
- **Connected clients survive configuration changes.** Adding, editing or removing
  *another* server used to leave every connected SSE client without a session (it hung);
  transports are now kept per server across route rebuilds.

### Dashboard, tools, clients, secrets, groups

- **Dashboard.** The table shows calls per minute, p95 latency and sessions per server;
  selecting a row opens the **Detail** tab (process, uptime, restarts, sessions, requests
  in flight, calls, errors, cache hits, p50/p95, last error and the recent calls). Only
  method names (plus a tool's *name*) and durations are kept: never arguments or results.
  `GET /api/servers/{name}/metrics` has the same data; `hub.metrics: false` turns it off.
- **Tool tab.** Lists a server's tools with a checkbox each: unticking hides a tool from
  clients, live, with no restart (the sessions stay and clients are told the list
  changed). A tool hidden by a deny *pattern* is shown greyed out: edit the pattern from
  the server dialog. The same tab has a **tester**: an argument form generated from the
  tool's input schema (JSON text when there is none) and the result with its timing. A
  tool that is not declared read-only asks for confirmation first. Reading a lazy or idle
  server's tools starts it, so the list loads only while the tab is open.
- **Sync clients.** `mcp-hub apply --client claude|cursor|vscode` (and the GUI's *Sync
  clients* button) writes the hub's endpoints into Claude Code (`~/.claude.json` or a
  project `.mcp.json`), Cursor (`~/.cursor/mcp.json`, `.cursor/mcp.json`) and VS Code (the
  user `mcp.json` on Windows, `.vscode/mcp.json` in a project). It always plans first and
  shows a diff with secrets masked (`--dry-run` stops there), backs the file up, writes
  atomically, keeps the file's own indentation and touches only entries named like a hub
  server. A file with comments or trailing commas is refused rather than guessed.
  `mcp-hub rollback --client X` restores the newest backup (and is itself undoable).
  `--scope project --project-dir D` targets a project file; `--only a,b` and
  `--group NAME` narrow the servers. The old `apply --to <file>` still works unchanged.
  Not offered: the VS Code user file on macOS/Linux (path not confirmed); Cursor follows
  its documentation but was not run here.
- **Secrets.** An env value can be `${secret:<server>/<KEY>}`: the real value lives in the
  OS credential store (Windows Credential Manager, macOS Keychain, Linux Secret Service)
  under service `mcp-hub`, and is looked up only when the hub starts the process; config.json,
  logs, status and metrics only ever hold the reference. The server dialog has **Sposta i
  segreti nel credential store**; the CLI has `mcp-hub secrets list|migrate|set|delete`
  (never printing a value). With no credential store everything keeps working with plain
  env values. Renaming or removing a server does not delete its stored secrets.
- **Groups.** Named sets of servers (`groups` in config.json): a selector filters the
  table, *Avvia/Ferma gruppo* start and stop a whole set, *Gestisci gruppi…* edits them,
  and `apply --group NAME` writes only that set into a client or project. A group is not
  an endpoint: there is no aggregated MCP server behind it.
- **MCP Registry search** (off by default: *Settings → Abilita la ricerca nel registry
  MCP*). *Add server* then offers **Cerca nel registry MCP…**: it searches the official
  registry (the search text is the only thing sent) and shows the full command line, the
  environment variables and what is still missing. A pick only *pre-fills* the form; you
  read, complete and save it yourself. Only npm (`npx`), pypi (`uvx`) and oci (`docker`)
  stdio packages are offered; remote servers and other package kinds are listed with the
  reason. Results are cached for six hours.

### Rizzo Flow / Jev (built-in)

Rizzo Flow — the local, Jev-compatible model server used by the
`fast-jev-compaction` Claude Code plugin — is **built into the GUI**: open the
**Rizzo Flow / Jev** tab (next to *Server*). There is nothing to add by hand
(no command, args, cwd or health URL):

- **State at a glance**: Not installed / Installing (current step + progress) /
  Installed, stopped / Starting / Running / Error, with GPU and VRAM, port,
  model, `--ctx`, KV cache type and the last health check.
- **Install** (only shown when something is missing): checks the prerequisites
  (git, uv, node, npm, and a GPU: NVIDIA on Windows, Apple Silicon on macOS), clones the two forks, `uv sync`, downloads
  weights + the llama.cpp runtime (~3.9 GB, with bytes/percent/speed), `npm install`
  for the plugin and activates Rizzo Flow in the hub. Every step is skipped
  when already done, can be cancelled (whole process tree is killed) and
  resumed. Free disk space >= 6 GB is checked before the download.
- **Already installed?** The tab detects it on opening and goes straight to
  *Start* / *Stop* (plus *Re-check* and *Repair*, which re-runs `uv sync`,
  `npm install` and the activation, never the download). Start/Stop use the same
  hub calls as any service.
- **Settings** (collapsed by default): install folder, port (8017), weights
  quantization, `--ctx` (16384), KV cache type (`q8_0`) and "start with the hub"
  (off). The compute device (`cuda` on Windows/Linux, `metal` on Apple Silicon, `cpu` on Intel Macs;
  `auto` lets Rizzo choose) is `rizzo.device` in `config.json` / `PUT /api/rizzo`. They are stored under the `rizzo` key of `config.json`; the hub builds
  the `uv run rizzo serve ...` command from them. Saving while the server runs
  stops it (new command line).
- **Use with Claude Code (Jev)**: a *Test connection* button (POSTs one tiny
  `noul` question to `http://127.0.0.1:<port>/v1/systemone` and shows
  OK/latency/model/VRAM), the two `/plugin` commands and the plugin `userConfig`
  (`baseUrl`, `model` = `rizzo-latest`, placeholder `apiKey`, `maxStateTokens`
  6000, `maxRequestTokens` 14000, `maxQuestionsPerRequest` 64) with Copy
  buttons. mcp-hub never writes Claude Code's `settings.json`: installing the
  plugin inside Claude Code stays manual. The plugin uses Claude Code's function hooks
  (an early-access feature): start Claude Code with `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`, otherwise the
  plugin shows as installed but never runs.

Under the hood it is a `service` (see below) named `rizzo-flow`, generated by
the hub from the settings. That name is reserved: the service is not stored in
`servers`, is hidden from the *Server* table, and the generic
`POST/DELETE /api/servers/rizzo-flow` answer `409`. Its Start/Stop/logs use the
usual `/api/servers/rizzo-flow/...` routes; settings use `GET/PUT /api/rizzo`.
A `rizzo-flow` service added by hand with 1.0.6 is adopted into the settings
(its folder and port) when `config.json` is loaded.

### Services (non-MCP processes the hub starts and stops)

A server entry with `"type": "service"` is a plain long-running process —
for example a local HTTP server — that the hub
only starts, stops and monitors. It is **not** an MCP server: its stdio is
not proxied, no `/<name>/sse` route is mounted, and `apply`/`import` never
touch it in Claude Code's config. Existing entries have no `type` and keep
working as `"mcp"`.

| Field           | Meaning (services only) |
|-----------------|-------------------------|
| `type`          | `"service"` (default `"mcp"`). |
| `cwd`           | Working directory of the process. |
| `healthUrl`     | URL polled after start; status stays `starting` until it answers 2xx, then `running`. Without it the service is `running` right after spawn. |
| `port`          | TCP port the service listens on (taken from `healthUrl` when unset). The hub refuses to start while the port is already in use (so it never kills a foreign process) and checks the port is free again after `stop`. |
| `healthTimeout` | Seconds to wait for `healthUrl` (default 120); on timeout the process is stopped and marked `crashed`. |
| `autostart`     | With `enabled`, start it when the hub starts. Default `false`: services are started manually from the GUI (or `POST /api/servers/<name>/start`). |

`stop` terminates the whole process tree (`uv run` spawns child processes).
Output (stdout and stderr) goes to the same redacted log buffer shown in the
GUI. `concurrency` is not used for services. The GUI shows them with type
"Service" and their port instead of a concurrency mode; Start/Stop are
enabled according to the current status. (Rizzo Flow is not one of these
hand-written entries: it is built in, see above.)

## Running the hub

```
.venv\Scripts\python -m mcp_hub serve
```

- Starts every `enabled` server, then serves the ASGI app on
  `hub.host:hub.port`.
- Refuses to start (exits with a clear message) if `host` isn't
  `127.0.0.1`/`localhost` and no `authToken` is set.
- On shutdown (Ctrl-C or otherwise), every managed subprocess is stopped —
  nothing is left orphaned.
- Check it's alive: `curl http://127.0.0.1:37450/api/status`

## Running the GUI

```
.venv\Scripts\python -m mcp_hub.gui
```

The GUI is a thin client over the hub's management API for everything
except first-run setup and importing from Claude Code (below) — those write
`config.json` directly, since there's no hub running yet to route through
the first time.

- **First-run setup wizard**: if `%LOCALAPPDATA%\mcp-hub\config.json`
  doesn't exist yet, launching the GUI shows this instead of an empty
  window — offers to import servers from an existing `.claude.json` and
  pick which ones to enable, or start from an empty config. Either way it
  writes `config.json` and starts the hub for you (detached), so the
  window that follows has something to connect to. Only runs once; after
  `config.json` exists, the GUI opens straight to the server table.
- **Server table**: live status (`stopped`/`starting`/`running`/`crashed`)
  for every configured server, refreshed every 2 seconds, with Start/Stop
  buttons per row.
- **Add server**: opens a dialog to define a new server (command, args,
  concurrency, env vars). Env var fields whose key looks like a secret
  (`TOKEN`/`SECRET`/`PASS`/`KEY`/`AUTH`) render masked by default, with a
  per-row "show" checkbox.
- **Log panel**: select a row to see that server's last 500 (redacted) log
  lines.
- **Avvia con Windows**: registers/removes autostart via Windows Task
  Scheduler (see below).
- **Import from Claude Code / Sync clients**: GUI wrappers around the `import` and
  `apply` commands below (*Sync clients* shows a diff and backs up before writing, for
  Claude Code, Cursor and VS Code; see
  [Dashboard, tools, clients, secrets, groups](#dashboard-tools-clients-secrets-groups)).
- **Update banner**: if a newer version is published on GitHub Releases
  (and `hub.checkForUpdates` is true), a banner appears with a link to
  download it.
- **Installa e riavvia** (only shown when running as the built `.exe`, not
  `python -m mcp_hub.gui`, and only when the release has both exe assets):
  one click downloads `mcp-hub.exe`/`mcp-hub-gui.exe` for the new version,
  then replaces the installed exes and relaunches both — see
  [Auto-update](#auto-update) below for exactly what happens and why it
  needs a click rather than running silently.

## Moving servers to/from Claude Code's config

These commands read/write Claude Code's own `.claude.json` (or a project's
scoped config with `--project`), never the hub's `config.json` directly
except to record what was imported.

**Import** existing server definitions from `.claude.json` into the hub,
disabled by default (so nothing starts until you review and enable them):

```
mcp_hub import --from C:\Users\<you>\.claude.json [--project <scope>]
```

**Apply** enabled hub servers into `.claude.json`, pointing Claude Code at
the hub instead of a locally-spawned process:

```
mcp_hub apply --to C:\Users\<you>\.claude.json [--project <scope>] [--cleanup] [--yes]
```

- Always makes a timestamped backup (`.claude.json.bak-<timestamp>`) next
  to the target file before writing, and verifies the result parses as
  valid JSON before treating the write as successful.
- Rewrites each migrated server's entry to `{"type": "http", "url": "http://<hub host:port>/<name>/sse"}`.
- `--cleanup`: after applying, looks for already-running legacy processes
  matching the migrated servers' `command`/`args` and offers to close them
  (single yes/no confirmation, not a per-process checklist). **Always
  excludes the ancestor-process chain of the process running the command
  itself** — but does *not* know about other terminals/sessions that also
  have their own copies of those servers running; closing those is exactly
  the point of `--cleanup`; run it only when you're fine with other
  sessions' matching processes being closed too (they'll get fresh
  hub-backed connections next time they need that tool).
- `--yes`: skips the interactive confirmation (useful for scripting; still
  prints the plan first).

Typical one-time cutover for an existing `.claude.json`:

```
mcp_hub import --from C:\Users\<you>\.claude.json
:: review %LOCALAPPDATA%\mcp-hub\config.json, set "enabled": true on what you want migrated
mcp_hub serve                                    :: (leave running, or use Task Scheduler autostart)
mcp_hub apply --to C:\Users\<you>\.claude.json --cleanup
:: start a fresh Claude Code session and confirm the migrated tools still work
```

## Migration guide: making the USER-level config win everywhere

`mcp_hub apply` (above) rewrites `mcpServers` entries to point at the hub, at
whichever single scope you point it at (`.claude.json`'s top-level =
**user** scope, applies to every project; `--project <path>` = **project**
scope, applies only there). **Claude Code resolves a server name at
project scope BEFORE user scope** — a project-level entry with the same
name always wins, even if the user-level one already points at the hub.
Left over from before you adopted the hub (or from an old per-project
setup), these silently shadow your hub-backed entry: that project's
sessions keep spawning their own local copy of the server instead of using
the one shared instance. Symptom: `claude mcp list` in that project shows
the raw `command`/`args` line, not the hub's `http://127.0.0.1:37450/<name>/sse`
URL.

### 1. Find the shadowing entries

`.claude.json` also holds a lot of unrelated state (history, caches,
per-project stats) — don't open it in an editor and start deleting by hand.
Save this as `check-overrides.js` (inline `node -e "..."` one-liners get
mangled differently by PowerShell/cmd/bash — a file sidesteps all of that).
It reads the file structurally and only reports project-level server names
that ALSO exist at user scope (i.e. genuine duplicates worth removing — a
project-only server with no user-level counterpart is probably there on
purpose, leave it alone):

```js
// check-overrides.js
const fs = require("fs");
const path = process.env.USERPROFILE + "/.claude.json";
const c = JSON.parse(fs.readFileSync(path, "utf8"));
const userNames = new Set(Object.keys(c.mcpServers || {}));
for (const [proj, cfg] of Object.entries(c.projects || {})) {
  const shadowed = Object.keys(cfg.mcpServers || {}).filter((n) => userNames.has(n));
  if (shadowed.length) console.log(proj, "->", shadowed.join(", "));
}
```

```
node check-overrides.js
```

### 2. Remove them (after reviewing step 1's output)

Save this as `clean-overrides.js`. It always backs up `.claude.json`
first (with a timestamp, next to the original) before touching anything,
and only deletes the project-level names that duplicate a user-level one:

```js
// clean-overrides.js
const fs = require("fs");
const path = process.env.USERPROFILE + "/.claude.json";
fs.copyFileSync(path, `${path}.bak-${Date.now()}`);
const c = JSON.parse(fs.readFileSync(path, "utf8"));
const userNames = new Set(Object.keys(c.mcpServers || {}));
for (const cfg of Object.values(c.projects || {})) {
  if (!cfg.mcpServers) continue;
  for (const name of Object.keys(cfg.mcpServers)) {
    if (userNames.has(name)) delete cfg.mcpServers[name];
  }
}
fs.writeFileSync(path, JSON.stringify(c, null, 2));
console.log("done");
```

```
node clean-overrides.js
```

### 3. Verify

Restart any open Claude Code sessions in the affected project(s) (MCP
config is read at session start, not hot-reloaded), then:

```
claude mcp list
```

The cleaned-up server should now show the hub's `http://127.0.0.1:37450/<name>/sse`
URL instead of a local `command`/`args` line.

### Prompt for an agent

Paste this into a Claude Code session (adjust the paths/server names) to
have an agent do the whole install-and-migrate flow, including this
project-level cleanup, itself:

```
Install and adopt mcp-hub (https://github.com/LuigiElleBalotta/mcp-hub) on
this Windows machine, then migrate my existing Claude Code MCP servers to
it:

1. Download the latest `mcp-hub.exe` and `mcp-hub-gui.exe` from the repo's
   GitHub Releases page and put both in
   `%LOCALAPPDATA%\Programs\mcp-hub\` (create it if missing).
2. Run `mcp-hub.exe import --from %USERPROFILE%\.claude.json`, then show
   me the resulting `%LOCALAPPDATA%\mcp-hub\config.json` and ask me which
   imported servers to set `"enabled": true` on before continuing.
3. Start the hub (`mcp-hub.exe serve`, or set up autostart via
   `scripts\install_task.ps1 -Enable` from a checkout, or just launch
   `mcp-hub-gui.exe`) and confirm `http://127.0.0.1:37450/api/status`
   answers.
4. Run `mcp-hub.exe apply --to %USERPROFILE%\.claude.json --cleanup`
   (confirm the plan it prints before it closes any legacy processes) so
   my USER-level `.claude.json` points enabled servers at the hub.
5. Audit `.claude.json` for PROJECT-level `mcpServers` entries that
   duplicate a name now defined at user scope (project scope wins over
   user scope in Claude Code, so a leftover project-level entry would
   silently keep bypassing the hub for that project). Show me the list
   before touching anything. Back up `.claude.json` first, then remove
   only the duplicate names from each project's `mcpServers` -- never
   touch a project-level server that has no user-level counterpart.
6. Tell me which sessions/projects need a restart to pick this up, and
   verify with `claude mcp list` that the migrated servers now show the
   hub's http URL instead of a local command.

Ask before any destructive step (closing processes, deleting config
entries). Stop and report if anything doesn't match what this describes.
```

## Autostart (Windows Task Scheduler)

```
scripts\install_task.ps1 -Enable    # registers a logon task ("McpHub") that runs `mcp_hub serve`
scripts\install_task.ps1 -Disable   # removes it
```

Also available as the "Avvia con Windows" checkbox in the GUI. Requires
permission to create scheduled tasks for the current user — on a
locked-down/managed machine this can fail with "Accesso negato"; if so,
ask IT to grant Task Scheduler creation rights, or start the hub manually
each session.

## Releases and versioning (git flow)

This repo follows **git flow**: `master` holds released history,
`develop` is the integration branch for ongoing work.

Pushing a version tag triggers `.github/workflows/release.yml`, which
builds two Windows executables with PyInstaller (`mcp-hub.exe` — the CLI,
`serve`/`import`/`apply`; `mcp-hub-gui.exe` — the GUI) and publishes them
as assets on a GitHub Release for that tag:

| Tag pattern         | Example      | Result |
|----------------------|--------------|--------|
| `x.y.z` (`v` prefix optional) | `1.2.0`, `v1.2.0` | Stable release build, published as a normal (non-prerelease) GitHub Release. |
| `x.y.z-n`            | `1.2.0-1`     | Beta build, published as a **prerelease** GitHub Release. |

Any other tag shape is ignored by the workflow.

The GUI's update checker (`hub.checkForUpdates`) polls this same GitHub
Releases list; set `hub.includeBetaUpdates: true` to also be notified about
`x.y.z-n` prereleases, not just stable tags.

## Auto-update

**macOS:** the PowerShell updater below is Windows-only. A copy installed with Homebrew
(`brew install --cask mcp-hub`) updates with `brew upgrade --cask mcp-hub`, and the update banner's
**Installa e riavvia** button runs exactly that, then reopens the app. A manual install shows the banner
with the download link only.

**Install layout this depends on:** `mcp-hub.exe` and `mcp-hub-gui.exe`
must sit in the same folder (exactly how the release workflow builds
them and how you should unzip/place them). The GUI locates the hub exe as
"the file named `mcp-hub.exe` next to my own exe" — `self_update.py`.

Clicking **Installa e riavvia** in the update banner:

1. Confirms once (this restarts the hub — see the warning it shows).
2. Fetches `hub_pid` from the running hub (`GET /api/pid`).
3. Downloads the new `mcp-hub.exe`/`mcp-hub-gui.exe` from the GitHub
   Release's assets to `%LOCALAPPDATA%\mcp-hub\update-staging\`.
4. Launches a detached PowerShell helper that waits for both the hub
   process and this GUI process to actually exit (their exe files are
   locked while running — Windows can't overwrite them in place), then
   copies the new exes over the old ones and relaunches `mcp-hub.exe serve`
   and `mcp-hub-gui.exe`.
5. Tells the hub to shut down gracefully (`POST /api/shutdown` — this is
   the same clean exit path as Ctrl-C, so every managed subprocess is
   stopped rather than orphaned) and then closes its own window, so both
   locks are released and the helper's wait in step 4 unblocks.

This only ever runs after your click — nothing downloads or restarts
anything on its own. It only appears at all when running the built exe
(`sys.frozen`); in a dev checkout (`python -m mcp_hub.gui`) you still get
the banner and its manual download link, since there's no installed exe
for the GUI to replace.

**What it does to other open Claude Code sessions:** the same thing the
hub restarting for any other reason would — every session currently
talking to the hub loses its connection for the few seconds the hub takes
to relaunch, then reconnects automatically. This is exactly why the button
requires a click and a warning instead of running unattended.

## Security notes

- The hub refuses to bind anywhere other than `127.0.0.1`/`localhost`
  unless `hub.authToken` is set — enforced at startup, not just suggested
  in the GUI.
- Up to 1.1.2 `hub.authToken` was **not checked on requests**, and the
  management API (which can add and start any command) had no `Origin`/`Host`
  check, so a web page could in principle have driven a hub reachable on
  localhost. Since this version every request is checked: `Host` and `Origin`
  allow-lists always, the bearer token when one is set, and management
  routes that carry a body must be `application/json`. If you ran an older
  version on a machine where you browse untrusted pages, review the servers in
  `config.json`.
- Every log line (hub log, per-server ring buffers, the management API's
  `/api/servers/{name}/logs`) redacts the value of any env/config key
  matching `TOKEN|SECRET|PASS|KEY|AUTH` (case-insensitive) before it's
  written or returned — the raw secret only ever exists in
  `%LOCALAPPDATA%\mcp-hub\config.json` and the spawned process's actual
  environment.
- `apply` never overwrites `.claude.json` without a timestamped backup and
  a post-write JSON-validity check.

## Development

```
.venv\Scripts\pytest -q                 # run the full test suite
.venv\Scripts\python scripts\smoke_test.py <server-name>   # compare a hub-proxied tool call against a direct stdio call
.venv\Scripts\python scripts\concurrency_check.py           # verify exclusive-mode serialization under real concurrent load
```

Project layout, task-by-task history, and the exact interfaces each module
exposes are in the implementation plan linked at the top of this file.
