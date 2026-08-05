"""Tests for optional Radarr registration."""
import os

import ia_radarr


def settings(**overrides):
    base = {
        "enabled": True,
        "url": "http://radarr.local:7878",
        "api_key": "secret-key",
        "local_movie_root": "/local/Movies",
        "root_folder": "/radarr/Movies",
        "quality_profile_id": 3,
        "monitor_movie": True,
        "search_on_add": False,
        "timeout_s": 5,
    }
    base.update(overrides)
    return ia_radarr.RadarrSettings(**base)


class FakeClient:
    def __init__(self):
        self.movies_data = []
        self.lookup_data = []
        self.tmdb_lookup_data = []
        self.root_folders_data = [{"path": "/radarr/Movies"}]
        self.quality_profiles_data = [{"id": 3, "name": "Archive"}]
        self.added = []
        self.updated = []
        self.refreshed = []
        self.raise_error = None

    def root_folders(self):
        return self.root_folders_data

    def quality_profiles(self):
        return self.quality_profiles_data

    def movies(self):
        if self.raise_error:
            raise self.raise_error
        return self.movies_data

    def lookup_tmdb(self, tmdb_id):
        self.tmdb_id = tmdb_id
        return self.tmdb_lookup_data

    def lookup(self, title, year=""):
        self.lookup_term = (title, year)
        return self.lookup_data

    def add_movie(self, payload):
        self.added.append(payload)
        movie = dict(payload)
        movie["id"] = 44
        return movie

    def update_movie(self, movie_id, payload):
        self.updated.append((movie_id, payload))
        movie = dict(payload)
        movie["id"] = movie_id
        return movie

    def refresh_movie(self, movie_id):
        self.refreshed.append(movie_id)
        return {"id": 99}


def write_movie(tmp_path, rel="Metropolis (1927)/Metropolis (1927).mp4"):
    path = tmp_path / "Movies" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"movie")
    return path


def test_disabled_integration_skips(tmp_path):
    movie = write_movie(tmp_path)
    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(enabled=False, local_movie_root=str(tmp_path / "Movies")),
        client=FakeClient(),
    )

    assert result.status == "disabled"


def test_missing_api_key_or_required_config(tmp_path):
    movie = write_movie(tmp_path)

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(api_key="", local_movie_root=str(tmp_path / "Movies")),
        client=FakeClient(),
    )

    assert result.status == "missing_config"
    assert "API key" in result.message


def test_successful_lookup_by_tmdb_id(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.tmdb_lookup_data = [{"title": "Metropolis", "year": 1927, "tmdbId": 19, "titleSlug": "metropolis-19"}]

    result = ia_radarr.register_completed_movie(
        str(movie),
        metadata={"metadata": {"external-identifier": "tmdb:19"}},
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    assert result.status == "added"
    assert client.tmdb_id == 19


def test_successful_fallback_lookup_by_title_and_year(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.lookup_data = [{"title": "Metropolis", "year": 1927, "tmdbId": 19, "titleSlug": "metropolis-19"}]

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    assert result.status == "added"
    assert client.lookup_term == ("Metropolis", "1927")


def test_ambiguous_lookup_result(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.lookup_data = [
        {"title": "Metropolis", "year": 1927, "tmdbId": 19},
        {"title": "Metropolis", "year": 1927, "tmdbId": 999},
    ]

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    assert result.status == "lookup_failed"
    assert "ambiguous" in result.message
    assert client.added == []


def test_movie_already_present_with_correct_path_dry_run_does_not_refresh(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.movies_data = [{"id": 7, "title": "Metropolis", "year": 1927, "tmdbId": 19, "path": "/radarr/Movies/Metropolis (1927)"}]

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
        dry_run=True,
    )

    assert result.status == "already_registered"
    assert result.changed is False
    assert result.movie_id == 7
    assert result.radarr_path == "/radarr/Movies/Metropolis (1927)"
    assert result.message == "Radarr movie already registered; no changes made."
    assert client.refreshed == []


def test_movie_already_present_with_correct_path_apply_refreshes(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.movies_data = [{"id": 7, "title": "Metropolis", "year": 1927, "tmdbId": 19, "path": "/radarr/Movies/Metropolis (1927)"}]

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    assert result.status == "already_registered"
    assert result.changed is False
    assert result.movie_id == 7
    assert result.radarr_path == "/radarr/Movies/Metropolis (1927)"
    assert result.message == "Radarr movie already registered; refresh requested."
    assert client.refreshed == [7]


def test_movie_already_present_with_stale_path(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.movies_data = [{"id": 7, "title": "Metropolis", "year": 1927, "tmdbId": 19, "path": "/old/Metropolis"}]

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    assert result.status == "path_updated"
    assert client.updated[0][1]["path"] == "/radarr/Movies/Metropolis (1927)"
    assert client.refreshed == [7]


def test_new_movie_added_monitored_with_no_search_command(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.lookup_data = [{"title": "Metropolis", "year": 1927, "tmdbId": 19, "titleSlug": "metropolis-19"}]

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    payload = client.added[0]
    assert result.status == "added"
    assert payload["monitored"] is True
    assert payload["addOptions"] == {"searchForMovie": False}
    assert client.refreshed == [44]


def test_radarr_unavailable(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.raise_error = ia_radarr.RadarrError("Radarr unavailable: refused.")

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    assert result.status == "radarr_failed"
    assert movie.exists()


def test_authentication_failure(tmp_path):
    movie = write_movie(tmp_path)
    client = FakeClient()
    client.raise_error = ia_radarr.RadarrError("Radarr authentication failed.", status=401)

    result = ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    assert result.status == "radarr_failed"
    assert "authentication" in result.message


def test_registration_failure_does_not_corrupt_completed_movie(tmp_path):
    movie = write_movie(tmp_path)
    original = movie.read_bytes()
    client = FakeClient()
    client.raise_error = ia_radarr.RadarrError("Radarr unavailable.")

    ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
    )

    assert movie.exists()
    assert movie.read_bytes() == original


def test_api_key_is_not_exposed_in_logs(tmp_path):
    movie = write_movie(tmp_path)
    logs = []
    client = FakeClient()
    client.raise_error = ia_radarr.RadarrError("bad secret-key")

    ia_radarr.register_completed_movie(
        str(movie),
        settings=settings(local_movie_root=str(tmp_path / "Movies")),
        client=client,
        logger=logs.append,
    )

    assert logs
    assert all("secret-key" not in line for line in logs)


def test_local_to_radarr_path_mapping():
    radarr_path, err = ia_radarr.map_local_to_radarr_path(
        "/mnt/ssd/media/Movies/Metropolis (1927)",
        settings(local_movie_root="/mnt/ssd/media/Movies", root_folder="/data/movies"),
    )

    assert err == ""
    assert radarr_path == "/data/movies/Metropolis (1927)"


def test_path_mapping_refuses_unmapped_folder():
    radarr_path, err = ia_radarr.map_local_to_radarr_path(
        "/mnt/ssd/media/TV/Show",
        settings(local_movie_root="/mnt/ssd/media/Movies", root_folder="/data/movies"),
    )

    assert radarr_path == ""
    assert "not under" in err


def test_load_settings_uses_environment_api_key():
    cfg = {
        "radarr_enabled": True,
        "radarr_url": "http://radarr",
        "radarr_api_key": "file-key",
        "radarr_local_movie_root": "/local/Movies",
        "radarr_root_folder": "/radarr/Movies",
        "radarr_quality_profile_id": 2,
        "radarr_monitor_movie": True,
        "radarr_timeout_s": 3,
    }

    loaded = ia_radarr.load_settings(cfg, environ={"RADARR_API_KEY": "env-key"})

    assert loaded.api_key == "env-key"
    assert loaded.search_on_add is False
