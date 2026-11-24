"""Per-server activity metrics for the dashboard (hub-gui-and-clients R1).

What is kept, deliberately little: counters, the durations of the last
`WINDOW` calls, and a short list of recent calls as (time, method, ms, ok).
NEVER arguments, results or anything a tool returned: those can hold secrets
and personal data, and a dashboard has no use for them. The only identifier
kept besides the JSON-RPC method is a tool's NAME for `tools/call`.
"""
from __future__ import annotations

import collections
import math
import time
from dataclasses import dataclass, field

WINDOW = 200  # calls the latency percentiles look at
RECENT = 50  # calls kept for the "recent activity" list
MAX_LABEL = 80


def label_for(method: str, params: object) -> str:
    """`tools/call` -> `tools/call <tool name>`; every other method as it is.
    Only the tool name is read from `params`, never its arguments."""
    if method == "tools/call" and isinstance(params, dict) and isinstance(params.get("name"), str):
        return f"{method} {params['name']}"[:MAX_LABEL]
    return method[:MAX_LABEL]


@dataclass
class Call:
    at: float  # wall clock, for display
    label: str
    ms: float
    ok: bool


@dataclass
class ServerMetrics:
    enabled: bool = True
    calls: int = 0
    errors: int = 0
    cache_hits: int = 0
    last_error: str | None = None
    _durations: collections.deque = field(default_factory=lambda: collections.deque(maxlen=WINDOW), repr=False)
    _recent: collections.deque = field(default_factory=lambda: collections.deque(maxlen=RECENT), repr=False)
    _times: collections.deque = field(default_factory=collections.deque, repr=False)  # monotonic, last minute

    def record(self, label: str, seconds: float, ok: bool, error: str | None = None) -> None:
        if not self.enabled:
            return
        self.calls += 1
        ms = max(0.0, seconds * 1000.0)
        self._durations.append(ms)
        self._recent.append(Call(time.time(), label, round(ms, 1), ok))
        self._times.append(time.monotonic())
        if not ok:
            self.errors += 1
            if error:
                self.last_error = error[:300]

    def record_cache_hit(self) -> None:
        if self.enabled:
            self.cache_hits += 1

    def calls_per_minute(self) -> int:
        cutoff = time.monotonic() - 60.0
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
        return len(self._times)

    def percentile(self, p: float) -> float | None:
        """Nearest-rank percentile of the last WINDOW call durations (ms)."""
        if not self._durations:
            return None
        ordered = sorted(self._durations)
        rank = max(1, math.ceil(p / 100 * len(ordered)))
        return round(ordered[min(rank, len(ordered)) - 1], 1)

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "errors": self.errors,
            "cacheHits": self.cache_hits,
            "callsPerMin": self.calls_per_minute(),
            "p50": self.percentile(50),
            "p95": self.percentile(95),
            "lastError": self.last_error,
        }

    def recent(self) -> list[dict]:
        return [{"at": c.at, "method": c.label, "ms": c.ms, "ok": c.ok} for c in reversed(self._recent)]
