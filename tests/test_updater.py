import httpx

from mcp_hub.updater import check_for_update


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_fetch(releases):
    def fetch(url: str) -> httpx.Response:
        return _FakeResponse(releases)
    return fetch


def test_no_update_when_current_is_latest():
    releases = [{"tag_name": "1.2.0", "html_url": "u", "prerelease": False, "draft": False}]
    assert check_for_update("1.2.0", fetch=_fake_fetch(releases)) is None


def test_update_available_stable():
    releases = [{"tag_name": "1.3.0", "html_url": "https://example/1.3.0", "prerelease": False, "draft": False}]
    info = check_for_update("1.2.0", fetch=_fake_fetch(releases))
    assert info is not None
    assert info.version == "1.3.0"
    assert info.url == "https://example/1.3.0"
    assert info.prerelease is False


def test_beta_excluded_by_default():
    releases = [{"tag_name": "1.3.0-1", "html_url": "u", "prerelease": True, "draft": False}]
    assert check_for_update("1.2.0", include_beta=False, fetch=_fake_fetch(releases)) is None


def test_beta_included_when_flag_set():
    releases = [{"tag_name": "1.3.0-1", "html_url": "u", "prerelease": True, "draft": False}]
    info = check_for_update("1.2.0", include_beta=True, fetch=_fake_fetch(releases))
    assert info is not None
    assert info.version == "1.3.0-1"
    assert info.prerelease is True


def test_stable_release_beats_beta_of_same_version():
    releases = [
        {"tag_name": "1.3.0-1", "html_url": "beta", "prerelease": True, "draft": False},
        {"tag_name": "1.3.0", "html_url": "stable", "prerelease": False, "draft": False},
    ]
    info = check_for_update("1.2.0", include_beta=True, fetch=_fake_fetch(releases))
    assert info.version == "1.3.0"


def test_ignores_draft_releases():
    releases = [{"tag_name": "9.9.9", "html_url": "u", "prerelease": False, "draft": True}]
    assert check_for_update("1.2.0", fetch=_fake_fetch(releases)) is None


def test_returns_none_on_network_error():
    def fetch(url: str) -> httpx.Response:
        raise httpx.ConnectError("nope")
    assert check_for_update("1.2.0", fetch=fetch) is None
