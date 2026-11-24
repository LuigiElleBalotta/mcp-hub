# Proxy hardening: security, failure handling, restart, health

## Background

A review of `hub_app.py`, `manager.py` and `management_api.py` (against ideas
from mcp-mux, supergateway, mcp-proxy and the MCP spec 2025-11-25) found real
gaps in the current proxy. Items marked (code) are read directly from the
source; items marked (hypothesis) must be reproduced in task 1 before being
treated as fact.

- (code) `hub.authToken` is stored, shown in the GUI and written into Claude
  Code's config by `apply`, but no route ever validates it. `__main__.py`
  only refuses a non-local bind when it is unset.
- (hypothesis) The management API (`POST /api/servers/{name}` accepts
  `command`/`args`; `/api/shutdown`) has no Origin/Host check and parses the
  body with `request.json()` regardless of Content-Type, so a cross-site
  `text/plain` POST (a CORS "simple request", no preflight) may be accepted:
  a web page could make the hub run a command.
- (code) When the upstream dies, `_reject_pending` sets `ConnectionError` on
  the waiting futures; it is raised inside `handle_message`, which runs in an
  anyio task group, so it tears down the whole SSE connection instead of
  answering that one request with a JSON-RPC error.
- (code) `progressToken` and `notifications/cancelled.requestId` are not
  remapped, and every notification is broadcast to all subscribers: with two
  sessions on one upstream, progress and cancellation can cross sessions.
- (code) No auto-restart: a dead upstream stays `crashed` until a manual start.
- (code) Every client's `initialize` is forwarded to the one shared upstream.
  A respawned upstream would never be initialised while clients believe they
  are. Restart therefore needs a hub-owned handshake.
- (code) Health is "process alive" only for MCP servers.

## Requirements

- R1. Security. (a) When `hub.authToken` is set, every route (MCP and
  management) requires `Authorization: Bearer <token>`, compared in constant
  time; missing/wrong -> 401. (b) Always, regardless of token: reject (403) a
  request whose `Origin` header is present and not in the allow-list, and one
  whose `Host` is not in the allow-list (default: `127.0.0.1`, `localhost`,
  `[::1]` plus the configured `hub.host`, each with the hub port). A request
  without `Origin` (non-browser client) is allowed. Allow-lists are extendable
  via `hub.allowedHosts` / `hub.allowedOrigins`. (c) Management routes that
  accept a body require `Content-Type: application/json` (else 415). (d) The
  GUI, `import`/`apply` and the self-update flow send the token when set.
- R2. Failure answers. When the upstream dies, each in-flight request gets a
  JSON-RPC error response carrying the client's original `id` (code -32000,
  message naming the server); the SSE connection stays open and other
  requests on it are unaffected. Requests are never replayed automatically.
  Requests that arrive while the upstream is down fail fast with the same
  error (no queueing, no hang).
- R3. Session routing. The hub rewrites `params._meta.progressToken` to a
  hub-unique token and keeps a table token -> (session, original token);
  `notifications/progress` goes only to the owning session with the original
  token restored. `notifications/cancelled.requestId` is translated through
  the request table; an unknown or already-completed id is ignored. Other
  notifications (logging, `*/list_changed`) stay broadcast. A server-initiated
  request (roots/sampling/elicitation) goes to the session owning the oldest
  in-flight request, or is answered with a JSON-RPC error if there is none.
- R4. Hub-owned handshake. The hub performs `initialize` +
  `notifications/initialized` once per upstream spawn and caches the result.
  A client's `initialize` is answered from the cache (protocolVersion
  negotiated per client when supported, else the cached one); it is never
  forwarded. A client's `notifications/initialized` is swallowed.
- R5. Auto-restart. New per-server settings `restart` (`"never"` |
  `"on-failure"`, default `"on-failure"`), `restartMaxAttempts` (default 5)
  and `restartWindow` (seconds, default 300). On an unexpected exit the hub
  respawns with exponential backoff (1s doubling to 60s, +/-20% jitter),
  re-runs the handshake (R4), and resets the counter after one stable window.
  Exceeding `restartMaxAttempts` inside the window leaves the server
  `crashed` (circuit open) until a manual start. New status `restarting`.
  A deliberate stop never restarts.
- R6. Health ping. Every `healthInterval` seconds (default 30, 0 = off) the
  hub sends a hub-originated `ping` that bypasses the concurrency guard.
  `healthFailures` (default 3) consecutive failures while there are no
  in-flight client requests mark the server unhealthy and apply R5.
  A ping timeout while a request is in flight does not count.
- R7. Backward compatibility. A config.json without any new field loads and
  behaves as before except for R1b/R1c/R2/R3/R4 (bug fixes) and R5's default.
  The management API stays additive.
- R8. GUI (minimal, full UI lives in `hub-gui-and-clients`): settings dialog
  edits `allowedHosts`/`allowedOrigins` and has a "generate token" button;
  server dialog edits `restart`, `restartMaxAttempts`, `healthInterval`; the
  status column shows `restarting`; GUI requests carry the token.
