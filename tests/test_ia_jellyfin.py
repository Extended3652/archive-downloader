"""Tests for Jellyfin library refresh helpers."""

import ia_jellyfin


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_request_library_rescan_posts_refresh_endpoint():
    calls = []

    def fake_open(req, timeout=0):
        calls.append((req, timeout))
        return FakeResponse()

    ok, msg = ia_jellyfin.request_library_rescan(
        jellyfin_url="http://jellyfin.local:8096",
        token="secret",
        opener=fake_open,
        timeout=2,
    )

    req, timeout = calls[0]
    assert ok is True
    assert msg == "Jellyfin library rescan requested."
    assert timeout == 2
    assert req.full_url == "http://jellyfin.local:8096/Library/Refresh"
    assert req.get_method() == "POST"
    assert req.get_header("X-emby-token") == "secret"


def test_request_library_rescan_skips_without_config():
    ok, msg = ia_jellyfin.request_library_rescan(environ={})

    assert ok is False
    assert msg == "Jellyfin rescan skipped; set JELLYFIN_URL and JELLYFIN_API_KEY."
