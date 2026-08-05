"""Tests for ia-config CLI."""
import json

import ia_config_cli


def test_path_prints_config_path(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.json"
    monkeypatch.setenv("IA_CONFIG_PATH", str(path))

    rc = ia_config_cli.main(["path"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == str(path)


def test_show_prints_effective_config(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text('{"default_bucket": "Music", "radarr_api_key": "secret"}', encoding="utf-8")
    monkeypatch.setenv("IA_CONFIG_PATH", str(path))

    rc = ia_config_cli.main(["show"])

    captured = capsys.readouterr()
    assert rc == 0
    data = json.loads(captured.out)
    assert data["default_bucket"] == "Music"
    assert data["radarr_api_key"] == "[redacted]"
    assert data["media_root"]


def test_set_writes_config_and_prints_effective_config(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.json"
    monkeypatch.setenv("IA_CONFIG_PATH", str(path))

    rc = ia_config_cli.main(["set", "license_gate", "true"])

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(path.read_text(encoding="utf-8"))["license_gate"] is True
    assert json.loads(captured.out)["license_gate"] is True


def test_set_search_defaults(monkeypatch, tmp_path, capsys):
    path = tmp_path / "config.json"
    monkeypatch.setenv("IA_CONFIG_PATH", str(path))

    assert ia_config_cli.main(["set", "default_filter", "texts"]) == 0
    assert ia_config_cli.main(["set", "default_sort", "date desc"]) == 0
    assert ia_config_cli.main(["set", "title_only", "true"]) == 0

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["default_filter"] == "texts"
    assert data["default_sort"] == "date desc"
    assert data["title_only"] is True


def test_set_invalid_value_returns_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("IA_CONFIG_PATH", str(tmp_path / "config.json"))

    rc = ia_config_cli.main(["set", "rows_per_page", "0"])

    captured = capsys.readouterr()
    assert rc == 2
    assert "rows_per_page" in captured.err
