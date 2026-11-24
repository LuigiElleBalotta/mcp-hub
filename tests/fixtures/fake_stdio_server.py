"""Minimal JSON-RPC-over-stdio test double, NOT a real MCP server.

Reads newline-delimited JSON-RPC requests from stdin, writes newline-delimited
JSON-RPC responses to stdout. Used by tests/test_hub_app_proxy.py and
tests/test_session_routing.py to drive `mcp_hub.hub_app._proxy` against a real
subprocess without depending on a real MCP server being installed.

Supported methods:
  - "test/echo": {"params": {"value": ...}} -> {"result": {"echo": ...}}
  - "test/big":  {"params": {"size": N}}     -> {"result": {"blob": "x" * N}}
    (used to prove a response line larger than asyncio.StreamReader's default
    64 KiB limit doesn't crash the reader)
  - "test/sleep": {"params": {"seconds": N}} -> waits N seconds, then
    {"result": {"slept": N}} (used to observe request-level, not
    session-level, serialization under an "exclusive" guard)
  - "test/die": exits the process immediately, WITHOUT writing any response
    (used to simulate a subprocess crashing/dying while a request is still
    in flight, per the round-2 review's Critical finding)
  - "test/progress": emits `notifications/progress` carrying the request's
    own `_meta.progressToken` (after an optional `seconds` sleep), then
    answers {"sawToken": <token as received>}. Used to prove the hub remaps
    progress tokens and routes progress to the owning session only.
  - "test/ask_roots": sends a server-initiated `roots/list` request, waits for
    the client's answer, and returns {"asked": <result or error>}.
  - "test/ask_later": answers at once, then ~0.3s later sends an unsolicited
    server-initiated `roots/list` request (no request in flight) and records
    whatever answer comes back (see "test/last_srv_reply").
  - "test/last_cancel" -> {"cancel": <params of the last notifications/cancelled
    received, or None>}.
  - "test/last_srv_reply" -> {"reply": <last answer to a server request>}.
  - "initialize": counted; answers a plausible MCP initialize result, or a
    JSON-RPC error when the env var FAKE_FAIL_INIT is set. A client's
    `notifications/initialized` is counted too.
  - "ping": counted and answered {} -- unless env FAKE_IGNORE_PING is set, in
    which case it is swallowed (an alive-but-hung server).
  - "test/ping_count" -> {"pings": n}.
  - "tools/list", "prompts/list", "resources/list", "resources/templates/list":
    counted per method; tools are named "t<version>" and the version bumps on
    "test/emit_list_changed". With env FAKE_PAGINATE the first page (no cursor)
    carries nextCursor "p2" and a cursor request returns page 2.
  - "test/list_calls" -> {"calls": {method: n}}; "test/emit_list_changed" ->
    emits notifications/tools/list_changed (bumping the version), then answers {}.
  - "test/handshake_stats" -> {"initializes": n, "initializeds": m,
    "protocolVersion": <version the initialize asked for>}.
"""
import json
import os
import sys
import threading
import time

_LIST_KEYS = {
    "tools/list": "tools", "prompts/list": "prompts",
    "resources/list": "resources", "resources/templates/list": "resourceTemplates",
}
_out_lock = threading.Lock()
_state = {"last_cancel": None, "last_srv_reply": None, "initializes": 0, "initializeds": 0,
          "protocolVersion": None, "pings": 0,
          "list_calls": {}, "list_version": 0}


def _write(obj: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _read_line_obj() -> dict | None:
    for line in sys.stdin:
        line = line.strip()
        if line:
            return json.loads(line)
    return None


def _wait_for_srv_reply(srv_id) -> dict | None:
    """Consumes stdin until the client's answer to server request `srv_id`."""
    while True:
        msg = _read_line_obj()
        if msg is None:
            return None
        if msg.get("id") == srv_id and "method" not in msg:
            return msg
        _dispatch(msg)  # unrelated traffic is still served


def _later_ask() -> None:
    time.sleep(0.3)
    _write({"jsonrpc": "2.0", "id": "srv-later", "method": "roots/list"})


def _dispatch(req: dict) -> None:
    method = req.get("method")
    params = req.get("params") or {}
    req_id = req.get("id")

    if method is None:  # an answer to a server-initiated request
        if req_id == "srv-later":
            _state["last_srv_reply"] = req
        return
    if method == "notifications/cancelled":
        _state["last_cancel"] = params
        return
    if method == "notifications/initialized":
        _state["initializeds"] += 1
        return
    if method == "initialize":
        _state["initializes"] += 1
        _state["protocolVersion"] = params.get("protocolVersion")
        if os.environ.get("FAKE_FAIL_INIT"):
            _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "init refused"}})
            return
        _write({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": params.get("protocolVersion"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "9.9"},
        }})
        return
    if method == "ping":
        _state["pings"] += 1
        if os.environ.get("FAKE_IGNORE_PING"):
            return
        _write({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return
    if method == "test/ping_count":
        _write({"jsonrpc": "2.0", "id": req_id, "result": {"pings": _state["pings"]}})
        return
    if method in _LIST_KEYS:
        _state["list_calls"][method] = _state["list_calls"].get(method, 0) + 1
        key = _LIST_KEYS[method]
        if method == "tools/list":
            items = [{"name": f"t{_state['list_version']}", "inputSchema": {"type": "object"}}]
        else:
            items = []
        result = {key: items}
        if os.environ.get("FAKE_PAGINATE") and not params.get("cursor"):
            result["nextCursor"] = "p2"
        _write({"jsonrpc": "2.0", "id": req_id, "result": result})
        return
    if method == "test/list_calls":
        _write({"jsonrpc": "2.0", "id": req_id, "result": {"calls": _state["list_calls"]}})
        return
    if method == "test/emit_list_changed":
        _state["list_version"] += 1
        _write({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        _write({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return
    if method == "test/handshake_stats":
        _write({"jsonrpc": "2.0", "id": req_id, "result": {
            "initializes": _state["initializes"], "initializeds": _state["initializeds"],
            "protocolVersion": _state["protocolVersion"]}})
        return

    if method == "test/echo":
        result = {"echo": params.get("value")}
    elif method == "test/big":
        result = {"blob": "x" * int(params.get("size", 200_000))}
    elif method == "test/sleep":
        time.sleep(float(params.get("seconds", 0)))
        result = {"slept": params.get("seconds", 0)}
    elif method == "test/die":
        # Hard-exit with no response and no clean shutdown: closes stdout
        # immediately, simulating a crash while this request is still
        # registered as in-flight on the hub side.
        os._exit(1)
    elif method == "test/progress":
        time.sleep(float(params.get("seconds", 0)))
        token = (params.get("_meta") or {}).get("progressToken")
        _write({"jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": token, "progress": 1, "total": 2}})
        result = {"sawToken": token}
    elif method == "test/ask_roots":
        _write({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
        answer = _wait_for_srv_reply("srv-1") or {}
        result = {"asked": answer.get("result", answer.get("error"))}
    elif method == "test/ask_later":
        threading.Thread(target=_later_ask, daemon=True).start()
        result = {}
    elif method == "test/last_cancel":
        result = {"cancel": _state["last_cancel"]}
    elif method == "test/last_srv_reply":
        result = {"reply": _state["last_srv_reply"]}
    else:
        result = {}

    if req_id is not None:
        _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def main() -> None:
    while True:
        req = _read_line_obj()
        if req is None:
            return
        _dispatch(req)


if __name__ == "__main__":
    main()
