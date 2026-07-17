"""Tests for yt-dlp-backed YouTube helpers."""
import json

import yt_api


def test_yt_dlp_version_uses_configured_path():
    calls = []

    def runner(cmd, timeout=60):
        calls.append((cmd, timeout))
        return 0, "2026.06.09\n", ""

    ok, msg = yt_api.yt_dlp_version("/tmp/fake-yt-dlp", runner=runner)

    assert ok is True
    assert msg == "2026.06.09"
    assert calls == [(["/tmp/fake-yt-dlp", "--version"], 10)]


def test_yt_search_parses_flat_playlist_json():
    raw = json.dumps(
        {
            "entries": [
                {
                    "id": "abc123",
                    "title": "Sample Video",
                    "webpage_url": "https://www.youtube.com/watch?v=abc123",
                    "uploader": "Channel Name",
                    "duration": 123,
                    "upload_date": "20260609",
                }
            ]
        }
    )

    results = yt_api.parse_yt_search_json(raw)

    assert len(results) == 1
    row = results[0]
    assert row.source == "youtube"
    assert row.identifier == "yt-abc123"
    assert row.video_id == "abc123"
    assert row.title == "Sample Video"
    assert row.webpage_url == "https://www.youtube.com/watch?v=abc123"
    assert row.uploader == "Channel Name"
    assert row.duration == 123
    assert row.upload_date == "20260609"


def test_yt_search_missing_fields_do_not_crash():
    results = yt_api.parse_yt_search_json(json.dumps({"entries": [{"id": "abc123"}]}))

    assert len(results) == 1
    assert results[0].title == "(no title)"
    assert results[0].webpage_url == "https://www.youtube.com/watch?v=abc123"


def test_yt_search_command_uses_machine_readable_json_and_configured_path():
    calls = []

    def runner(cmd, timeout=60):
        calls.append((cmd, timeout))
        return 0, json.dumps({"entries": [{"id": "abc123", "title": "One"}]}), ""

    results, total, err = yt_api.yt_search("test terms", 10, yt_dlp_path="/opt/yt-dlp", runner=runner)

    assert err == ""
    assert total == 1
    assert results[0].source == "youtube"
    assert calls == [(["/opt/yt-dlp", "-J", "--flat-playlist", "ytsearch10:test terms"], 60)]


def test_yt_metadata_url_uses_no_playlist_and_same_parser():
    calls = []

    def runner(cmd, timeout=60):
        calls.append((cmd, timeout))
        return 0, json.dumps({"id": "abc123", "title": "Direct"}), ""

    result, err = yt_api.yt_metadata_url(
        "https://www.youtube.com/watch?v=abc123",
        yt_dlp_path="/opt/yt-dlp",
        runner=runner,
    )

    assert err == ""
    assert result is not None
    assert result.source == "youtube"
    assert result.webpage_url == "https://www.youtube.com/watch?v=abc123"
    assert calls == [(["/opt/yt-dlp", "-J", "--no-playlist", "https://www.youtube.com/watch?v=abc123"], 60)]
