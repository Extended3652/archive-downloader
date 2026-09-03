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


def test_ia_files_preserves_metadata_and_deduplicates_proven_pair():
    payload = {
        "files": [
            {"name": "foo.ia.mp4", "size": "10", "format": "h.264 IA",
             "source": "derivative", "original": "foo.mp4", "sha1": "derivative"},
            {"name": "foo.mp4", "size": "10", "format": "MPEG4",
             "source": "original", "sha1": "original"},
            {"name": "The Critic Webisodes.mp4", "size": "20", "format": "MPEG4",
             "source": "original"},
        ]
    }
    files, _meta, err = ia_api.ia_files("item", runner=runner_for(stdout=json.dumps(payload)))

    assert err == ""
    assert [f.name for f in files] == ["The Critic Webisodes.mp4", "foo.mp4"]
    assert files[1].source == "original"
    assert files[1].raw_metadata["source"] == "original"


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


def _rerank_payload():
    return {
        "response": {
            "numFound": 3,
            "docs": [
                {"identifier": "meta", "title": "Unrelated Feature",
                 "description": "shot on a toad road", "downloads": 99999},
                {"identifier": "exact", "title": "Toad Road", "downloads": 1},
                {"identifier": "prefix", "title": "Toad Road: The Cut", "downloads": 2},
            ],
        }
    }


def _cmd_has(cmd, needle):
    return any(needle == part for part in cmd)


def test_rerank_fetches_wide_window_and_reorders_by_title():
    runner = runner_for(stdout=json.dumps(_rerank_payload()))

    results, total, err = ia_api.ia_search_via_curl(
        "q", 30, 1, "", runner=runner, rerank_text="Toad Road", media_filter="movies"
    )

    assert err == ""
    assert total == 3
    # Exact title first, prefix second, high-download metadata-only match last.
    assert [r.identifier for r in results] == ["exact", "prefix", "meta"]
    cmd = runner.calls[0][0]
    # A single wide candidate window is fetched from page 1, not the 30-row page.
    assert _cmd_has(cmd, f"rows={ia_api.RANK_POOL_ROWS}")
    assert _cmd_has(cmd, "page=1")


def test_rerank_paginates_within_ranked_pool():
    runner = runner_for(stdout=json.dumps(_rerank_payload()))
    page1, total1, _ = ia_api.ia_search_via_curl(
        "q", 2, 1, "", runner=runner, rerank_text="Toad Road"
    )
    page2, total2, _ = ia_api.ia_search_via_curl(
        "q", 2, 2, "", runner=runner, rerank_text="Toad Road"
    )

    assert total1 == total2 == 3
    assert [r.identifier for r in page1] == ["exact", "prefix"]
    assert [r.identifier for r in page2] == ["meta"]


def test_rerank_disabled_for_explicit_sort():
    runner = runner_for(stdout=json.dumps(_rerank_payload()))

    results, _total, _err = ia_api.ia_search_via_curl(
        "q", 30, 1, "downloads desc", runner=runner, rerank_text="Toad Road"
    )

    # Explicit IA sort is honoured: original doc order, no wide window.
    assert [r.identifier for r in results] == ["meta", "exact", "prefix"]
    cmd = runner.calls[0][0]
    assert _cmd_has(cmd, "rows=30")


def test_rerank_deep_page_falls_back_to_raw_ia_paging():
    runner = runner_for(stdout=json.dumps(_rerank_payload()))

    results, _total, _err = ia_api.ia_search_via_curl(
        "q", 100, 3, "", runner=runner, rerank_text="Toad Road"
    )

    # Page 3 at 100 rows is beyond the candidate window (200 rows -> 2 pages),
    # so we fetch that raw IA page directly and leave its order untouched.
    assert [r.identifier for r in results] == ["meta", "exact", "prefix"]
    cmd = runner.calls[0][0]
    assert _cmd_has(cmd, "rows=100")
    assert _cmd_has(cmd, "page=3")


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
