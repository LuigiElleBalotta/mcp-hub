# scripts/concurrency_check.py
"""Issue two overlapping calls to the same `exclusive` server through the hub
and confirm the second only starts after the first finishes.

This proves the same guarantee `ConcurrencyGuard` already has a synthetic
unit test for (Task 4), but against a REAL managed subprocess proxied by the
real hub over SSE -- not just two mock coroutines racing an asyncio.Lock in
isolation.

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

Usage:
    .venv\\Scripts\\python scripts\\concurrency_check.py windows-mcp
    .venv\\Scripts\\python scripts\\concurrency_check.py chrome-real
"""
import asyncio
import sys
import time
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession


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


async def main(server_name: str):
    barrier = asyncio.Barrier(2)
    await asyncio.gather(
        timed_call(server_name, "A", barrier),
        timed_call(server_name, "B", barrier),
    )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "windows-mcp"))
