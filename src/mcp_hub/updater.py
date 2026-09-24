from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import httpx

REPO = "LuigiElleBalotta/mcp-hub"
_RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases"

_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(\d+))?$")


@dataclass
class UpdateInfo:
    version: str
    url: str
    prerelease: bool


def _version_key(tag: str) -> tuple[int, int, int, int, int] | None:
    m = _TAG_RE.match(tag)
    if not m:
        return None
    major, minor, patch, beta = m.groups()
    if beta is None:
        return (int(major), int(minor), int(patch), 1, 0)
    return (int(major), int(minor), int(patch), 0, int(beta))


def _default_fetch(url: str) -> httpx.Response:
    return httpx.get(url, timeout=5.0, headers={"Accept": "application/vnd.github+json"})


def check_for_update(
    current_version: str,
    include_beta: bool = False,
    fetch: Callable[[str], httpx.Response] = _default_fetch,
) -> UpdateInfo | None:
    current_key = _version_key(current_version)
    if current_key is None:
        return None
    try:
        response = fetch(_RELEASES_URL)
        response.raise_for_status()
        releases = response.json()
    except Exception:
        return None

    best_key = None
    best_release = None
    for release in releases:
        if release.get("draft"):
            continue
        if release.get("prerelease") and not include_beta:
            continue
        key = _version_key(release.get("tag_name", ""))
        if key is None or key <= current_key:
            continue
        if best_key is None or key > best_key:
            best_key = key
            best_release = release

    if best_release is None:
        return None
    return UpdateInfo(
        version=best_release["tag_name"],
        url=best_release["html_url"],
        prerelease=bool(best_release["prerelease"]),
    )
