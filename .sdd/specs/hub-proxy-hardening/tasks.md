# Proxy hardening — Tasks

## Tasks

- [ ] 1. Reproduce the hypotheses safely (a payload that executes nothing): cross-origin text/plain POST accepted by `/api/servers/{name}`; authToken ignored; failure answer tears down the SSE connection; progress crossing sessions. Record the results in this file.
- [ ] 2. Security: `security.py` middleware, config fields, token/Origin/Host/Content-Type tests, GUI api_client / apply / self-update send the token
- [ ] 3. Failure answers: JSON-RPC error with the original id, fast-fail while down, connection stays open
- [ ] 4. Session routing: `RequestTable`, progressToken remap, cancelled translation, server-initiated request routing
- [ ] 5. Hub-owned handshake and initialize cache
- [ ] 6. Auto-restart supervisor with backoff, circuit breaker, `restarting` status
- [ ] 7. Health ping loop wired to the restart policy
- [ ] 8. GUI: settings dialog (allow-lists, generate token), server dialog (restart/health fields), `restarting` status
- [ ] 9. Docs: README (EN+IT) security section and restart/health settings, `config.example.json`
- [ ] 10. Manual verification with real servers (gitlab, mariadb, chrome-real), then review/merge (`git flow feature finish`); patch release for R1 first if wanted
