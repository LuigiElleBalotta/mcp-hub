"""Search the official MCP Registry and turn a result into a DRAFT server
(hub-gui-and-clients R6).

Registry entries are written by anyone, so everything here is untrusted input. A
result only ever pre-fills the Add-server form; it is never run: the user sees the
complete command line, reviews it, and saves and enables it themselves. Only the
package kinds whose command line is unambiguous are offered (npm -> npx, pypi ->
uvx, oci -> docker); every other kind, and every remote (non-stdio) server, is
listed as "not installable here" with the reason instead of being guessed at.

The API (v0.1, reads need no auth): GET /v0.1/servers?search=&limit=&cursor=&version=latest
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REGISTRY_URL = "https://registry.modelcontextprotocol.io"
CACHE_TTL = 6 * 3600.0
MIN_QUERY = 3  # the registry answers 422 below this
MAX_LIMIT = 100
MAX_BYTES = 2_000_000
TIMEOUT = 8.0


class RegistryError(Exception):
    pass


@dataclass
class EnvVar:
    name: str
    value: str = ""
    secret: bool = False
    required: bool = False
    description: str = ""


@dataclass
class Option:
    """One way to run a registry entry."""
    label: str
    installable: bool
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: list[EnvVar] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)  # required inputs the registry does not pre-fill
    reason: str = ""  # why it is not installable

    def command_line(self) -> str:
        return " ".join([self.command, *self.args]).strip()


@dataclass
class Draft:
    registry_name: str
    suggested_name: str
    title: str
    description: str
    version: str
    options: list[Option]


def suggest_name(registry_name: str) -> str:
    """`io.github.user/some-server` -> `some-server` (letters, digits, `_`, `-`; <= 40)."""
    tail = registry_name.rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", tail).strip("-")
    return cleaned[:40] or "server"


def _arguments(items: object, needs: list[str]) -> list[str]:
    """Registry `Argument`s -> command-line words. A required argument without a
    value or default cannot be filled in for the user: it is reported in `needs`."""
    out: list[str] = []
    for arg in items if isinstance(items, list) else []:
        if not isinstance(arg, dict):
            continue
        value = arg.get("value") if arg.get("value") not in (None, "") else arg.get("default")
        name = arg.get("name") if isinstance(arg.get("name"), str) else ""
        if isinstance(value, str) and "{" in value and "}" in value:
            value = None  # a template to fill in: not ours to guess
        if arg.get("type") == "named" and name:
            if value not in (None, ""):
                out.extend([name, str(value)])
            elif arg.get("isRequired"):
                needs.append(f"{name} <{arg.get('valueHint') or 'valore'}>")
            elif not arg.get("valueHint"):
                out.append(name)  # a bare flag
        else:
            if value not in (None, ""):
                out.append(str(value))
            elif arg.get("isRequired"):
                needs.append(f"<{arg.get('valueHint') or name or 'argomento'}>")
    return out


def _env(items: object) -> list[EnvVar]:
    out = []
    for var in items if isinstance(items, list) else []:
        if isinstance(var, dict) and isinstance(var.get("name"), str) and var["name"]:
            value = var.get("value") if var.get("value") not in (None, "") else var.get("default")
            secret = bool(var.get("isSecret"))
            out.append(EnvVar(
                name=var["name"],
                # a secret is never pre-filled from the registry: the user types it
                value="" if secret or not isinstance(value, str) or "{" in value else value,
                secret=secret, required=bool(var.get("isRequired")),
                description=str(var.get("description") or "")[:200],
            ))
    return out


def _package_option(pkg: dict) -> Option:
    kind = str(pkg.get("registryType") or "").lower()
    ident = pkg.get("identifier")
    version = pkg.get("version")
    label = f"{kind or '?'}: {ident}" + (f"@{version}" if version else "")
    if not isinstance(ident, str) or not ident:
        return Option(label, False, reason="pacchetto senza identificatore")
    transport = (pkg.get("transport") or {}).get("type", "stdio")
    if transport != "stdio":
        return Option(label, False, reason=f"trasporto {transport}: l'hub avvia processi locali stdio")
    needs: list[str] = []
    runtime_args = _arguments(pkg.get("runtimeArguments"), needs)
    package_args = _arguments(pkg.get("packageArguments"), needs)
    env = _env(pkg.get("environmentVariables"))
    if kind == "npm":
        spec = f"{ident}@{version}" if version else ident
        return Option(label, True, "npx", ["-y", *runtime_args, spec, *package_args], env, needs)
    if kind == "pypi":
        spec = f"{ident}=={version}" if version else ident
        return Option(label, True, "uvx", [*runtime_args, spec, *package_args], env, needs)
    if kind == "oci":
        image = f"{ident}:{version}" if version and ":" not in ident and "@" not in ident else ident
        env_flags = [part for var in env for part in ("-e", var.name)]  # values come from the child's environment
        return Option(label, True, "docker", ["run", "-i", "--rm", *env_flags, *runtime_args, image, *package_args],
                      env, needs)
    return Option(label, False, reason=f"tipo di pacchetto '{kind}' non supportato (npm, pypi e oci lo sono)")


def to_draft(entry: object) -> Draft | None:
    """One registry list item -> a Draft, or None if it is not shaped like one."""
    server = entry.get("server") if isinstance(entry, dict) else None
    if not isinstance(server, dict) or not isinstance(server.get("name"), str):
        return None
    options = [_package_option(p) for p in server.get("packages") or [] if isinstance(p, dict)]
    for remote in server.get("remotes") or []:
        if isinstance(remote, dict):
            options.append(Option(f"remoto ({remote.get('type', '?')}): {remote.get('url', '?')}", False,
                                  reason="server remoto: l'hub avvia processi locali, non si collega a server remoti"))
    return Draft(
        registry_name=server["name"],
        suggested_name=suggest_name(server["name"]),
        title=str(server.get("title") or server["name"]),
        description=str(server.get("description") or "")[:300],
        version=str(server.get("version") or ""),
        options=options,
    )


def default_fetch(url: str, params: dict) -> dict:
    """GET with a timeout and a size cap; any failure is a RegistryError."""
    import httpx

    try:
        with httpx.stream("GET", url, params=params, timeout=TIMEOUT, follow_redirects=False,
                          headers={"Accept": "application/json", "User-Agent": "mcp-hub"}) as resp:
            if resp.status_code != 200:
                raise RegistryError(f"il registry ha risposto {resp.status_code}")
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise RegistryError("risposta del registry troppo grande")
        return json.loads(body)
    except RegistryError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise RegistryError(f"registry non raggiungibile: {exc.__class__.__name__}") from exc


@dataclass
class SearchResult:
    drafts: list[Draft]
    next_cursor: str | None = None
    stale: bool = False  # served from an expired cache because the registry could not be reached


class RegistryClient:
    def __init__(self, cache_dir: Path, fetch: Callable[[str, dict], dict] = default_fetch,
                 ttl: float = CACHE_TTL, clock: Callable[[], float] = time.time) -> None:
        self.cache_dir = cache_dir
        self._fetch = fetch
        self.ttl = ttl
        self._clock = clock

    def _cache_path(self, params: dict) -> Path:
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, path: Path) -> tuple[float, dict] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return float(data["at"]), data["response"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def search(self, query: str, cursor: str | None = None, limit: int = 20) -> SearchResult:
        query = (query or "").strip()
        if len(query) < MIN_QUERY:
            raise ValueError(f"scrivi almeno {MIN_QUERY} caratteri")
        params: dict = {"search": query, "limit": max(1, min(int(limit), MAX_LIMIT)), "version": "latest"}
        if cursor:
            params["cursor"] = cursor
        path = self._cache_path(params)
        cached = self._read_cache(path)
        stale = False
        if cached is not None and self._clock() - cached[0] < self.ttl:
            response = cached[1]
        else:
            try:
                response = self._fetch(f"{REGISTRY_URL}/v0.1/servers", params)
                self._write_cache(path, response)
            except RegistryError:
                if cached is None:
                    raise
                response, stale = cached[1], True  # offline: the old answer beats none
        servers = response.get("servers") if isinstance(response, dict) else None
        if not isinstance(servers, list):
            raise RegistryError("risposta del registry non riconosciuta")
        drafts = [d for d in (to_draft(e) for e in servers) if d is not None]
        meta = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
        next_cursor = meta.get("nextCursor") if isinstance(meta.get("nextCursor"), str) else None
        return SearchResult(drafts, next_cursor, stale)

    def _write_cache(self, path: Path, response: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"at": self._clock(), "response": response}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # a cache that cannot be written is only slower
