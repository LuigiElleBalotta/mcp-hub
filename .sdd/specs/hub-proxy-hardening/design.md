# Proxy hardening — Design

## Components

- `security.py` (new): pure ASGI middleware `HubSecurityMiddleware(app, config)`
  so it does not buffer the SSE stream. Order: Host check -> Origin check ->
  token check -> (management routes only) Content-Type check.
  `hmac.compare_digest` for the token. `/api/pid` also needs the token when
  one is set (the GUI already holds it).
- `config.py`: `HubConfig.allowedHosts/allowedOrigins: list[str]`;
  `ServerConfig.restart/restartMaxAttempts/restartWindow/healthInterval/
  healthFailures` (MCP-only fields, handled like the service-only fields in
  `server_to_dict` so untouched entries keep their on-disk shape).
- `session.py` (new): `HubSession` (id, subscriber callback, in-flight set)
  and `RequestTable` (hub id <-> (session, client id), progress token map).
  `ManagedServer.pending` becomes `RequestTable`-backed; `next_request_id`
  stays.
- `manager.py`:
  - `_read_stdout` routes via the table: responses resolve futures;
    `progress` goes to the owning session; other notifications broadcast.
    `cancelled` is client->server only and is translated in `handle_message`.
  - `_reject_pending` no longer sets `ConnectionError`; it resolves each
    future with a JSON-RPC error object built from the saved client id.
  - `handshake()`: send `initialize` with the hub's own client info and the
    highest protocol version the installed SDK supports, store the result,
    send `notifications/initialized`. Called inside `start()` before status
    becomes `running`; failure -> `crashed` with the error in the log.
  - `_supervise()` replaces the fire-and-forget `_watch`: owns the restart
    loop, backoff, circuit breaker and the `restarting` status; `stop()` sets
    `_stopping` and cancels it.
  - `_health_loop()`: ping task, started after the handshake.
- `hub_app.py`: `handle_message` answers `initialize` from the cache, swallows
  `notifications/initialized`, and never lets an upstream error escape the
  task group (catch -> JSON-RPC error to that client).

## Decisions and risks

- Protocol version: the hub's own handshake pins one version per spawn. A
  client asking for another version gets the cached one; if it cannot speak
  it, it downgrades or fails as the spec defines. Verify with the real
  servers (gitlab, mariadb, chrome-real) in the manual test task.
- Client-dependent capabilities (roots, sampling, elicitation) are not
  available to the upstream, because the hub's own handshake declares none.
  Accepted: spec 2026-07-28 deprecates roots/sampling. Documented in README.
- Default `restart=on-failure` changes behaviour for servers that crash on
  purpose; the 5-attempt circuit breaker bounds it and every attempt is
  logged.
- Ping bypasses the exclusive guard so a long tool call never blocks it; the
  "no in-flight requests" rule prevents false positives.
- R1 is a bug fix and can ship alone as a patch release before the rest;
  task order reflects that.

## Test strategy

Extend `tests/fixtures/fake_stdio_server.py` (die on command, ignore ping,
emit progress with a token, slow tool). New tests: `test_security.py`,
`test_failure_answers.py`, `test_session_routing.py`, `test_handshake.py`,
`test_restart.py`, `test_health.py`. Existing proxy and manager tests must
keep passing.
