"""Minimal JSON-RPC-over-stdio test double, NOT a real MCP server.

Reads newline-delimited JSON-RPC requests from stdin, writes newline-delimited
JSON-RPC responses to stdout. Used by tests/test_hub_app_proxy.py to drive
`mcp_hub.hub_app._proxy` against a real subprocess without depending on a real
MCP server being installed.

Supported methods:
  - "test/echo": {"params": {"value": ...}} -> {"result": {"echo": ...}}
  - "test/big":  {"params": {"size": N}}     -> {"result": {"blob": "x" * N}}
    (used to prove a response line larger than asyncio.StreamReader's default
    64 KiB limit doesn't crash the reader)
  - "test/sleep": {"params": {"seconds": N}} -> waits N seconds, then
    {"result": {"slept": N}} (used to observe request-level, not
    session-level, serialization under an "exclusive" guard)
"""
import json
import sys
import time


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        params = req.get("params") or {}
        req_id = req.get("id")

        if method == "test/echo":
            result = {"echo": params.get("value")}
        elif method == "test/big":
            result = {"blob": "x" * int(params.get("size", 200_000))}
        elif method == "test/sleep":
            time.sleep(float(params.get("seconds", 0)))
            result = {"slept": params.get("seconds", 0)}
        else:
            result = {}

        if req_id is not None:
            resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
