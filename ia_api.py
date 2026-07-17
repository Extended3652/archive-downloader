"""Internet Archive command/search/metadata access helpers."""
import json
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from ia_common import IAFile, SearchResult

Logger = Callable[[str], None]
SEARCH_TIMEOUT_S = 20
SEARCH_CURL_CONNECT_TIMEOUT_S = 8
METADATA_TIMEOUT_S = 8
METADATA_CURL_CONNECT_TIMEOUT_S = 4


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


def ia_search_via_curl(
    query: str,
    rows: int,
    page: int,
    sort: str = "",
    runner: Callable[..., Tuple[int, str, str]] = run_cmd,
) -> Tuple[List[SearchResult], int, str]:
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
        f"rows={rows}",
        "--data-urlencode",
        f"page={page}",
    ]
    if sort:
        cmd += ["--data-urlencode", f"sort[]={sort}"]

    code, out, err = runner(cmd, timeout=SEARCH_TIMEOUT_S)
    if code != 0:
        msg = (err or out).strip()
        return [], 0, msg or f"search failed (code {code})"

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], 0, "search returned non-JSON"

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
                date=str(d.get("date", "") or "").strip(),
                publicdate=str(d.get("publicdate", "") or "").strip(),
                collection=str(collection_raw or "").strip(),
                licenseurl=str(d.get("licenseurl", "") or "").strip(),
                rights=str(d.get("rights", "") or "").strip(),
            )
        )

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
        files.append(IAFile(name=name, size=size, fmt=fmt))

    files.sort(key=lambda x: x.size or 0, reverse=True)
    return files, meta, ""
