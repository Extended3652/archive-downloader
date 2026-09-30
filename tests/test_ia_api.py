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


def test_ia_files_reports_dark_items_instead_of_silent_zero_files():
    # archive.org returns HTTP 200 with a stub payload (is_dark, no
    # metadata/files keys) for restricted/taken-down items, not a 404.
    payload = {"created": 123, "is_dark": True, "dir": "/1/items/foo"}
    files, meta, err = ia_api.ia_files("foo", runner=runner_for(stdout=json.dumps(payload)))

    assert files == []
    assert meta == payload
    assert "dark" in err.lower()


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


MB = 1024 * 1024


def _sized_doc(identifier, item_size=None):
    doc = {"identifier": identifier, "title": identifier}
    if item_size is not None:
        doc["item_size"] = item_size
    return doc


def test_min_item_size_excludes_small_and_keeps_unknown_and_large():
    payload = {
        "response": {
            "numFound": 7,
            "docs": [
                _sized_doc("tiny44", 44 * MB),
                _sized_doc("tiny87", 87 * MB),
                _sized_doc("tiny180", 180 * MB),
                _sized_doc("at-threshold", 250 * MB),
                _sized_doc("big600", 600 * MB),
                _sized_doc("big2_4g", int(2.4 * 1024 * MB)),
                _sized_doc("unknown-size"),
            ],
        }
    }
    runner = runner_for(stdout=json.dumps(payload))

    results, total, err = ia_api.ia_search_via_curl(
        "q", 10, 1, runner=runner, min_item_size_bytes=250 * MB
    )

    assert err == ""
    # A single fetch (7 docs, fewer than RANK_POOL_ROWS) confirms genuine
    # exhaustion, so the exact eligible count (4) is known and reported --
    # not IA's raw numFound (7), which would expose a phantom extra page.
    assert total == 4
    assert [r.identifier for r in results] == ["at-threshold", "big600", "big2_4g", "unknown-size"]


def test_min_item_size_zero_disables_filter():
    payload = {
        "response": {
            "numFound": 2,
            "docs": [_sized_doc("tiny44", 44 * MB), _sized_doc("big600", 600 * MB)],
        }
    }
    runner = runner_for(stdout=json.dumps(payload))

    results, _total, _err = ia_api.ia_search_via_curl("q", 10, 1, runner=runner, min_item_size_bytes=0)

    assert [r.identifier for r in results] == ["tiny44", "big600"]


def test_min_item_size_backfills_page_from_wider_pool_instead_of_shrinking_it():
    # Page 1 asks for 3 rows. The first 3 raw docs are all below threshold, but
    # the pool (fetched wider, like the reranker's candidate window) has three
    # more eligible docs further down -- page 1 should come back full (3 items)
    # rather than empty, and page 2 should pick up cleanly after it.
    docs = [_sized_doc(f"small{i}", 10 * MB) for i in range(3)]
    docs += [_sized_doc(f"big{i}", 600 * MB) for i in range(3)]
    payload = {"response": {"numFound": len(docs), "docs": docs}}
    runner = runner_for(stdout=json.dumps(payload))

    page1, total1, err1 = ia_api.ia_search_via_curl("q", 3, 1, runner=runner, min_item_size_bytes=250 * MB)
    page2, total2, err2 = ia_api.ia_search_via_curl("q", 3, 2, runner=runner, min_item_size_bytes=250 * MB)

    assert err1 == err2 == ""
    # Exhaustion confirmed in one fetch (6 docs, fewer than RANK_POOL_ROWS):
    # the exact eligible count (3) is known and reported for both pages.
    assert total1 == total2 == 3
    assert [r.identifier for r in page1] == ["big0", "big1", "big2"]
    assert page2 == []
    cmd = runner.calls[0][0]
    # Backfill fetches the wide candidate window from page 1, same as reranking.
    assert _cmd_has(cmd, f"rows={ia_api.RANK_POOL_ROWS}")
    assert _cmd_has(cmd, "page=1")


def test_min_item_size_deep_page_still_backfills_not_bounded_by_rerank_pool():
    # Unlike reranking (which only pools the leading pool_pages), the size
    # filter has to hold at any depth -- a small-item-heavy run can appear
    # anywhere, not just early on. A deep page request with only one raw doc
    # total (genuinely exhausted, and it's below threshold) must still come
    # back empty rather than erroring or hanging, and the fetch must be a
    # pool-style walk (RANK_POOL_ROWS from page 1), not a bare rows=5/page=41
    # request the way the old rerank-only pool bound produced.
    docs = [_sized_doc("tiny", 10 * MB)]
    payload = {"response": {"numFound": 1, "docs": docs}}
    runner = runner_for(stdout=json.dumps(payload))

    results, _total, _err = ia_api.ia_search_via_curl("q", 5, 41, runner=runner, min_item_size_bytes=250 * MB)

    assert results == []
    cmd = runner.calls[0][0]
    assert _cmd_has(cmd, f"rows={ia_api.RANK_POOL_ROWS}")
    assert _cmd_has(cmd, "page=1")
    # Only one request: the single raw doc is fewer than RANK_POOL_ROWS, so
    # exhaustion is detected immediately without spending the full batch cap.
    assert len(runner.calls) == 1


def test_min_item_size_page6_backfills_past_first_pool_batch():
    # The exact scenario this fix targets: more than RANK_POOL_ROWS=200 raw
    # results, with few enough eligible docs in the first 200 that filtered
    # page 6 (rows=30 -> positions 150-179) can't be filled from that first
    # batch alone (100 eligible < 180 needed), but a second batch (raw
    # positions 200-399, another 100 eligible) pushes the cumulative eligible
    # count past 180. Page 6 must come back full by walking into that second
    # batch, not return a near-empty page just because the first 200-row pool
    # ran dry. A final third, shorter batch (20 more, all eligible) lets a
    # later page hit genuine exhaustion (220 eligible total).
    docs = []
    for batch_start in (0, 200):
        for i in range(200):
            # Every other doc is eligible: 100 per 200-row batch.
            if i % 2 == 0:
                docs.append(_sized_doc(f"big{batch_start + i}", 600 * MB))
            else:
                docs.append(_sized_doc(f"small{batch_start + i}", 10 * MB))
    docs += [_sized_doc(f"big-tail{i}", 600 * MB) for i in range(20)]  # raw 400-419, all eligible
    assert len(docs) == 420

    def paged_runner(cmd, timeout=60):
        # Extract page/rows from the --data-urlencode args and slice the
        # fixture like a real paginated IA response would.
        rows_arg = next(p for p in cmd if p.startswith("rows="))
        page_arg = next(p for p in cmd if p.startswith("page="))
        rows = int(rows_arg.split("=", 1)[1])
        page_num = int(page_arg.split("=", 1)[1])
        start = (page_num - 1) * rows
        page_docs = docs[start : start + rows]
        payload = {"response": {"numFound": len(docs), "docs": page_docs}}
        return 0, json.dumps(payload), ""

    calls = []
    def counting_runner(cmd, timeout=60):
        calls.append(cmd)
        return paged_runner(cmd, timeout)
    counting_runner.calls = calls

    eligible_total = sum(1 for d in docs if d.get("item_size", 0) >= 250 * MB)
    assert eligible_total == 220  # 100 + 100 + 20

    page6, total6, err6 = ia_api.ia_search_via_curl("q", 30, 6, runner=counting_runner, min_item_size_bytes=250 * MB)

    assert err6 == ""
    assert len(page6) == 30, f"page 6 should be a full backfilled page, got {len(page6)}"
    assert all(r.item_size >= 250 * MB for r in page6)
    # 100 eligible in the first batch alone can't cover page 6 (needs 180);
    # walking into a second raw batch (page=2, positions 200-399) was required.
    assert len(calls) >= 2
    assert _cmd_has(calls[1], "page=2")
    # Page 6 stopped once it had enough for the page (both raw batches were
    # full-length, RANK_POOL_ROWS docs each) -- exhaustion was never
    # confirmed, so the exact total beyond what was walked is unknown and the
    # raw total (420) is reported, not a guess.
    assert total6 == len(docs)

    # Paging onward must not skip or duplicate eligible items, and must
    # eventually hit the genuine end (220 eligible items -> 7 full pages of
    # 30, 10 left over on a short-but-real final page).
    seen_ids = set()
    for page_num in range(1, 8):
        page_results, _t, _e = ia_api.ia_search_via_curl(
            "q", 30, page_num, runner=counting_runner, min_item_size_bytes=250 * MB
        )
        ids = [r.identifier for r in page_results]
        assert not (seen_ids & set(ids)), f"page {page_num} overlapped an earlier page"
        assert len(page_results) == 30, f"page {page_num} should be a full page, got {len(page_results)}"
        seen_ids.update(ids)
    assert len(seen_ids) == eligible_total - 10

    # A genuinely final short page (page 8: only 10 of 220 eligible remain)
    # is allowed to be short -- exhaustion is real here, not a pool artifact.
    # Reaching it required a third, short batch (20 docs, raw 400-419), which
    # positively confirms exhaustion -- so the exact eligible total (220) is
    # now known and reported instead of the raw total (420).
    page8, total8, _e = ia_api.ia_search_via_curl("q", 30, 8, runner=counting_runner, min_item_size_bytes=250 * MB)
    assert len(page8) == 10
    assert total8 == eligible_total == 220

    # No phantom later pages: page 9 is entirely past the exact, now-known
    # end (220 eligible / 30 rows = 8 pages), and reports the same exact
    # total rather than the total bouncing back up to the raw count.
    page9, total9, _e = ia_api.ia_search_via_curl("q", 30, 9, runner=counting_runner, min_item_size_bytes=250 * MB)
    assert page9 == []
    assert total9 == 220


def test_min_item_size_capped_before_exhaustion_uses_conservative_total_not_partial():
    # State B: MAX_POOL_BATCHES is reached without IA ever signalling true
    # exhaustion (every batch is a full RANK_POOL_ROWS docs, never short).
    # A deep page with a very low eligibility rate can't be filled within the
    # batch cap. The exact filtered total is genuinely unknown here -- it
    # must not collapse to the small partial eligible count accumulated so
    # far, and repeat calls must not bounce.
    num_found = 100000

    def infinite_low_density_runner(cmd, timeout=60):
        page_arg = next(p for p in cmd if p.startswith("page="))
        page_num = int(page_arg.split("=", 1)[1])
        # 1 eligible doc per 200-row batch (0.5% density): every batch is a
        # full RANK_POOL_ROWS docs, so raw exhaustion is never confirmed.
        docs = [
            _sized_doc(f"p{page_num}-{i}", 600 * MB if i == 0 else 10 * MB)
            for i in range(ia_api.RANK_POOL_ROWS)
        ]
        payload = {"response": {"numFound": num_found, "docs": docs}}
        return 0, json.dumps(payload), ""

    calls = []
    def counting_runner(cmd, timeout=60):
        calls.append(cmd)
        return infinite_low_density_runner(cmd, timeout)

    # Deep enough that MAX_POOL_BATCHES * 1 eligible/batch can't reach it.
    results, total, err = ia_api.ia_search_via_curl(
        "q", 30, 50, runner=counting_runner, min_item_size_bytes=250 * MB
    )

    assert err == ""
    assert len(results) < 30  # short page, but exhaustion was never confirmed
    assert len(calls) == ia_api.MAX_POOL_BATCHES  # cap reached, not exceeded
    assert total != 5, "must not collapse to the partial eligible count accumulated before the cap"
    assert total == num_found, "capped-before-exhaustion must report the conservative raw total"

    # Repeating the same call (as paging would) must not bounce the total.
    _results2, total2, _err2 = ia_api.ia_search_via_curl(
        "q", 30, 50, runner=counting_runner, min_item_size_bytes=250 * MB
    )
    assert total2 == total


def test_min_item_size_filters_before_rerank_and_keeps_ranked_order():
    payload = _rerank_payload()
    # "exact" is the best title match but is below the size threshold; it
    # should be dropped before ranking, not merely sorted last.
    payload["response"]["docs"][1]["item_size"] = 10 * MB
    payload["response"]["docs"][2]["item_size"] = 600 * MB
    runner = runner_for(stdout=json.dumps(payload))

    results, _total, err = ia_api.ia_search_via_curl(
        "q", 30, 1, "", runner=runner, rerank_text="Toad Road", media_filter="movies", min_item_size_bytes=250 * MB
    )

    assert err == ""
    assert [r.identifier for r in results] == ["prefix", "meta"]


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
