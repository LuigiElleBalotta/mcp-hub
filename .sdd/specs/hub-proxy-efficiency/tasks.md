# Proxy efficiency — Tasks

## Tasks

- [ ] 1. Spike: feed the SDK `StreamableHTTPServerTransport` to a transport-independent pump; confirm Claude Code connects with `type: http` and a bearer header. Record the result here; choose SDK transport or hand-written handler.
- [ ] 2. Refactor `_proxy` into `SessionPump` + SSE transport with no behaviour change (existing tests stay green)
- [ ] 3. List cache (memory) with invalidation, then disk cache
- [ ] 4. Lazy start, `ensure_running`, idle watcher, `persistent`, `idle` status
- [ ] 5. Session modes: `isolated` pool and `session-aware` `_meta` injection
- [ ] 6. Streamable HTTP endpoint and `preferredTransport` in `apply`
- [ ] 7. Graceful drain, shutdown/self-update integration
- [ ] 8. Tool filters and prefix
- [ ] 9. GUI: server dialog and settings dialog fields, `idle` status
- [ ] 10. Docs (README EN+IT, `config.example.json`), manual verification with real servers, review/merge
