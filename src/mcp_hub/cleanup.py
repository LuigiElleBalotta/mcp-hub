from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import psutil

from mcp_hub.config import ServerConfig


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    name: str
    command_line: str


def list_processes() -> list[ProcessInfo]:
    result = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        info = proc.info
        cmdline = " ".join(info.get("cmdline") or [])
        result.append(ProcessInfo(pid=info["pid"], ppid=info["ppid"] or 0, name=info["name"] or "", command_line=cmdline))
    return result


def ancestor_pids(pid: int, processes: list[ProcessInfo], max_depth: int = 20) -> set[int]:
    by_pid = {p.pid: p for p in processes}
    result: set[int] = set()
    current = pid
    for _ in range(max_depth):
        proc = by_pid.get(current)
        if proc is None or proc.ppid in (0, current):
            break
        result.add(proc.ppid)
        current = proc.ppid
    return result


def _descendant_pids(root_pid: int, processes: list[ProcessInfo]) -> set[int]:
    by_parent: dict[int, list[int]] = {}
    for p in processes:
        by_parent.setdefault(p.ppid, []).append(p.pid)
    result: set[int] = set()
    queue = [root_pid]
    while queue:
        cur = queue.pop()
        for child in by_parent.get(cur, []):
            if child not in result:
                result.add(child)
                queue.append(child)
    return result


def _matches_server(command_line: str, server: ServerConfig) -> bool:
    return server.command in command_line and all(arg in command_line for arg in server.args)


def find_legacy_processes(
    processes: list[ProcessInfo], migrated: dict[str, ServerConfig], self_pid: int
) -> list[ProcessInfo]:
    by_pid = {p.pid: p for p in processes}
    self_ancestors = ancestor_pids(self_pid, processes)

    # Anchor self-protection at the nearest claude.exe ancestor (self's own
    # session root) instead of walking all the way up self's full ancestor
    # chain. Two independent sessions commonly share a high-level ancestor
    # (explorer.exe, a services host, or simply an unresolved/off-list pid)
    # well within a 20-hop bound -- protecting based on ANY shared ancestor,
    # at any depth, made every other session's legacy processes look
    # "protected" too (confirmed live: a two-session test fixture sharing
    # one such ancestor caused the other session's own process to be
    # wrongly excluded). Stopping at the nearest claude.exe keeps the
    # protected set scoped to this specific session.
    session_root = None
    for candidate_pid in (self_pid, *self_ancestors):
        proc = by_pid.get(candidate_pid)
        if proc is not None and proc.name == "claude.exe":
            session_root = candidate_pid
            break

    protected = {self_pid} | self_ancestors
    if session_root is not None:
        protected |= _descendant_pids(session_root, processes)
    # else: no resolvable claude.exe boundary found in self's ancestor
    # chain -- fall back to protecting only self's own direct ancestor
    # chain. This under-protects self's sibling processes in that edge
    # case rather than risk over-protecting an unrelated session.

    matches = []
    for proc in processes:
        if proc.pid in protected:
            continue
        for server in migrated.values():
            if _matches_server(proc.command_line, server):
                matches.append(proc)
                break
    return matches


def describe_plan(matches: list[ProcessInfo]) -> str:
    lines = [f"pid {m.pid} ({m.name}): {m.command_line}" for m in matches]
    lines.append(f"Total: {len(matches)} process(es)")
    return "\n".join(lines)


def execute_cleanup(matches: list[ProcessInfo], kill_fn: Callable[[int], None] | None = None) -> int:
    if kill_fn is None:
        def kill_fn(pid: int) -> None:
            try:
                psutil.Process(pid).terminate()
            except psutil.NoSuchProcess:
                pass
    count = 0
    for match in matches:
        kill_fn(match.pid)
        count += 1
    return count
