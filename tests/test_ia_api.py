"""Tests for Internet Archive command/search/metadata helpers."""
import json

import ia_api


def runner_for(returncode=0, stdout="", stderr=""):
    calls = []

    def runner(cmd, timeout=60):
        calls.append((cmd, timeout))
        return returncode, stdout, stderr

    runner.calls = calls
    return runner


def test_ia_ok_success_and_failure():
    ok_runner = runner_for(stdout="ia 5.7.2\n")
    fail_runner = runner_for(returncode=127, stderr="command not found")

    assert ia_api.ia_ok(runner=ok_runner) == (True, "ia 5.7.2")
    assert ia_api.ia_ok(runner=fail_runner) == (False, "command not found")


def test_curl_version_returns_first_line():
    runner = runner_for(stdout="curl 8.0\nfeatures\n")

    assert ia_api.curl_version(runner=runner) == (True, "curl 8.0")


def test_search_parses_docs_and_skips_missing_identifier():
    payload = {
        "response": {
            "numFound": 3,
            "docs": [
                {
                    "identifier": "id1",
                    "title": "Title One",
                    "year": 1999,
                    "creator": "Creator",
                    "description": ["a", "b"],
                    "mediatype": "movies",
                    "downloads": "1234",
                    "date": "1999-01-01",
                    "publicdate": "2005-01-01",
                    "collection": ["feature_films", "public_domain"],
                    "format": ["Archive BitTorrent", "MPEG4"],
                    "licenseurl": "https://creativecommons.org/licenses/by/4.0/",
                    "rights": "CC-BY",
                },
                {"identifier": "", "title": "skip"},
                {"identifier": "id2", "description": "x" * 600},
            ],
        }
    }
    runner = runner_for(stdout=json.dumps(payload))

    results, total, err = ia_api.ia_search_via_curl("q", 10, 2, "downloads desc", runner=runner)

    assert err == ""
    assert total == 3
    assert [r.identifier for r in results] == ["id1", "id2"]
    assert results[0].description == "a b"
    assert results[0].mediatype == "movies"
    assert results[0].downloads == 1234
    assert results[0].date == "1999-01-01"
    assert results[0].publicdate == "2005-01-01"
    assert results[0].collection == "feature_films, public_domain"
    assert results[0].formats == "Archive BitTorrent, MPEG4"
    assert results[0].licenseurl.startswith("https://creativecommons.org/")
    assert results[0].rights == "CC-BY"
    assert results[1].title == "(no title)"
    assert len(results[1].description) == 500
    cmd, timeout = runner.calls[0]
    assert timeout == ia_api.SEARCH_TIMEOUT_S
    assert "--connect-timeout" in cmd
    assert str(ia_api.SEARCH_CURL_CONNECT_TIMEOUT_S) in cmd
    assert "--max-time" in cmd
    assert "sort[]=downloads desc" in cmd
    for field in ("fl[]=mediatype", "fl[]=downloads", "fl[]=licenseurl", "fl[]=rights"):
        assert field in cmd
    assert "fl[]=format" in cmd


def test_search_handles_curl_failure_and_non_json():
    fail_runner = runner_for(returncode=22, stderr="bad request")
    json_runner = runner_for(stdout="not json")

    assert ia_api.ia_search_via_curl("q", 10, 1, runner=fail_runner) == ([], 0, "bad request")
    assert ia_api.ia_search_via_curl("q", 10, 1, runner=json_runner) == ([], 0, "search returned non-JSON")


def test_metadata_parses_clean_and_trailing_json():
    clean = runner_for(stdout='{"metadata": {"title": "A"}}')
    noisy = runner_for(stdout='noise\n{"metadata": {"title": "B"}}\n')

    assert ia_api.ia_metadata_json("id", runner=clean) == ({"metadata": {"title": "A"}}, "")
    assert ia_api.ia_metadata_json("id", runner=noisy) == ({"metadata": {"title": "B"}}, "")
    cmd, timeout = clean.calls[0]
    assert cmd[0] == "curl"
    assert cmd[-1] == "https://archive.org/metadata/id"
    assert timeout == ia_api.METADATA_TIMEOUT_S + 2


def test_metadata_falls_back_to_ia_when_curl_is_missing():
    calls = []

    def runner(cmd, timeout=60):
        calls.append((cmd, timeout))
        if cmd[0] == "curl":
            return 127, "", "command not found"
        return 0, '{"metadata": {"title": "A"}}', ""

    assert ia_api.ia_metadata_json("id with spaces", runner=runner)[1] == ""
    assert calls[0][0] == [
        "curl",
        "-sS",
        "--fail",
        "--connect-timeout",
        str(ia_api.METADATA_CURL_CONNECT_TIMEOUT_S),
        "--max-time",
        str(ia_api.METADATA_TIMEOUT_S),
        "https://archive.org/metadata/id%20with%20spaces",
    ]
    assert calls[0][1] == ia_api.METADATA_TIMEOUT_S + 2
    assert calls[1] == (["ia", "metadata", "id with spaces"], ia_api.METADATA_TIMEOUT_S)


def test_metadata_uses_curl_without_calling_ia():
    calls = []

    def runner(cmd, timeout=60):
        calls.append((cmd, timeout))
        if cmd[0] == "ia":
            raise AssertionError("ia metadata should not be called when curl succeeds")
        return 0, '{"metadata": {"title": "A"}}', ""

    assert ia_api.ia_metadata_json("id", runner=runner) == ({"metadata": {"title": "A"}}, "")
    assert [call[0][0] for call in calls] == ["curl"]


def test_metadata_curl_timeout_does_not_fall_back_to_ia():
    calls = []

    def runner(cmd, timeout=60):
        calls.append((cmd, timeout))
        if cmd[0] == "ia":
            raise AssertionError("ia metadata should not be called after curl timeout")
        return 28, "", "curl: (28) Operation timed out"

    assert ia_api.ia_metadata_json("id", runner=runner) == (None, "curl: (28) Operation timed out")
    assert [call[0][0] for call in calls] == ["curl"]


def test_metadata_reports_ia_exception_after_curl_is_missing():
    calls = []

    def runner(cmd, timeout=60):
        calls.append((cmd, timeout))
        if cmd[0] == "curl":
            return 127, "", "command not found"
        raise RuntimeError("ia client timed out")

    assert ia_api.ia_metadata_json("id", runner=runner) == (None, "ia client timed out")
    assert [call[0][0] for call in calls] == ["curl", "ia"]


def test_metadata_handles_failure_and_non_json():
    fail_runner = runner_for(returncode=1, stderr="nope")
    bad_runner = runner_for(stdout="not json")

    assert ia_api.ia_metadata_json("id", runner=fail_runner) == (None, "nope")
    assert ia_api.ia_metadata_json("id", runner=bad_runner) == (None, "metadata returned non-JSON")


def test_ia_files_normalizes_and_sorts():
    payload = {
        "files": [
            {"name": "small.mp4", "size": "10", "format": "MPEG4"},
            {"name": "badsize.mkv", "size": "bad", "format": None},
            {"name": "", "size": "100"},
            {"name": "big.mp4", "size": 200, "format": "MPEG4"},
        ]
    }
    runner = runner_for(stdout=json.dumps(payload))

    files, meta, err = ia_api.ia_files("id", runner=runner)

    assert err == ""
    assert meta == payload
    assert [(f.name, f.size, f.fmt) for f in files] == [
        ("big.mp4", 200, "MPEG4"),
        ("small.mp4", 10, "MPEG4"),
        ("badsize.mkv", 0, "None"),
    ]
