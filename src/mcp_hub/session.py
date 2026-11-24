"""Who asked for what, on an upstream shared by several client sessions
(hub-proxy-hardening R3).

One subprocess serves many SSE sessions, so three things that a client picks
for itself are only unique *within* that client and must be translated:

- the JSON-RPC request id (already rewritten to a hub id by `hub_app`),
- the `_meta.progressToken` of a request,
- the `requestId` of a `notifications/cancelled`.

`RequestTable` remembers, per in-flight request, which session owns it and
which hub-generated progress token stands in for the client's own one.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


def _is_key(value: Any) -> bool:
    """JSON-RPC ids / progress tokens are strings or numbers; anything else
    (None, bool, list, dict) is not a usable key."""
    return isinstance(value, (str, int)) and not isinstance(value, bool)


@dataclass(eq=False)
class HubSession:
    """One connected client (one SSE connection)."""

    # `deliver(obj, related_request_id=None)`: hand a JSON-RPC object to this
    # client. `related_request_id` is the client's own id of the request the
    # message belongs to; Streamable HTTP uses it to pick the right response
    # stream (SSE ignores it).
    deliver: Callable[..., Awaitable[None]]
    # client-chosen request id -> hub id, for requests currently in flight.
    inflight: dict[Any, str] = field(default_factory=dict)


@dataclass
class _Entry:
    session: HubSession
    client_id: Any
    hub_progress: str | None


class RequestTable:
    def __init__(self, server_name: str) -> None:
        self._name = server_name
        self._tokens = itertools.count(1)
        self._requests: dict[str, _Entry] = {}  # insertion order: oldest first
        self._progress: dict[str, tuple[HubSession, Any, Any]] = {}

    def register(self, session: HubSession, client_id: Any, hub_id: str, progress_token: Any) -> str | None:
        """Records an in-flight request. Returns the hub progress token that
        must replace the client's `progressToken` in the forwarded request, or
        None when the request carried no (usable) token."""
        hub_progress: str | None = None
        if _is_key(progress_token):
            hub_progress = f"hubp:{self._name}:{next(self._tokens)}"
            self._progress[hub_progress] = (session, progress_token, client_id)
        self._requests[hub_id] = _Entry(session, client_id, hub_progress)
        if _is_key(client_id):
            session.inflight[client_id] = hub_id
        return hub_progress

    def complete(self, hub_id: str) -> None:
        entry = self._requests.pop(hub_id, None)
        if entry is None:
            return
        if entry.hub_progress is not None:
            self._progress.pop(entry.hub_progress, None)
        if _is_key(entry.client_id) and entry.session.inflight.get(entry.client_id) == hub_id:
            del entry.session.inflight[entry.client_id]

    def progress_owner(self, hub_token: Any) -> tuple[HubSession, Any, Any] | None:
        """(session, the client's original token, the client's request id) for
        a hub progress token, or None when the request already finished (a
        late/stale notification)."""
        if not isinstance(hub_token, str):
            return None
        return self._progress.get(hub_token)

    def oldest_request(self) -> tuple[HubSession, Any] | None:
        """(session, client request id) of the oldest in-flight request: the one
        a server-initiated request (roots/sampling) most plausibly belongs to."""
        for entry in self._requests.values():
            return entry.session, entry.client_id
        return None

    def translate_cancel(self, session: HubSession, client_id: Any) -> str | None:
        """Hub id for a client's `notifications/cancelled.requestId`, or None
        when it is unknown, already completed, or not this session's request
        (the spec lets a receiver ignore such a cancellation)."""
        if not _is_key(client_id):
            return None
        return session.inflight.get(client_id)

    def __len__(self) -> int:
        return len(self._requests)
