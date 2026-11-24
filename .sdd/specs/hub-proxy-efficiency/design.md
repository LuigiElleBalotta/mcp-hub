# Proxy efficiency — Design

## Components

- `cache.py` (new): `ListCache` per `ManagedServer` (dict method -> result,
  generation counter) plus `DiskCache` (JSON file per server, atomic write,
  loaded at start, ignored when the key hash differs). The proxy asks the
  cache first; a miss goes to the upstream and fills it.
- `manager.py`: new status `idle`. `ensure_running()` is the single entry the
  proxy calls before forwarding: spawns a lazy/idle server, serialised by an
  asyncio lock so concurrent first requests spawn once, and waits for the
  handshake. `_idle_watcher()` task implements R2 using the in-flight count
  of `RequestTable`. `IsolatedPool` holds one `ManagedServer`-like upstream
  per session for R3 `isolated` (same start/stop/handshake code, no shared
  cache).
- `hub_app.py`: `_proxy` is split into a transport-independent
  `SessionPump` (read side -> table -> upstream, upstream -> session) and two
  thin transports: the existing SSE one and a new Streamable HTTP one built
  on `mcp.server.streamable_http.StreamableHTTPServerTransport`. The task 1
  spike must prove the SDK transport can be fed to `SessionPump` without
  its own session manager owning the lifecycle; the fallback is a small
  hand-written Streamable HTTP handler over the same pump.
- `filters.py` (new): pure functions `filter_tools(list, allow, deny, prefix)`
  and `resolve_call(name, ...)`, easy to unit test.
- `management_api.py`: `/api/drain`; `/api/shutdown` accepts `drainTimeout`;
  `self_update.py` calls drain first.
- `claude_config.py`: `apply` writes `type: "sse"` or `type: "http"` and the
  matching URL (`/sse` or `/mcp`) per `hub.preferredTransport`.

## Decisions and risks

- `session-aware` uses a namespaced `_meta` key, not mcp-mux's own key, to
  avoid a de facto dependency on another project. Servers must be adapted to
  read it; for those that are not, it is harmless.
- No tool-name heuristics for classification (mcp-mux does that and its own
  README calls it an inference). The user chooses the mode explicitly; the
  GUI explains each.
- Disk cache can serve stale lists when a server changes on upgrade; the
  cache key includes the command line and a version string if the server
  reports one in `serverInfo`, and a GUI "clear cache" action (in
  `hub-gui-and-clients`) exists.
- `isolated` multiplies processes by sessions; the GUI shows the count. It
  is opt-in per server.
- Idle shutdown loses in-memory server state; hence `persistent`, and the
  default stays 0 (never).
- Streamable HTTP in Claude Code: verify the client version supports the
  `http` type and header auth before flipping any default. Default stays SSE.

## Test strategy

`tests/test_list_cache.py`, `test_lazy_idle.py`, `test_modes.py`,
`test_streamable_http.py`, `test_drain.py`, `test_filters.py`; reuse and
extend the fake stdio server. Concurrency tests assert at most one spawn
under parallel first requests.
