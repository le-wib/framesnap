"""Update check: one request to GitHub, a correct comparison, safe links."""
import io
import json

import pytest

from framesnap import __version__, app
from framesnap.app import Api, check_for_update, is_newer, parse_version


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(monkeypatch, payload=None, error=None):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if error:
            raise error
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(app.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv(app.NO_UPDATE_CHECK_ENV, raising=False)
    return calls


def test_versions_compare_as_numbers():
    assert parse_version("v2.10.0") == (2, 10, 0) and parse_version("2.3") == (2, 3, 0)
    assert parse_version("nightly") is None and parse_version(None) is None
    assert is_newer("v2.10.0", "2.9.9") and is_newer("2.3.1", "2.3.0")
    assert not is_newer("v2.2.2", "2.3.0")                # an older release is not an update
    assert not is_newer("v2.3.0", "2.3.0") and not is_newer("garbage", "2.3.0")


def test_a_newer_release_is_announced(monkeypatch):
    calls = serve(monkeypatch, {"tag_name": "v9.0.0",
                                "html_url": "https://github.com/le-wib/framesnap/releases/tag/v9.0.0"})
    r = check_for_update("2.3.0")
    assert r == {"current": "2.3.0", "latest": "9.0.0", "update_available": True,
                 "url": "https://github.com/le-wib/framesnap/releases/tag/v9.0.0", "error": False}
    assert calls == ["https://api.github.com/repos/le-wib/framesnap/releases/latest"]


def test_an_older_or_equal_release_is_not(monkeypatch):
    serve(monkeypatch, {"tag_name": "v2.2.2"})
    r = check_for_update("2.3.0")
    assert r["update_available"] is False and r["latest"] == "2.2.2" and not r["error"]


def test_a_foreign_link_is_replaced_by_the_project_page(monkeypatch):
    serve(monkeypatch, {"tag_name": "v9.0.0", "html_url": "https://evil.example/x"})
    assert check_for_update("2.3.0")["url"] == "https://github.com/le-wib/framesnap/releases/tag/v9.0.0"


@pytest.mark.parametrize("payload,error", [(None, OSError("offline")), ({"tag_name": "nightly"}, None),
                                           (["not", "a", "dict"], None)])
def test_failures_are_reported_quietly(monkeypatch, payload, error):
    serve(monkeypatch, payload, error)
    r = check_for_update("2.3.0")
    assert r["error"] is True and r["update_available"] is False


def test_the_check_can_be_switched_off(monkeypatch):
    calls = serve(monkeypatch, {"tag_name": "v9.0.0"})
    monkeypatch.setenv(app.NO_UPDATE_CHECK_ENV, "1")
    assert check_for_update("2.3.0")["error"] is True and calls == []


def test_attaching_the_window_sends_no_request(monkeypatch):
    calls = serve(monkeypatch, {"tag_name": "v9.0.0"})
    api = Api()

    class Win:
        def evaluate_js(self, code):
            pass

    api._attach(Win())
    assert calls == []                      # the page asks once, after it has loaded
    assert api.check_update()["current"] == __version__ and len(calls) == 1


def test_only_release_pages_can_be_opened(monkeypatch):
    opened = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    api = Api()
    assert api.open_url("https://github.com/le-wib/framesnap/releases/tag/v9.0.0")
    assert api.open_url("https://github.com/le-wib/framesnap/releases")
    for bad in ("https://evil.example", "file:///etc/passwd", "https://github.com/le-wib/framesnap-evil",
                "https://github.com/le-wib/framesnap/releases.evil.example", None, 42):
        assert not api.open_url(bad)
    assert len(opened) == 2
