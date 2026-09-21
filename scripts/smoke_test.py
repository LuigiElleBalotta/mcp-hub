# scripts/smoke_test.py
"""Manual smoke test: compare a tool call through the hub against calling
the same MCP server directly over stdio.

Usage:
    1. Put a real, enabled "mariadb" (or "headroom") entry in
       %LOCALAPPDATA%\\mcp-hub\\config.json
    2. Run: .venv\\Scripts\\python -m mcp_hub serve   (leave running)
    3. In a second terminal: .venv\\Scripts\\python scripts\\smoke_test.py mariadb
"""
import asyncio
import sys
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession


async def call_via_hub(server_name: str, port: int = 37450):
    url = f"http://127.0.0.1:{port}/{server_name}/sse"
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"[hub:{server_name}] tools = {[t.name for t in tools.tools]}")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "mariadb"
    asyncio.run(call_via_hub(name))
