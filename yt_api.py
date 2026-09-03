"""YouTube search helpers backed by yt-dlp JSON output."""
import json
import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

import ia_config
from ia_common import SearchResult

Logger = Callable[[str], None]
SEARCH_TIMEOUT_S = 60


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


def yt_dlp_version(
    yt_dlp_path: Optional[str] = None,
    runner: Callable[..., Tuple[int, str, str]] = run_cmd,
) -> Tuple[bool, str]:
    path = yt_dlp_path or ia_config.load_config()["yt_dlp_path"]
    code, out, err = runner([path, "--version"], timeout=10)
    msg = (out.splitlines()[0] if out else (err or "")).strip()
    return code == 0, msg or "yt-dlp not available"


def _string_field(data: Dict[str, Any], *names: str) -> str:
    for name in names:
        value = data.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _int_field(data: Dict[str, Any], *names: str) -> int:
    for name in names:
        try:
            return int(data.get(name) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return cleaned[:120] or "video"


def parse_yt_search_json(raw: str) -> List[SearchResult]:
    """Parse yt-dlp -J --flat-playlist ytsearch JSON into SearchResult rows."""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return []

    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        entries = [data] if isinstance(data, dict) else []

    results: List[SearchResult] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = _string_field(entry, "id", "display_id")
        url = _string_field(entry, "webpage_url", "url", "original_url")
        if url and url.startswith("http"):
            webpage_url = url
        elif video_id:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        else:
            webpage_url = url

        raw_identifier = video_id or webpage_url
        if not raw_identifier:
            continue
        identifier = _safe_identifier(raw_identifier)

        title = _string_field(entry, "title", "fulltitle") or "(no title)"
        uploader = _string_field(entry, "uploader", "channel", "channel_id")
        upload_date = _string_field(entry, "upload_date", "release_date", "timestamp")
        duration = _int_field(entry, "duration")
        results.append(
            SearchResult(
                identifier=f"yt-{identifier}",
                title=title,
                creator=uploader,
                mediatype="video",
                date=upload_date,
                source="youtube",
                webpage_url=webpage_url,
                video_id=video_id,
                uploader=uploader,
                duration=duration,
                upload_date=upload_date,
            )
        )
    return results


def _raw_entry_count(raw: str) -> int:
    """Count entries in yt-dlp's JSON before SearchResult filtering, for diagnostics.

    Returns -1 if the output wasn't valid JSON at all (a real parsing bug),
    vs. 0+ if yt-dlp's JSON simply contained no (or fewer) entries.
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return -1
    entries = data.get("entries") if isinstance(data, dict) else None
    if isinstance(entries, list):
        return len(entries)
    return 1 if isinstance(data, dict) else 0


def yt_search(
    query: str,
    rows: int = 10,
    *,
    yt_dlp_path: Optional[str] = None,
    runner: Callable[..., Tuple[int, str, str]] = run_cmd,
    logger: Optional[Logger] = None,
) -> Tuple[List[SearchResult], int, str]:
    terms = str(query or "").strip()
    if not terms:
        return [], 0, "YouTube search text is blank"
    count = max(1, int(rows or 10))
    path = yt_dlp_path or ia_config.load_config()["yt_dlp_path"]
    code, out, err = runner([path, "-J", "--flat-playlist", f"ytsearch{count}:{terms}"], timeout=SEARCH_TIMEOUT_S)
    if code != 0:
        msg = (err or out).strip()
        return [], 0, msg or f"yt-dlp search failed (code {code})"
    results = parse_yt_search_json(out)
    if not results and logger:
        logger(
            f"YT_SEARCH_EMPTY: query={terms!r} stdout_bytes={len(out)} "
            f"raw_entries={_raw_entry_count(out)}"
        )
    return results, len(results), ""


def yt_metadata_url(
    url: str,
    *,
    yt_dlp_path: Optional[str] = None,
    runner: Callable[..., Tuple[int, str, str]] = run_cmd,
) -> Tuple[Optional[SearchResult], str]:
    target = str(url or "").strip()
    if not target:
        return None, "YouTube URL is blank"
    path = yt_dlp_path or ia_config.load_config()["yt_dlp_path"]
    code, out, err = runner([path, "-J", "--no-playlist", target], timeout=SEARCH_TIMEOUT_S)
    if code != 0:
        msg = (err or out).strip()
        return None, msg or f"yt-dlp metadata failed (code {code})"
    results = parse_yt_search_json(out)
    if not results:
        return None, "yt-dlp metadata returned no video"
    row = results[0]
    if not row.webpage_url:
        row.webpage_url = target
    return row, ""
