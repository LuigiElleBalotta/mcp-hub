"""hub-gui-and-clients R6: searching the MCP Registry and drafting a server from a result."""
import json

import pytest

from mcp_hub import registry
from mcp_hub.registry import (
    MIN_QUERY, RegistryClient, RegistryError, default_fetch, suggest_name, to_draft,
)


def entry(name="io.github.acme/weather-mcp", packages=None, remotes=None, **server):
    body = {"name": name, "description": "Weather for agents", "version": "1.2.0", **server}
    if packages is not None:
        body["packages"] = packages
    if remotes is not None:
        body["remotes"] = remotes
    return {"server": body, "_meta": {"io.modelcontextprotocol.registry/official": {"status": "active"}}}


NPM = {"registryType": "npm", "identifier": "@acme/weather-mcp", "version": "1.2.0", "transport": {"type": "stdio"},
       "environmentVariables": [
           {"name": "WEATHER_API_KEY", "description": "your key", "isRequired": True, "isSecret": True, "value": "leak-me"},
           {"name": "UNITS", "default": "metric"},
           {"name": "REGION", "value": "{region}"}],
       "packageArguments": [
           {"type": "named", "name": "--port", "value": "8080"},
           {"type": "named", "name": "--verbose"},
           {"type": "named", "name": "--db", "isRequired": True, "valueHint": "path"},
           {"type": "positional", "value": "serve"},
           {"type": "positional", "valueHint": "dir", "isRequired": True}]}


# ------------------------------------------------------------------------------ names

@pytest.mark.parametrize("raw,expected", [
    ("io.github.acme/weather-mcp", "weather-mcp"), ("acme/My Server!", "My-Server"),
    ("no-slash", "no-slash"), ("a/", "server"), ("x/" + "y" * 90, "y" * 40), ("a/$$$", "server")])
def test_suggested_names_are_safe(raw, expected):
    assert suggest_name(raw) == expected


# --------------------------------------------------------------------------- npm / pypi / oci

def test_npm_package_becomes_an_npx_command_with_arguments_and_env():
    draft = to_draft(entry(packages=[NPM]))
    opt = draft.options[0]
    assert draft.suggested_name == "weather-mcp" and draft.description == "Weather for agents"
    assert opt.installable and opt.command == "npx"
    assert opt.args == ["-y", "@acme/weather-mcp@1.2.0", "--port", "8080", "--verbose", "serve"]
    assert opt.needs == ["--db <path>", "<dir>"], "required inputs the registry leaves open are reported, not invented"
    env = {v.name: v for v in opt.env}
    assert env["WEATHER_API_KEY"].secret and env["WEATHER_API_KEY"].value == "", "a secret is never pre-filled"
    assert env["WEATHER_API_KEY"].required and env["UNITS"].value == "metric"
    assert env["REGION"].value == "", "a {template} value is not guessed"
    assert opt.command_line().startswith("npx -y @acme/weather-mcp@1.2.0")


def test_pypi_and_oci_packages():
    pypi = to_draft(entry(packages=[{"registryType": "pypi", "identifier": "weather-mcp", "version": "0.3.1",
                                     "transport": {"type": "stdio"}}])).options[0]
    assert (pypi.command, pypi.args) == ("uvx", ["weather-mcp==0.3.1"])
    oci = to_draft(entry(packages=[{"registryType": "oci", "identifier": "ghcr.io/acme/weather", "version": "2.0",
                                    "transport": {"type": "stdio"},
                                    "environmentVariables": [{"name": "TOKEN_X", "isSecret": True}]}])).options[0]
    assert oci.command == "docker"
    assert oci.args == ["run", "-i", "--rm", "-e", "TOKEN_X", "ghcr.io/acme/weather:2.0"]
    pinned = to_draft(entry(packages=[{"registryType": "oci", "identifier": "ghcr.io/acme/w@sha256:ab",
                                       "version": "2.0", "transport": {"type": "stdio"}}])).options[0]
    assert pinned.args[-1] == "ghcr.io/acme/w@sha256:ab", "an already-pinned image is not given a second tag"


def test_unsupported_and_remote_options_are_listed_with_the_reason_not_guessed():
    draft = to_draft(entry(
        packages=[{"registryType": "nuget", "identifier": "Acme.Weather", "transport": {"type": "stdio"}},
                  {"registryType": "npm", "identifier": "x", "transport": {"type": "streamable-http", "url": "http://h"}},
                  {"registryType": "npm", "transport": {"type": "stdio"}}],
        remotes=[{"type": "streamable-http", "url": "https://api.acme.com/mcp"}]))
    assert [o.installable for o in draft.options] == [False, False, False, False]
    assert "nuget" in draft.options[0].reason and "non supportato" in draft.options[0].reason
    assert "streamable-http" in draft.options[1].reason
    assert "identificatore" in draft.options[2].reason
    assert "remoto" in draft.options[3].label and "processi locali" in draft.options[3].reason


def test_a_package_without_a_version_is_not_pinned():
    opt = to_draft(entry(packages=[{"registryType": "npm", "identifier": "pkg", "transport": {"type": "stdio"}}])).options[0]
    assert opt.args == ["-y", "pkg"]


# ----------------------------------------------------------------------- hostile / odd input

@pytest.mark.parametrize("bad", [None, 5, "x", [], {}, {"server": None}, {"server": {}}, {"server": {"name": 5}}])
def test_entries_that_are_not_shaped_like_results_are_skipped(bad):
    assert to_draft(bad) is None


def test_odd_fields_inside_a_valid_entry_do_not_crash_it():
    draft = to_draft(entry(packages=["junk", {"registryType": "npm", "identifier": "ok", "transport": {"type": "stdio"},
                                                "packageArguments": ["x", None, {"type": "named", "name": 5}],
                                                "environmentVariables": [None, {"name": ""}, {"name": 5}]}],
                           remotes=["junk", None], description=None, title=None))
    assert draft is not None and len(draft.options) == 1 and draft.options[0].args == ["-y", "ok"]
    assert draft.title == "io.github.acme/weather-mcp" and draft.description == ""


def test_a_hostile_description_is_truncated_and_a_command_cannot_be_injected_through_args():
    draft = to_draft(entry(description="x" * 5000, packages=[{
        "registryType": "npm", "identifier": "ok; rm -rf /", "transport": {"type": "stdio"}}]))
    assert len(draft.description) == 300
    # one word per argument and no shell: the identifier stays ONE argument, never interpreted
    assert draft.options[0].args == ["-y", "ok; rm -rf /"]


# ------------------------------------------------------------------------------ client

class Fetcher:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if self.error:
            raise self.error
        return self.response


def payload(*entries, cursor=None):
    return {"servers": list(entries), "metadata": {"count": len(entries), **({"nextCursor": cursor} if cursor else {})}}


def test_search_calls_the_documented_endpoint_and_validates_the_query(tmp_path):
    fetch = Fetcher(payload(entry(packages=[NPM]), {"junk": 1}, cursor="abc"))
    client = RegistryClient(tmp_path, fetch)
    result = client.search("  weather ", limit=500)
    assert fetch.calls == [("https://registry.modelcontextprotocol.io/v0.1/servers",
                            {"search": "weather", "limit": 100, "version": "latest"})]
    assert [d.suggested_name for d in result.drafts] == ["weather-mcp"], "the junk entry was dropped"
    assert result.next_cursor == "abc" and result.stale is False
    for short in ("", "ab", "  ", None):
        with pytest.raises(ValueError, match=str(MIN_QUERY)):
            client.search(short)
    client.search("weather", cursor="abc")
    assert fetch.calls[-1][1]["cursor"] == "abc"


def test_results_are_cached_for_six_hours_per_query(tmp_path):
    now = [1000.0]
    fetch = Fetcher(payload(entry(packages=[NPM])))
    client = RegistryClient(tmp_path, fetch, clock=lambda: now[0])
    client.search("weather")
    client.search("weather")
    assert len(fetch.calls) == 1, "second search within the TTL: from disk"
    client.search("github")
    assert len(fetch.calls) == 2, "another query is another cache entry"
    now[0] += 6 * 3600 - 1
    client.search("weather")
    assert len(fetch.calls) == 2
    now[0] += 2
    client.search("weather")
    assert len(fetch.calls) == 3, "after six hours it asks again"


def test_offline_with_an_old_cache_serves_it_marked_stale_and_without_one_fails(tmp_path):
    now = [0.0]
    client = RegistryClient(tmp_path, Fetcher(payload(entry(packages=[NPM]))), clock=lambda: now[0])
    client.search("weather")
    now[0] += 10 * 3600
    offline = RegistryClient(tmp_path, Fetcher(error=RegistryError("down")), clock=lambda: now[0])
    result = offline.search("weather")
    assert result.stale is True and len(result.drafts) == 1
    with pytest.raises(RegistryError):
        offline.search("never-searched")


@pytest.mark.parametrize("response", [None, [], {}, {"servers": "x"}, {"servers": None}, "text"])
def test_an_unrecognised_response_is_an_error_not_a_crash(tmp_path, response):
    with pytest.raises(RegistryError, match="non riconosciuta"):
        RegistryClient(tmp_path, Fetcher(response)).search("weather")


def test_a_corrupt_cache_file_is_ignored(tmp_path):
    fetch = Fetcher(payload(entry(packages=[NPM])))
    client = RegistryClient(tmp_path, fetch)
    client.search("weather")
    for f in tmp_path.glob("*.json"):
        f.write_text("{broken", encoding="utf-8")
    assert len(client.search("weather").drafts) == 1 and len(fetch.calls) == 2


def test_an_unwritable_cache_only_makes_it_slower(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    fetch = Fetcher(payload(entry(packages=[NPM])))
    client = RegistryClient(blocker / "sub", fetch)
    assert len(client.search("weather").drafts) == 1 and len(client.search("weather").drafts) == 1
    assert len(fetch.calls) == 2


# ----------------------------------------------------------------------- the HTTP layer

class _Resp:
    def __init__(self, status, chunks):
        self.status_code, self._chunks = status, chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        return iter(self._chunks)


def test_default_fetch_parses_json_and_rejects_errors_and_oversized_bodies(monkeypatch):
    import httpx

    body = json.dumps(payload(entry())).encode()
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Resp(200, [body[:10], body[10:]]))
    assert default_fetch("http://x", {})["servers"][0]["server"]["name"] == "io.github.acme/weather-mcp"
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Resp(422, [b"{}"]))
    with pytest.raises(RegistryError, match="422"):
        default_fetch("http://x", {})
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Resp(200, [b"x" * 1_000_000] * 3))
    with pytest.raises(RegistryError, match="troppo grande"):
        default_fetch("http://x", {})
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Resp(200, [b"not json"]))
    with pytest.raises(RegistryError, match="raggiungibile"):
        default_fetch("http://x", {})

    def boom(*a, **k):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "stream", boom)
    with pytest.raises(RegistryError, match="ConnectError"):
        default_fetch("http://x", {})


def test_default_fetch_does_not_follow_redirects_and_sends_no_identifying_data(monkeypatch):
    import httpx

    seen = {}

    def spy(method, url, **kw):
        seen.update(kw, url=url)
        return _Resp(200, [b"{}"])

    monkeypatch.setattr(httpx, "stream", spy)
    default_fetch("https://registry.modelcontextprotocol.io/v0.1/servers", {"search": "weather"})
    assert seen["follow_redirects"] is False and seen["params"] == {"search": "weather"}
    assert set(seen["headers"]) == {"Accept", "User-Agent"} and seen["headers"]["User-Agent"] == "mcp-hub"
