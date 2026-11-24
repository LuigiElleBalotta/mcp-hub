"""Cache of an upstream's list answers (hub-proxy-efficiency R1) and its
on-disk copy (used by lazy start, R2).

`tools/list`, `prompts/list`, `resources/list` and `resources/templates/list`
are asked by every client at every connect, and answered by the same shared
upstream each time. They are served from memory once the upstream has
answered, and dropped when it says they changed or when it is respawned.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

# list method -> the upstream notifications that make it stale
CACHEABLE_LISTS: dict[str, tuple[str, ...]] = {
    "tools/list": ("notifications/tools/list_changed",),
    "prompts/list": ("notifications/prompts/list_changed",),
    "resources/list": ("notifications/resources/list_changed",),
    "resources/templates/list": ("notifications/resources/list_changed",),
}


def has_cursor(params: Any) -> bool:
    """A paginated request: its answer is one page, never cached."""
    return isinstance(params, dict) and params.get("cursor") is not None


class ListCache:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        # Bumped on every invalidation. A caller records the generation before
        # asking the upstream and `put`s with it: an answer that raced with a
        # `list_changed` is then discarded instead of cached stale.
        self._generation: dict[str, int] = {method: 0 for method in CACHEABLE_LISTS}

    def get(self, method: str) -> dict | None:
        return self._data.get(method)

    def generation(self, method: str) -> int:
        return self._generation.get(method, 0)

    def put(self, method: str, result: dict, generation: int) -> bool:
        """Stores `result` unless it is paginated, uncacheable, or the cache was
        invalidated since `generation` was read. Returns whether it was stored."""
        if method not in CACHEABLE_LISTS or generation != self._generation[method]:
            return False
        if not isinstance(result, dict) or result.get("nextCursor") is not None:
            return False  # one page of several: caching it would hide the rest
        self._data[method] = result
        return True

    def invalidate(self, method: str) -> None:
        if method in self._generation:
            self._generation[method] += 1
        self._data.pop(method, None)

    def invalidate_for_notification(self, notification: str) -> bool:
        stale = [m for m, notes in CACHEABLE_LISTS.items() if notification in notes]
        for method in stale:
            self.invalidate(method)
        return bool(stale)

    def clear(self) -> None:
        for method in CACHEABLE_LISTS:
            self.invalidate(method)

    def snapshot(self) -> dict[str, dict]:
        return dict(self._data)

    def load(self, data: dict[str, dict]) -> None:
        for method, result in data.items():
            if method in CACHEABLE_LISTS and isinstance(result, dict):
                self._data[method] = result


def cache_key(command: str, args: list[str], env_keys: list[str]) -> str:
    """Identifies what was cached: another command line (or another set of env
    variable NAMES, never values) is another server, so its file is ignored."""
    raw = json.dumps([command, list(args), sorted(env_keys)], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class DiskCache:
    """One JSON file per server holding its initialize result and list answers.

    Corrupt, unreadable or foreign (other key) files read as "nothing cached";
    a write failure is ignored: the cache is an optimisation, never a
    dependency."""

    def __init__(self, directory: Path, name: str, key: str) -> None:
        self.path = directory / f"{_UNSAFE.sub('_', name)}.json"
        self.key = key

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("key") != self.key:
            return None
        if not isinstance(data.get("initialize"), dict):
            return None
        lists = data.get("lists")
        return {"initialize": data["initialize"], "lists": lists if isinstance(lists, dict) else {}}

    def save(self, initialize: dict | None, lists: dict[str, dict]) -> None:
        if initialize is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"key": self.key, "initialize": initialize, "lists": lists}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass
