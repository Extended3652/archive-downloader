"""Tests for JSON persistence helpers."""
from dataclasses import dataclass

import ia_state


@dataclass
class FakeFile:
    name: str
    size: int
    fmt: str = ""


def test_default_favs_includes_all_folder_buckets():
    favs = ia_state.default_favs()

    assert favs["items"] == []
    assert favs["files"] == []
    assert set(favs["folders"]) == {"TV", "Movies", "Music", "Other"}


def test_load_favs_missing_file_returns_default(tmp_path):
    favs = ia_state.load_favs(str(tmp_path / "missing.json"))

    assert favs == ia_state.default_favs()


def test_load_favs_normalizes_invalid_sections(tmp_path):
    path = tmp_path / "favs.json"
    path.write_text(
        '{"items": "bad", "files": [{"filename": "a.mp4"}], "folders": {"Music": ["Album"], "TV": "bad"}}',
        encoding="utf-8",
    )

    favs = ia_state.load_favs(str(path))

    assert favs["items"] == []
    assert favs["files"] == [{"filename": "a.mp4"}]
    assert favs["folders"]["Music"] == ["Album"]
    assert favs["folders"]["TV"] == []


def test_load_favs_corrupt_json_returns_default(tmp_path):
    path = tmp_path / "favs.json"
    path.write_text("{bad", encoding="utf-8")

    assert ia_state.load_favs(str(path)) == ia_state.default_favs()


def test_save_and_load_session_round_trip(tmp_path):
    path = tmp_path / "session.json"

    assert ia_state.save_session(str(path), {"query_text": "abc", "page": 2})

    assert ia_state.load_session(str(path)) == {"query_text": "abc", "page": 2}


def test_load_session_invalid_json_returns_empty_dict(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("[", encoding="utf-8")

    assert ia_state.load_session(str(path)) == {}


def test_pending_payload_and_load_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    monkeypatch.setattr(ia_state.time, "strftime", lambda _fmt: "2026-01-02 03:04:05")
    data = ia_state.pending_payload(
        "item1",
        "Item One",
        [FakeFile("a.mp4", 123, "MPEG4")],
        "prefix",
        "prefix*",
        ["done.mp4"],
    )

    assert data["timestamp"] == "2026-01-02 03:04:05"
    assert ia_state.save_pending(str(path), data)

    assert ia_state.load_pending(str(path)) == data


def test_load_pending_requires_identifier(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text('{"files": []}', encoding="utf-8")

    assert ia_state.load_pending(str(path)) is None


def test_clear_pending_removes_file_and_allows_missing(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text("{}", encoding="utf-8")

    assert ia_state.clear_pending(str(path))
    assert not path.exists()
    assert ia_state.clear_pending(str(path))
