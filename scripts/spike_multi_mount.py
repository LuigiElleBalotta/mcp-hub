"""Spike: can Starlette mount two independent MCP SSE servers under one app?

This validates the single highest-risk assumption in the mcp-hub plan: that
two (or more) `mcp` SDK servers can be mounted under one Starlette app, each
with its own tool set and session state, without interfering with each other.

Run: .venv\\Scripts\\python.exe scripts\\spike_multi_mount.py
Then, in a second terminal, run the companion client:
    .venv\\Scripts\\python.exe scripts\\spike_multi_mount_client.py
which connects to both /echo-a/sse and /echo-b/sse simultaneously, calls
`echo` on each with different text, and asserts each mount answers with its
own server name embedded and neither interferes with the other.

--- Deviations from the original brief sketch (installed `mcp` SDK is 2.2.0,
not the decorator-based ~1.x API the brief's sketch assumed) ---

1. `mcp.server.Server` (and `mcp.server.lowlevel.Server`) no longer exposes
   `@server.list_tools()` / `@server.call_tool()` decorators. Handlers are now
   passed as `on_list_tools=` / `on_call_tool=` constructor keyword arguments,
   each shaped `async def handler(ctx, params) -> types.<X>Result`. See
   `.venv/Lib/site-packages/mcp/server/lowlevel/server.py` module docstring.

2. `mcp.types.Tool` / `CallToolRequestParams` etc. use snake_case field names
   (`input_schema`, not `inputSchema`) -- pydantic aliasing still accepts the
   wire's camelCase on the network, but constructing the objects in Python
   uses snake_case kwargs.

3. `SseServerTransport`'s constructor endpoint argument must be the path
   *relative to this mount's root_path*, i.e. "/messages/" -- NOT
   f"/{name}/messages" as in the brief's sketch. `connect_sse` builds the
   client-facing POST URL as `scope["root_path"] + self._endpoint`, and
   `root_path` is already "/{name}" once Starlette resolves the outer Mount.
   Using f"/{name}/messages" (as the brief sketch did) doubles the prefix
   into "/{name}/{name}/messages", which does not match the actual mounted
   route and breaks the client's POST callback. This was caught empirically
   when curl showed the doubled endpoint path in the SSE stream's first event.
   (Trailing slash: both `SseServerTransport` and `Mount` below use
   "/messages/" with a trailing slash, matching the SDK's own docstring
   example -- using "/messages" without it still works but causes an extra
   307 redirect on every POST, since Starlette's `Mount` normalizes to the
   trailing-slash form.)

Everything else (`Mount(name, routes=[Route(sse), Mount(messages)])` repeated
per server, `transport.connect_sse(...)` as an async context manager yielding
(read, write) streams, `server.run(read, write, server.create_initialization_options())`)
matches the brief's sketch and the SDK's documented pattern.

To make the spike actually discriminate between "the two mounts share state"
and "the two mounts are truly independent" (rather than just routing calls to
the right handler), each server here:
  - exposes a **distinctly named tool** (`echo_a` on the "echo-a" mount,
    `echo_b` on the "echo-b" mount), so a leaked/merged tool registry would
    show up as a wrong or duplicated tool list; and
  - keeps a **per-server call counter** baked into the closure, so a shared
    Server/transport under the hood would produce an interleaved,
    non-monotonic counter sequence instead of each mount independently
    counting 1..N under concurrent load.
"""
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
import uvicorn


def make_echo_server(name: str, tool_name: str) -> Server:
    # Per-server call counter, baked into this closure. If two mounts were
    # accidentally sharing a Server/transport under the hood, concurrent
    # interleaved calls would produce a shared, non-monotonic sequence here
    # instead of each mount independently counting 1..N.
    state = {"calls": 0}

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=tool_name,
                    description=f"echo back input, tagged with {name}",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                )
            ]
        )

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        state["calls"] += 1
        text = (params.arguments or {}).get("text", "")
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=f"[{name}#{state['calls']}] {text}"
                )
            ]
        )

    return Server(name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def mount_for(name: str, tool_name: str) -> Mount:
    server = make_echo_server(name, tool_name)
    # Endpoint is relative to THIS mount's root_path (e.g. "/echo-a"), which
    # Starlette sets from the outer Mount(f"/{name}", ...) below. Do NOT
    # repeat the mount name here -- see deviation #3 in the module docstring.
    # Trailing slash matches Mount("/messages/", ...) below and the SDK's own
    # docstring example, avoiding an extra 307 redirect on every POST.
    transport = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read, write):
            await server.run(read, write, server.create_initialization_options())
        # Returning a Response avoids a TypeError on client disconnect.
        return Response()

    return Mount(
        f"/{name}",
        routes=[
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=transport.handle_post_message),
        ],
    )


app = Starlette(
    routes=[
        mount_for("echo-a", "echo_a"),
        mount_for("echo-b", "echo_b"),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=37450)
