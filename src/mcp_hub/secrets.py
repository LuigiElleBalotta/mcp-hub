"""Secrets in the operating system's credential store instead of config.json
(hub-gui-and-clients R4).

An env value in config.json can be a reference, `${secret:<server>/<KEY>}`; the
value itself lives in Windows Credential Manager / macOS Keychain / the Linux
Secret Service (through `keyring`) and is looked up only when the hub spawns the
server. The hub and the GUI run as the same user, so both reach the same store
and no secret ever travels over the hub's HTTP API.

If no usable store exists (`available()` is False) nothing changes: plaintext
env values keep working and the GUI hides the feature.
"""
from __future__ import annotations

import re

SERVICE = "mcp-hub"
REF_RE = re.compile(r"^\$\{secret:([^/}]+)/([^}]+)\}$")
SECRET_KEY_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.IGNORECASE)


class SecretError(OSError):
    """A secret could not be read or stored. An OSError so the hub's spawn paths
    treat it like any other failure to start a server."""


class SecretStoreUnavailable(SecretError):
    pass


def _backend():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError as exc:
        raise SecretStoreUnavailable("the 'keyring' package is not installed") from exc
    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
        raise SecretStoreUnavailable("no credential store is available on this system")
    return keyring


def available() -> bool:
    try:
        _backend()
    except SecretStoreUnavailable:
        return False
    return True


def make_ref(server: str, key: str) -> str:
    return f"${{secret:{server}/{key}}}"


def parse_ref(value: object) -> tuple[str, str] | None:
    """(server, key) if `value` is a secret reference, else None."""
    if not isinstance(value, str):
        return None
    match = REF_RE.match(value)
    return (match.group(1), match.group(2)) if match else None


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key))


def _username(server: str, key: str) -> str:
    return f"{server}/{key}"


def get_secret(server: str, key: str) -> str | None:
    keyring = _backend()
    try:
        return keyring.get_password(SERVICE, _username(server, key))
    except Exception as exc:  # noqa: BLE001 - backends raise their own error types
        raise SecretError(f"cannot read secret {make_ref(server, key)}: {exc.__class__.__name__}") from exc


def set_secret(server: str, key: str, value: str) -> None:
    keyring = _backend()
    try:
        keyring.set_password(SERVICE, _username(server, key), value)
    except Exception as exc:  # noqa: BLE001
        raise SecretError(f"cannot store secret {make_ref(server, key)}: {exc.__class__.__name__}") from exc


def delete_secret(server: str, key: str) -> bool:
    """Removes a stored secret; False when there was none."""
    keyring = _backend()
    try:
        keyring.delete_password(SERVICE, _username(server, key))
    except keyring.errors.PasswordDeleteError:
        return False
    except Exception as exc:  # noqa: BLE001
        raise SecretError(f"cannot delete secret {make_ref(server, key)}: {exc.__class__.__name__}") from exc
    return True


def resolve_env(env: dict[str, str]) -> dict[str, str]:
    """`env` with every secret reference replaced by its stored value. A reference
    with no stored value raises SecretError naming the REFERENCE (never a value);
    values that are not references pass through untouched."""
    resolved = {}
    for name, value in env.items():
        ref = parse_ref(value)
        if ref is None:
            resolved[name] = value
            continue
        secret = get_secret(*ref)
        if secret is None:
            raise SecretError(f"{value} (for {name}) is not in the credential store")
        resolved[name] = secret
    return resolved


def migrate_config_secrets(servers: dict, only: str | None = None) -> list[str]:
    """Moves every plaintext secret env value (a key naming a token, secret,
    password, key or auth) of `servers` into the store and leaves a reference in
    its place. Idempotent: references and non-secret values are not touched.
    Mutates the ServerConfig objects; returns the references created. The caller
    saves the config."""
    created = []
    for name, server in servers.items():
        if only is not None and name != only:
            continue
        for key, value in list(server.env.items()):
            if not value or parse_ref(value) is not None or not is_secret_key(key):
                continue
            set_secret(name, key, value)
            server.env[key] = make_ref(name, key)
            created.append(server.env[key])
    return created


def stored_names(servers: dict) -> list[tuple[str, str, bool]]:
    """(reference, env key, is it in the store?) for every reference in the config.
    Names only: values are never read into the result."""
    out = []
    for name, server in servers.items():
        for key, value in server.env.items():
            ref = parse_ref(value)
            if ref is not None:
                out.append((value, f"{name}:{key}", get_secret(*ref) is not None))
    return out
