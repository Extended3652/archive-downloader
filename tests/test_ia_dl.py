"""Tests for the scriptable ia_dl CLI helpers."""
import os
import stat

import pytest

import ia_dl
import ia_minotaur_events
from ia_common import IACommandError, IAFile, SearchResult


def test_ia_search_uses_shared_advancedsearch(monkeypatch):
    calls = []

    def fake_search(query, rows, page, sort, runner=None):
        calls.append((query, rows, page, sort, runner))
        return [SearchResult("item1", "Item One")], 1, ""

    monkeypatch.setattr(ia_dl.ia_api, "ia_search_via_curl", fake_search)

    results = ia_dl.ia_search("query", 5, page=2, sort="downloads desc")

    assert [r.identifier for r in results] == ["item1"]
    assert calls == [("query", 5, 2, "downloads desc", ia_dl.curl_runner)]


def test_ia_search_raises_command_error_on_search_error(monkeypatch):
    monkeypatch.setattr(
        ia_dl.ia_api,
        "ia_search_via_curl",
        lambda query, rows, page, sort, runner=None: ([], 0, "bad query"),
    )

    with pytest.raises(IACommandError) as excinfo:
        ia_dl.ia_search("bad", 5)

    assert excinfo.value.cmd == ["curl", "https://archive.org/advancedsearch.php"]
    assert excinfo.value.stderr == "bad query"


def test_ia_search_best_effort_tries_precise_title_year_first(monkeypatch):
    calls = []

    def fake_search(query, rows, page=1, sort=""):
        calls.append((query, rows, page, sort))
        if query == 'title:("Fargo") AND year:1996 AND mediatype:movies':
            return [SearchResult("fargo-1996_202605", "Fargo (1996)", year="1996")]
        return []

    monkeypatch.setattr(ia_dl, "ia_search", fake_search)

    results = ia_dl.ia_search_best_effort("Fargo 1996", 10, "movies", False, page=2, sort="downloads desc")

    assert [r.identifier for r in results] == ["fargo-1996_202605"]
    assert calls == [('title:("Fargo") AND year:1996 AND mediatype:movies', 10, 2, "downloads desc")]


def test_search_result_line_includes_rich_metadata():
    line = ia_dl.search_result_line(
        SearchResult(
            "item1",
            "Item One",
            year="1930",
            mediatype="movies",
            formats="Archive BitTorrent, MPEG4",
            downloads=1250,
            licenseurl="https://creativecommons.org/licenses/by/4.0/",
        )
    )

    assert line == "item1\tItem One (1930)\tmovies | 1.2K dl | torrent | lic:open"


def test_biggest_file_prefers_real_media_over_metadata():
    files = [
        IAFile("item.xml", 5000, "XML"),
        IAFile("movie.mp4", 1000, "MPEG4"),
        IAFile("preview.jpg", 9000, "JPEG"),
    ]

    assert ia_dl.biggest_file(files).name == "movie.mp4"


def test_ia_list_files_reuses_shared_api(monkeypatch):
    calls = []

    def fake_ia_files(identifier, runner=None):
        calls.append((identifier, runner))
        return [IAFile("movie.mp4", 123, "MPEG4")], {"files": []}, ""

    monkeypatch.setattr(ia_dl.ia_api, "ia_files", fake_ia_files)

    files = ia_dl.ia_list_files("item1")

    assert files == [IAFile("movie.mp4", 123, "MPEG4")]
    assert calls[0][0] == "item1"
    assert callable(calls[0][1])


def test_ia_list_files_raises_command_error_on_shared_api_error(monkeypatch):
    monkeypatch.setattr(
        ia_dl.ia_api,
        "ia_files",
        lambda identifier, runner=None: ([], None, "metadata returned non-JSON"),
    )

    with pytest.raises(IACommandError) as excinfo:
        ia_dl.ia_list_files("baditem")

    assert excinfo.value.cmd == ["ia", "metadata", "baditem"]
    assert excinfo.value.stderr == "metadata returned non-JSON"


def test_filter_files_filters_by_extension_and_regex():
    files = [
        IAFile("Movie.1080p.mp4", 100, "MPEG4"),
        IAFile("Movie.txt", 1, "Text"),
        IAFile("Trailer.mkv", 50, "Matroska"),
    ]

    filtered = ia_dl.filter_files(files, ["mp4", ".mkv"], "movie|trailer")

    assert [f.name for f in filtered] == ["Movie.1080p.mp4", "Trailer.mkv"]


def test_filter_files_raises_for_bad_regex():
    with pytest.raises(ia_dl.BadFileRegex):
        ia_dl.filter_files([IAFile("a.mp4", 1)], None, "[")


def test_main_list_bad_regex_returns_cli_error(monkeypatch, capsys):
    monkeypatch.setattr(ia_dl, "ia_list_files", lambda _identifier: [IAFile("a.mp4", 1)])

    rc = ia_dl.main(["list", "item1", "--regex", "["])

    captured = capsys.readouterr()
    assert rc == 2
    assert "Bad regex:" in captured.err


def test_main_download_bad_regex_returns_cli_error(monkeypatch, capsys):
    monkeypatch.setattr(ia_dl, "ia_list_files", lambda _identifier: [IAFile("a.mp4", 1)])

    rc = ia_dl.main(["download", "item1", "--regex", "["])

    captured = capsys.readouterr()
    assert rc == 2
    assert "Bad regex:" in captured.err


def test_main_search_builds_query_and_prints_results(monkeypatch, capsys):
    calls = []

    def fake_search(query, rows, page=1, sort=""):
        calls.append((query, rows, page, sort))
        return [SearchResult("metropolis", "Metropolis", year="1927", mediatype="movies")]

    monkeypatch.setattr(ia_dl, "ia_search", fake_search)

    rc = ia_dl.main(["search", "Metropolis 1927", "--filter", "movies", "--sort", "downloads", "--rows", "3", "--page", "2"])

    captured = capsys.readouterr()
    assert rc == 0
    assert calls == [
        (
            'title:("Metropolis") AND year:1927 AND mediatype:movies',
            3,
            2,
            "downloads desc",
        )
    ]
    assert "metropolis\tMetropolis (1927)\tmovies" in captured.out


def test_main_search_supports_archive_presets(monkeypatch, capsys):
    calls = []

    def fake_search(query, rows, page=1, sort=""):
        calls.append((query, rows, page, sort))
        return [SearchResult("item1", "Item One")]

    monkeypatch.setattr(ia_dl, "ia_search", fake_search)

    rc = ia_dl.main(["search", "Chaplin", "--preset", "public_domain_movies", "--rows", "2"])

    captured = capsys.readouterr()
    assert rc == 0
    assert calls[0][0].startswith("(mediatype:movies AND")
    assert "title:(\"Chaplin\")" in calls[0][0]
    assert "item1\tItem One" in captured.out


def test_minotaur_event_failure_does_not_crash():
    def failing_runner(*_args, **_kwargs):
        raise OSError("minotaur unavailable")

    ok = ia_minotaur_events.emit_minotaur_event(
        "archive.started",
        "Archive started",
        "safe test",
        wrapper_path="/mnt/ssd/home-pi/projects/minotaur_core/scripts/minotaur-event",
        runner=failing_runner,
    )

    assert ok is False


def test_minotaur_event_uses_env_wrapper(monkeypatch, tmp_path):
    wrapper = tmp_path / "minotaur-event"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    calls = []

    def fake_runner(cmd, **_kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setenv("MINOTAUR_EVENT_WRAPPER", str(wrapper))

    ok = ia_minotaur_events.emit_minotaur_event(
        "archive.started",
        "Archive started",
        "safe test",
        runner=fake_runner,
    )

    assert ok is True
    assert calls[0][0] == str(wrapper)


def test_ia_download_emits_started_and_completed(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(ia_dl, "emit_archive_started", lambda message: calls.append(("started", message)))
    monkeypatch.setattr(ia_dl, "emit_archive_completed", lambda message: calls.append(("completed", message)))
    monkeypatch.setattr(ia_dl, "emit_archive_failed", lambda message: calls.append(("failed", message)))

    def fake_run(_cmd, check=True):
        item_dir = tmp_path / "item1"
        item_dir.mkdir()
        output = item_dir / "movie.mp4"
        output.write_bytes(b"movie")
        item_dir.chmod(0o2755)
        output.chmod(0o644)

    monkeypatch.setattr(ia_dl, "run", fake_run)

    ia_dl.ia_download("item1", str(tmp_path), None, "movie.mp4")

    assert calls == [
        ("started", "item1 movie.mp4"),
        ("completed", str(tmp_path / "item1" / "movie.mp4")),
    ]
    assert (tmp_path / "item1" / "movie.mp4").read_bytes() == b"movie"
    assert stat.S_IMODE(os.stat(tmp_path / "item1").st_mode) == 0o2775
    assert stat.S_IMODE(os.stat(tmp_path / "item1" / "movie.mp4").st_mode) == 0o664


def test_ia_download_emits_failure_without_swallowing_exception(monkeypatch, tmp_path):
    calls = []

    def fail_run(_cmd, check=True):
        raise RuntimeError("download exploded")

    monkeypatch.setattr(ia_dl, "emit_archive_started", lambda message: calls.append(("started", message)))
    monkeypatch.setattr(ia_dl, "emit_archive_completed", lambda message: calls.append(("completed", message)))
    monkeypatch.setattr(ia_dl, "emit_archive_failed", lambda message: calls.append(("failed", message)))
    monkeypatch.setattr(ia_dl, "run", fail_run)

    with pytest.raises(RuntimeError):
        ia_dl.ia_download("item1", str(tmp_path), None, "movie.mp4")

    assert calls[0] == ("started", "item1 movie.mp4")
    assert calls[1][0] == "failed"
    assert "download exploded" in calls[1][1]


def test_main_sets_process_umask(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ia_dl, "set_process_umask", lambda: calls.append("umask"))
    monkeypatch.setattr(ia_dl, "ia_search", lambda *_args, **_kwargs: [])

    rc = ia_dl.main(["search", "nothing", "--rows", "1"])

    assert rc == 1
    assert calls == ["umask"]
