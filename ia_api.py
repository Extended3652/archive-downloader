"""Internet Archive command/search/metadata access helpers."""
import json
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import ia_ranking
from ia_common import IAFile, SearchResult, deduplicate_file_variants

Logger = Callable[[str], None]
SEARCH_TIMEOUT_S = 20
SEARCH_CURL_CONNECT_TIMEOUT_S = 8
METADATA_TIMEOUT_S = 8
METADATA_CURL_CONNECT_TIMEOUT_S = 4

# When local relevance re-ranking is requested for a plain-text query, fetch a
# larger candidate window from IA in a single request and rank it locally. IA's
# default relevance ordering can bury an exact-title match dozens of results
# deep (e.g. the "Toad Road" movie ranks ~70th for query "Toad Road"), so
# ranking only the rows already on the current page would not surface it. The
# window is bounded to keep the request cheap; ranked results fill the leading
# pages and deeper pages fall back to IA's raw ordering.
RANK_POOL_ROWS = 200

# A min-item-size filter can thin out any one RANK_POOL_ROWS-sized batch of
# raw candidates (small items removed). One batch not containing enough
# eligible items isn't evidence the result list is exhausted -- it may just
# be a locally dense run of small items -- so up to this many batches are
# fetched, walking further into IA's raw ordering, before giving up and
# returning whatever was found. Bounds worst-case requests per call.
MAX_POOL_BATCHES = 5


def run_cmd(cmd: List[str], timeout: int = 60, logger: Optional[Logger] = None) -> Tuple[int, str, str]:
    try:
        if logger:
            logger(f"CMD: {' '.join(cmd)}")
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if logger:
            logger(f"RC: {p.returncode}")
            if p.stderr:
                logger(f"STDERR: {p.stderr.strip()[:2000]}")
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        if logger:
            logger("RC: 127 (command not found)")
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        if logger:
            logger(f"RC: 124 (timeout {timeout}s)")
        return 124, "", "command timed out"


def ia_ok(runner: Callable[..., Tuple[int, str, str]] = run_cmd) -> Tuple[bool, str]:
    code, out, err = runner(["ia", "--version"], timeout=10)
    if code == 0:
        return True, out.strip()
    msg = (err or out).strip()
    return False, msg or "ia not available"


def curl_version(runner: Callable[..., Tuple[int, str, str]] = run_cmd) -> Tuple[bool, str]:
    code, out, err = runner(["curl", "--version"], timeout=10)
    msg = (out.splitlines()[0] if out else (err or "")).strip()
    return code == 0, msg or "not available"


def _passes_min_item_size(result: SearchResult, min_item_size_bytes: int) -> bool:
    """A known size below the threshold is excluded; an unknown size (IA
    omits item_size for some items, e.g. collections) is kept rather than
    penalizing a result we can't judge."""
    if min_item_size_bytes <= 0:
        return True
    size = int(getattr(result, "item_size", 0) or 0)
    return size <= 0 or size >= min_item_size_bytes


def _fetch_ia_page(
    query: str,
    fetch_rows: int,
    fetch_page: int,
    sort: str,
    runner: Callable[..., Tuple[int, str, str]],
) -> Tuple[List[SearchResult], int, int, str]:
    """Fetch one raw page from IA's advancedsearch endpoint.

    Returns (results, raw_doc_count, num_found, err). raw_doc_count is how
    many docs IA actually returned (before the missing-identifier skip below),
    which is what tells a caller walking multiple pages whether this was a
    full page or the true end of the raw result list.
    """
    cmd = [
        "curl",
        "-sS",
        "-G",
        "--connect-timeout",
        str(SEARCH_CURL_CONNECT_TIMEOUT_S),
        "--max-time",
        str(SEARCH_TIMEOUT_S),
        "https://archive.org/advancedsearch.php",
        "--data-urlencode",
        f"q={query}",
        "--data-urlencode",
        "fl[]=identifier",
        "--data-urlencode",
        "fl[]=title",
        "--data-urlencode",
        "fl[]=year",
        "--data-urlencode",
        "fl[]=creator",
        "--data-urlencode",
        "fl[]=description",
        "--data-urlencode",
        "fl[]=mediatype",
        "--data-urlencode",
        "fl[]=downloads",
        "--data-urlencode",
        "fl[]=item_size",
        "--data-urlencode",
        "fl[]=date",
        "--data-urlencode",
        "fl[]=publicdate",
        "--data-urlencode",
        "fl[]=collection",
        "--data-urlencode",
        "fl[]=format",
        "--data-urlencode",
        "fl[]=licenseurl",
        "--data-urlencode",
        "fl[]=rights",
        "--data-urlencode",
        "output=json",
        "--data-urlencode",
        f"rows={fetch_rows}",
        "--data-urlencode",
        f"page={fetch_page}",
    ]
    if sort:
        cmd += ["--data-urlencode", f"sort[]={sort}"]

    code, out, err = runner(cmd, timeout=SEARCH_TIMEOUT_S)
    if code != 0:
        msg = (err or out).strip()
        return [], 0, 0, msg or f"search failed (code {code})"

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], 0, 0, "search returned non-JSON"

    response = (data or {}).get("response") or {}
    num_found = int(response.get("numFound") or 0)
    docs = response.get("docs") or []
    results: List[SearchResult] = []
    for d in docs:
        ident = str(d.get("identifier", "")).strip()
        if not ident:
            continue
        title = str(d.get("title", "")).strip() or "(no title)"
        year = str(d.get("year", "")).strip()
        creator = str(d.get("creator", "")).strip()
        desc_raw = d.get("description", "")
        if isinstance(desc_raw, list):
            desc_raw = " ".join(str(x) for x in desc_raw)
        desc = str(desc_raw or "").strip()[:500]
        downloads_raw = d.get("downloads", 0)
        try:
            downloads = int(downloads_raw or 0)
        except (TypeError, ValueError):
            downloads = 0
        try:
            item_size = int(d.get("item_size") or 0)
        except (TypeError, ValueError):
            item_size = 0
        collection_raw = d.get("collection", "")
        if isinstance(collection_raw, list):
            collection_raw = ", ".join(str(x) for x in collection_raw[:3])
        formats_raw = d.get("format", [])
        if isinstance(formats_raw, list):
            formats = ", ".join(str(x) for x in formats_raw[:8] if str(x).strip())
        else:
            formats = str(formats_raw or "").strip()
        results.append(
            SearchResult(
                ident,
                title,
                year,
                creator,
                desc,
                mediatype=str(d.get("mediatype", "") or "").strip(),
                formats=formats,
                downloads=downloads,
                item_size=item_size,
                date=str(d.get("date", "") or "").strip(),
                publicdate=str(d.get("publicdate", "") or "").strip(),
                collection=str(collection_raw or "").strip(),
                licenseurl=str(d.get("licenseurl", "") or "").strip(),
                rights=str(d.get("rights", "") or "").strip(),
            )
        )

    return results, len(docs), num_found, ""


def ia_search_via_curl(
    query: str,
    rows: int,
    page: int,
    sort: str = "",
    runner: Callable[..., Tuple[int, str, str]] = run_cmd,
    *,
    rerank_text: str = "",
    media_filter: str = "any",
    min_item_size_bytes: int = 0,
) -> Tuple[List[SearchResult], int, str]:
    # Local re-ranking only applies to relevance ordering (empty sort); when the
    # user chose an explicit IA sort we honour it untouched. It also only kicks
    # in for the leading pages that the candidate window can cover.
    rows = max(1, int(rows or 1))
    min_item_size_bytes = max(0, int(min_item_size_bytes or 0))
    size_filter_active = min_item_size_bytes > 0
    pool_pages = max(1, RANK_POOL_ROWS // rows)
    do_rerank = bool((rerank_text or "").strip()) and not sort and page <= pool_pages
    # Reranking only pools the leading pages (bounded window, see above); a
    # size filter has to hold for every page, since a small-item-heavy run
    # can appear at any depth, not just early on -- so it isn't bounded by
    # pool_pages the way reranking is.
    use_pool = do_rerank or size_filter_active

    if not use_pool:
        results, _raw_count, num_found, err = _fetch_ia_page(query, rows, page, sort, runner)
        return results, num_found, err

    # Pool path: walk raw IA pages from page 1, RANK_POOL_ROWS at a time,
    # keeping eligible (post-size-filter) candidates, until there are enough
    # to cover the requested page, IA's raw list is genuinely exhausted (a
    # batch comes back shorter than requested), or MAX_POOL_BATCHES is hit.
    # A batch being thin on eligible items is not itself evidence of
    # exhaustion -- it may just be a locally dense run of filtered-out items
    # -- so we keep walking forward instead of stopping there.
    target_count = page * rows
    eligible: List[SearchResult] = []
    num_found = 0
    raw_page = 1
    exhausted_confirmed = False
    for _ in range(MAX_POOL_BATCHES):
        batch_results, raw_count, batch_num_found, err = _fetch_ia_page(query, RANK_POOL_ROWS, raw_page, sort, runner)
        if err:
            return [], 0, err
        num_found = batch_num_found
        if size_filter_active:
            eligible.extend(r for r in batch_results if _passes_min_item_size(r, min_item_size_bytes))
        else:
            eligible.extend(batch_results)
        # A batch shorter than requested is IA saying its raw list ends here,
        # not just this batch being thin on eligible items -- that's the one
        # unambiguous exhaustion signal (see MAX_POOL_BATCHES vs. genuine
        # end-of-list note below).
        exhausted_confirmed = raw_count < RANK_POOL_ROWS
        if len(eligible) >= target_count or exhausted_confirmed:
            break
        raw_page += 1

    if do_rerank:
        eligible = ia_ranking.rerank(eligible, rerank_text, media_filter)

    start = (page - 1) * rows
    results = eligible[start : start + rows]

    # If the walk positively confirmed it reached the true end of IA's raw
    # result list, `eligible` is the complete filtered set and its length is
    # the exact total -- report that instead of IA's raw numFound, so paging
    # built from it doesn't expose phantom pages past the last real eligible
    # result. If we only stopped because the requested page was already full,
    # or because MAX_POOL_BATCHES was hit, there may be more eligible items
    # further into the raw list that were never looked at, so the exact total
    # is genuinely unknown -- keep reporting the conservative raw total in
    # that case (never the partial eligible count; that would just trade one
    # wrong number for a differently wrong, and unstable, one).
    if size_filter_active and exhausted_confirmed:
        num_found = len(eligible)

    return results, num_found, ""


def _parse_metadata_json(out: str) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        return json.loads(out), ""
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\})\s*$", out.strip(), re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1)), ""
            except Exception:
                pass
        return None, "metadata returned non-JSON"


def ia_metadata_json(
    identifier: str,
    runner: Callable[..., Tuple[int, str, str]] = run_cmd,
) -> Tuple[Optional[Dict[str, Any]], str]:
    ident = str(identifier or "").strip()
    if not ident:
        return None, "metadata identifier is blank"

    curl_err = ""
    try:
        code, out, err = runner(
            [
                "curl",
                "-sS",
                "--fail",
                "--connect-timeout",
                str(METADATA_CURL_CONNECT_TIMEOUT_S),
                "--max-time",
                str(METADATA_TIMEOUT_S),
                f"https://archive.org/metadata/{quote(ident, safe='')}",
            ],
            timeout=METADATA_TIMEOUT_S + 2,
        )
    except Exception as exc:
        code, out, err = 127, "", str(exc)
        curl_err = err.strip()

    if code == 0:
        return _parse_metadata_json(out)

    msg = (err or out).strip()
    curl_missing = code == 127 or "command not found" in msg.lower()
    if not curl_missing:
        return None, msg or f"metadata failed (code {code})"

    try:
        code, out, err = runner(["ia", "metadata", ident], timeout=METADATA_TIMEOUT_S)
    except Exception as exc:
        msg = str(exc).strip()
        return None, msg or curl_err or "metadata failed"
    if code == 0:
        return _parse_metadata_json(out)
    if code != 0:
        msg = (err or out).strip()
        return None, msg or curl_err or f"metadata failed (code {code})"
    return None, curl_err or "metadata failed"


def ia_files(
    identifier: str,
    runner: Callable[..., Tuple[int, str, str]] = run_cmd,
) -> Tuple[List[IAFile], Optional[Dict[str, Any]], str]:
    meta, err = ia_metadata_json(identifier, runner=runner)
    if err or not meta:
        return [], None, err or "metadata error"

    # archive.org returns HTTP 200 with a stub payload (no "metadata"/"files"
    # keys) for items that exist but are dark (taken down/restricted), rather
    # than a 404. Left unchecked this looks like a normal item with zero
    # files instead of the takedown/restriction it actually is.
    if meta.get("is_dark"):
        return [], meta, f"Item '{identifier}' is dark (restricted or taken down) on archive.org"

    files: List[IAFile] = []
    for f in meta.get("files", []) or []:
        name = str(f.get("name", "")).strip()
        if not name:
            continue
        size_raw = f.get("size", 0)
        try:
            size = int(size_raw) if size_raw is not None else 0
        except Exception:
            size = 0
        fmt = str(f.get("format", "")).strip()
        files.append(
            IAFile(
                name=name,
                size=size,
                fmt=fmt,
                source=str(f.get("source", "") or "").strip(),
                original=str(f.get("original", "") or "").strip(),
                md5=str(f.get("md5", "") or "").strip(),
                sha1=str(f.get("sha1", "") or "").strip(),
                crc32=str(f.get("crc32", "") or "").strip(),
                raw_metadata=dict(f),
            )
        )

    files = deduplicate_file_variants(files)
    files.sort(key=lambda x: x.size or 0, reverse=True)
    return files, meta, ""
