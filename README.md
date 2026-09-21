# mcp-hub

Runs local MCP servers once, shares them across every Claude Code session via
HTTP/SSE, instead of each session spawning its own copy.

Design: `docs/superpowers/specs/2026-09-21-mcp-hub-design.md`
Plan: `docs/superpowers/plans/2026-09-21-mcp-hub-implementation.md`

## Setup

    python -m venv .venv
    .venv\Scripts\activate
    pip install -e ".[dev]"

Config lives at `%LOCALAPPDATA%\mcp-hub\config.json`, not in this repo.
Copy `config.example.json` there and edit it, or use `python -m mcp_hub import`.
