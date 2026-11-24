"""Per-server tool filtering (hub-proxy-efficiency R6).

What a client may see and call of an upstream's tools, decided by the hub
rather than by the server: `toolsAllow` / `toolsDeny` are glob patterns on the
upstream's own tool names (deny wins; an empty allow list means "all"), and
`toolPrefix` is put in front of every name the client sees, which keeps
tools of different servers apart when a client merges them.

Pure functions, applied to the answer AFTER the list cache (the cache keeps
the raw upstream list, so changing a filter never needs a refetch).
"""
from __future__ import annotations

import re
from fnmatch import fnmatchcase

# MCP tool names: letters, digits, underscore, hyphen, dot.
PREFIX_RE = re.compile(r"[A-Za-z0-9_.-]{0,64}")


def is_glob(pattern: str) -> bool:
    """Whether `pattern` has wildcard characters (else it is an exact tool name)."""
    return any(ch in pattern for ch in "*?[")


def tool_visible(name: str, allow: list[str], deny: list[str]) -> bool:
    if any(fnmatchcase(name, pattern) for pattern in deny):
        return False
    return not allow or any(fnmatchcase(name, pattern) for pattern in allow)


def edit_filters(
    tool_names: list[str], visible: set[str], allow: list[str], deny: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """The smallest change to the allow/deny lists that makes exactly `visible`
    of `tool_names` visible. Returns (allow, deny, locked).

    Hiding adds the exact name to `deny`. Showing removes an exact name from
    `deny`, and adds it to `allow` when the allow-list is what hides it. A tool
    hidden by a deny GLOB cannot be shown by editing exact names (deny wins):
    it is reported in `locked` and left as it is. The inputs are not modified."""
    allow, deny, locked = list(allow), list(deny), []
    for name in tool_names:
        want = name in visible
        if want == tool_visible(name, allow, deny):
            continue
        if not want:
            deny.append(name)
            continue
        while name in deny:
            deny.remove(name)
        if tool_visible(name, allow, deny):
            continue
        if any(is_glob(p) and fnmatchcase(name, p) for p in deny):
            locked.append(name)
            continue
        allow.append(name)  # only the allow-list was hiding it
    return allow, deny, locked


def filter_tools_result(result: dict, allow: list[str], deny: list[str], prefix: str) -> dict:
    """A `tools/list` result with hidden tools dropped and the rest renamed.
    Returns a new dict; `result` (which may be the cached raw list) is untouched."""
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    shown = []
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if isinstance(name, str) and tool_visible(name, allow, deny):
            shown.append({**tool, "name": f"{prefix}{name}"})
    return {**result, "tools": shown}


def resolve_call(name: object, allow: list[str], deny: list[str], prefix: str) -> str | None:
    """The upstream's own name for a tool the client called, or None when that
    tool does not exist for this client (hidden, or not carrying the prefix)."""
    if not isinstance(name, str):
        return None
    if prefix:
        if not name.startswith(prefix):
            return None
        name = name[len(prefix):]
    return name if tool_visible(name, allow, deny) else None
