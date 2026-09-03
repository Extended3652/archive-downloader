"""Tests for optional Bazarr subtitle handoff."""
import json
import os
from urllib import error

import ia_bazarr


def settings(**overrides):
    base = {
        "enabled": True,
        "url": "http://bazarr.local:6767",
        "api_key": "secret-key",
        "timeout_s": 5,
        "wait_timeout_s": 0.2,
        "poll_interval_s": 0.1,
    }
    base.update(overrides)
    return ia_bazarr.BazarrSettings(**base)


def write_movie(tmp_path, rel="Metropolis (1927)/Metropolis (1927).mp4"):
    path = tmp_path / "Movies" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"movie")
    return path


class FakeClient:
    def __init__(self):
        self.actions = []
        self.movie_data = {
            "radarrId": 7,
            "missing_subtitles": {"en": "English"},
            "subtitles": [],
        }
        self.raise_error = None

    def movie_action(self, radarr_id, action):
        self.actions.append((radarr_id, action))
        if self.raise_error:
            raise self.raise_error

    def movie(self, radarr_id):
        return dict(self.movie_data)


def test_successful_targeted_bazarr_refresh_search_download(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    calls = []

    def sleeper(_seconds):
        calls.append("sleep")
        if calls == ["sleep"]:
            (movie.parent / "Metropolis (1927).eng.srt").write_text("subtitle", encoding="utf-8")

    result = ia_bazarr.handoff_movie(
        str(movie),
        7,
        settings=settings(),
        client=client,
        sleeper=sleeper,
    )

    assert result.status == "downloaded"
    assert client.actions == [(7, "sync"), (7, "scan-disk"), (7, "search-missing")]
    assert result.subtitle_path.endswith(".eng.srt")


def test_bazarr_unavailable_is_nonfatal(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.raise_error = ia_bazarr.BazarrError("Bazarr unavailable: timed out.")

    result = ia_bazarr.handoff_movie(str(movie), 7, settings=settings(), client=client)

    assert result.ok is False
    assert result.status == "bazarr_failed"
    assert movie.exists()


def test_bazarr_search_returns_no_subtitle(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()

    result = ia_bazarr.handoff_movie(str(movie), 7, settings=settings(), client=client)

    assert result.ok is True
    assert result.status == "not_found"
    assert client.actions == [(7, "sync"), (7, "scan-disk"), (7, "search-missing")]


def test_existing_subtitle_skips_provider_search(tmp_path):
    movie = write_movie(tmp_path)
    sub = movie.parent / "Metropolis (1927).eng.sdh.srt"
    sub.write_text("existing", encoding="utf-8")
    client = FakeClient()

    result = ia_bazarr.handoff_movie(str(movie), 7, settings=settings(), client=client)

    assert result.status == "existing_subtitle"
    assert client.actions == [(7, "sync"), (7, "scan-disk")]
    assert sub.read_text(encoding="utf-8") == "existing"


def test_no_missing_english_skips_provider_search(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.movie_data = {"radarrId": 7, "missing_subtitles": {}, "subtitles": []}

    result = ia_bazarr.handoff_movie(str(movie), 7, settings=settings(), client=client)

    assert result.status == "no_missing"
    assert client.actions == [(7, "sync"), (7, "scan-disk")]


def test_missing_identity_does_not_fail_import(tmp_path):
    movie = write_movie(tmp_path)

    result = ia_bazarr.handoff_movie(str(movie), 0, settings=settings(), client=FakeClient())

    assert result.ok is False
    assert result.status == "missing_identity"
    assert movie.exists()


def test_client_redacts_api_key_in_errors(tmp_path):
    movie = write_movie(tmp_path)

    def opener(_req, timeout):
        raise RuntimeError("failed with secret-key")

    client = ia_bazarr.BazarrClient(settings(), opener=opener)

    result = ia_bazarr.handoff_movie(
        str(movie),
        7,
        settings=settings(),
        client=client,
    )

    assert "secret-key" not in result.message


def test_client_uses_bazarr_patch_form_api():
    requests = []

    class Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b""

    def opener(req, timeout):
        requests.append((req, timeout))
        return Resp()

    client = ia_bazarr.BazarrClient(settings(), opener=opener)
    client.movie_action(12, "search-missing")

    req, timeout = requests[0]
    assert req.full_url == "http://bazarr.local:6767/api/movies"
    assert req.get_method() == "PATCH"
    assert req.get_header("X-api-key") == "secret-key"
    assert req.data == b"radarrid=12&action=search-missing"
    assert timeout == 5


def test_client_gets_movie_metadata():
    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"data": [{"radarrId": 12, "missing_subtitles": {"en": "English"}}]}).encode("utf-8")

    client = ia_bazarr.BazarrClient(settings(), opener=lambda _req, timeout: Resp())

    assert client.movie(12)["radarrId"] == 12


def test_http_error_is_reported_without_key(tmp_path):
    movie = write_movie(tmp_path)

    def opener(_req, _timeout):
        raise error.HTTPError("http://bazarr.local/api/movies?apikey=secret-key", 500, "boom", {}, None)

    st = settings()
    client = ia_bazarr.BazarrClient(st, opener=opener)
    result = ia_bazarr.handoff_movie(str(movie), 7, settings=st, client=client)

    assert result.status == "bazarr_failed"
    assert "secret-key" not in result.message
