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


def test_load_config_normalizes_valid_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "media_root": "~/MediaRoot",
                "default_bucket": "music",
                "default_filter": "audio",
                "default_sort": "downloads desc",
                "title_only": "true",
                "license_gate": "yes",
                "no_change_timestamp": "off",
                "rows_per_page": "50",
            }
        ),
        encoding="utf-8",
    )

    cfg = ia_config.load_config(str(path), environ={})

    assert cfg["media_root"] == os.path.expanduser("~/MediaRoot")
    assert cfg["default_bucket"] == "Music"
    assert cfg["default_filter"] == "audio"
    assert cfg["default_sort"] == "downloads desc"
    assert cfg["title_only"] is True
    assert cfg["license_gate"] is True
    assert cfg["no_change_timestamp"] is False
    assert cfg["rows_per_page"] == 50


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


def test_save_and_set_config_value_round_trip(tmp_path):
    path = tmp_path / "config" / "config.json"

    cfg = ia_config.set_config_value("default_bucket", "movies", str(path), environ={})

    assert cfg["default_bucket"] == "Movies"
    assert ia_config.load_raw_config(str(path), environ={})["default_bucket"] == "Movies"

    cfg = ia_config.set_config_value("default_filter", "texts", str(path), environ={})
    assert cfg["default_filter"] == "texts"

    cfg = ia_config.set_config_value("default_sort", "date asc", str(path), environ={})
    assert cfg["default_sort"] == "date asc"


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
