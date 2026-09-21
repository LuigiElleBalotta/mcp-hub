# scripts/concurrency_check.py
"""Issue two overlapping calls to the same `exclusive` server through the hub
and confirm the second only starts after the first finishes.

This proves the same guarantee `ConcurrencyGuard` already has a synthetic
unit test for (Task 4), but against a REAL managed subprocess proxied by the
real hub over SSE -- not just two mock coroutines racing an asyncio.Lock in
isolation.

Review finding (post-Task-13): the CLIENT-observed timestamps below (printed
by `timed_call`) can show near-simultaneous completion for A and B even when
the hub's actual dispatch to the subprocess was cleanly serialized. That gap
is real SSE response-delivery latency downstream of the guard -- in the `mcp`
SDK's transport layer (`SseServerTransport` / `sse_client`'s reading loop),
confirmed by direct instrumentation of `hub_app.py`'s dispatch path during
the original investigation -- NOT a guard failure, and NOT something this
script attempts to fix (that's a separate, deeper investigation, out of
scope here). So the client-side numbers below are kept for context, but they
are no longer the proof. The actual proof is the HUB-SIDE dispatch log this
script fetches afterwards (`fetch_dispatch_windows`, using the permanent
`[hub-dispatch]` log line `write_and_maybe_wait` now writes via
`managed.append_log` in `src/mcp_hub/hub_app.py`, read back here via
`GET /api/servers/{name}/logs`, the same endpoint Task 8's GUI log panel
uses). Those timestamps are taken under `managed.guard`'s lock itself, so
they are immune to the downstream SSE latency and are the only thing this
script's pass/fail verdict is based on.

Both sessions call `initialize()` first, then wait on a 2-party
`asyncio.Barrier` before firing their timed operation. Without the barrier,
session B's own `initialize()` request would queue behind session A's timed
call through the SAME exclusive guard (the guard serializes every request to
an exclusive server, not just the one this script is timing), making B's
observed "start" late for a reason unrelated to what's being proven. The
barrier releases both timed operations at (as close as achievable to) the
same wall-clock instant, so any serialization visible afterwards is
attributable only to the guard around the timed call itself.

Per-server timed operation (the task's safety constraint: only side-effect-
free, inert calls against real stateful servers):
  - windows-mcp: `Wait` (a pure sleep/no-op tool, already in this server's
    autoApprove list) with a DIFFERENT duration per call (2s vs 1s). Distinct
    multi-second durations turn "no overlap" into a gap large enough to be
    unambiguous at a glance, not something that could be measurement noise.
  - anything else (e.g. chrome-real): `list_tools()`, a protocol-level call
    answered by the server process itself with no browser/desktop side
    effects. Note: with two near-instant calls the observed gap may be too
    small to be a meaningful timing proof on its own -- see the printed
    caveat and cross-check against the windows-mcp run, which uses
    deliberately distinguishable durations for exactly this reason.

Exit status: 0 if the hub-side dispatch windows for the two timed calls do
not overlap (serialization proven), 1 otherwise -- so this script is usable
as a pass/fail check, not just something to eyeball.

Usage:
    .venv\\Scripts\\python scripts\\concurrency_check.py windows-mcp
    .venv\\Scripts\\python scripts\\concurrency_check.py chrome-real
"""
import asyncio
import re
import sys
import time

import httpx
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession

# Matches the permanent `[hub-dispatch]` log lines `write_and_maybe_wait`
# (src/mcp_hub/hub_app.py) writes via `managed.append_log` at guard-acquire/
# write-start ("start") and at response-ready, before SSE delivery ("done";
# "error" on a rejected/failed request -- see hub_app.py for why).
_DISPATCH_RE = re.compile(
    r"\[hub-dispatch\] phase=(?P<phase>start|done|error) id=(?P<id>\S+) "
    r"method=(?P<method>\S+) t=(?P<t>[\d.]+)"
)


def fetch_dispatch_windows(server_name: str, method: str, port: int = 37450) -> list[tuple[str, float, float]]:
    """Fetches this server's log ring buffer (the same `GET
    /api/servers/{name}/logs` endpoint Task 8's GUI log panel uses) and
    returns [(hub_id, start_t, done_t), ...] for every completed dispatch
    whose JSON-RPC method matches `method`, sorted by start time.

    This is the hub-side, guard-scoped proof: `start_t`/`done_t` are both
    `time.monotonic()` values taken from INSIDE `managed.guard`'s lock (see
    hub_app.py), so they are immune to the SSE response-delivery latency that
    makes the CLIENT-observed timestamps in `timed_call` above an unreliable
    signal on their own.
    """
    resp = httpx.get(f"http://127.0.0.1:{port}/api/servers/{server_name}/logs", timeout=5.0)
    resp.raise_for_status()
    starts: dict[str, float] = {}
    dones: dict[str, float] = {}
    for line in resp.json()["lines"]:
        m = _DISPATCH_RE.search(line)
        if not m or m.group("method") != method:
            continue
        t = float(m.group("t"))
        if m.group("phase") == "start":
            starts[m.group("id")] = t
        elif m.group("phase") == "done":
            dones[m.group("id")] = t
        # "error" entries are intentionally not paired into a window here --
        # an errored request proves nothing about serialization timing.
    windows = [(hub_id, starts[hub_id], dones[hub_id]) for hub_id in starts if hub_id in dones]
    windows.sort(key=lambda w: w[1])
    return windows


async def timed_call(server_name: str, tag: str, barrier: "asyncio.Barrier", port: int = 37450):
    url = f"http://127.0.0.1:{port}/{server_name}/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            init_done = time.monotonic()

            # Wait for the other session to finish its own initialize() too,
            # so both timed operations are released at the same instant
            # regardless of how long each session's own initialize() queued
            # behind the guard.
            await barrier.wait()

            start = time.monotonic()
            if server_name == "windows-mcp":
                # Distinct durations per tag: A waits 2s, B waits 1s. Both
                # inert -- Wait is a pure sleep, no real mouse/keyboard/
                # window interaction.
                duration = 2 if tag == "A" else 1
                await session.call_tool("Wait", {"duration": duration})
            else:
                # Cheap, side-effect-free protocol call: no browser is
                # touched (chrome-devtools-mcp launches Chrome lazily, on
                # the first tool call that actually needs it -- tools/list
                # is answered by the node process itself).
                await session.list_tools()
            end = time.monotonic()

            print(
                f"{tag}: init_done={init_done:.3f} start={start:.3f} "
                f"end={end:.3f} duration={end - start:.3f}s"
            )


async def main(server_name: str) -> int:
    # The JSON-RPC method each branch of `timed_call` actually issues --
    # needed to filter `fetch_dispatch_windows` down to the two dispatches
    # this run cares about (excludes `initialize`, notifications, etc.,
    # which pass through the same guarded path and would otherwise pollute
    # the match).
    method = "tools/call" if server_name == "windows-mcp" else "tools/list"

    barrier = asyncio.Barrier(2)
    await asyncio.gather(
        timed_call(server_name, "A", barrier),
        timed_call(server_name, "B", barrier),
    )

    print(
        "\n(Client-observed timestamps above can be misleading -- see the "
        "module docstring: SSE response-delivery latency downstream of the "
        "guard can make both calls appear to finish near-simultaneously "
        "even when dispatch itself was cleanly serialized. The hub-side "
        "evidence below is authoritative.)\n"
    )

    all_windows = fetch_dispatch_windows(server_name, method)
    # The ring buffer (`ManagedServer.logs`, maxlen=500) may still hold
    # dispatch lines from earlier runs against this same server -- hub_id is
    # unique for the ManagedServer's whole lifetime, so those are harmless
    # to have present, but we only want the two most recent completed
    # dispatches of `method`, which are this run's A and B.
    windows = all_windows[-2:]

    print(f"Hub-side dispatch windows for method={method!r} (authoritative, from managed server log):")
    for hub_id, start_t, done_t in windows:
        print(f"  id={hub_id}: start={start_t:.6f} done={done_t:.6f} duration={done_t - start_t:.3f}s")

    if len(windows) < 2:
        print(
            f"\nFAIL: expected 2 hub-side dispatch windows for method={method!r}, "
            f"found {len(windows)} -- cannot verify serialization. Is the hub "
            f"running with the '[hub-dispatch]' logging in hub_app.py, and did "
            f"both calls actually reach it?"
        )
        return 1

    (id1, start1, done1), (id2, start2, done2) = windows
    overlap = start2 < done1
    if overlap:
        print(
            f"\nFAIL: hub-side dispatch windows OVERLAP -- {id2}'s start "
            f"({start2:.6f}) is before {id1}'s done ({done1:.6f}). The "
            f"exclusive guard did NOT serialize these two requests."
        )
        return 1

    gap = start2 - done1
    print(
        f"\nPASS: hub-side dispatch windows do NOT overlap -- {id2}'s start "
        f"({start2:.6f}) is at or after {id1}'s done ({done1:.6f}), gap="
        f"{gap:.6f}s. This proves the exclusive guard serialized real "
        f"dispatch to the subprocess, independent of client-side SSE "
        f"delivery latency."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "windows-mcp")))
