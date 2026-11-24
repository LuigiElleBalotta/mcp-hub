"""A REAL MCP server (the official SDK's MCPServer over stdio), not a test
double: speaks the actual protocol, so the end-to-end tests exercise the hub
against genuine initialize / tools/list / tools/call / progress traffic.

Tools: add(a, b); slow(steps) reports progress; whoami() the process id;
die() kills the process (simulates a crash).
"""
import asyncio
import os

from mcp.server.mcpserver import Context, MCPServer

server = MCPServer("real-fixture")


@server.tool()
def add(a: int, b: int) -> int:
    return a + b


@server.tool()
async def slow(steps: int, ctx: Context) -> str:
    for i in range(steps):
        await ctx.report_progress(i + 1, steps)
        await asyncio.sleep(0.05)
    return "done"


@server.tool()
def whoami() -> int:
    return os.getpid()


@server.tool()
def die() -> str:
    os._exit(1)


if __name__ == "__main__":
    server.run("stdio")
