"""Companion client for scripts/spike_multi_mount.py.

Connects to both /echo-a/sse and /echo-b/sse *simultaneously* (concurrently,
in the same process, via anyio task group), calls each mount's echo tool
repeatedly with distinguishable text, and asserts:
  - each mount exposes its OWN, distinctly-named tool (echo_a vs echo_b) --
    not a merged or duplicated registry
  - each mount's response carries its own server name
  - each mount's per-call counter is independently monotonic (1..N) even
    under concurrent, interleaved calls against the other mount -- this is
    what actually distinguishes "two isolated Server/session instances"
    from "one shared instance that happens to route by name"

Run the server first (leave running):
    .venv\\Scripts\\python.exe scripts\\spike_multi_mount.py
Then, in a second terminal:
    .venv\\Scripts\\python.exe scripts\\spike_multi_mount_client.py
"""
import anyio
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

BASE = "http://127.0.0.1:37450"

MOUNTS = {"echo-a": "echo_a", "echo-b": "echo_b"}

results: dict[str, list[str]] = {"echo-a": [], "echo-b": []}
errors: list[str] = []


async def run_against(name: str, calls: int = 5):
    tool_name = MOUNTS[name]
    url = f"{BASE}/{name}/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]
            if tool_names != [tool_name]:
                errors.append(
                    f"{name}: expected tool list [{tool_name!r}], got {tool_names}"
                )

            for i in range(calls):
                text = f"{name}-msg-{i}"
                result = await session.call_tool(tool_name, {"text": text})
                out = result.content[0].text
                results[name].append(out)
                # Expected shape: "[{name}#{call_number}] {text}" where
                # call_number is 1-indexed and strictly this mount's own
                # count -- a shared/leaking server would break this sequence
                # under the other mount's concurrent traffic.
                expected = f"[{name}#{i + 1}] {text}"
                if out != expected:
                    errors.append(
                        f"{name} call {i}: expected {expected!r}, got {out!r}"
                    )


async def main():
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_against, "echo-a")
        tg.start_soon(run_against, "echo-b")

    print("=== results ===")
    for name, vals in results.items():
        for v in vals:
            print(f"{name}: {v}")

    if errors:
        print("=== ERRORS ===")
        for e in errors:
            print(e)
        raise SystemExit(1)

    # Cross-check: no echo-a response ever contains "echo-b" text and vice versa,
    # and each mount's per-call counter is its own strictly monotonic 1..N
    # sequence -- proof the two Server instances never shared state, even
    # though both were hit with interleaved concurrent traffic.
    for v in results["echo-a"]:
        assert v.startswith("[echo-a#"), v
        assert "echo-b" not in v, v
    for v in results["echo-b"]:
        assert v.startswith("[echo-b#"), v
        assert "echo-a" not in v, v

    print(
        "\nOK: both mounts exposed distinct tools, responded independently, "
        "and kept independent per-mount call counters under concurrent load."
    )


if __name__ == "__main__":
    anyio.run(main)
