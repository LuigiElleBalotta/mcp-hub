from mcp_hub.config import ServerConfig
from mcp_hub.cleanup import ProcessInfo, ancestor_pids, find_legacy_processes, describe_plan, execute_cleanup


def _p(pid, ppid, name, cmd):
    return ProcessInfo(pid=pid, ppid=ppid, name=name, command_line=cmd)


def test_ancestor_pids_walks_parent_chain():
    processes = [_p(3, 2, "cmd.exe", "cmd"), _p(2, 1, "claude.exe", "claude"), _p(1, 0, "explorer.exe", "explorer")]
    assert ancestor_pids(3, processes) == {2, 1}


def test_find_legacy_processes_matches_by_command_and_args():
    processes = [
        _p(10, 1, "claude.exe", "claude.exe"),
        _p(11, 10, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),
        _p(12, 10, "node.exe", "node unrelated-tool.js"),
    ]
    migrated = {"mariadb": ServerConfig(enabled=True, command="npx", args=["-y", "@oleander/mcp-server-mariadb"], env={})}
    matches = find_legacy_processes(processes, migrated, self_pid=999)
    assert [m.pid for m in matches] == [11]


def test_find_legacy_processes_excludes_self_ancestor_tree():
    processes = [
        _p(20, 1, "claude.exe", "claude.exe"),          # self's own session root
        _p(21, 20, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),  # self's own MCP child - must be excluded
        _p(30, 1, "claude.exe", "claude.exe"),          # another, unrelated session
        _p(31, 30, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),  # must be included
    ]
    migrated = {"mariadb": ServerConfig(enabled=True, command="npx", args=["-y", "@oleander/mcp-server-mariadb"], env={})}
    matches = find_legacy_processes(processes, migrated, self_pid=21)
    assert [m.pid for m in matches] == [31]


def test_describe_plan_lists_each_match():
    matches = [_p(11, 10, "npx.cmd", "npx -y @oleander/mcp-server-mariadb")]
    text = describe_plan(matches)
    assert "11" in text and "mariadb" in text


def test_execute_cleanup_calls_kill_fn_for_each_match_and_counts():
    matches = [_p(11, 10, "npx.cmd", "cmd-a"), _p(12, 10, "npx.cmd", "cmd-b")]
    killed = []
    count = execute_cleanup(matches, kill_fn=killed.append)
    assert killed == [11, 12]
    assert count == 2


def test_find_legacy_processes_does_not_over_protect_via_deep_shared_system_ancestor():
    """Regression test: two independent sessions that both eventually trace
    back to the SAME high-level system ancestor (explorer.exe here, a
    handful of hops above each session's own claude.exe) must not cause one
    session's legacy process to protect the other's. Self-protection must
    anchor at the nearest claude.exe, not walk all the way to a shared
    system root -- confirmed live as a real bug: an earlier version of this
    function protected candidates whose ancestor chain merely intersected
    self's ancestor chain at any depth, which two unrelated sessions will
    almost always do on a real machine well within a 20-hop bound."""
    processes = [
        _p(1, 0, "explorer.exe", "explorer"),
        _p(2, 1, "WindowsTerminal.exe", "wt"),
        _p(3, 2, "cmd.exe", "cmd"),
        _p(40, 3, "claude.exe", "claude"),          # self's session root
        _p(41, 40, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),  # self, must be excluded
        _p(4, 1, "WindowsTerminal.exe", "wt"),
        _p(5, 4, "cmd.exe", "cmd"),
        _p(50, 5, "claude.exe", "claude"),          # another, unrelated session
        _p(51, 50, "npx.cmd", "npx -y @oleander/mcp-server-mariadb"),  # must be included
    ]
    migrated = {"mariadb": ServerConfig(enabled=True, command="npx", args=["-y", "@oleander/mcp-server-mariadb"], env={})}
    matches = find_legacy_processes(processes, migrated, self_pid=41)
    assert [m.pid for m in matches] == [51]
