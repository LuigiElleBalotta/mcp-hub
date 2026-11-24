# Proxy efficiency: caching, lazy start, session modes, Streamable HTTP, tool filters

Depends on `hub-proxy-hardening` (hub-owned handshake, request table,
supervisor). Do not start before its tasks 3-6 are merged.

## Background

Ideas taken from mcp-mux (cached `initialize` / `*/list`, cache-only lazy
startup, shared/isolated/session-aware modes, graceful upgrade),
supergateway and mcp-proxy (Streamable HTTP, tool filtering/prefix) and the
MCP spec 2025-11-25 (SSE deprecated in favour of Streamable HTTP). Today the
hub spawns every enabled server at startup and keeps it forever, offers only
`exclusive`/`parallel`, speaks only SSE, and its self-update drops every
connection. Installed `mcp` SDK is 1.28.1, which ships
`mcp.server.streamable_http`.

## Requirements

- R1. List cache. `tools/list`, `prompts/list`, `resources/list` and
  `resources/templates/list` without a cursor are answered from a per-server
  cache once filled. The cache is invalidated by the matching
  `notifications/*/list_changed`, by an upstream restart, and by a config
  change of that server. Requests with a cursor are always forwarded.
- R2. Lazy start and idle shutdown. New per-server settings `startMode`
  (`"eager"` default | `"lazy"`), `idleTimeout` (seconds, default 0 = never)
  and `persistent` (bool, default false). A lazy server is not spawned at hub
  start; it is spawned on the first request that cannot be answered from the
  cache. With the cache persisted on disk (`data_dir/cache/<name>.json`,
  keyed by a hash of command+args+env keys) a lazy server can answer
  `initialize` and the first `*/list` without spawning. A non-persistent
  server with no in-flight request and no request for `idleTimeout` seconds
  is stopped (drain: wait for in-flight, then stop) and respawned on demand.
  A server in `isolated` mode (R3) idles per session.
- R3. Session modes. `concurrency` accepts `"exclusive"` (default),
  `"parallel"`, `"isolated"` and `"session-aware"`. `isolated`: one upstream
  process per client session, spawned on connect and stopped on disconnect,
  no shared cache. `session-aware`: one shared upstream (like `parallel`);
  the hub adds `params._meta["io.github.mcp-hub/sessionId"]` to every
  forwarded request so the server can keep per-session state. A server may
  opt in by advertising the same key under `capabilities.experimental` in its
  initialize result, but only an explicit config value is authoritative
  (no tool-name heuristics).
- R4. Streamable HTTP. Each mounted server also serves
  `/<name>/mcp` (POST/GET/DELETE per spec 2025-11-25) next to the legacy
  `/<name>/sse`. `MCP-Session-Id` is issued on `initialize`; an unknown or
  expired id gets 404 so the client reinitialises. Origin/Host/token checks
  of `hub-proxy-hardening` apply unchanged. `hub.preferredTransport`
  (`"sse"` default | `"http"`) selects what `apply` writes into client
  configs. Both transports share the same request table and guard.
- R5. Graceful drain. `POST /api/drain` (and `/api/shutdown`, with
  `drainTimeout`, default 20s) stops accepting new requests (503 +
  `Retry-After`), waits for in-flight requests up to the timeout, then
  proceeds. The GUI self-update uses drain before replacing the exe, and
  after the hub restarts Streamable HTTP clients get 404 and reinitialise.
- R6. Tool filters. Per server `toolsAllow` and `toolsDeny` (lists of glob
  patterns on the original tool name, deny wins) and `toolPrefix` (string).
  `tools/list` responses are filtered and prefixed; `tools/call` for a
  hidden tool or an unprefixed name is answered with JSON-RPC error -32602.
  Applied after the cache, so the cache stores the raw upstream list.
- R7. Backward compatibility. Defaults reproduce today's behaviour (eager,
  no idle shutdown, exclusive, SSE advertised by `apply`, no filters). The
  new `concurrency` values extend the Literal; unknown values are rejected
  with a clear error on load and with 400 on the API.
- R8. GUI (minimal, full UI in `hub-gui-and-clients`): server dialog edits
  `startMode`, `idleTimeout`, `persistent`, the four concurrency modes with a
  one-line explanation each, `toolsAllow`/`toolsDeny`/`toolPrefix`; settings
  dialog edits `preferredTransport` and `drainTimeout`; status column shows
  `idle` for a lazy/idle-stopped server.
