# Proxy hardening — Tasks

## Tasks

- [x] 1. Reproduce the hypotheses safely (a payload that executes nothing): cross-origin text/plain POST accepted by `/api/servers/{name}`; authToken ignored; failure answer tears down the SSE connection; progress crossing sessions. Record the results in this file.
  - Security hypotheses CONFIRMED at hub level (in-memory ASGI client, sandboxed config, `enabled=false`, nothing executed): with `hub.authToken` set, `POST /api/servers/x` without any Authorization header -> 200; same with `Content-Type: text/plain`, `Origin: https://evil.example`, `Host: evil.example` -> 200; `GET /api/status` with hostile Host/Origin -> 200 and lists servers. With `enabled=true` the code path runs `start()` (read, not executed). Not tested: how far a real browser blocks the cross-site request (e.g. Private Network Access); do not rely on it.
  - Failure-answer and progress-crossing hypotheses are reproduced as failing tests at the start of tasks 3 and 4 (test first), not as scripts.
- [x] 2. Security: `security.py` middleware, config fields, token/Origin/Host/Content-Type tests, GUI api_client / apply / self-update send the token
  - Done: `security.py`, `HubConfig.allowedHosts/allowedOrigins` (omitted from config.json when empty, unknown hub keys ignored on load), `HubApiClient` sends the bearer token and re-reads it from config.json on a 401. `apply` already wrote the Authorization header. A hub bound to 0.0.0.0/:: skips the Host check (token mandatory there); `allowedHosts: ["*"]` also skips it.
- [x] 3. Failure answers: JSON-RPC error with the original id, fast-fail while down, connection stays open
- [x] 4. Session routing: `RequestTable`, progressToken remap, cancelled translation, server-initiated request routing
  - Done in `session.py` + `manager._route_progress/_route_server_request` + `hub_app.handle_message`. The hub answers a server `ping` itself. A cancel for an unknown/finished/foreign id is dropped. Limitation: a cancel for a request still queued behind an exclusive guard is dropped too (not yet dispatched, so not in the table); the request then runs.
  - Found while testing (pre-existing): on an `exclusive` server, notifications and the client's answers to server requests also went through the guard, so an answer to a roots/sampling question deadlocked behind the very call asking it. They now bypass the guard (they take no turn). Verified by disabling the fix: the test times out.
- [x] 5. Hub-owned handshake and initialize cache
- [x] 6. Auto-restart supervisor with backoff, circuit breaker, `restarting` status
- [ ] 7. Health ping loop wired to the restart policy
- [ ] 8. GUI: settings dialog (allow-lists, generate token), server dialog (restart/health fields), `restarting` status
- [ ] 9. Docs: README (EN+IT) security section and restart/health settings, `config.example.json`
- [ ] 10. Manual verification with real servers (gitlab, mariadb, chrome-real), then review/merge (`git flow feature finish`); patch release for R1 first if wanted
