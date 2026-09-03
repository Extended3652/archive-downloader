"""Tests for read-only archive-downloader configuration."""
import json
import os

import ia_config


def test_config_path_uses_override_and_xdg_home():
    assert ia_config.config_path({"IA_CONFIG_PATH": "~/custom.json"}) == os.path.expanduser("~/custom.json")

    path = ia_config.config_path({"XDG_CONFIG_HOME": "/tmp/xdg"})

    assert path == "/tmp/xdg/archive-downloader/config.json"


def test_load_config_defaults_when_missing_or_corrupt(tmp_path):
    missing = tmp_path / "missing.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{bad", encoding="utf-8")

    assert ia_config.load_config(str(missing), environ={}) == ia_config.DEFAULT_CONFIG
    assert ia_config.load_config(str(corrupt), environ={}) == ia_config.DEFAULT_CONFIG
    assert ia_config.DEFAULT_CONFIG["default_bucket"] == "Movies"


def test_load_config_normalizes_valid_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "media_root": "~/MediaRoot",
                "yt_dlp_path": "~/bin/yt-dlp",
                "default_bucket": "music",
                "default_filter": "audio",
                "default_sort": "downloads desc",
                "title_only": "true",
                "license_gate": "yes",
                "no_change_timestamp": "off",
                "rows_per_page": "50",
                "radarr_enabled": "true",
                "radarr_url": "http://radarr:7878/",
                "radarr_api_key": "file-key",
                "radarr_local_movie_root": "~/Movies",
                "radarr_root_folder": "/data/movies",
                "radarr_quality_profile_id": "3",
                "radarr_monitor_movie": "false",
                "radarr_search_on_add": "true",
                "radarr_timeout_s": "12.5",
                "bazarr_enabled": "true",
                "bazarr_url": "http://bazarr:6767/",
                "bazarr_api_key": "bazarr-file-key",
                "bazarr_timeout_s": "9.5",
                "bazarr_wait_timeout_s": "30",
                "bazarr_poll_interval_s": "1.5",
            }
        ),
        encoding="utf-8",
    )

    cfg = ia_config.load_config(str(path), environ={})

    assert cfg["media_root"] == os.path.expanduser("~/MediaRoot")
    assert cfg["yt_dlp_path"] == os.path.expanduser("~/bin/yt-dlp")
    assert cfg["default_bucket"] == "Music"
    assert cfg["default_filter"] == "audio"
    assert cfg["default_sort"] == "downloads desc"
    assert cfg["title_only"] is True
    assert cfg["license_gate"] is True
    assert cfg["no_change_timestamp"] is False
    assert cfg["rows_per_page"] == 50
    assert cfg["radarr_enabled"] is True
    assert cfg["radarr_url"] == "http://radarr:7878/"
    assert cfg["radarr_api_key"] == "file-key"
    assert cfg["radarr_local_movie_root"] == os.path.expanduser("~/Movies")
    assert cfg["radarr_root_folder"] == "/data/movies"
    assert cfg["radarr_quality_profile_id"] == 3
    assert cfg["radarr_monitor_movie"] is False
    assert cfg["radarr_search_on_add"] is False
    assert cfg["radarr_timeout_s"] == 12.5
    assert cfg["bazarr_enabled"] is True
    assert cfg["bazarr_url"] == "http://bazarr:6767/"
    assert cfg["bazarr_api_key"] == "bazarr-file-key"
    assert cfg["bazarr_timeout_s"] == 9.5
    assert cfg["bazarr_wait_timeout_s"] == 30
    assert cfg["bazarr_poll_interval_s"] == 1.5


def test_load_config_rejects_invalid_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "media_root": "",
                "default_bucket": "Bad",
                "default_filter": "bad",
                "default_sort": "bad",
                "title_only": "maybe",
                "license_gate": "maybe",
                "no_change_timestamp": "maybe",
                "rows_per_page": 0,
            }
        ),
        encoding="utf-8",
    )

    assert ia_config.load_config(str(path), environ={}) == ia_config.DEFAULT_CONFIG


def test_environment_overrides_config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "media_root": "/from-file",
                "default_bucket": "TV",
                "default_filter": "movies",
                "default_sort": "",
                "title_only": False,
                "license_gate": False,
                "no_change_timestamp": True,
                "rows_per_page": 10,
                "radarr_api_key": "file-key",
            }
        ),
        encoding="utf-8",
    )

    cfg = ia_config.load_config(
        str(path),
        environ={
            "IA_MEDIA_ROOT": "~/env-media",
            "IA_DEFAULT_BUCKET": "other",
            "IA_DEFAULT_FILTER": "texts",
            "IA_DEFAULT_SORT": "date desc",
            "IA_TITLE_ONLY": "true",
            "IA_LICENSE_GATE": "1",
            "IA_NO_CHANGE_TIMESTAMP": "false",
            "IA_ROWS_PER_PAGE": "25",
            "IA_RADARR_ENABLED": "true",
            "IA_RADARR_URL": "http://env-radarr:7878",
            "IA_RADARR_API_KEY": "env-key",
            "IA_RADARR_LOCAL_MOVIE_ROOT": "/env/local/Movies",
            "IA_RADARR_ROOT_FOLDER": "/env/radarr/Movies",
            "IA_RADARR_QUALITY_PROFILE_ID": "5",
            "IA_RADARR_MONITOR_MOVIE": "false",
            "IA_RADARR_TIMEOUT_S": "7",
            "IA_BAZARR_ENABLED": "true",
            "IA_BAZARR_URL": "http://env-bazarr:6767",
            "IA_BAZARR_API_KEY": "env-bazarr-key",
            "IA_BAZARR_TIMEOUT_S": "8",
            "IA_BAZARR_WAIT_TIMEOUT_S": "40",
            "IA_BAZARR_POLL_INTERVAL_S": "2",
        },
    )

    assert cfg["media_root"] == os.path.expanduser("~/env-media")
    assert cfg["default_bucket"] == "Other"
    assert cfg["default_filter"] == "texts"
    assert cfg["default_sort"] == "date desc"
    assert cfg["title_only"] is True
    assert cfg["license_gate"] is True
    assert cfg["no_change_timestamp"] is False
    assert cfg["rows_per_page"] == 25
    assert cfg["radarr_enabled"] is True
    assert cfg["radarr_url"] == "http://env-radarr:7878"
    assert cfg["radarr_api_key"] == "env-key"
    assert cfg["radarr_local_movie_root"] == "/env/local/Movies"
    assert cfg["radarr_root_folder"] == "/env/radarr/Movies"
    assert cfg["radarr_quality_profile_id"] == 5
    assert cfg["radarr_monitor_movie"] is False
    assert cfg["radarr_timeout_s"] == 7
    assert cfg["bazarr_enabled"] is True
    assert cfg["bazarr_url"] == "http://env-bazarr:6767"
    assert cfg["bazarr_api_key"] == "env-bazarr-key"
    assert cfg["bazarr_timeout_s"] == 8
    assert cfg["bazarr_wait_timeout_s"] == 40
    assert cfg["bazarr_poll_interval_s"] == 2


def test_save_and_set_config_value_round_trip(tmp_path):
    path = tmp_path / "config" / "config.json"

    cfg = ia_config.set_config_value("default_bucket", "movies", str(path), environ={})

    assert cfg["default_bucket"] == "Movies"
    assert ia_config.load_raw_config(str(path), environ={})["default_bucket"] == "Movies"

    cfg = ia_config.set_config_value("default_filter", "texts", str(path), environ={})
    assert cfg["default_filter"] == "texts"

    cfg = ia_config.set_config_value("default_sort", "date asc", str(path), environ={})
    assert cfg["default_sort"] == "date asc"

    cfg = ia_config.set_config_value("radarr_quality_profile_id", "3", str(path), environ={})
    assert cfg["radarr_quality_profile_id"] == 3

    cfg = ia_config.set_config_value("radarr_search_on_add", "true", str(path), environ={})
    assert cfg["radarr_search_on_add"] is False

    cfg = ia_config.set_config_value("bazarr_enabled", "true", str(path), environ={})
    assert cfg["bazarr_enabled"] is True

    cfg = ia_config.set_config_value("bazarr_wait_timeout_s", "45", str(path), environ={})
    assert cfg["bazarr_wait_timeout_s"] == 45


def test_set_config_value_rejects_invalid_key_and_value(tmp_path):
    path = tmp_path / "config.json"

    try:
        ia_config.set_config_value("bad", "x", str(path), environ={})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "unknown config key" in str(e)

    try:
        ia_config.set_config_value("rows_per_page", "0", str(path), environ={})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "rows_per_page" in str(e)
