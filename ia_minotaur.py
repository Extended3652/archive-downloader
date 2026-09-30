#!/usr/bin/env python3
import argparse
import curses
from copy import deepcopy
import os
import re
import shlex
import shutil
import threading
import sys
import textwrap
import time
from dataclasses import dataclass, field, replace
from typing import List, Tuple, Optional, Dict, Any, Set

from ia_common import (
    IAFile,
    SearchResult,
    VIDEO_EXTS,
    VIDEO_FORMAT_HINTS,
    compact_count,
    deduplicate_file_variants,
    human_size,
    is_archive_torrent_format,
    is_dvd_iso_file,
    is_video_file,
    safe_path_under,
)
from ia_paths import (
    BUCKET_MOVIES,
    BUCKET_MUSIC,
    BUCKET_OTHER,
    BUCKET_TV,
    FAVS_PATH,
    LOG_PATH,
    MEDIA_ROOT,
    PENDING_PATH,
    SESSION_PATH,
    STAGING_ROOT,
    check_writable_dir,
    normalize_media_permissions,
    safe_staging_file_path,
    set_process_umask,
    staging_file_path,
)
import ia_api
import ia_audit
import ia_config
import ia_downloads
import ia_dvd
import ia_bazarr
import ia_jellyfin
import ia_minotaur_events
import ia_radarr
import ia_ranking
import yt_api
import yt_downloads
from ia_organize import (
    archive_query_preset_labels,
    auto_clean_movie_folder_name,
    build_archive_preset_query,
    build_collection_search_query,
    build_field_query,
    build_query_attempts,
    build_sideways_searches,
    build_title_year_query,
    build_within_collection_query,
    build_query,
    detect_sxxeyy,
    infer_bucket,
    is_openly_licensed,
    license_status_from_fields,
    looks_like_advanced_query,
    normalize_collection_identifier,
    replace_mediatype_filter,
    sanitize_folder,
    split_title_year,
)
import ia_state

FILTERS = ["movies", "audio", "texts", "software", "any"]
SORT_OPTIONS = [
    ("relevance", ""),
    ("date (new)", "date desc"),
    ("date (old)", "date asc"),
    ("title A-Z", "titleSorter asc"),
    ("downloads", "downloads desc"),
]
APP_CONFIG = ia_config.load_config()
ROWS_PER_PAGE = int(APP_CONFIG["rows_per_page"])
MAX_HISTORY = 20

MIN_H = 14
MIN_W = 50

# Keep downloaded file mtimes as "now" so normal tools like find -mmin work as expected.
# This also reduces confusion when verifying "new downloads" by timestamp.
IA_NO_CHANGE_TIMESTAMP = bool(APP_CONFIG["no_change_timestamp"])

LARGE_VIDEO_BYTES = 500 * 1024 * 1024

# Kill the download subprocess if no bytes arrive for this long.
STALL_TIMEOUT_S = 120
STALL_AUTO_RETRIES = 2
STALL_RETRY_DELAY_S = 8
BULK_CONFIRM_FILE_THRESHOLD = 10
BULK_CONFIRM_BYTES_THRESHOLD = 5 * 1024 * 1024 * 1024
MAX_STATUS_ERROR_CHARS = 180
MAX_DETAIL_ERROR_CHARS = 600
MOUSE_WHEEL_LINES = 4


def is_enter_key(ch: int) -> bool:
    return ch in (10, 13, curses.KEY_ENTER)


def is_backspace_key(ch: int) -> bool:
    return ch in (curses.KEY_BACKSPACE, 127, 8)


def mouse_wheel_direction(button_state: int) -> int:
    """Return -1 for wheel up, 1 for wheel down, or 0 for non-wheel events."""
    up_masks = (
        getattr(curses, "BUTTON4_PRESSED", 0),
        getattr(curses, "BUTTON4_CLICKED", 0),
        getattr(curses, "BUTTON4_RELEASED", 0),
    )
    down_masks = (
        getattr(curses, "BUTTON5_PRESSED", 0),
        getattr(curses, "BUTTON5_CLICKED", 0),
        getattr(curses, "BUTTON5_RELEASED", 0),
    )
    if any(mask and button_state & mask for mask in up_masks):
        return -1
    if any(mask and button_state & mask for mask in down_masks):
        return 1
    return 0


def scroll_index(index: int, direction: int, total: int, *, lines: int = MOUSE_WHEEL_LINES) -> int:
    if total <= 0 or direction == 0:
        return max(0, index)
    step = max(1, int(lines))
    return max(0, min(total - 1, index + (direction * step)))


def normalize_save_bucket(value: str, default: str = "Other") -> str:
    bucket = str(value or "").strip().lower()
    if bucket == "tv":
        return "TV"
    if bucket == "movies":
        return "Movies"
    if bucket == "music":
        return "Music"
    if bucket == "other":
        return "Other"
    return default if default in ("TV", "Movies", "Music", "Other") else "Other"


def compact_error_text(text: str, *, max_chars: int = MAX_DETAIL_ERROR_CHARS) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "Unknown error"

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    traceback_marker = "Traceback (most recent call last):"
    if traceback_marker in raw:
        prefix = raw.split(traceback_marker, 1)[0].strip()
        for line in reversed(lines):
            if (
                traceback_marker in line
                or line.startswith("File ")
                or line.startswith("^")
                or line.startswith("During handling ")
                or line.startswith("The above exception ")
            ):
                continue
            raw = f"{prefix} {line}".strip() if prefix else line
            break

    raw = re.sub(r"\s+", " ", raw).strip()
    if len(raw) > max_chars:
        return raw[: max(0, max_chars - 3)].rstrip() + "..."
    return raw


def shaded_progress_bar(written: int, total: int, width: int) -> str:
    """Return a fixed-width shaded progress bar for terminal rendering."""
    if width <= 0:
        return ""
    if total <= 0:
        return "▒" * width

    ratio = max(0.0, min(1.0, float(written) / float(total)))
    filled = int(ratio * width)
    if filled >= width:
        return "█" * width

    partial_ratio = (ratio * width) - filled
    if partial_ratio >= 0.66:
        partial = "▓"
    elif partial_ratio >= 0.33:
        partial = "▒"
    elif partial_ratio > 0:
        partial = "░"
    else:
        partial = ""

    empty = max(0, width - filled - len(partial))
    return ("█" * filled) + partial + ("░" * empty)


def display_size(n: Any, *, unknown: str = "unknown") -> str:
    try:
        value = int(n)
    except (TypeError, ValueError):
        return unknown
    if value <= 0:
        return unknown
    return human_size(value)


def log_line(msg: str) -> None:
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def run_cmd(cmd: List[str], timeout: int = 60) -> Tuple[int, str, str]:
    return ia_api.run_cmd(cmd, timeout=timeout, logger=log_line)


def ensure_dirs() -> None:
    os.makedirs(STAGING_ROOT, exist_ok=True)
    os.makedirs(BUCKET_TV, exist_ok=True)
    os.makedirs(BUCKET_MOVIES, exist_ok=True)
    os.makedirs(BUCKET_MUSIC, exist_ok=True)
    os.makedirs(BUCKET_OTHER, exist_ok=True)
    for path in (STAGING_ROOT, BUCKET_TV, BUCKET_MOVIES, BUCKET_MUSIC, BUCKET_OTHER):
        normalize_media_permissions(path)


def environment_checks() -> List[Tuple[str, bool, str]]:
    checks: List[Tuple[str, bool, str]] = []

    ok, msg = ia_ok()
    checks.append(("ia CLI", ok, msg or "available"))

    curl_ok, curl_msg = ia_api.curl_version(runner=run_cmd)
    checks.append(("curl", curl_ok, curl_msg))

    yt_ok, yt_msg = yt_api.yt_dlp_version(APP_CONFIG["yt_dlp_path"], runner=run_cmd)
    checks.append(("yt-dlp", yt_ok, yt_msg))

    for label, path in (
        ("media root", MEDIA_ROOT),
        ("staging dir", STAGING_ROOT),
        ("TV bucket", BUCKET_TV),
        ("Movies bucket", BUCKET_MOVIES),
        ("Music bucket", BUCKET_MUSIC),
        ("Other bucket", BUCKET_OTHER),
    ):
        path_ok, path_msg = check_writable_dir(path)
        checks.append((label, path_ok, path_msg))

    for label, path in (
        ("session dir", os.path.dirname(SESSION_PATH) or "."),
        ("pending dir", os.path.dirname(PENDING_PATH) or "."),
        ("log dir", os.path.dirname(LOG_PATH) or "."),
    ):
        path_ok, path_msg = check_writable_dir(path)
        checks.append((label, path_ok, path_msg))

    for binary in ("lsdvd", "HandBrakeCLI"):
        found = shutil.which(binary)
        checks.append((binary, True, found or f"optional for DVD ISO scanning; {binary} not found on PATH"))

    return checks


def print_environment_check() -> int:
    checks = environment_checks()
    print("Internet Archive Minotaur setup check")
    print("-------------------------------------")
    for label, ok, msg in checks:
        status = "OK" if ok else "FAIL"
        print(f"{status:4}  {label:<12}  {msg}")
    return 0 if all(ok for _label, ok, _msg in checks) else 1


def ia_ok() -> Tuple[bool, str]:
    return ia_api.ia_ok(runner=run_cmd)


def ia_search_via_curl(
    query: str,
    rows: int,
    page: int,
    sort: str = "",
    *,
    rerank_text: str = "",
    media_filter: str = "any",
    min_item_size_bytes: int = 0,
) -> Tuple[List[SearchResult], int, str]:
    return ia_api.ia_search_via_curl(
        query,
        rows,
        page,
        sort,
        runner=run_cmd,
        rerank_text=rerank_text,
        media_filter=media_filter,
        min_item_size_bytes=min_item_size_bytes,
    )


def ia_files(identifier: str) -> Tuple[List[IAFile], Optional[Dict[str, Any]], str]:
    return ia_api.ia_files(identifier, runner=run_cmd)


def yt_search(query: str, rows: int = 10) -> Tuple[List[SearchResult], int, str]:
    return yt_api.yt_search(query, rows, yt_dlp_path=APP_CONFIG["yt_dlp_path"], runner=run_cmd, logger=log_line)


def youtube_result_status(results: List[SearchResult]) -> str:
    if results:
        return f"YouTube — {len(results)} result(s). [YT] rows open as single-video downloads."
    return (
        "YouTube — 0 results. yt-dlp ran without error but returned nothing; "
        "YouTube may have rate-limited/blocked the search — try again in a moment."
    )


def yt_metadata_url(url: str) -> Tuple[Optional[SearchResult], str]:
    return yt_api.yt_metadata_url(url, yt_dlp_path=APP_CONFIG["yt_dlp_path"], runner=run_cmd)


JOB_TERMINAL_STATUSES = ("done", "failed", "canceled")


@dataclass
class DownloadJob:
    """One queued item's download, processed sequentially by the background worker.

    Byte transfer (this class's own fields) happens off the main thread; the
    interactive "where should this go" placement step only runs once a job
    reaches "awaiting_import", back on the main thread (see
    finish_download_progress). That split is what lets search/browsing stay
    live while bytes move in the background.
    """

    job_id: int
    identifier: str
    title: str
    files: List[IAFile]
    preview_prefix: str = ""
    batch: Optional[Dict[str, str]] = None
    is_youtube: bool = False
    webpage_url: str = ""
    video_id: str = ""
    owner_item: Optional[SearchResult] = None
    owner_metadata: Optional[Dict[str, Any]] = None
    status: str = "queued"  # queued -> downloading -> awaiting_import -> done/failed/canceled
    file_statuses: List[Dict[str, Any]] = field(default_factory=list)
    current_file_name: str = ""
    written: int = 0
    total: int = 0
    speed_bps: float = 0.0
    eta_s: float = 0.0
    cancel_requested: bool = False
    error: str = ""
    created_at: float = 0.0
    finished_at: float = 0.0
    bookkept: bool = False  # main-thread failed_queue/download_log/import bookkeeping done

    def set_file_status(
        self,
        filename: str,
        status: str,
        detail: str = "",
        size: Optional[int] = None,
        real_name: Optional[str] = None,
    ) -> None:
        # real_name covers YouTube, where the file yt-dlp actually writes to
        # staging (resolved after download) can differ from the planned name.
        for row in self.file_statuses:
            if row.get("name") == filename:
                row["status"] = status
                row["detail"] = detail
                if size is not None:
                    row["size"] = int(size or 0)
                if real_name is not None:
                    row["real_name"] = real_name
                return
        row = {"name": filename, "status": status, "detail": detail, "size": int(size or 0)}
        if real_name is not None:
            row["real_name"] = real_name
        self.file_statuses.append(row)

    def files_needing_import(self) -> List[Dict[str, Any]]:
        return [row for row in self.file_statuses if row.get("status") == "downloaded"]

    def summary_label(self) -> str:
        total = len(self.files)
        done = sum(1 for row in self.file_statuses if row.get("status") in ("done", "staged", "skipped"))
        return f"{done}/{total} file(s)" if total > 1 else (self.files[0].name if self.files else "")


@dataclass
class FailedDownload:
    """A failed file together with the item state that authorized its job."""

    file: IAFile
    owner_item: Optional[SearchResult]
    owner_metadata: Optional[Dict[str, Any]] = None
    error: str = ""


class RetroWaveIA:
    def __init__(self, stdscr):
        self.stdscr = stdscr

        self.ia_present, self.ia_version = ia_ok()
        self.status = "Ready"
        self.mode = "RESULTS"  # RESULTS / FILES / FAVS / HELP / ERROR / DOWNLOADING / TOO_SMALL / PREVIEW_DL

        self.query_text = ""
        self.query_built = ""
        self.filter = str(APP_CONFIG["default_filter"])
        self.title_only = bool(APP_CONFIG["title_only"])
        self.hide_small_items = bool(APP_CONFIG["hide_small_items"])
        self.min_item_size_mb = int(APP_CONFIG["min_item_size_mb"])
        self.hide_small_video_files = bool(APP_CONFIG["hide_small_video_files"])
        self.min_video_file_size_mb = int(APP_CONFIG["min_video_file_size_mb"])
        self.enforce_license_gate = bool(APP_CONFIG["license_gate"])
        self.sort_by = str(APP_CONFIG["default_sort"])
        self.page = 1
        self.total_results: int = 0
        self.search_history: List[str] = []
        self.result_filter = ""
        self.last_search_text = ""
        self.search_source = "ia"
        self.last_search_used_label = ""
        self.last_search_attempts: List[Tuple[str, str]] = []
        self._search_load_lock = threading.RLock()
        self._search_load_token: int = 0
        self._search_load_loading: bool = False
        self._search_load_result: Optional[Dict[str, Any]] = None
        self._search_load_thread: Optional[threading.Thread] = None

        self.results: List[SearchResult] = []
        self._search_cache_lock = threading.RLock()
        self._all_results_cache: List[SearchResult] = []
        self._all_results_pages: List[Optional[List[SearchResult]]] = []
        self._all_results_cache_key: str = ""
        self._all_results_loaded_pages: int = 0
        self._all_results_total_pages: int = 0
        self._all_results_loading: bool = False
        self._all_results_loader_error: str = ""
        self._all_results_loader_token: int = 0
        self._all_results_loader_thread: Optional[threading.Thread] = None
        self.sel_r = 0

        self.files: List[IAFile] = []
        # Files and metadata must stay bound to the item that supplied them;
        # result prefetch can otherwise change selected_result() underneath a
        # FILES view.
        self.file_owner_item: Optional[SearchResult] = None
        self.sel_f = 0
        self.file_kw = ""
        self.video_only = False
        self.selected_file_names: Set[str] = set()
        self.selected_file_order: List[str] = []
        self.file_view_state: Dict[str, Dict[str, Any]] = {}
        self._file_load_lock = threading.RLock()
        self._file_load_token: int = 0
        self._file_load_loading: bool = False
        self._file_load_result: Optional[Dict[str, Any]] = None
        self._file_load_thread: Optional[threading.Thread] = None

        self.last_bucket = str(APP_CONFIG["default_bucket"])  # TV/Movies/Music/Other
        self.download_log: List[str] = []
        self.failed_queue: List[FailedDownload] = []
        self._retry_failed_entries: List[FailedDownload] = []
        self._retry_failed_metadata: Optional[Dict[str, Any]] = None
        self.show_welcome = True
        self.theme_name = "Retro"

        self.focus = "MENU"  # MENU or LIST
        self.menu_idx = 0
        self.help_overlay = False

        self.exit_requested = False

        self.favs = self.load_favs()
        self.favs_tab = "ITEMS"  # ITEMS / FILES / FOLDERS
        self.favs_idx = 0

        self.cur_meta: Optional[Dict[str, Any]] = None

        self.preview_item: Optional[SearchResult] = None
        self.preview_file: Optional[IAFile] = None
        self.preview_files: List[IAFile] = []
        self.preview_prefix: str = ""
        self.preview_msg: str = ""
        self.preview_existing: List[str] = []
        self.preview_destinations: List[str] = []
        self.last_error_detail: str = ""

        self.jellyfin_rescan_needed: bool = False

        self.download_queue: List[DownloadJob] = []
        self._download_lock = threading.RLock()
        self._download_job_seq: int = 0
        self._download_worker_thread: Optional[threading.Thread] = None
        self.queue_sel: int = 0
        self.queue_return_mode: str = "RESULTS"

        self._dvd_scan_lock = threading.RLock()
        self._dvd_scan_jobs: Dict[str, Dict[str, Any]] = {}

        self._post_import_lock = threading.RLock()
        self._post_import_jobs: Dict[str, Dict[str, Any]] = {}

        if not self.ia_present:
            self.mode = "ERROR"
            self.status = self.ia_version

    # ---------- favorites persistence ----------
    def load_favs(self) -> Dict[str, Any]:
        return ia_state.load_favs(FAVS_PATH)

    def save_favs(self) -> None:
        ia_state.save_favs(FAVS_PATH, self.favs)

    def _save_session(self) -> None:
        ia_state.save_session(
            SESSION_PATH,
            {
                "filter": getattr(self, "filter", "any"),
                "title_only": getattr(self, "title_only", False),
                "hide_small_items": getattr(self, "hide_small_items", True),
                "min_item_size_mb": getattr(self, "min_item_size_mb", 0),
                "hide_small_video_files": getattr(self, "hide_small_video_files", True),
                "min_video_file_size_mb": getattr(self, "min_video_file_size_mb", 0),
                "sort_by": getattr(self, "sort_by", ""),
                "enforce_license_gate": getattr(self, "enforce_license_gate", False),
                "search_history": getattr(self, "search_history", [])[:MAX_HISTORY],
            },
        )

    def _restore_session(self) -> None:
        try:
            data = ia_state.load_session(SESSION_PATH)
            if not data:
                return
            if data.get("filter") in FILTERS:
                self.filter = data["filter"]
            self.title_only = bool(data.get("title_only", False))
            self.hide_small_items = bool(data.get("hide_small_items", APP_CONFIG["hide_small_items"]))
            try:
                self.min_item_size_mb = max(0, int(data.get("min_item_size_mb", APP_CONFIG["min_item_size_mb"])))
            except (TypeError, ValueError):
                self.min_item_size_mb = int(APP_CONFIG["min_item_size_mb"])
            self.hide_small_video_files = bool(data.get("hide_small_video_files", APP_CONFIG["hide_small_video_files"]))
            try:
                self.min_video_file_size_mb = max(
                    0, int(data.get("min_video_file_size_mb", APP_CONFIG["min_video_file_size_mb"]))
                )
            except (TypeError, ValueError):
                self.min_video_file_size_mb = int(APP_CONFIG["min_video_file_size_mb"])
            sort_val = str(data.get("sort_by") or "")
            if any(v == sort_val for _, v in SORT_OPTIONS):
                self.sort_by = sort_val
            self.enforce_license_gate = bool(data.get("enforce_license_gate", False))
            hist = data.get("search_history")
            if isinstance(hist, list):
                self.search_history = [str(x) for x in hist if str(x).strip()][:MAX_HISTORY]
        except Exception:
            pass

    # ---------- pending download persistence ----------
    def _save_pending(
        self,
        identifier: str,
        item_title: str,
        files: "List[IAFile]",
        preview_prefix: str,
        glob_pat: str,
        completed_names: "List[str]",
    ) -> None:
        try:
            data = ia_state.pending_payload(
                identifier,
                item_title,
                files,
                preview_prefix,
                glob_pat,
                completed_names,
            )
            if ia_state.save_pending(PENDING_PATH, data):
                log_line(f"PENDING_SAVED: {identifier} ({len(files)} files, {len(completed_names)} done)")
        except Exception as e:
            log_line(f"PENDING_SAVE_ERR: {e}")

    def _clear_pending(self) -> None:
        ia_state.clear_pending(PENDING_PATH)

    def _load_pending(self) -> "Optional[Dict[str, Any]]":
        return ia_state.load_pending(PENDING_PATH)

    def is_fav_item(self, identifier: str) -> bool:
        ident = (identifier or "").strip()
        for it in self.favs.get("items", []):
            if str(it.get("identifier", "")).strip() == ident:
                return True
        return False

    def toggle_fav_item(self, r: SearchResult) -> None:
        ident = (r.identifier or "").strip()
        if not ident:
            return
        items = self.favs.get("items", [])
        if not isinstance(items, list):
            items = []
            self.favs["items"] = items

        if self.is_fav_item(ident):
            self.favs["items"] = [it for it in items if str(it.get("identifier", "")).strip() != ident]
            self.status = "Removed favorite item."
        else:
            items.insert(0, {"identifier": r.identifier, "title": r.title, "year": r.year, "creator": r.creator})
            self.status = "Added favorite item."
        self.save_favs()

    def file_fav_key(self, identifier: str, filename: str) -> str:
        return f"{(identifier or '').strip()}::{(filename or '').strip()}"

    def is_fav_file(self, identifier: str, filename: str) -> bool:
        key = self.file_fav_key(identifier, filename)
        for it in self.favs.get("files", []):
            k2 = self.file_fav_key(it.get("identifier", ""), it.get("filename", ""))
            if k2 == key:
                return True
        return False

    def toggle_fav_file(self, item: SearchResult, f: IAFile) -> None:
        ident = (item.identifier or "").strip()
        fname = (f.name or "").strip()
        if not ident or not fname:
            return

        files = self.favs.get("files", [])
        if not isinstance(files, list):
            files = []
            self.favs["files"] = files

        if self.is_fav_file(ident, fname):
            self.favs["files"] = [
                it
                for it in files
                if self.file_fav_key(it.get("identifier", ""), it.get("filename", "")) != self.file_fav_key(ident, fname)
            ]
            self.status = "Removed favorite file."
        else:
            files.insert(
                0,
                {
                    "identifier": item.identifier,
                    "item_title": item.title,
                    "year": item.year,
                    "creator": item.creator,
                    "filename": f.name,
                    "size": int(f.size or 0),
                    "fmt": f.fmt,
                },
            )
            self.status = "Added favorite file."
        self.save_favs()

    def add_folder_fav(self, bucket: str, folder_name: str) -> None:
        bucket = bucket if bucket in ("TV", "Movies", "Music", "Other") else "Other"
        name = sanitize_folder(folder_name)
        arr = self.favs.get("folders", {}).get(bucket, [])
        if not isinstance(arr, list):
            self.favs["folders"][bucket] = []
            arr = self.favs["folders"][bucket]
        lowered = {str(x).strip().lower() for x in arr}
        if name.strip().lower() not in lowered:
            arr.insert(0, name)
            self.favs["folders"][bucket] = arr[:30]
            self.save_favs()

    # ---------- safe drawing ----------
    def safe_addstr(self, y: int, x: int, s: str, attr: int = 0) -> None:
        try:
            h, w = self.stdscr.getmaxyx()
            if y < 0 or x < 0 or y >= h or x >= w:
                return
            if w <= 1:
                return
            s2 = s
            if x + len(s2) > w - 1:
                s2 = s2[: max(0, (w - 1) - x)]
            if attr:
                self.stdscr.addstr(y, x, s2, attr)
            else:
                self.stdscr.addstr(y, x, s2)
        except curses.error:
            return

    def init_colors(self) -> None:
        curses.start_color()
        curses.use_default_colors()
        supports_256 = (getattr(curses, "COLORS", 0) or 0) >= 256
        if supports_256 and self.theme_name not in ("Minimal", "High contrast"):
            colors = (
                250,  # title/header
                178,  # borders/section accent
                178,  # gold accent
                114,  # good/selected
                167,  # warning/error
                253,  # normal text
                16,   # selected foreground
                16,   # prompt foreground
                16,   # action foreground
            )
            backs = (-1, -1, -1, -1, -1, -1, 178, 208, 220)
        elif supports_256 and self.theme_name == "High contrast":
            colors = (220, 178, 220, 114, 167, 253, 16, 16, 16)
            backs = (-1, -1, -1, -1, -1, -1, 178, 208, 220)
        elif supports_256 and self.theme_name == "Minimal":
            colors = (253, 178, 253, 114, 167, 253, 16, 16, 16)
            backs = (-1, -1, -1, -1, -1, -1, 253, 178, 220)
        elif self.theme_name == "Minimal":
            colors = (
                curses.COLOR_WHITE,
                curses.COLOR_YELLOW,
                curses.COLOR_WHITE,
                curses.COLOR_GREEN,
                curses.COLOR_RED,
                curses.COLOR_WHITE,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
            )
            backs = (-1, -1, -1, -1, -1, -1, curses.COLOR_WHITE, curses.COLOR_YELLOW, curses.COLOR_YELLOW)
        elif self.theme_name == "High contrast":
            colors = (
                curses.COLOR_YELLOW,
                curses.COLOR_YELLOW,
                curses.COLOR_YELLOW,
                curses.COLOR_GREEN,
                curses.COLOR_RED,
                curses.COLOR_WHITE,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
            )
            backs = (-1, -1, -1, -1, -1, -1, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_YELLOW)
        else:
            colors = (
                curses.COLOR_YELLOW,
                curses.COLOR_YELLOW,
                curses.COLOR_YELLOW,
                curses.COLOR_GREEN,
                curses.COLOR_RED,
                curses.COLOR_WHITE,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
            )
            backs = (-1, -1, -1, -1, -1, -1, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_YELLOW)
        for i, (fg, bg) in enumerate(zip(colors, backs), start=1):
            curses.init_pair(i, fg, bg)

    def term_too_small(self) -> bool:
        h, w = self.stdscr.getmaxyx()
        return h < MIN_H or w < MIN_W

    # ---------- UI pieces ----------
    def draw_banner(self, w: int) -> int:
        y = 0
        title = "MINOTAUR IA BROWSER"
        banner_width = min(len(title) + 8, max(10, w - 2))
        start_x = max(0, (w - banner_width) // 2)

        top = "╔" + "═" * (banner_width - 2) + "╗"
        mid = "║" + title.center(banner_width - 2) + "║"
        bot = "╚" + "═" * (banner_width - 2) + "╝"

        self.safe_addstr(y, start_x, top, curses.color_pair(2)); y += 1
        self.safe_addstr(y, start_x, mid, curses.color_pair(1) | curses.A_BOLD); y += 1
        self.safe_addstr(y, start_x, bot, curses.color_pair(2)); y += 1
        return y + 1

    def draw_top_status(self, y: int, w: int) -> int:
        search_mode = "Title" if self.title_only else "Broad"
        source_label = self.search_source_label()

        header = "Search Results"
        if self.mode == "FILES":
            item = self.selected_result()
            name = item.title if item else "(none)"
            header = f"Files for: {name}"
        elif self.mode == "FAVS":
            header = "Favorites"
        elif self.mode == "HELP":
            header = "Help"
        elif self.mode == "QUEUE":
            header = "Download queue"
        elif self.mode == "PREVIEW_DL":
            header = "Confirm download"
        elif self.mode == "ERROR":
            header = "Error"

        if self.total_results > 0:
            total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            local = f"  |  Local: {self.result_filter}" if self.result_filter else ""
            page_info = f"Page: {self.page}/{total_pages}  ({self.total_results} found){local}"
        else:
            local = f"  |  Local: {self.result_filter}" if self.result_filter else ""
            page_info = f"Page: {self.page}{local}"
        sort_info = f"  |  Sort: {self._sort_label()}" if self.sort_by else ""
        focus_info = f"Focus: {self.focus}"
        line1 = f"{header}  |  {focus_info}  |  Source: {source_label}  |  Filter: {self.filter}  |  Search: {search_mode}{sort_info}  |  {page_info}"
        self.safe_addstr(y, 0, line1[: max(0, w - 1)].ljust(max(0, w - 1)), curses.color_pair(3)); y += 1

        breadcrumb = self.breadcrumb()
        if self.query_built and self.mode in ("RESULTS", "SEARCH"):
            line2 = f"{breadcrumb}  |  Query: {self.query_built[:45]}  |  Root: {MEDIA_ROOT}  |  Staging: {STAGING_ROOT}"
        else:
            line2 = f"{breadcrumb}  |  Root: {MEDIA_ROOT}   Staging: {STAGING_ROOT}"
        self.safe_addstr(y, 0, line2[: max(0, w - 1)].ljust(max(0, w - 1)), curses.color_pair(3)); y += 1
        return y

    def breadcrumb(self) -> str:
        crumbs = ["Search"]
        if self.mode in ("RESULTS", "SEARCH"):
            crumbs.append("Results")
        elif self.mode == "FILES":
            item = self.selected_result()
            ident = item.identifier if item else "item"
            crumbs += ["Results", f"Files:{ident}"]
        elif self.mode == "FAVS":
            crumbs.append(f"Favorites:{self.favs_tab}")
        elif self.mode == "PREVIEW_DL":
            crumbs += ["Files", "Preview"]
        elif self.mode == "QUEUE":
            crumbs.append("Queue")
        elif self.mode == "HELP":
            crumbs.append("Help")
        elif self.mode == "ERROR":
            crumbs.append("Error")
        return " > ".join(crumbs)

    def _sort_label(self) -> str:
        sort_by = getattr(self, "sort_by", "")
        for label, val in SORT_OPTIONS:
            if val == sort_by:
                return label
        return "relevance"

    def search_source_label(self) -> str:
        source = str(getattr(self, "search_source", "ia") or "ia").lower()
        if source == "youtube_url":
            return "YouTube URL"
        if source.startswith("youtube"):
            return "YouTube"
        if source == "all":
            return "All"
        return "IA"

    def search_source_badge(self) -> str:
        source = str(getattr(self, "search_source", "ia") or "ia").lower()
        if source.startswith("youtube"):
            return "[YT]"
        if source == "all":
            return "[ALL]"
        return "[IA]"

    def result_source_badge(self, r: Optional[SearchResult]) -> str:
        return "[YT]" if self.is_youtube_result(r) else "[IA]"

    def result_license_label(self, r: SearchResult) -> str:
        if self.is_youtube_result(r):
            return "yt"
        status, _why = license_status_from_fields(r.licenseurl, r.rights)
        return {
            "open": "lic:open",
            "blocked": "lic:block",
            "unclear": "lic:unclear",
            "unknown": "?",
        }.get(status, "?")

    def is_youtube_result(self, r: Optional[SearchResult]) -> bool:
        return bool(r and getattr(r, "source", "ia") == "youtube")

    def youtube_file_for_result(self, r: SearchResult) -> IAFile:
        return IAFile(
            name=yt_downloads.display_filename(r.title, r.video_id or r.identifier),
            size=0,
            fmt="YouTube video",
        )

    def result_meta_summary(self, r: SearchResult) -> str:
        if self.is_youtube_result(r):
            parts = []
            if r.uploader or r.creator:
                parts.append(r.uploader or r.creator)
            if r.duration:
                parts.append(f"{int(r.duration)}s")
            if r.upload_date:
                parts.append(r.upload_date)
            return " | ".join(parts)
        parts = []
        if r.year:
            parts.append(str(r.year))
        if r.mediatype:
            parts.append(str(r.mediatype))
        if r.downloads:
            parts.append(f"{compact_count(r.downloads)} dl")
        if r.formats and is_archive_torrent_format(r.formats):
            parts.append("torrent")
        lic = self.result_license_label(r)
        if lic != "?":
            parts.append(lic)
        return " | ".join(parts)

    def youtube_result_details_lines(self, item: SearchResult) -> List[str]:
        lines = [
            "Selected:",
            "  Source: [YT] YouTube",
            f"  Title:   {item.title or '(no title)'}",
        ]
        if item.uploader or item.creator:
            lines.append(f"  Channel: {item.uploader or item.creator}")
        if item.duration:
            lines.append(f"  Duration: {int(item.duration)}s")
        if item.upload_date or item.date:
            lines.append(f"  Upload date: {item.upload_date or item.date}")
        if item.video_id:
            lines.append(f"  Video ID: {item.video_id}")
        if item.webpage_url:
            lines.append(f"  URL: {item.webpage_url}")
        lines += [
            "",
            "Enter or [Open] to preview/download",
            "Single-video download via yt-dlp",
            f"Query: {self.query_built or '(none)'}",
        ]
        return lines

    def result_row_attr(self, r: SearchResult, selected: bool) -> int:
        if selected:
            attr = curses.color_pair(7) if self.focus == "LIST" else curses.color_pair(6)
            if self.focus == "LIST":
                attr |= curses.A_BOLD
            return attr
        if self.is_youtube_result(r):
            return curses.color_pair(3) | curses.A_BOLD
        return curses.color_pair(6)

    def show_audit_summary(self) -> None:
        try:
            report = ia_audit.analyze_library(MEDIA_ROOT, probe=False, max_probe=0)
        except Exception as e:
            self.status = f"Audit summary failed: {e}"
            return

        summary = report.get("summary") or {}
        self.status = (
            "Audit: weird {weird_filenames} | dup movies {duplicate_movies} | dup eps {duplicate_episodes} | "
            "metadata {metadata_issues} | rename {rename_suggestions} | cleanup {cleanup_candidates}. "
            "Run ia-audit for details."
        ).format(
            weird_filenames=int(summary.get("weird_filenames") or 0),
            duplicate_movies=int(summary.get("duplicate_movies") or 0),
            duplicate_episodes=int(summary.get("duplicate_episodes") or 0),
            metadata_issues=int(summary.get("metadata_issues") or 0),
            rename_suggestions=int(summary.get("rename_suggestions") or 0),
            cleanup_candidates=int(summary.get("cleanup_candidates") or 0),
        )

    def result_filter_blob(self, r: SearchResult) -> str:
        status, _why = license_status_from_fields(r.licenseurl, r.rights)
        values = [
            getattr(r, "source", ""),
            getattr(r, "webpage_url", ""),
            getattr(r, "video_id", ""),
            getattr(r, "uploader", ""),
            r.identifier,
            r.title,
            r.year,
            r.creator,
            r.description,
            r.mediatype,
            r.formats,
            str(r.downloads or ""),
            r.date,
            r.publicdate,
            r.collection,
            status,
            r.rights,
            r.licenseurl,
        ]
        return " ".join(str(v or "") for v in values).lower()

    def _search_cache_key(self) -> str:
        query = (getattr(self, "query_built", "") or getattr(self, "query_text", "")).strip()
        return "\0".join(
            [
                query,
                str(getattr(self, "filter", "")),
                str(getattr(self, "sort_by", "")),
                "1" if bool(getattr(self, "title_only", False)) else "0",
            ]
        )

    def _rerank_args(self, *, custom: bool = False) -> Tuple[str, str]:
        """Return (rerank_text, media_filter) for local relevance ranking.

        Only plain user-text searches under relevance sort get re-ranked; custom
        or advanced queries are left in Internet Archive's own order. The chosen
        values are stashed so background page prefetch ranks pages identically.
        """
        qt = str(getattr(self, "query_text", "") or "")
        if custom or not qt.strip() or looks_like_advanced_query(qt):
            args = ("", "any")
        else:
            args = (qt, str(getattr(self, "filter", "any") or "any"))
        self._active_rerank_text, self._active_rerank_filter = args
        return args

    def _ensure_search_cache_state(self) -> None:
        if not hasattr(self, "_search_cache_lock") or getattr(self, "_search_cache_lock", None) is None:
            self._search_cache_lock = threading.RLock()
        if not hasattr(self, "_all_results_cache"):
            self._all_results_cache = []
        if not hasattr(self, "_all_results_pages"):
            self._all_results_pages = []
        if not hasattr(self, "_all_results_cache_key"):
            self._all_results_cache_key = ""
        if not hasattr(self, "_all_results_loaded_pages"):
            self._all_results_loaded_pages = 0
        if not hasattr(self, "_all_results_total_pages"):
            self._all_results_total_pages = 0
        if not hasattr(self, "_all_results_loading"):
            self._all_results_loading = False
        if not hasattr(self, "_all_results_loader_error"):
            self._all_results_loader_error = ""
        if not hasattr(self, "_all_results_loader_token"):
            self._all_results_loader_token = 0
        if not hasattr(self, "_all_results_loader_thread"):
            self._all_results_loader_thread = None
        if not hasattr(self, "_local_filter_extra_results"):
            self._local_filter_extra_results = []
        if not hasattr(self, "_local_filter_refinement_key"):
            self._local_filter_refinement_key = ""

    def _ensure_search_load_state(self) -> None:
        if not hasattr(self, "_search_load_lock") or getattr(self, "_search_load_lock", None) is None:
            self._search_load_lock = threading.RLock()
        if not hasattr(self, "_search_load_token"):
            self._search_load_token = 0
        if not hasattr(self, "_search_load_loading"):
            self._search_load_loading = False
        if not hasattr(self, "_search_load_result"):
            self._search_load_result = None
        if not hasattr(self, "_search_load_thread"):
            self._search_load_thread = None

    def cancel_search_load(self) -> None:
        self._ensure_search_load_state()
        with self._search_load_lock:
            self._search_load_token += 1
            self._search_load_loading = False
            self._search_load_result = None

    def cancel_result_prefetch(self) -> None:
        self._ensure_search_cache_state()
        with self._search_cache_lock:
            self._all_results_loader_token += 1
            self._all_results_loading = False
            self._all_results_loader_error = ""

    def _reset_search_cache(self) -> None:
        self._ensure_search_cache_state()
        with self._search_cache_lock:
            self._all_results_cache = []
            self._all_results_pages = []
            self._all_results_cache_key = ""
            self._all_results_loaded_pages = 0
            self._all_results_total_pages = 0
            self._all_results_loading = False
            self._all_results_loader_error = ""
            self._all_results_loader_thread = None
            self._local_filter_extra_results = []
            self._local_filter_refinement_key = ""

    def _prime_search_cache(self, key: str, page_num: int, page_results: List[SearchResult], total_pages: int) -> None:
        self._ensure_search_cache_state()
        with self._search_cache_lock:
            previous_key = self._all_results_cache_key
            self._all_results_cache_key = key
            if total_pages <= 0:
                total_pages = 1
            if len(self._all_results_pages) != total_pages or previous_key != key:
                self._all_results_pages = [None] * total_pages
            if 1 <= page_num <= total_pages:
                self._all_results_pages[page_num - 1] = list(page_results)
            self._all_results_cache = [r for page in self._all_results_pages if page for r in page]
            self._all_results_loaded_pages = sum(1 for page in self._all_results_pages if page)
            self._all_results_total_pages = max(0, total_pages)
            self._all_results_loader_error = ""

    def _start_search_prefetch(
        self,
        query: str,
        total_pages: int,
        sort_by: str,
        current_page: int,
        rerank_text: str = "",
        media_filter: str = "any",
        min_item_size_bytes: int = 0,
    ) -> None:
        self._ensure_search_cache_state()
        if total_pages <= 1:
            with self._search_cache_lock:
                self._all_results_loading = False
            return

        key = self._search_cache_key()
        with self._search_cache_lock:
            if (
                self._all_results_loading
                and self._all_results_cache_key == key
                and self._all_results_loader_thread is not None
                and self._all_results_loader_thread.is_alive()
            ):
                return

            self._all_results_loader_token += 1
            token = self._all_results_loader_token
            self._all_results_loading = True
            self._all_results_loader_error = ""

        def worker() -> None:
            for page in range(1, total_pages + 1):
                if page == current_page:
                    continue
                with self._search_cache_lock:
                    if token != self._all_results_loader_token or self._all_results_cache_key != key:
                        return
                page_results, _page_total, err = ia_search_via_curl(
                    query,
                    rows=ROWS_PER_PAGE,
                    page=page,
                    sort=sort_by,
                    rerank_text=rerank_text,
                    media_filter=media_filter,
                    min_item_size_bytes=min_item_size_bytes,
                )
                if err:
                    with self._search_cache_lock:
                        if token == self._all_results_loader_token and self._all_results_cache_key == key:
                            self._all_results_loading = False
                            self._all_results_loader_error = err
                    return
                with self._search_cache_lock:
                    if token != self._all_results_loader_token or self._all_results_cache_key != key:
                        return
                    if len(self._all_results_pages) != total_pages:
                        self._all_results_pages = [None] * total_pages
                    self._all_results_pages[page - 1] = list(page_results)
                    self._all_results_cache = [r for page_list in self._all_results_pages if page_list for r in page_list]
                    self._all_results_loaded_pages = sum(1 for page_list in self._all_results_pages if page_list)
            with self._search_cache_lock:
                if token == self._all_results_loader_token and self._all_results_cache_key == key:
                    self._all_results_cache = [r for page_list in self._all_results_pages if page_list for r in page_list]
                    self._all_results_loaded_pages = sum(1 for page_list in self._all_results_pages if page_list)
                    self._all_results_loading = False

        thread = threading.Thread(target=worker, daemon=True)
        with self._search_cache_lock:
            self._all_results_loader_thread = thread
        thread.start()

    def _ensure_all_search_results_loaded(self) -> None:
        self._ensure_search_cache_state()
        query = (getattr(self, "query_built", "") or getattr(self, "query_text", "")).strip()
        if not query:
            return

        key = self._search_cache_key()
        current_page = max(1, int(getattr(self, "page", 1) or 1))
        with self._search_cache_lock:
            cached_key = self._all_results_cache_key
            loading = self._all_results_loading
            loaded_pages = self._all_results_loaded_pages
            total_pages = self._all_results_total_pages

        if cached_key != key:
            page_results = list(getattr(self, "results", []))
            total = int(getattr(self, "total_results", 0) or len(page_results))
            total_pages = max(1, (total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            self._prime_search_cache(key, current_page, page_results, total_pages)
            self._start_search_prefetch(
                query,
                total_pages,
                getattr(self, "sort_by", ""),
                current_page,
                getattr(self, "_active_rerank_text", ""),
                getattr(self, "_active_rerank_filter", "any"),
                self._search_min_item_size_bytes(),
            )
            return

        if not loading and total_pages > loaded_pages:
            self._start_search_prefetch(
                query,
                total_pages,
                getattr(self, "sort_by", ""),
                current_page,
                getattr(self, "_active_rerank_text", ""),
                getattr(self, "_active_rerank_filter", "any"),
                self._search_min_item_size_bytes(),
            )

    def _load_all_search_results(self) -> List[SearchResult]:
        self._ensure_search_cache_state()
        with self._search_cache_lock:
            if self._all_results_cache_key == self._search_cache_key() and self._all_results_cache:
                base = list(self._all_results_cache)
            else:
                base = list(getattr(self, "results", []) or [])
            extras = list(getattr(self, "_local_filter_extra_results", []) or [])
        if not extras:
            return base
        seen = {(r.identifier or "").strip() for r in base if (r.identifier or "").strip()}
        merged = list(base)
        for item in extras:
            ident = (item.identifier or "").strip()
            if ident and ident in seen:
                continue
            if ident:
                seen.add(ident)
            merged.append(item)
        return merged

    def _search_min_item_size_bytes(self) -> int:
        """Bytes threshold for the configured min_item_size_mb, or 0 when the
        hide-small-items filter is off. Centralizes the MB->bytes conversion
        so search fetches (early filtering) and get_visible_results (display
        safety net) can't drift out of sync."""
        if not getattr(self, "hide_small_items", False):
            return 0
        return max(0, int(getattr(self, "min_item_size_mb", 0) or 0)) * 1024 * 1024

    def _passes_size_filter(self, r: SearchResult) -> bool:
        min_bytes = self._search_min_item_size_bytes()
        if min_bytes <= 0:
            return True
        size = int(getattr(r, "item_size", 0) or 0)
        # Unknown size (0, e.g. some YouTube/collection results never carry
        # item_size) fails open rather than hiding results we can't judge.
        return size <= 0 or size >= min_bytes

    def get_visible_results(self) -> List[SearchResult]:
        needle = self.result_filter.strip().lower()
        results = getattr(self, "results", [])
        if not needle:
            return [r for r in results if self._passes_size_filter(r)]
        terms = [t for t in needle.split() if t]
        scope = self._load_all_search_results()
        visible = [
            r
            for r in scope
            if all(t in self.result_filter_blob(r) for t in terms) and self._passes_size_filter(r)
        ]
        # The local filter merges in extra results from a separate year
        # refinement query (see load_year_refinement_for_local_filter) that
        # are appended in raw IA order, not ranked. Re-rank the whole filtered
        # view against the original search text so the merge doesn't bury a
        # strong title match under already-loaded, unranked items.
        rerank_text = str(getattr(self, "_active_rerank_text", "") or getattr(self, "query_text", "") or "")
        if rerank_text.strip():
            rerank_filter = str(getattr(self, "_active_rerank_filter", "") or getattr(self, "filter", "any") or "any")
            visible = ia_ranking.rerank(visible, rerank_text, rerank_filter)
        return visible

    def selected_result(self) -> Optional[SearchResult]:
        visible = self.get_visible_results()
        if not visible:
            return None
        if self.sel_r >= len(visible):
            self.sel_r = max(0, len(visible) - 1)
        return visible[self.sel_r]

    def _find_result_location(self, identifier: str) -> Optional[Tuple[int, int]]:
        self._ensure_search_cache_state()
        ident = (identifier or "").strip()
        if not ident:
            return None
        with self._search_cache_lock:
            pages = list(getattr(self, "_all_results_pages", []) or [])
        for page_idx, page_results in enumerate(pages):
            if not page_results:
                continue
            for row_idx, r in enumerate(page_results):
                if (r.identifier or "").strip() == ident:
                    return page_idx + 1, row_idx
        for row_idx, r in enumerate(getattr(self, "results", []) or []):
            if (r.identifier or "").strip() == ident:
                return max(1, int(getattr(self, "page", 1) or 1)), row_idx
        return None

    def _sync_page_to_result(self, item: SearchResult) -> None:
        location = self._find_result_location(item.identifier)
        if not location:
            return
        page_num, _row_idx = location
        if not getattr(self, "result_filter", ""):
            return
        if page_num == getattr(self, "page", 1):
            return
        with self._search_cache_lock:
            pages = list(getattr(self, "_all_results_pages", []) or [])
            page_results = list(pages[page_num - 1]) if 1 <= page_num <= len(pages) and pages[page_num - 1] else []
        if not page_results:
            return
        self.page = page_num
        self.results = page_results

    def set_error_status(self, msg: str, *, detail: str = "") -> None:
        raw_status = str(msg or "Unknown error").strip()
        raw_detail = str(detail or raw_status).strip()
        self.status = compact_error_text(raw_status, max_chars=MAX_STATUS_ERROR_CHARS)
        self.last_error_detail = compact_error_text(raw_detail, max_chars=MAX_DETAIL_ERROR_CHARS)
        if raw_detail and raw_detail != self.last_error_detail:
            log_line(f"TUI_ERROR_RAW: {raw_detail}")
        log_line(f"TUI_ERROR: {self.last_error_detail}")

    def set_result_filter(self, value: str) -> None:
        self.result_filter = (value or "").strip()
        self.sel_r = 0
        if self.result_filter:
            self._ensure_all_search_results_loaded()
            self.load_year_refinement_for_local_filter()
        else:
            self.cancel_result_prefetch()
            self._local_filter_extra_results = []
            self._local_filter_refinement_key = ""
        n = len(self.get_visible_results())
        progress = self.local_filter_progress_label()
        suffix = f"; {progress}" if progress else ""
        self.status = f"Local result filter: {self.result_filter or '(none)'} ({n} visible{suffix})"
        self._save_session()

    def local_filter_year_refinement_query(self) -> str:
        year = str(getattr(self, "result_filter", "") or "").strip()
        if not re.fullmatch(r"(?:19\d{2}|20\d{2})", year):
            return ""
        query_text = str(getattr(self, "query_text", "") or "").strip()
        if not query_text or looks_like_advanced_query(query_text):
            return ""
        _title, existing_year = split_title_year(query_text)
        if existing_year:
            return ""
        return build_title_year_query(f"{query_text} {year}", getattr(self, "filter", "any"))

    def load_year_refinement_for_local_filter(self) -> None:
        self._ensure_search_cache_state()
        query = self.local_filter_year_refinement_query()
        if not query:
            self._local_filter_extra_results = []
            self._local_filter_refinement_key = ""
            return
        key = "\0".join([self._search_cache_key(), str(getattr(self, "result_filter", "") or ""), query])
        if getattr(self, "_local_filter_refinement_key", "") == key:
            return
        results, _total, err = ia_search_via_curl(
            query,
            rows=ROWS_PER_PAGE,
            page=1,
            sort=getattr(self, "sort_by", ""),
            min_item_size_bytes=self._search_min_item_size_bytes(),
        )
        if err:
            log_line(f"LOCAL_FILTER_YEAR_REFINE_ERR: {err}")
            self._local_filter_extra_results = []
            self._local_filter_refinement_key = key
            return
        self._local_filter_extra_results = list(results or [])
        self._local_filter_refinement_key = key

    def clear_result_filter(self) -> None:
        if not self.result_filter:
            self.status = "Local result filter already clear."
            return
        self.result_filter = ""
        self.sel_r = 0
        self.cancel_result_prefetch()
        self._local_filter_extra_results = []
        self._local_filter_refinement_key = ""
        self._save_session()
        n = len(self.get_visible_results())
        self.status = f"Local result filter cleared. ({n} visible)"

    def edit_result_filter(self) -> None:
        s = self.prompt("Local result filter (blank clears): ", self.result_filter)
        if s is not None:
            self.set_result_filter(s)

    def local_filter_progress_label(self) -> str:
        if not getattr(self, "result_filter", ""):
            return ""
        with self._search_cache_lock:
            loading = bool(getattr(self, "_all_results_loading", False))
            loaded_pages = int(getattr(self, "_all_results_loaded_pages", 0) or 0)
            total_pages = int(getattr(self, "_all_results_total_pages", 0) or 0)
            loaded_items = len(getattr(self, "_all_results_cache", []) or [])
            loader_error = str(getattr(self, "_all_results_loader_error", "") or "")
        if loader_error:
            return f"scan paused: {loader_error}"
        if loading and total_pages > 0:
            return f"scanning {loaded_pages}/{total_pages} pages ({loaded_items} loaded)"
        if total_pages > 0 and loaded_pages >= total_pages and loaded_items:
            return f"scan complete ({loaded_items} loaded)"
        if loaded_items:
            return f"{loaded_items} loaded"
        return ""

    def collection_choices_from_results(self, limit: int = 12) -> List[str]:
        counts: Dict[str, int] = {}
        for r in self.results:
            for raw in str(r.collection or "").split(","):
                coll = normalize_collection_identifier(raw)
                if coll:
                    counts[coll] = counts.get(coll, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        return [f"{coll} ({count})" for coll, count in ordered[:limit]]

    def results_state_chips(self) -> List[str]:
        self._ensure_download_state()
        chips: List[str] = []
        if bool(getattr(self, "_search_load_loading", False)):
            chips.append("Searching...")
        if self.query_text:
            chips.append(f"Query: {self.query_text}")
        if self.filter:
            chips.append(f"Media: {self.filter}")
        if self.title_only:
            chips.append("Title only: On")
        if self.result_filter:
            chips.append(f"Local: {self.result_filter}")
        if self.sort_by:
            chips.append(f"Sort: {self._sort_label()}")
        total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE) if self.total_results else 1
        if self.total_results > 0:
            chips.append(f"Page: {self.page}/{total_pages}")
        with self._search_cache_lock:
            if self._all_results_loading:
                chips.append(self.local_filter_progress_label() or "Scanning local scope...")
            elif self.result_filter and self._all_results_loaded_pages and self._all_results_total_pages and self._all_results_loaded_pages < self._all_results_total_pages:
                chips.append(self.local_filter_progress_label())
        if self.download_queue:
            chips.append(f"{self.download_queue_summary()}  (Q to view)")
        return chips

    def effective_search_total(self, page: int, results: List[SearchResult], reported_total: int) -> int:
        page_num = max(1, int(page or 1))
        visible_count = len(results or [])
        reported = int(reported_total or 0)
        # A page shorter than ROWS_PER_PAGE normally means we've reached the
        # true end of IA's result list, so shrink the reported total to match.
        # That assumption breaks when the min-item-size filter is active: a
        # page can come back short simply because the fixed-size candidate
        # pool (or, past the pool bound, that one raw IA page) didn't contain
        # enough *eligible* items, not because IA is out of raw results. Doing
        # the correction there was observed to make total_results/total_pages
        # bounce around unpredictably while paging deep into a filtered
        # search, so trust IA's raw total instead in that case.
        if self._search_min_item_size_bytes() > 0:
            return reported or visible_count
        if 0 < visible_count < ROWS_PER_PAGE:
            terminal_total = ((page_num - 1) * ROWS_PER_PAGE) + visible_count
            return min(reported, terminal_total) if reported > 0 else terminal_total
        return reported or visible_count

    def _collection_from_choice(self, choice: str) -> str:
        return re.sub(r"\s+\(\d+\)\s*$", "", choice or "").strip()

    def set_query_and_search(self, query_text: str, *, built_query: Optional[str] = None) -> None:
        self.query_text = query_text
        self.query_built = built_query or ""
        self.show_welcome = False
        self.start_search_async(reset_page=True, built_query=built_query)

    def open_search_tools(self) -> None:
        options = [
            ("New search", "search"),
            ("Combined IA + YouTube", "combined_search"),
            ("YouTube search", "youtube_search"),
            ("YouTube URL", "youtube_url"),
            ("Search history", "history"),
            ("Search presets", "search_preset"),
            ("Search attempts", "search_attempts"),
            ("Field search", "field_search"),
            ("Collection search", "collection_search"),
            ("Within collection", "within_collection"),
            ("Result collections", "collection_facets"),
            ("Media filter", "filter"),
            ("Sort order", "sort"),
            ("Toggle title-only", "title"),
            ("Local loaded-result filter", "result_filter"),
        ]
        pick = self.prompt_list("Search tools", [label for label, _action in options])
        if not pick:
            self.status = "Search tools canceled."
            return
        for label, action in options:
            if label == pick:
                self.activate_menu_action(action)
                return

    def choose_search_attempt(self) -> None:
        attempts = [(label, query) for label, query in getattr(self, "last_search_attempts", []) if query]
        if not attempts:
            self.status = "No search attempts recorded yet."
            return

        labels: List[str] = []
        for label, query in attempts:
            marker = "*" if label == getattr(self, "last_search_used_label", "") else " "
            short = query if len(query) <= 72 else query[:69] + "..."
            labels.append(f"{marker} {label}: {short}")

        pick = self.prompt_list("Search attempts", labels)
        if not pick:
            self.status = "Search attempt unchanged."
            return

        idx = labels.index(pick)
        label, query = attempts[idx]
        self.start_search_async(
            reset_page=True,
            built_query=query,
            built_query_label=label,
            attempts_display=attempts,
        )
        self.status = f"Searching with {label} attempt..."

    def choose_search_source(self) -> None:
        options = [
            ("IA search", "search"),
            ("Combined IA + YouTube", "combined_search"),
            ("YouTube search", "youtube_search"),
            ("YouTube direct URL", "youtube_url"),
        ]
        pick = self.prompt_list("Source", [label for label, _action in options])
        if not pick:
            self.status = "Source unchanged."
            return
        for label, action in options:
            if label == pick:
                self.activate_menu_action(action)
                return

    def jump_to_result_number(self, target: int) -> None:
        if target < 1:
            self.status = "Result number must be >= 1."
            return
        visible = self.get_visible_results()
        if self.result_filter:
            if target > len(visible):
                self.status = f"Result must be 1-{len(visible)}."
                return
            self.sel_r = target - 1
            self.focus = "LIST"
            self.status = f"Selected result {target}."
            return
        reported_total = int(getattr(self, "total_results", 0) or 0)
        effective_total = self.effective_search_total(getattr(self, "page", 1), self.results, reported_total)
        if reported_total > 0 and effective_total > 0:
            self.total_results = effective_total
            total_pages = max(1, (effective_total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            target_page = ((target - 1) // ROWS_PER_PAGE) + 1
            if target_page > total_pages:
                self.status = f"Result must be 1-{effective_total}."
                return
            self.page = target_page
            self.sel_r = min(max(0, (target - 1) % ROWS_PER_PAGE), max(0, len(self.get_visible_results()) - 1))
            self.focus = "LIST"
            self.start_search_async(reset_page=False)
            self.status = f"Loading result {target}..."
            return
        visible = self.get_visible_results()
        if target > len(visible):
            self.status = f"Result must be 1-{len(visible)}."
            return
        self.sel_r = target - 1
        self.status = f"Selected result {target}."

    def get_menu_items(self) -> List[Tuple[str, str]]:
        theme = getattr(self, "theme_name", "Retro")
        if self.mode in ("RESULTS", "SEARCH"):
            selected = self.selected_result()
            if self.is_youtube_result(selected) or str(getattr(self, "search_source", "ia")).startswith("youtube"):
                return [
                    ("Actions", "actions"),
                    ("IA Search", "search"),
                    ("YT Search", "youtube_search"),
                    ("Source", "source_switch"),
                    ("YT URL", "youtube_url"),
                    ("Open", "open"),
                    ("Favs", "favs"),
                    ("Help", "help"),
                    ("Quit", "quit"),
                ]
            fav_label = "Fav Item"
            if selected and self.is_fav_item(selected.identifier):
                fav_label = "Unfav Item"
            return [
                ("Actions", "actions"),
                ("IA Search", "search"),
                ("YT Search", "youtube_search"),
                ("Source", "source_switch"),
                (f"Local: {getattr(self, 'result_filter', '') or 'Off'}", "result_filter"),
                ("Clear Local", "clear_result_filter"),
                ("Tools", "search_tools"),
                (f"Filter: {getattr(self, 'filter', 'any')}", "filter"),
                (f"Sort: {self._sort_label()}", "sort"),
                ("Prev", "prev_page"),
                ("Next", "next_page"),
                ("Open", "open"),
                (fav_label, "fav_item"),
                ("Favs", "favs"),
                (self.queue_menu_label(), "queue_view"),
                ("Help", "help"),
                ("Quit", "quit"),
            ]
        if self.mode == "FILES":
            loading_files = bool(getattr(self, "_file_load_loading", False))
            if loading_files:
                return [
                    ("Actions", "actions"),
                    ("Back", "back"),
                    ("Opening...", "noop"),
                    ("Favs", "favs"),
                    ("Help", "help"),
                    ("Quit", "quit"),
                ]
            item = self.selected_result()
            visible = self.get_visible_files()
            sel = visible[self.sel_f] if (visible and 0 <= self.sel_f < len(visible)) else None
            is_f = False
            if item and sel:
                is_f = self.is_fav_file(item.identifier, sel.name)
            fav_file_label = "Fav File" if not is_f else "Unfav File"
            return [
                ("Actions", "actions"),
                ("Back", "back"),
                ("Preview", "preview"),
                ("Download", "download"),
                ("Folder", "folder"),
                ("Item", "item"),
                (f"Video: {'On' if self.video_only else 'Off'}", "video_only"),
                (f"Filter: {self.file_kw or ('Video' if self.video_only else 'All')}", "keyword"),
                (f"Save to: {self.last_bucket}", "bucket"),
                (fav_file_label, "fav_file"),
                (f"Theme: {theme}", "theme"),
                ("Favs", "favs"),
                (self.queue_menu_label(), "queue_view"),
                ("Help", "help"),
                ("Quit", "quit"),
            ]
        if self.mode == "PREVIEW_DL":
            return [("Confirm", "confirm_download"), ("Cancel", "cancel_preview"), (f"Theme: {theme}", "theme")]
        if self.mode == "QUEUE":
            return [
                ("Back", "back"),
                ("Cancel", "queue_cancel"),
                ("Remove", "queue_remove"),
                ("Help", "help"),
                ("Quit", "quit"),
            ]
        if self.mode == "FAVS":
            return [
                ("Back", "back"),
                (f"Tab: {self.favs_tab}", "tab"),
                ("Open", "primary"),
                ("Remove", "remove"),
                (f"Theme: {theme}", "theme"),
                ("Help", "help"),
                ("Quit", "quit"),
            ]
        if self.mode in ("HELP", "TOO_SMALL"):
            return [("Back", "back"), ("Quit", "quit")]
        if self.mode == "ERROR":
            return [("Quit", "quit")]
        return [("Quit", "quit")]

    def draw_menu_bar(self, y: int, w: int) -> int:
        items = self.get_menu_items()
        if not items:
            return y

        selected = max(0, min(getattr(self, "menu_idx", 0), len(items) - 1))
        start = 0
        if self.focus == "MENU":
            used = 3 if selected > 0 else 0
            for i in range(selected, -1, -1):
                pill_len = len(f" {items[i][0]} ")
                if used + pill_len >= w - 5:
                    start = i + 1
                    break
                used += pill_len

        x = 0
        if start > 0 and w > 4:
            self.safe_addstr(y, x, "‹ ", curses.color_pair(3) | curses.A_BOLD)
            x += 2

        hidden_right = 0
        for i, (label, _action) in enumerate(items[start:], start=start):
            is_sel = (self.focus == "MENU" and i == self.menu_idx)
            pill = f" > {label} < " if is_sel else f" {label} "
            if x + len(pill) >= w - 1:
                hidden_right = len(items) - i
                break

            attr = curses.color_pair(6) | curses.A_DIM
            if is_sel:
                attr = curses.color_pair(1) | curses.A_BOLD

            self.safe_addstr(y, x, pill, attr)
            x += len(pill)

        if hidden_right and x < w - 7:
            more = f" +{hidden_right} › "
            self.safe_addstr(y, x, more[: max(0, w - 1 - x)], curses.color_pair(3) | curses.A_BOLD)
            x += len(more)

        if x < w - 1:
            self.safe_addstr(y, x, " " * (w - 1 - x), curses.color_pair(6) | curses.A_DIM)

        if self.focus == "MENU" and y + 1 < self.stdscr.getmaxyx()[0] - 1:
            label, _action = items[selected]
            parts = [f"MENU FOCUS: > {label} <", "Enter run", "Left/Right choose", "Tab list"]
            if start > 0 or hidden_right:
                parts.append(f"{start + hidden_right} hidden")
            line = "  |  ".join(parts)
            self.safe_addstr(y + 1, 0, line[: max(0, w - 1)].ljust(max(0, w - 1)), curses.color_pair(2) | curses.A_BOLD)
            return y + 2

        return y + 1

    def command_footer(self) -> str:
        if self.mode in ("RESULTS", "SEARCH"):
            if getattr(self, "show_welcome", False) and not getattr(self, "results", []):
                return "IA / or Search   YT menu   Source menu   Help ?   Quit q"
            if str(getattr(self, "search_source", "ia")).startswith("youtube"):
                return "Open Enter/o   IA /   YT menu   Source menu   Help ?   Quit q"
            return "Open Enter/o   IA /   Local l/f   Page [/]   Actions a   Help ?   Quit q"
        if self.mode == "FILES":
            return "Preview Enter/p   Mark Space/m   Download d   Filter f/F   Help ?   Quit q"
        if self.mode == "PREVIEW_DL":
            return "Confirm Enter   Cancel Esc/Backspace   Help ?   Quit q"
        if self.mode == "FAVS":
            return "Open Enter/o   Tab switch   Remove Del   Help ?   Quit q"
        if self.mode == "QUEUE":
            return "Cancel c   Remove x   Back Esc/Backspace/q   Help ?"
        return "j/k navigate   Enter select   a actions   ? help   q quit"

    def hint_bar(self, include_overlay_state: bool = True) -> str:
        if include_overlay_state and self.help_overlay:
            return "?/Esc closes help  |  Backspace back  |  q quit"
        if self.mode == "QUEUE":
            return "c cancel  |  x remove  |  j/k select  |  Backspace/q back  |  progress updates live"
        if self.mode in ("RESULTS", "SEARCH"):
            return "Enter/o open  |  / search  |  l/f local filter  |  L/F clear local  |  a actions  |  n/p page  |  Q queue  |  ? help  |  q quit"
        if self.mode == "FILES":
            return "Enter/p preview  |  o folder  |  Space mark+next  |  m range  |  d marked  |  D all visible  |  f filter  |  Q queue  |  ? help"
        if self.mode == "FAVS":
            return "Enter/o open  |  Tab tab  |  Backspace back  |  ? help  |  q quit"
        if self.mode == "PREVIEW_DL":
            return "Enter confirm  |  Esc/Backspace cancel  |  ? help  |  q quit"
        return "j/k navigate  |  Enter select  |  a actions  |  Tab menu/list  |  Backspace back  |  ? help  |  q quit"

    def footer_status_text(self) -> str:
        status = str(getattr(self, "status", "") or "")
        if (
            getattr(self, "mode", "") in ("RESULTS", "SEARCH")
            and getattr(self, "result_filter", "")
            and status.startswith("Local result filter:")
        ):
            visible_count = len(self.get_visible_results())
            progress = self.local_filter_progress_label()
            suffix = f"; {progress}" if progress else ""
            return f"Local result filter: {self.result_filter} ({visible_count} visible{suffix})"
        return status

    def draw_footer(self, h: int, w: int) -> None:
        if h < 4 or w < 2:
            return

        status = self.footer_status_text()[: max(0, w - 1)]
        self.safe_addstr(h - 3, 0, status.ljust(max(0, w - 1)), curses.color_pair(6))
        keybar = self.command_footer()
        self.safe_addstr(h - 2, 0, keybar[: max(0, w - 1)].ljust(max(0, w - 1)), curses.color_pair(2) | curses.A_BOLD)
        self.safe_addstr(h - 1, 0, ("═" * max(0, w - 1)), curses.color_pair(1))

    def prompt(self, label: str, default: str = "", history: Optional[List[str]] = None) -> Optional[str]:
        h, w = self.stdscr.getmaxyx()
        if h < 6 or w < 20:
            return None

        box_h = 3
        box_w = min(max(0, w - 4), max(40, int(w * 0.6)))
        box_w = max(box_w, min(max(0, w - 4), len(label) + 12))
        top = max(1, (h - box_h) // 2)
        left = max(1, (w - box_w) // 2)
        inner_w = max(1, box_w - 4)

        title = label.strip()
        if title.endswith(":"):
            title = title[:-1].strip()

        buf = list(default)
        pos = len(buf)
        hist_idx = -1
        saved_buf = ""
        view_start = 0
        self.stdscr.nodelay(False)
        try:
            try:
                self.stdscr.timeout(-1)
            except Exception:
                pass
            try:
                curses.curs_set(1)
            except Exception:
                pass
            while True:
                text = "".join(buf)
                if pos < view_start:
                    view_start = pos
                if pos > view_start + inner_w - 1:
                    view_start = pos - inner_w + 1
                visible_text = text[view_start : view_start + inner_w]

                for row in range(top, top + box_h):
                    self.safe_addstr(row, left, " " * max(0, box_w), curses.color_pair(6))

                top_border = "┌" + "─" * max(0, box_w - 2) + "┐"
                self.safe_addstr(top, left, top_border, curses.color_pair(2))
                t = f" {title} "
                self.safe_addstr(top, left + 2, t[: max(0, box_w - 4)], curses.color_pair(1) | curses.A_BOLD)

                self.safe_addstr(top + 1, left, "│", curses.color_pair(2))
                self.safe_addstr(top + 1, left + box_w - 1, "│", curses.color_pair(2))
                self.safe_addstr(top + 1, left + 2, visible_text.ljust(inner_w), curses.color_pair(8))

                bottom_border = "└" + "─" * max(0, box_w - 2) + "┘"
                self.safe_addstr(top + box_h - 1, left, bottom_border, curses.color_pair(2))
                hint = "Enter confirm  Esc cancel"
                if history:
                    hint += "  Up/Down history"
                hint_text = f" {hint} "
                self.safe_addstr(top + box_h - 1, left + 2, hint_text[: max(0, box_w - 4)], curses.color_pair(3))

                try:
                    self.stdscr.move(top + 1, left + 2 + (pos - view_start))
                except curses.error:
                    pass
                self.stdscr.refresh()

                ch = self.stdscr.getch()
                if is_enter_key(ch):
                    return "".join(buf).strip()
                if ch in (27,):
                    return None
                if ch == curses.KEY_UP and history:
                    if hist_idx == -1:
                        saved_buf = "".join(buf)
                    if hist_idx < len(history) - 1:
                        hist_idx += 1
                        buf = list(history[hist_idx])
                        pos = len(buf)
                elif ch == curses.KEY_DOWN and history:
                    if hist_idx > 0:
                        hist_idx -= 1
                        buf = list(history[hist_idx])
                        pos = len(buf)
                    elif hist_idx == 0:
                        hist_idx = -1
                        buf = list(saved_buf)
                        pos = len(buf)
                elif ch == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif ch == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif ch == curses.KEY_HOME or ch == 1:   # Ctrl+A
                    pos = 0
                elif ch == curses.KEY_END or ch == 5:    # Ctrl+E
                    pos = len(buf)
                elif ch == 21:                           # Ctrl+U — clear whole line
                    buf = []
                    pos = 0
                elif ch == 11:                           # Ctrl+K — clear to end
                    buf = buf[:pos]
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    if pos > 0:
                        buf.pop(pos - 1)
                        pos -= 1
                elif ch == curses.KEY_DC:               # Delete forward
                    if pos < len(buf):
                        buf.pop(pos)
                elif 32 <= ch <= 126:
                    buf.insert(pos, chr(ch))
                    pos += 1
        finally:
            try:
                self.stdscr.timeout(100)
            except Exception:
                pass
            try:
                curses.curs_set(0)
            except Exception:
                pass

    def prompt_list(self, title: str, options: List[str], default_idx: int = 0) -> Optional[str]:
        if not options:
            return None

        h, w = self.stdscr.getmaxyx()
        box_h = min(12, max(7, h - 6))
        box_w = min(w - 4, max(30, int(w * 0.85)))
        top = max(2, (h - box_h) // 2)
        left = max(2, (w - box_w) // 2)

        idx = max(0, min(default_idx, len(options) - 1))
        start = 0
        query_buf: List[str] = []
        query_pos = 0

        self.stdscr.nodelay(False)
        try:
            try:
                self.stdscr.timeout(-1)
            except Exception:
                pass
            try:
                curses.curs_set(1)
            except Exception:
                pass
            while True:
                query = "".join(query_buf)
                visible_options = self.filter_options(options, query)
                if idx >= len(visible_options):
                    idx = max(0, len(visible_options) - 1)
                if not visible_options:
                    idx = 0

                for y in range(top, top + box_h):
                    self.safe_addstr(y, left, " " * max(0, box_w), curses.color_pair(6))

                self.safe_addstr(top, left, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(2))
                self.safe_addstr(top + box_h - 1, left, "└" + "─" * (box_w - 2) + "┘", curses.color_pair(2))
                for y in range(top + 1, top + box_h - 1):
                    self.safe_addstr(y, left, "│", curses.color_pair(2))
                    self.safe_addstr(y, left + box_w - 1, "│", curses.color_pair(2))

                t = f" {title} "
                self.safe_addstr(top, left + 2, t[: max(0, box_w - 4)], curses.color_pair(1) | curses.A_BOLD)
                q = f" find: {query}"
                self.safe_addstr(top + 1, left + 2, q[: max(0, box_w - 4)], curses.color_pair(3))

                body_top = top + 2
                body_bottom = top + box_h - 2
                max_rows = max(1, body_bottom - body_top)

                if visible_options and idx < start:
                    start = idx
                if visible_options and idx >= start + max_rows:
                    start = idx - max_rows + 1
                if not visible_options:
                    start = 0

                if visible_options:
                    for i in range(start, min(len(visible_options), start + max_rows)):
                        row_y = body_top + (i - start)
                        s = visible_options[i]
                        line = f" {i+1:02d}. {s}"
                        line = line[: max(0, box_w - 2)].ljust(max(0, box_w - 2))
                        if i == idx:
                            self.safe_addstr(row_y, left + 1, line, curses.color_pair(9) | curses.A_BOLD)
                        else:
                            self.safe_addstr(row_y, left + 1, line, curses.color_pair(6))
                else:
                    empty = " No matches"
                    self.safe_addstr(body_top, left + 1, empty[: max(0, box_w - 2)].ljust(max(0, box_w - 2)), curses.color_pair(6))

                hint = "Type to filter  Up/Down choose  Enter select  Backspace edit  Ctrl+U clear  Esc cancel"
                self.safe_addstr(top + box_h - 1, left + 2, hint[: max(0, box_w - 4)], curses.color_pair(3))

                try:
                    self.stdscr.move(top + 1, min(left + box_w - 2, left + 2 + len(" find: ") + query_pos))
                except curses.error:
                    pass
                self.stdscr.refresh()
                ch = self.stdscr.getch()

                if ch in (27,):
                    return None
                if is_enter_key(ch):
                    if not visible_options:
                        continue
                    return visible_options[idx]
                if ch == curses.KEY_UP and visible_options:
                    idx = max(0, idx - 1)
                elif ch == curses.KEY_DOWN and visible_options:
                    idx = min(len(visible_options) - 1, idx + 1)
                elif ch == curses.KEY_LEFT:
                    query_pos = max(0, query_pos - 1)
                elif ch == curses.KEY_RIGHT:
                    query_pos = min(len(query_buf), query_pos + 1)
                elif ch == curses.KEY_HOME or ch == 1:   # Ctrl+A
                    query_pos = 0
                elif ch == curses.KEY_END or ch == 5:    # Ctrl+E
                    query_pos = len(query_buf)
                elif ch == 21:                           # Ctrl+U — clear whole line
                    query_buf = []
                    query_pos = 0
                    idx = 0
                    start = 0
                elif ch == 23:                           # Ctrl+W — delete previous word
                    while query_pos > 0 and query_buf[query_pos - 1].isspace():
                        query_buf.pop(query_pos - 1)
                        query_pos -= 1
                    while query_pos > 0 and not query_buf[query_pos - 1].isspace():
                        query_buf.pop(query_pos - 1)
                        query_pos -= 1
                    idx = 0
                    start = 0
                elif ch == 11:                           # Ctrl+K — clear to end
                    del query_buf[query_pos:]
                    idx = 0
                    start = 0
                elif is_backspace_key(ch):
                    if query_pos > 0:
                        query_buf.pop(query_pos - 1)
                        query_pos -= 1
                        idx = 0
                        start = 0
                elif ch == curses.KEY_DC:               # Delete forward
                    if query_pos < len(query_buf):
                        query_buf.pop(query_pos)
                        idx = 0
                        start = 0
                elif 32 <= ch <= 126:
                    query_buf.insert(query_pos, chr(ch))
                    query_pos += 1
                    idx = 0
                    start = 0
        finally:
            try:
                self.stdscr.timeout(100)
            except Exception:
                pass
            try:
                curses.curs_set(0)
            except Exception:
                pass

    def filter_options(self, options: List[str], query: str) -> List[str]:
        needle = (query or "").strip().lower()
        if not needle:
            return list(options)
        terms = [t for t in needle.split() if t]
        return [opt for opt in options if all(self.fuzzy_match(str(opt).lower(), term) for term in terms)]

    def fuzzy_match(self, text: str, pattern: str) -> bool:
        if not pattern:
            return True
        if pattern in text:
            return True
        pos = 0
        for ch in pattern:
            found = text.find(ch, pos)
            if found < 0:
                return False
            pos = found + 1
        return True

    def prefix_suggestions_for_file(self, filename: str) -> List[str]:
        name = (filename or "").strip()
        suggestions: List[str] = []
        if not name:
            return suggestions
        parts = [p for p in name.split("/") if p]
        if len(parts) > 1:
            acc = ""
            for part in parts[:-1]:
                acc = f"{acc}{part}/"
                suggestions.append(acc)
        base = os.path.basename(name)
        stem, _ext = os.path.splitext(base)
        for sep in (" - ", "_", "."):
            if sep in stem:
                chunk = stem.split(sep)[0].strip()
                if len(chunk) >= 3:
                    suggestions.append(chunk)
        deduped: List[str] = []
        seen = set()
        for s in suggestions:
            if s and s not in seen:
                deduped.append(s)
                seen.add(s)
        return deduped[:12]

    def results_action_specs(self) -> List[Tuple[str, str, str]]:
        try:
            selected = self.selected_result()
        except AttributeError:
            selected = None
        if self.is_youtube_result(selected) or str(getattr(self, "search_source", "ia")).startswith("youtube"):
            return [
                ("Open / selected YouTube video", "open", "Enter/o"),
                ("Open / result details", "details", "r"),
                ("Search / new IA query (/ find archive)", "search", "/ or s"),
                ("Search / combined IA + YouTube", "combined_search", None),
                ("Search / YouTube via yt-dlp", "youtube_search", None),
                ("Search / source chooser", "source_switch", None),
                ("Search / YouTube direct URL", "youtube_url", None),
                ("App / favorites (saved items files folders)", "favs", None),
                ("App / theme (retro minimal high contrast)", "theme", "T"),
                ("App / help (? shortcuts)", "help", "?"),
                ("App / quit (exit)", "quit", "q"),
            ]
        return [
            ("Open / selected result (open enter item files)", "open", "Enter/o"),
            ("Open / result details (metadata rights description)", "details", "r"),
            ("Search / new query (/ find archive)", "search", "/ or s"),
            ("Search / combined IA + YouTube", "combined_search", None),
            ("Search / YouTube via yt-dlp", "youtube_search", None),
            ("Search / source chooser", "source_switch", None),
            ("Search / YouTube direct URL", "youtube_url", None),
            ("Search / tools (history fields collections local filter)", "search_tools", "a"),
            ("Search / attempts (inspect or re-run fallback query)", "search_attempts", None),
            ("Local / clear filter", "clear_result_filter", "L/F"),
            ("Search / collections (mediatype collection)", "collection_search", None),
            ("Search / fields (title creator subject date collection)", "field_search", None),
            ("Search / inside collection (collection identifier)", "within_collection", None),
            ("Search / result collections (facet narrow)", "collection_facets", None),
            ("Filter / media type (movies audio texts software any)", "filter", None),
            ("Filter / local result refine (loaded results)", "result_filter", "l/f"),
            ("Filter / title-only mode (title exact)", "title", None),
            (
                f"Filter / hide small items ({'on' if getattr(self, 'hide_small_items', False) else 'off'}, "
                f"<{getattr(self, 'min_item_size_mb', 0)}MB)",
                "toggle_hide_small_items",
                None,
            ),
            ("Filter / min item size (MB)", "edit_min_item_size", None),
            ("Filter / license gate (rights license block)", "license_gate", None),
            ("Sort / result order (date downloads title relevance)", "sort", None),
            ("App / audit summary (library health counts)", "audit", "y"),
            ("App / favorite selected item", "fav_item", None),
            ("Page / previous (prev older [)", "prev_page", "p/["),
            ("Page / next (next more ])", "next_page", "n/]"),
            ("App / favorites (saved items files folders)", "favs", None),
            ("App / theme (retro minimal high contrast)", "theme", "T"),
            ("App / help (? shortcuts)", "help", "?"),
            ("App / quit (exit)", "quit", "q"),
        ]

    def files_action_specs(self) -> List[Tuple[str, str, Optional[str]]]:
        return [
            ("Open / preview selected file", "preview", "Enter/p"),
            ("Select / toggle file mark", "toggle_file_mark", "Space"),
            ("Select / mark file range", "mark_file_range", "m"),
            ("Select / mark all visible files", "mark_all_visible", "A"),
            ("Select / invert visible marks", "invert_visible_marks", "I"),
            ("Select / clear marked files", "clear_file_marks", "U"),
            ("Download / marked files batch queue", "download", "d"),
            ("Download / retry failed files retry failed", "retry_failed", "R"),
            ("Download / folder prefix folder", "folder", "o"),
            ("Download / all visible files all", "item", "D"),
            ("Filter / file filter menu", "keyword", "f/F"),
            ("Filter / video only", "video_only", "v"),
            (
                f"Filter / hide small videos ({'on' if getattr(self, 'hide_small_video_files', False) else 'off'}, "
                f"<{getattr(self, 'min_video_file_size_mb', 0)}MB)",
                "toggle_hide_small_video_files",
                None,
            ),
            ("Filter / min video file size (MB)", "edit_min_video_file_size", None),
            ("Download / save bucket folder", "bucket", None),
            ("App / audit summary (library health counts)", "audit", "y"),
            ("Filter / rights license", "license_gate", None),
            ("Alias / movie video", "video_only", "v"),
            ("Alias / audio keyword filter", "keyword", "f/F"),
            ("Alias / all", "item", None),
            ("Alias / clear", "clear_file_marks", "U"),
            ("Alias / queue", "download", None),
            ("App / theme retro minimal high contrast", "theme", "T"),
            ("App / favorites", "favs", None),
            ("App / back", "back", "Backspace"),
            ("App / help", "help", "?"),
            ("App / quit", "quit", "q"),
        ]

    def action_palette_options(self) -> List[Tuple[str, str]]:
        if self.mode in ("RESULTS", "SEARCH"):
            return [(label, action) for label, action, _hint in self.results_action_specs()]
        if self.mode == "FILES":
            return [(label, action) for label, action, _hint in self.files_action_specs()]
        if self.mode == "FAVS":
            return [
                ("Open / selected favorite", "primary"),
                ("Filter / favorites tab", "tab"),
                ("App / remove favorite", "remove"),
                ("App / audit summary (library health counts)", "audit"),
                ("App / theme retro minimal high contrast", "theme"),
                ("App / back", "back"),
                ("App / help", "help"),
                ("App / quit", "quit"),
            ]
        if self.mode == "PREVIEW_DL":
            return [
                ("Download / confirm", "confirm_download"),
                ("App / theme retro minimal high contrast", "theme"),
                ("App / cancel", "cancel_preview"),
            ]
        return self.get_menu_items()

    def open_action_palette(self) -> None:
        options = self.action_palette_options()
        labels = [label for label, _action in options]
        pick = self.prompt_list("Actions", labels)
        if not pick:
            self.status = "Action canceled."
            return
        for label, action in options:
            if label == pick:
                self.activate_menu_action(action)
                return

    def toggle_help_overlay(self) -> None:
        self.help_overlay = not self.help_overlay
        self.status = "Help overlay" if self.help_overlay else "Help closed"

    def cycle_theme(self) -> None:
        order = ["Retro", "Minimal", "High contrast"]
        try:
            i = order.index(getattr(self, "theme_name", "Retro"))
        except ValueError:
            i = 0
        self.theme_name = order[(i + 1) % len(order)]
        try:
            self.init_colors()
        except Exception:
            pass
        self.status = f"Theme: {self.theme_name}"

    # ---------- logic ----------
    def choose_filter(self) -> bool:
        current_idx = FILTERS.index(self.filter) if self.filter in FILTERS else 0
        pick = self.prompt_list("Media filter", FILTERS, default_idx=current_idx)
        if pick is None:
            self.status = "Filter unchanged."
            return False
        if pick == self.filter:
            self.status = f"Filter unchanged: {self.filter}"
            return False
        self.filter = pick
        self.query_text = replace_mediatype_filter(getattr(self, "query_text", ""), self.filter)
        built_query = getattr(self, "query_built", "")
        if built_query:
            rewritten = replace_mediatype_filter(built_query, self.filter)
            self.query_built = rewritten if rewritten != built_query else ""
        else:
            self.query_built = ""
        self.status = f"Filter set to: {self.filter}"
        return True

    def choose_sort(self) -> bool:
        labels = [label for label, _value in SORT_OPTIONS]
        values = [value for _label, value in SORT_OPTIONS]
        try:
            current_idx = values.index(self.sort_by)
        except ValueError:
            current_idx = 0
        pick = self.prompt_list("Sort order", labels, default_idx=current_idx)
        if pick is None:
            self.status = "Sort unchanged."
            return False
        chosen = SORT_OPTIONS[labels.index(pick)]
        label, value = chosen
        if value == self.sort_by:
            self.status = f"Sort unchanged: {label}"
            return False
        self.sort_by = value
        self.status = f"Sort: {label}"
        return True

    def _add_to_history(self, query: str) -> None:
        q = query.strip()
        if not q:
            return
        self.search_history = [q] + [h for h in self.search_history if h != q]
        self.search_history = self.search_history[:MAX_HISTORY]

    def search_hint_text(self, used_label: str, results: List[SearchResult]) -> str:
        if used_label == "title-any-type":
            # The exact title only turned up outside the requested media
            # filter -- say so plainly instead of silently falling back to a
            # bag-of-words match still scoped to the (wrong) filter, which
            # for common-word titles returns near-random results.
            mediatypes = sorted({r.mediatype for r in results[:5] if r.mediatype and r.mediatype != self.filter})
            found_as = f" as {'/'.join(mediatypes)}" if mediatypes else ""
            return f" (not found in '{self.filter}' — matched{found_as}; switch Filter to see it)"
        if used_label in ("", "title", "custom", "advanced"):
            return ""
        return f" ({used_label} match)"

    def do_search(self, reset_page: bool = True, built_query: Optional[str] = None) -> None:
        self.cancel_file_load()
        self.cancel_result_prefetch()
        self.search_source = "ia"
        if reset_page:
            self.page = 1
        attempts = [("custom", built_query)] if built_query is not None else build_query_attempts(self.query_text, self.filter, self.title_only)
        attempts = [(label, query) for label, query in attempts if query]
        if not attempts:
            self.query_built = ""
            self.status = "Select [Search] in the menu to search."
            return

        previous_query_text = getattr(self, "last_search_text", "")
        preserve_local_filter = built_query is None and bool(previous_query_text) and previous_query_text == self.query_text
        self.last_search_text = self.query_text
        self._add_to_history(self.query_text)
        self._save_session()
        self.status = "Searching..."
        self.render()

        self.last_search_attempts = list(attempts)
        used_label = ""
        last_err = ""
        previous_cache_key = self._search_cache_key()
        self.results = []
        self.total_results = 0
        rerank_text, rerank_filter = self._rerank_args(custom=built_query is not None)
        for label, query in attempts:
            self.query_built = query
            self.results, self.total_results, err = ia_search_via_curl(
                query,
                rows=ROWS_PER_PAGE,
                page=self.page,
                sort=self.sort_by,
                rerank_text=rerank_text,
                media_filter=rerank_filter,
                min_item_size_bytes=self._search_min_item_size_bytes(),
            )
            if err:
                last_err = err
                break
            used_label = label
            if self.results or self.total_results:
                break

        if last_err:
            self.status = last_err
            return

        self.total_results = self.effective_search_total(self.page, self.results, self.total_results)
        current_key = self._search_cache_key()
        if current_key != previous_cache_key:
            self._reset_search_cache()
        total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE) if self.total_results else 1
        self._prime_search_cache(current_key, self.page, self.results, total_pages)

        self.sel_r = 0
        if not preserve_local_filter:
            self.result_filter = ""
        self.mode = "RESULTS"
        self.focus = "LIST"
        self.last_search_text = self.query_text
        self.last_search_used_label = used_label or ""
        search_hint = self.search_hint_text(used_label, self.results)
        if self.total_results > 0:
            total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            self.status = f"Page {self.page}/{total_pages} — {self.total_results} total results{search_hint}. Arrows to select, Enter to open."
        else:
            self.status = f"Page {self.page} — {len(self.results)} results{search_hint}. Arrows to select, Enter to open."

    def start_search_async(
        self,
        reset_page: bool = True,
        built_query: Optional[str] = None,
        built_query_label: str = "custom",
        attempts_display: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        self._ensure_search_load_state()
        self.cancel_file_load()
        self.cancel_result_prefetch()
        self.search_source = "ia"
        if reset_page:
            self.page = 1
        attempts = [(built_query_label, built_query)] if built_query is not None else build_query_attempts(self.query_text, self.filter, self.title_only)
        attempts = [(label, query) for label, query in attempts if query]
        display_attempts = [(label, query) for label, query in (attempts_display or attempts) if query]
        if not attempts:
            self.query_built = ""
            self.status = "Select [Search] in the menu to search."
            return

        previous_query_text = getattr(self, "last_search_text", "")
        preserve_local_filter = built_query is None and bool(previous_query_text) and previous_query_text == self.query_text
        self.last_search_text = self.query_text
        self.query_built = attempts[0][1]
        self._add_to_history(self.query_text)
        self._save_session()
        self.show_welcome = False
        self.status = "Searching... press Esc to cancel waiting."
        self.last_search_attempts = list(display_attempts)
        previous_cache_key = self._search_cache_key()

        with self._search_load_lock:
            self._search_load_token += 1
            token = self._search_load_token
            self._search_load_loading = True
            self._search_load_result = {
                "pending": True,
                "source": "ia",
                "previous_cache_key": previous_cache_key,
                "preserve_local_filter": preserve_local_filter,
                "page": self.page,
            }

        page = self.page
        sort_by = self.sort_by
        rerank_text, rerank_filter = self._rerank_args(custom=built_query is not None)
        min_item_size_bytes = self._search_min_item_size_bytes()

        def worker() -> None:
            used_label = ""
            last_err = ""
            final_results: List[SearchResult] = []
            final_total = 0
            final_query = ""
            for label, query in attempts:
                results, total, err = ia_search_via_curl(
                    query,
                    rows=ROWS_PER_PAGE,
                    page=page,
                    sort=sort_by,
                    rerank_text=rerank_text,
                    media_filter=rerank_filter,
                    min_item_size_bytes=min_item_size_bytes,
                )
                if err:
                    last_err = err
                    break
                used_label = label
                final_query = query
                final_results = results
                final_total = total
                if results or total:
                    break
            result: Dict[str, Any] = {
                "source": "ia",
                "err": last_err,
                "results": final_results,
                "total": final_total,
                "query": final_query,
                "used_label": used_label,
                "attempts": list(display_attempts),
                "previous_cache_key": previous_cache_key,
                "preserve_local_filter": preserve_local_filter,
                "page": page,
            }
            with self._search_load_lock:
                if token == self._search_load_token:
                    self._search_load_result = result
                    self._search_load_loading = False

        thread = threading.Thread(target=worker, daemon=True)
        with self._search_load_lock:
            self._search_load_thread = thread
        thread.start()

    def start_combined_search_async(self, query_text: str) -> None:
        self._ensure_search_load_state()
        self.cancel_file_load()
        terms = str(query_text or "").strip()
        if not terms:
            self.status = "Combined search canceled."
            return

        self.cancel_result_prefetch()
        self.search_source = "all"
        self.page = 1
        self.query_text = terms
        self.query_built = terms
        self.show_welcome = False
        self._add_to_history(terms)
        self._save_session()
        self.status = "Searching IA + YouTube... press Esc to cancel waiting."

        attempts = [(label, query) for label, query in build_query_attempts(terms, self.filter, self.title_only) if query]
        previous_cache_key = self._search_cache_key()

        with self._search_load_lock:
            self._search_load_token += 1
            token = self._search_load_token
            self._search_load_loading = True
            self._search_load_result = {"pending": True, "source": "all"}

        sort_by = self.sort_by
        rerank_text, rerank_filter = self._rerank_args()
        min_item_size_bytes = self._search_min_item_size_bytes()

        def worker() -> None:
            ia_results: List[SearchResult] = []
            ia_total = 0
            ia_query = ""
            ia_label = ""
            ia_err = ""
            for label, query in attempts:
                results, total, err = ia_search_via_curl(
                    query,
                    rows=ROWS_PER_PAGE,
                    page=1,
                    sort=sort_by,
                    rerank_text=rerank_text,
                    media_filter=rerank_filter,
                    min_item_size_bytes=min_item_size_bytes,
                )
                if err:
                    ia_err = err
                    break
                ia_label = label
                ia_query = query
                ia_results = results
                ia_total = total
                if results or total:
                    break

            yt_results, _yt_total, yt_err = yt_search(terms, rows=10)
            merged = list(ia_results) + list(yt_results)
            err = ""
            if not merged and ia_err and yt_err:
                err = f"IA: {ia_err}; YouTube: {yt_err}"

            result: Dict[str, Any] = {
                "source": "all",
                "err": err,
                "results": merged,
                "total": len(merged),
                "query": ia_query or terms,
                "used_label": "combined",
                "attempts": list(attempts) + [("youtube", f"ytsearch10:{terms}")],
                "previous_cache_key": previous_cache_key,
                "page": 1,
                "ia_count": len(ia_results),
                "yt_count": len(yt_results),
                "ia_total": ia_total,
                "ia_err": ia_err,
                "yt_err": yt_err,
            }
            with self._search_load_lock:
                if token == self._search_load_token:
                    self._search_load_result = result
                    self._search_load_loading = False

        thread = threading.Thread(target=worker, daemon=True)
        with self._search_load_lock:
            self._search_load_thread = thread
        thread.start()

    def start_youtube_search_async(self, query_text: str) -> None:
        self._ensure_search_load_state()
        self.cancel_file_load()
        self.cancel_result_prefetch()
        self.search_source = "youtube"
        self.page = 1
        self.query_text = query_text
        self.query_built = f"ytsearch10:{query_text}"
        self.show_welcome = False
        self._add_to_history(query_text)
        self._save_session()
        self.status = "Searching YouTube... press Esc to cancel waiting."

        with self._search_load_lock:
            self._search_load_token += 1
            token = self._search_load_token
            self._search_load_loading = True
            self._search_load_result = {"pending": True, "source": "youtube"}

        def worker() -> None:
            results, total, err = yt_search(query_text, rows=10)
            with self._search_load_lock:
                if token == self._search_load_token:
                    self._search_load_result = {
                        "source": "youtube",
                        "err": err,
                        "results": results,
                        "total": total,
                        "query": self.query_built,
                        "used_label": "youtube",
                        "attempts": [("youtube", self.query_built)],
                    }
                    self._search_load_loading = False

        thread = threading.Thread(target=worker, daemon=True)
        with self._search_load_lock:
            self._search_load_thread = thread
        thread.start()

    def start_youtube_url_async(self, url: str) -> None:
        self._ensure_search_load_state()
        self.cancel_file_load()
        self.cancel_result_prefetch()
        self.search_source = "youtube_url"
        self.page = 1
        self.query_text = url
        self.query_built = url
        self.show_welcome = False
        self._add_to_history(url)
        self._save_session()
        self.status = "Fetching YouTube metadata... press Esc to cancel waiting."

        with self._search_load_lock:
            self._search_load_token += 1
            token = self._search_load_token
            self._search_load_loading = True
            self._search_load_result = {"pending": True, "source": "youtube_url"}

        def worker() -> None:
            result, err = yt_metadata_url(url)
            results = [result] if result else []
            with self._search_load_lock:
                if token == self._search_load_token:
                    self._search_load_result = {
                        "source": "youtube_url",
                        "err": err,
                        "results": results,
                        "total": len(results),
                        "query": url,
                        "used_label": "youtube-url",
                        "attempts": [("youtube-url", url)],
                    }
                    self._search_load_loading = False

        thread = threading.Thread(target=worker, daemon=True)
        with self._search_load_lock:
            self._search_load_thread = thread
        thread.start()

    def finish_search_load_if_ready(self) -> bool:
        self._ensure_search_load_state()
        with self._search_load_lock:
            if self._search_load_loading:
                return False
            result = self._search_load_result
            self._search_load_result = None
        if not result or result.get("pending"):
            return False

        err = str(result.get("err") or "")
        source = str(result.get("source") or "ia")
        if err:
            label = "YouTube search" if source == "youtube" else "YouTube URL metadata" if source == "youtube_url" else "Search"
            self.set_error_status(err, detail=f"{label} failed: {err}")
            return True

        self.results = list(result.get("results") or [])
        self.total_results = int(result.get("total") or len(self.results))
        self.total_results = self.effective_search_total(int(result.get("page") or self.page or 1), self.results, self.total_results)
        self.query_built = str(result.get("query") or self.query_built)
        self.last_search_attempts = list(result.get("attempts") or [])
        self.last_search_used_label = str(result.get("used_label") or "")
        self.sel_r = 0
        self.mode = "RESULTS"
        self.focus = "LIST"
        self._reset_search_cache()
        if source == "ia":
            if not bool(result.get("preserve_local_filter")):
                self.result_filter = ""
            current_key = self._search_cache_key()
            total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE) if self.total_results else 1
            self._prime_search_cache(current_key, int(result.get("page") or self.page or 1), self.results, total_pages)
            search_hint = self.search_hint_text(self.last_search_used_label, self.results)
            if self.total_results > 0:
                self.status = f"Page {self.page}/{total_pages} — {self.total_results} total results{search_hint}. Arrows to select, Enter to open."
            else:
                self.status = f"Page {self.page} — {len(self.results)} results{search_hint}. Arrows to select, Enter to open."
        elif source == "youtube":
            self.result_filter = ""
            self.status = youtube_result_status(self.results)
        elif source == "all":
            self.result_filter = ""
            ia_count = int(result.get("ia_count") or 0)
            yt_count = int(result.get("yt_count") or 0)
            bits = []
            ia_err = str(result.get("ia_err") or "")
            yt_err = str(result.get("yt_err") or "")
            if ia_err and yt_count:
                bits.append("IA failed")
            if yt_err and ia_count:
                bits.append("YouTube failed")
            suffix = f" ({'; '.join(bits)})" if bits else ""
            self.status = f"Combined — {len(self.results)} result(s): IA {ia_count}, YouTube {yt_count}.{suffix}"
        else:
            self.result_filter = ""
            self.status = "YouTube URL loaded. Open the [YT] row to preview/download."
        return True

    def do_youtube_search(self, query_text: str) -> None:
        self.cancel_file_load()
        self.cancel_result_prefetch()
        self.search_source = "youtube"
        self.page = 1
        self.query_text = query_text
        self.query_built = f"ytsearch10:{query_text}"
        self.show_welcome = False
        self._add_to_history(query_text)
        self._save_session()
        self.status = "Searching YouTube..."
        self.render()

        results, total, err = yt_search(query_text, rows=10)
        if err:
            self.set_error_status(err, detail=f"YouTube search failed: {err}")
            return

        self._reset_search_cache()
        self.results = results
        self.total_results = total
        self.sel_r = 0
        self.result_filter = ""
        self.mode = "RESULTS"
        self.focus = "LIST"
        self.last_search_text = query_text
        self.last_search_used_label = "youtube"
        self.last_search_attempts = [("youtube", self.query_built)]
        self.status = youtube_result_status(results)

    def do_youtube_url(self, url: str) -> None:
        self.cancel_file_load()
        self.cancel_result_prefetch()
        self.search_source = "youtube_url"
        self.page = 1
        self.query_text = url
        self.query_built = url
        self.show_welcome = False
        self._add_to_history(url)
        self._save_session()
        self.status = "Fetching YouTube metadata..."
        self.render()

        result, err = yt_metadata_url(url)
        if err or not result:
            self.set_error_status(err or "YouTube metadata failed", detail=f"YouTube URL metadata failed: {err}")
            return

        self._reset_search_cache()
        self.results = [result]
        self.total_results = 1
        self.sel_r = 0
        self.result_filter = ""
        self.mode = "RESULTS"
        self.focus = "LIST"
        self.last_search_text = url
        self.last_search_used_label = "youtube-url"
        self.last_search_attempts = [("youtube-url", url)]
        self.status = "YouTube URL loaded. Open the [YT] row to preview/download."

    def next_page(self) -> None:
        source = str(getattr(self, "search_source", "ia"))
        if source.startswith("youtube") or source == "all":
            self.status = "This search source loads one page at a time."
            return
        if not self.query_text:
            self.status = "No search yet. Choose [Search]."
            return
        effective_total = self.effective_search_total(self.page, self.results, self.total_results)
        if effective_total > 0:
            self.total_results = effective_total
            total_pages = max(1, (effective_total + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            if self.page >= total_pages:
                self.status = "Already on last page."
                return
        saved_focus = self.focus
        saved_menu_idx = self.menu_idx
        self.page += 1
        self.start_search_async(reset_page=False)
        # Keep menu focus so the user can immediately paginate again.
        self.focus = saved_focus
        self.menu_idx = saved_menu_idx

    def prev_page(self) -> None:
        source = str(getattr(self, "search_source", "ia"))
        if source.startswith("youtube") or source == "all":
            self.status = "Already on first page for this search source."
            return
        if not self.query_text:
            self.status = "No search yet. Choose [Search]."
            return
        if self.page <= 1:
            self.status = "Already on first page."
            return
        saved_focus = self.focus
        saved_menu_idx = self.menu_idx
        self.page -= 1
        self.start_search_async(reset_page=False)
        # Keep menu focus so the user can immediately paginate again.
        self.focus = saved_focus
        self.menu_idx = saved_menu_idx

    def _ensure_file_load_state(self) -> None:
        if not hasattr(self, "_file_load_lock") or getattr(self, "_file_load_lock", None) is None:
            self._file_load_lock = threading.RLock()
        if not hasattr(self, "_file_load_token"):
            self._file_load_token = 0
        if not hasattr(self, "_file_load_loading"):
            self._file_load_loading = False
        if not hasattr(self, "_file_load_result"):
            self._file_load_result = None
        if not hasattr(self, "_file_load_thread"):
            self._file_load_thread = None

    def cancel_file_load(self) -> None:
        self._ensure_file_load_state()
        with self._file_load_lock:
            self._file_load_token += 1
            self._file_load_loading = False
            self._file_load_result = None

    def _start_file_load(self, item: SearchResult) -> None:
        self._ensure_file_load_state()
        ident = item.identifier
        title = item.title
        with self._file_load_lock:
            if self._file_load_loading:
                current = self._file_load_result or {}
                if current.get("identifier") == ident:
                    self.status = f"Still loading files for {ident}..."
                    return
            self._file_load_token += 1
            token = self._file_load_token
            self._file_load_loading = True
            self._file_load_result = {"identifier": ident, "title": title, "item": replace(item), "pending": True}

        self.cur_meta = None
        self.files = []
        self.file_owner_item = None
        self.sel_f = 0
        self.preview_item = item
        self.mode = "FILES"
        self.focus = "LIST"
        self.status = f"Loading file list for {ident}..."

        def worker() -> None:
            try:
                files, meta, err = ia_files(ident)
                result: Dict[str, Any] = {
                    "identifier": ident,
                    "title": title,
                    "item": replace(item),
                    "files": files,
                    "meta": meta,
                    "err": err,
                    "exc": "",
                }
            except Exception as e:
                result = {
                    "identifier": ident,
                    "title": title,
                    "item": replace(item),
                    "files": [],
                    "meta": None,
                    "err": f"File load failed for {ident}: {e}",
                    "exc": repr(e),
                }
            with self._file_load_lock:
                if token != self._file_load_token:
                    return
                self._file_load_result = result
                self._file_load_loading = False

        thread = threading.Thread(target=worker, daemon=True)
        with self._file_load_lock:
            self._file_load_thread = thread
        thread.start()

    def finish_file_load_if_ready(self) -> bool:
        self._ensure_file_load_state()
        with self._file_load_lock:
            if self._file_load_loading or not self._file_load_result:
                return False
            result = self._file_load_result
            self._file_load_result = None

        ident = str(result.get("identifier") or "")
        err = str(result.get("err") or "")
        if err:
            detail = str(result.get("exc") or f"File load failed for {ident}: {err}")
            self.set_error_status(err, detail=detail)
            self.mode = "FILES"
            self.focus = "LIST"
            return True

        self.last_error_detail = ""
        self.cur_meta = result.get("meta")
        self.files = list(result.get("files") or [])
        owner = result.get("item")
        self.file_owner_item = owner if isinstance(owner, SearchResult) and owner.identifier == ident else None
        if self.file_owner_item is None:
            self.set_error_status("File list owner is unavailable; reload the item.")
            return True
        self.restore_file_view_state(ident)
        self.mode = "FILES"
        self.focus = "LIST"
        all_videos = self._all_recognized_video_files()
        if all_videos and not self._eligible_video_files():
            self.status = (
                f"No video files meet the {self.min_video_file_size_mb}MB minimum "
                f"({len(all_videos)} filtered -- toggle 'hide small videos' to show)."
            )
        else:
            self.status = "Use arrows to choose a file, then [Preview], [Folder], [Item], or [Download]."
        return True

    def load_files(self, async_load: bool = False) -> None:
        self.save_current_file_view_state()
        item = self.selected_result()
        if not item:
            self.status = "No results to open."
            return
        self._sync_page_to_result(item)
        if self.is_youtube_result(item):
            self.cancel_file_load()
            self.cur_meta = {"source": "youtube", "webpage_url": item.webpage_url, "id": item.video_id}
            self.files = [self.youtube_file_for_result(item)]
            self.file_owner_item = replace(item)
            self.restore_file_view_state(item.identifier)
            self.mode = "FILES"
            self.focus = "LIST"
            self.preview_item = item
            self.status = "YouTube video ready. Preview then confirm to download with yt-dlp."
            return
        if async_load:
            self._start_file_load(item)
            return
        self.status = f"Loading files for {item.identifier}..."
        self.render()

        try:
            files, meta, err = ia_files(item.identifier)
        except Exception as e:
            self.set_error_status(f"File load failed for {item.identifier}: {e}", detail=repr(e))
            return
        if err:
            self.set_error_status(err, detail=f"File load failed for {item.identifier}: {err}")
            return

        self.last_error_detail = ""
        self.cur_meta = meta
        self.files = files
        self.file_owner_item = replace(item)
        self.restore_file_view_state(item.identifier)
        self.mode = "FILES"
        self.focus = "LIST"
        self.status = "Use arrows to choose a file, then [Preview], [Folder], [Item], or [Download]."

    def open_selected_result(self) -> None:
        self.show_welcome = False
        self.load_files(async_load=True)

    def save_current_file_view_state(self) -> None:
        if self.mode != "FILES":
            return
        if not hasattr(self, "file_view_state"):
            return
        item = getattr(self, "file_owner_item", None)
        if not item:
            return
        if not item:
            return
        ordered_selected = self._ordered_selected_file_names()
        self.file_view_state[item.identifier] = {
            "file_kw": self.file_kw,
            "video_only": self.video_only,
            "sel_f": self.sel_f,
            "selected_file_names": ordered_selected,
            "selected_file_order": ordered_selected,
        }

    def restore_file_view_state(self, identifier: str) -> None:
        state = self.file_view_state.get(identifier, {})
        self.file_kw = str(state.get("file_kw") or "")
        self.video_only = bool(state.get("video_only", False))
        self.sel_f = max(0, int(state.get("sel_f") or 0))
        valid_names = {f.name for f in self.files}
        ordered = [
            str(name)
            for name in (state.get("selected_file_order") or state.get("selected_file_names") or [])
            if str(name) in valid_names
        ]
        ordered = list(dict.fromkeys(ordered))
        self.selected_file_order = ordered
        self.selected_file_names = set(ordered)

    def _ensure_selection_state(self) -> None:
        if not hasattr(self, "selected_file_names") or self.selected_file_names is None:
            self.selected_file_names = set()
        if not hasattr(self, "selected_file_order") or self.selected_file_order is None:
            self.selected_file_order = []

    def _ordered_selected_file_names(self) -> List[str]:
        self._ensure_selection_state()
        ordered: List[str] = []
        seen = set()
        for name in self.selected_file_order:
            if name in self.selected_file_names and name not in seen:
                ordered.append(name)
                seen.add(name)
        if ordered:
            return ordered
        return [f.name for f in self.files if f.name in self.selected_file_names]

    def _mark_file_name(self, name: str) -> bool:
        self._ensure_selection_state()
        if name in self.selected_file_names:
            return False
        self.selected_file_names.add(name)
        self.selected_file_order.append(name)
        return True

    def _unmark_file_name(self, name: str) -> bool:
        self._ensure_selection_state()
        if name not in self.selected_file_names:
            return False
        self.selected_file_names.remove(name)
        self.selected_file_order = [n for n in self.selected_file_order if n != name]
        return True

    def _clear_selection(self) -> None:
        self._ensure_selection_state()
        self.selected_file_names.clear()
        self.selected_file_order.clear()

    def _min_video_file_size_bytes(self) -> int:
        """Bytes threshold for the configured min_video_file_size_mb, or 0
        when the hide-small-videos gate is off. Mirrors
        _search_min_item_size_bytes so the MB->bytes conversion can't drift
        out of sync between the two independent thresholds."""
        if not getattr(self, "hide_small_video_files", False):
            return 0
        return max(0, int(getattr(self, "min_video_file_size_mb", 0) or 0)) * 1024 * 1024

    def _passes_video_size_filter(self, f: "IAFile") -> bool:
        """Non-video files (subtitles, metadata, thumbnails, images, audio,
        etc.) are always eligible here -- this only gates recognized video
        files. A known size below the threshold is excluded; an unknown/zero
        size fails open rather than hiding a file we can't judge."""
        if not is_video_file(f.name, f.fmt):
            return True
        min_bytes = self._min_video_file_size_bytes()
        if min_bytes <= 0:
            return True
        size = int(getattr(f, "size", 0) or 0)
        return size <= 0 or size >= min_bytes

    def _all_recognized_video_files(self) -> List[IAFile]:
        """All video files in this item after dedup, ignoring the
        video_only/keyword view filters -- used to tell "no video in this
        item" apart from "every video is below the size minimum"."""
        files = deduplicate_file_variants(list(self.files))
        return [f for f in files if is_video_file(f.name, f.fmt)]

    def _eligible_video_files(self) -> List[IAFile]:
        return [f for f in self._all_recognized_video_files() if self._passes_video_size_filter(f)]

    def get_visible_files(self) -> List[IAFile]:
        files = deduplicate_file_variants(list(self.files))
        if self.video_only:
            files = [f for f in files if is_video_file(f.name, f.fmt)]
        files = [f for f in files if self._passes_video_size_filter(f)]
        kw = self.file_kw.strip()
        if kw:
            rx = re.compile(re.escape(kw), re.IGNORECASE)
            files = [f for f in files if rx.search(f.name) or rx.search(f.fmt)]
        return files

    def get_marked_visible_files(self) -> List[IAFile]:
        ordered_names = self._ordered_selected_file_names()
        if not ordered_names:
            return []
        visible_by_name = {f.name: f for f in self.get_visible_files()}
        return [visible_by_name[name] for name in ordered_names if name in visible_by_name]

    def toggle_current_file_mark(self) -> None:
        visible = self.get_visible_files()
        if not visible or not (0 <= self.sel_f < len(visible)):
            self.status = "No file selected."
            return
        name = visible[self.sel_f].name
        if name in self.selected_file_names:
            self._unmark_file_name(name)
            self.status = f"Unmarked: {name}"
        else:
            self._mark_file_name(name)
            self.status = f"Marked: {name}"
        self.save_current_file_view_state()

    def mark_current_file_and_advance(self) -> None:
        visible = self.get_visible_files()
        if not visible or not (0 <= self.sel_f < len(visible)):
            self.status = "No file selected."
            return
        self.toggle_current_file_mark()
        if visible and self.sel_f < len(visible) - 1:
            self.sel_f += 1
            self.save_current_file_view_state()

    def clear_file_marks(self) -> None:
        n = len(self.selected_file_names)
        self._clear_selection()
        self.save_current_file_view_state()
        self.status = f"Cleared {n} marked file(s)." if n else "No marked files."

    def mark_all_visible_files(self) -> None:
        visible = self.get_visible_files()
        if not visible:
            self.status = "No visible files to mark."
            return
        before = len(self.selected_file_names)
        for f in visible:
            self._mark_file_name(f.name)
        added = len(self.selected_file_names) - before
        self.save_current_file_view_state()
        self.status = f"Marked {added} new file(s); {len(self.selected_file_names)} total."

    def invert_visible_file_marks(self) -> None:
        visible = self.get_visible_files()
        if not visible:
            self.status = "No visible files to invert."
            return
        for f in visible:
            if f.name in self.selected_file_names:
                self._unmark_file_name(f.name)
            else:
                self._mark_file_name(f.name)
        self.save_current_file_view_state()
        self.status = f"Inverted visible marks; {len(self.selected_file_names)} marked."

    def mark_file_range(self) -> None:
        visible = self.get_visible_files()
        if not visible:
            self.status = "No visible files to mark."
            return
        if not (0 <= self.sel_f < len(visible)):
            self.status = "No file selected."
            return

        start_idx = self.sel_f
        current_num = start_idx + 1
        raw = self.prompt("Mark through file # (blank cancels): ", str(current_num))
        if raw is None:
            self.status = "Range mark canceled."
            return

        raw = raw.strip()
        if not raw.isdigit():
            self.status = "Enter a file number."
            return

        end_num = int(raw)
        if not (1 <= end_num <= len(visible)):
            self.status = f"File number must be 1-{len(visible)}."
            return

        end_idx = end_num - 1
        lo, hi = sorted((start_idx, end_idx))
        before = len(self.selected_file_names)
        for f in visible[lo : hi + 1]:
            self._mark_file_name(f.name)
        added = len(self.selected_file_names) - before
        self.save_current_file_view_state()
        self.status = f"Marked {hi - lo + 1} file(s) from {lo + 1} to {hi + 1}; {len(self.selected_file_names)} total ({added} new)."

    def file_filter_chips(self) -> List[str]:
        self._ensure_download_state()
        chips = []
        if bool(getattr(self, "_file_load_loading", False)):
            chips.append("Opening item")
        if self.file_kw.strip():
            chips.append(f"Keyword: {self.file_kw.strip()}")
        if self.video_only:
            chips.append("Video only: On")
        if self.selected_file_names:
            chips.append(f"Marked: {len(self.selected_file_names)}")
        if self.download_queue:
            chips.append(f"{self.download_queue_summary()}  (Q to view)")
        return chips

    def selected_item_header(self) -> str:
        if bool(getattr(self, "_file_load_loading", False)):
            item = self.selected_result()
            if not item:
                return "Opening item | waiting for IA file metadata"
            return f"Opening item | {item.title or '(no title)'} | {item.identifier} | waiting for IA file metadata"
        item = getattr(self, "file_owner_item", None)
        if not item:
            return "File list owner unavailable; reload the item"
        if self.is_youtube_result(item):
            title = item.title or "(no title)"
            channel = item.uploader or item.creator or "unknown channel"
            return f"[YT] {title} | {channel} | {item.video_id or item.identifier} | single video"
        license_status, _why = self.current_license_status()
        title = item.title or "(no title)"
        total = sum(int(f.size or 0) for f in self.files)
        parts = [
            title,
            item.identifier,
            f"{len(self.files)} files",
            f"{len(self.selected_file_names)} marked",
            human_size(total),
            f"license: {license_status}",
        ]
        return " | ".join(parts)

    def file_marker(self, index: int, filename: str) -> str:
        if filename in self.selected_file_names:
            return "●"
        if index == self.sel_f:
            return "▶"
        return "○"

    def choose_file_filter_action(self) -> None:
        options = [
            "Set keyword...",
            "Clear keyword",
            f"Video only: {'Off' if self.video_only else 'On'}",
            "Show all files",
        ]
        default_idx = 1 if self.file_kw else 0
        pick = self.prompt_list("File filter", options, default_idx=default_idx)
        if not pick:
            self.status = "File filter unchanged."
            self.focus = "LIST"
            return

        if pick == "Set keyword...":
            s = self.prompt("Keyword (blank clears): ", self.file_kw)
            if s is None:
                self.status = "Keyword unchanged."
            else:
                self.file_kw = s.strip()
                self.sel_f = 0
                self.status = "Keyword cleared." if not self.file_kw else f"Keyword: {self.file_kw}"
                self.save_current_file_view_state()
        elif pick == "Clear keyword":
            self.file_kw = ""
            self.sel_f = 0
            self.status = "Keyword cleared."
            self.save_current_file_view_state()
        elif pick.startswith("Video only:"):
            self.video_only = not self.video_only
            self.sel_f = 0
            self.status = "Video only: ON" if self.video_only else "Video only: OFF (showing all files)"
            self.save_current_file_view_state()
        elif pick == "Show all files":
            self.file_kw = ""
            self.video_only = False
            self.sel_f = 0
            self.status = "Showing all files."
            self.save_current_file_view_state()
        self.focus = "LIST"

    def handle_files_hotkey(self, ch: int) -> bool:
        if self.mode != "FILES":
            return False
        if ch in (ord('f'), ord('F')):
            self.choose_file_filter_action()
            return True
        if ch == ord(' '):
            self.mark_current_file_and_advance()
            self.focus = "LIST"
            return True
        if ch in (ord('m'), ord('M')):
            self.mark_file_range()
            self.focus = "LIST"
            return True
        if ch == ord('A'):
            self.mark_all_visible_files()
            self.focus = "LIST"
            return True
        if ch == ord('I'):
            self.invert_visible_file_marks()
            self.focus = "LIST"
            return True
        if ch == ord('U'):
            self.clear_file_marks()
            self.focus = "LIST"
            return True
        if ch in (ord('p'), ord('P')):
            self.set_preview_for_selected()
            return True
        if ch == ord('d'):
            self.set_preview_for_marked()
            return True
        if ch == ord('D'):
            self.set_preview_for_item()
            return True
        if ch == ord('r'):
            visible = self.get_visible_files()
            if visible and 0 <= self.sel_f < len(visible):
                f = visible[self.sel_f]
                marked = "marked" if f.name in self.selected_file_names else "unmarked"
                self.status = f"Details: {f.name} | {human_size(f.size)} | {f.fmt or '(unknown)'} | {marked}"
            else:
                self.status = "No file selected."
            return True
        if ch in (ord('o'), ord('O')):
            self.set_preview_for_prefix()
            return True
        if ch in (ord('v'), ord('V')):
            self.video_only = not self.video_only
            self.sel_f = 0
            self.status = "Video only: ON" if self.video_only else "Video only: OFF (showing all files)"
            self.save_current_file_view_state()
            self.focus = "LIST"
            return True
        return False

    def handle_results_hotkey(self, ch: int) -> bool:
        if self.mode not in ("RESULTS", "SEARCH"):
            return False
        if ch in (ord('l'), ord('f')):
            self.edit_result_filter()
            return True
        if ch in (ord('L'), ord('F')):
            self.clear_result_filter()
            return True
        return False

    def cycle_bucket(self) -> None:
        order = ["TV", "Movies", "Music", "Other"]
        try:
            i = order.index(self.last_bucket)
        except Exception:
            i = 0
        self.last_bucket = order[(i + 1) % len(order)]
        self.status = f"Save bucket: {self.last_bucket}"

    def pick_folder_fav_if_requested(self, bucket: str) -> Optional[str]:
        opts = self.favs.get("folders", {}).get(bucket, [])
        if not isinstance(opts, list) or not opts:
            return None
        return self.prompt_list(f"{bucket} favorites", [str(x) for x in opts if str(x).strip()])

    def pick_save_bucket(self, suggested: str, reason: str) -> Optional[str]:
        buckets = ["TV", "Movies", "Music", "Other"]
        default_idx = buckets.index(suggested) if suggested in buckets else buckets.index("Movies")
        title = f"Save destination  Suggested: {suggested} ({reason})"
        return self.prompt_list(title, buckets, default_idx=default_idx)

    def pick_folder_name(
        self,
        bucket: str,
        default_name: str,
        prompt_label: str,
    ) -> Optional[str]:
        default_name = sanitize_folder(default_name)
        options: List[str] = []
        if default_name:
            options.append(default_name)
        custom_label = "Type custom..."
        options.append(custom_label)
        favorites = self.favs.get("folders", {}).get(bucket, [])
        if isinstance(favorites, list):
            for fav in favorites:
                name = sanitize_folder(str(fav))
                if name and name not in options:
                    options.append(name)
        pick = self.prompt_list(f"{bucket} folder", options, default_idx=0)
        if pick is None:
            return None
        if pick == custom_label:
            raw = self.prompt(prompt_label, default_name)
            if raw is None:
                return None
            return sanitize_folder(raw)
        return sanitize_folder(pick)

    def movie_filename_for_folder(self, movie_folder: str, source_filename: str) -> str:
        movie = sanitize_folder(movie_folder)
        ext = os.path.splitext(os.path.basename(source_filename or ""))[1] or ".mp4"
        return f"{movie}{ext}"

    def sanitize_import_filename(self, name: str, fallback: str) -> str:
        candidate = os.path.basename((name or "").strip())
        if not candidate or candidate in (".", ".."):
            candidate = os.path.basename((fallback or "").strip())
        candidate = candidate.replace("/", "").replace("\\", "").strip()
        if not candidate or candidate in (".", ".."):
            candidate = "download"
        return candidate

    def choose_import_filename(self, default_name: str, source_filename: str) -> Optional[str]:
        default_name = self.sanitize_import_filename(default_name, source_filename)
        raw = self.prompt("Filename (Enter accepts, Esc leaves in staging): ", default_name)
        if raw is None:
            return None
        return self.sanitize_import_filename(raw, default_name)

    def choose_import_foldername(self, default_name: str) -> Optional[str]:
        default_name = sanitize_folder(default_name)
        raw = self.prompt("Folder name (Enter accepts, Esc leaves in staging): ", default_name)
        if raw is None:
            return None
        return sanitize_folder(raw)

    def editable_import_folder_dir(self, final_path: str) -> str:
        dirname = os.path.dirname(final_path)
        parent = os.path.dirname(dirname)
        if (
            os.path.basename(dirname).lower().startswith("season ")
            and safe_path_under(BUCKET_TV, parent)
            and os.path.basename(parent)
        ):
            return parent
        return dirname

    def confirm_final_import_path(self, final_path: str, source_filename: str) -> Optional[str]:
        while True:
            choice = self.prompt_list(
                f"Final path: {final_path}",
                ["Accept", "Edit folder", "Edit filename", "Cancel"],
                default_idx=0,
            )
            if choice is None or choice == "Cancel":
                return None
            if choice == "Accept":
                return final_path

            dirname = os.path.dirname(final_path)
            if choice == "Edit folder":
                editable_dir = self.editable_import_folder_dir(final_path)
                parent = os.path.dirname(editable_dir)
                current_folder = os.path.basename(editable_dir)
                new_folder = self.choose_import_foldername(current_folder)
                if new_folder is None:
                    return None
                suffix = os.path.relpath(final_path, editable_dir)
                final_path = os.path.join(parent, new_folder, suffix)
            else:
                current_name = os.path.basename(final_path)
                new_name = self.choose_import_filename(current_name, source_filename)
                if new_name is None:
                    return None
                final_path = os.path.join(dirname, new_name)

    def choose_bucket_and_path(
        self,
        identifier: str,
        filename: str,
        item_title: str,
        batch: Optional[Dict[str, str]] = None,
    ) -> str:
        staging_path, staging_err = safe_staging_file_path(identifier, filename)
        if staging_err or not staging_path:
            log_line(staging_err)
            return staging_err
        if not os.path.exists(staging_path):
            return f"Downloaded, but staging file not found: {staging_path}"

        if is_dvd_iso_file(filename):
            return self.begin_dvd_scan(staging_path)

        if batch:
            final_path = self.batch_destination_path(batch, filename, item_title)
            if not safe_path_under(MEDIA_ROOT, final_path):
                log_line(f"REFUSED move outside MEDIA_ROOT: {final_path}")
                return f"Refused: destination escapes media root: {final_path}"
            if os.path.exists(final_path):
                base, ext = os.path.splitext(final_path)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                final_path = f"{base}_{stamp}{ext}"
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            shutil.move(staging_path, final_path)
            normalize_media_permissions(final_path, include_parents=True)
            msg = f"Saved: {final_path}"
            self.begin_post_import_integrations(final_path, batch.get("bucket", ""), item_title)
            return msg

        def is_single_large_video(name: str) -> bool:
            try:
                video_files = [f for f in self.files if is_video_file(f.name, f.fmt)]
                large_video_files = [f for f in video_files if int(f.size or 0) >= LARGE_VIDEO_BYTES]
                return (len(large_video_files) == 1 and (large_video_files[0].name or "") == name)
            except Exception:
                return False

        ep = detect_sxxeyy(filename) or detect_sxxeyy(item_title)
        item = self.selected_result()
        mediatype = getattr(item, "mediatype", "") if item else ""
        suggested, reason = infer_bucket(
            filename,
            item_title,
            mediatype=mediatype,
            is_single_large_video=is_single_large_video(filename),
            default_bucket="Movies",
        )

        bucket = self.pick_save_bucket(suggested, reason)
        if bucket is None:
            return f"Left in staging: {staging_path}"
        self.last_bucket = bucket

        if bucket == "TV":
            show_default = sanitize_folder(item_title)
            show = self.pick_folder_name("TV", show_default, "Show name: ")
            if show is None:
                return f"Left in staging: {staging_path}"

            if ep:
                season, episode = ep
                episode_override: Optional[int] = None
            else:
                self.status = f"TV folder set to {show}. Enter season number next."
                self.render()
                s = self.prompt("Season number (01..): ", "01")
                if s is None:
                    return f"Left in staging: {staging_path}"
                try:
                    season = int(s)
                except Exception:
                    season = 1
                e = self.prompt("Episode number (01.., blank = keep name): ", "")
                if e is None:
                    return f"Left in staging: {staging_path}"
                try:
                    episode_override = int(e) if e.strip() else None
                except Exception:
                    episode_override = None

            self.add_folder_fav("TV", show)

            season_dir = os.path.join(BUCKET_TV, show, f"Season {season:02d}")

            new_name = filename
            if ep or episode_override is not None:
                ext = os.path.splitext(filename)[1] or ".mp4"
                ep_num = ep[1] if ep else (episode_override if episode_override is not None else 1)
                new_name = f"{show} - S{season:02d}E{ep_num:02d}{ext}"
            chosen_name = self.choose_import_filename(new_name, filename)
            if chosen_name is None:
                return f"Left in staging: {staging_path}"
            new_name = chosen_name

            final_path = os.path.join(season_dir, new_name)

        elif bucket == "Movies":
            title_default = auto_clean_movie_folder_name(item_title, filename)
            movie = self.pick_folder_name("Movies", title_default, "Movie folder: ")
            if movie is None:
                return f"Left in staging: {staging_path}"
            self.add_folder_fav("Movies", movie)

            movie_dir = os.path.join(BUCKET_MOVIES, movie)
            new_name = self.choose_import_filename(self.movie_filename_for_folder(movie, filename), filename)
            if new_name is None:
                return f"Left in staging: {staging_path}"
            final_path = os.path.join(movie_dir, new_name)

        elif bucket == "Music":
            artist_default = sanitize_folder(item_title)
            artist = self.pick_folder_name("Music", artist_default, "Artist/album folder: ")
            if artist is None:
                return f"Left in staging: {staging_path}"
            self.add_folder_fav("Music", artist)

            music_dir = os.path.join(BUCKET_MUSIC, artist)
            new_name = self.choose_import_filename(os.path.basename(filename), filename)
            if new_name is None:
                return f"Left in staging: {staging_path}"
            final_path = os.path.join(music_dir, new_name)

        else:
            sub = self.pick_folder_name("Other", "Misc", "Other subfolder: ")
            if sub is None:
                return f"Left in staging: {staging_path}"
            self.add_folder_fav("Other", sub)

            other_dir = os.path.join(BUCKET_OTHER, sub)
            new_name = self.choose_import_filename(os.path.basename(filename), filename)
            if new_name is None:
                return f"Left in staging: {staging_path}"
            final_path = os.path.join(other_dir, new_name)

        if os.path.exists(final_path):
            base, ext = os.path.splitext(final_path)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            final_path = f"{base}_{stamp}{ext}"

        final_path = self.confirm_final_import_path(final_path, filename)
        if final_path is None:
            return f"Left in staging: {staging_path}"

        if os.path.exists(final_path):
            base, ext = os.path.splitext(final_path)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            final_path = f"{base}_{stamp}{ext}"

        # Defense in depth: sanitize_folder() strips path separators but does
        # not block ".." components. Resolve and verify the destination is
        # actually under MEDIA_ROOT before moving any bytes.
        if not safe_path_under(MEDIA_ROOT, final_path):
            log_line(f"REFUSED move outside MEDIA_ROOT: {final_path}")
            return f"Refused: destination escapes media root: {final_path}"

        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        shutil.move(staging_path, final_path)
        normalize_media_permissions(final_path, include_parents=True)
        msg = f"Saved: {final_path}"
        self.begin_post_import_integrations(final_path, bucket, item_title)
        return msg

    def register_radarr_movie_if_needed(self, final_path: str, bucket: str, item_title: str) -> str:
        self.last_radarr_result = None
        if bucket != "Movies":
            return ""
        if not os.path.exists(final_path):
            return ""
        item = None
        try:
            item = self.selected_result()
        except Exception:
            item = getattr(self, "preview_item", None)
        meta = getattr(self, "cur_meta", None)
        result = ia_radarr.register_completed_movie(
            final_path,
            item_title=item_title or getattr(item, "title", ""),
            item_year=getattr(item, "year", ""),
            metadata=meta,
            logger=lambda message: log_line(f"RADARR: {message}"),
        )
        self.last_radarr_result = result
        if result.status == "disabled":
            return ""
        return f"Radarr: {result.message}"

    def notify_bazarr_if_needed(self, final_path: str, bucket: str, item_title: str) -> str:
        if bucket not in ("Movies", "TV"):
            return ""
        if not os.path.exists(final_path):
            return ""
        if bucket == "Movies":
            radarr_result = getattr(self, "last_radarr_result", None)
            radarr_id = int(getattr(radarr_result, "movie_id", 0) or 0)
            result = ia_bazarr.handoff_movie(
                final_path,
                radarr_id,
                logger=lambda message: log_line(f"BAZARR: {message}"),
            )
        else:
            result = ia_bazarr.handoff_series(
                final_path,
                0,
                logger=lambda message: log_line(f"BAZARR: {message}"),
            )
        if result.status == "disabled":
            return ""
        return f"Bazarr: {result.message}"

    def _ensure_post_import_state(self) -> None:
        if not hasattr(self, "_post_import_lock") or getattr(self, "_post_import_lock", None) is None:
            self._post_import_lock = threading.RLock()
        if not hasattr(self, "_post_import_jobs"):
            self._post_import_jobs = {}

    def begin_post_import_integrations(self, final_path: str, bucket: str, item_title: str) -> None:
        """Radarr registration is a couple of quick HTTP calls, but Bazarr's
        subtitle handoff can poll for up to bazarr_wait_timeout_s (2 minutes
        by default) waiting for a subtitle to land. Running that inline in
        choose_bucket_and_path used to freeze the curses main loop the same
        way the DVD ISO scan did, so it runs on a background thread instead;
        the result note surfaces later via finish_post_import_integrations_if_ready()."""
        self._ensure_post_import_state()
        with self._post_import_lock:
            self._post_import_jobs[final_path] = {"status": "running", "note": ""}

        def worker() -> None:
            self.last_radarr_result = None
            note = self.register_radarr_movie_if_needed(final_path, bucket, item_title)
            bazarr_note = self.notify_bazarr_if_needed(final_path, bucket, item_title)
            combined = " | ".join(value for value in (note, bazarr_note) if value)
            with self._post_import_lock:
                self._post_import_jobs[final_path] = {"status": "done", "note": combined}

        threading.Thread(target=worker, daemon=True).start()

    def finish_post_import_integrations_if_ready(self) -> bool:
        self._ensure_post_import_state()
        with self._post_import_lock:
            done_path = next(
                (path for path, info in self._post_import_jobs.items() if info.get("status") == "done"),
                None,
            )
            if done_path is None:
                return False
            note = self._post_import_jobs.pop(done_path).get("note", "")
        if not note:
            return True
        self.download_log.insert(0, note)
        self.download_log = self.download_log[:8]
        self.status = note
        return True

    def choose_batch_import_options(self, item_title: str) -> Optional[Dict[str, str]]:
        use_batch = self.prompt("Batch destination for this queue? Enter=yes, type n=no: ", "")
        if use_batch is None or use_batch.strip().lower().startswith("n"):
            return None
        default_bucket = normalize_save_bucket(self.last_bucket, "Movies")
        bucket = self.pick_save_bucket(default_bucket, "queue default")
        if bucket is None:
            return None
        self.last_bucket = bucket
        if bucket == "TV":
            folder = self.pick_folder_name("TV", sanitize_folder(item_title), "Queue show name: ")
            if folder is None:
                return None
            mode = self.prompt_list(
                "TV batch seasons",
                ["Auto from SxxEyy filenames", "One season for whole queue"],
                default_idx=0,
            )
            if mode is None:
                return None
            season = self.prompt("Fallback/queue season number: ", "01")
            if season is None:
                return None
            self.add_folder_fav("TV", folder)
            return {
                "bucket": bucket,
                "folder": folder,
                "season": season or "01",
                "season_mode": "auto" if mode.startswith("Auto") else "single",
            }
        if bucket == "Movies":
            folder = self.pick_folder_name("Movies", auto_clean_movie_folder_name(item_title, ""), "Queue movie folder: ")
            if folder is None:
                return None
            return {"bucket": bucket, "folder": folder}
        if bucket == "Music":
            folder = self.pick_folder_name("Music", sanitize_folder(item_title), "Queue artist/album folder: ")
            if folder is None:
                return None
            return {"bucket": bucket, "folder": folder}
        folder = self.pick_folder_name("Other", "Misc", "Queue other subfolder: ")
        if folder is None:
            return None
        return {"bucket": bucket, "folder": folder}

    def batch_destination_path(self, batch: Dict[str, str], filename: str, item_title: str) -> str:
        bucket = batch.get("bucket", "Other")
        folder = sanitize_folder(batch.get("folder") or item_title or "Misc")
        if bucket == "TV":
            try:
                season = int(batch.get("season") or "1")
            except Exception:
                season = 1
            ep = detect_sxxeyy(filename) or detect_sxxeyy(item_title)
            if batch.get("season_mode") == "auto" and ep:
                season = ep[0]
            new_name = filename
            if ep:
                ext = os.path.splitext(filename)[1] or ".mp4"
                new_name = f"{folder} - S{season:02d}E{ep[1]:02d}{ext}"
            return os.path.join(BUCKET_TV, folder, f"Season {season:02d}", new_name)
        if bucket == "Movies":
            return os.path.join(BUCKET_MOVIES, folder, self.movie_filename_for_folder(folder, filename))
        if bucket == "Music":
            return os.path.join(BUCKET_MUSIC, folder, filename)
        return os.path.join(BUCKET_OTHER, folder, filename)

    def find_existing_media_file(self, filename: str, expected_size: int = 0) -> Optional[str]:
        base = os.path.basename(filename or "")
        if not base:
            return None
        try:
            for root, _dirs, files in os.walk(MEDIA_ROOT):
                if os.path.abspath(root).startswith(os.path.abspath(STAGING_ROOT)):
                    continue
                if base not in files:
                    continue
                path = os.path.join(root, base)
                if expected_size and os.path.getsize(path) != int(expected_size):
                    continue
                return path
        except Exception:
            return None
        return None

    def likely_import_destination(self, filename: str, item_title: str) -> str:
        if is_dvd_iso_file(filename):
            return "DVD ISO: stays in staging for scan/manual rip review"
        preview_item = getattr(self, "preview_item", None)
        selected_result_fn = getattr(self, "selected_result", None)
        item = preview_item or (selected_result_fn() if callable(selected_result_fn) else None)
        mediatype = getattr(item, "mediatype", "") if item else ""
        bucket, _reason = infer_bucket(
            filename,
            item_title,
            mediatype=mediatype,
            is_single_large_video=False,
            default_bucket="Movies",
        )
        ep = detect_sxxeyy(filename) or detect_sxxeyy(item_title)
        if bucket == "TV" and ep:
            show = sanitize_folder(item_title)
            season, episode = ep
            ext = os.path.splitext(filename)[1] or ".mp4"
            return os.path.join(BUCKET_TV, show, f"Season {season:02d}", f"{show} - S{season:02d}E{episode:02d}{ext}")
        if bucket == "Movies":
            movie = auto_clean_movie_folder_name(item_title, filename)
            return os.path.join(BUCKET_MOVIES, movie, self.movie_filename_for_folder(movie, filename))
        if bucket == "Music":
            return os.path.join(BUCKET_MUSIC, sanitize_folder(item_title), filename)
        if bucket == "TV":
            return os.path.join(BUCKET_TV, sanitize_folder(item_title), filename)
        return os.path.join(BUCKET_OTHER, "Misc", filename)

    def scan_staged_dvd_iso(self, staging_path: str, *, dry_run: bool = False) -> str:
        try:
            result = ia_dvd.scan_dvd_iso(staging_path, dry_run=dry_run)
        except Exception as e:
            log_line(f"DVD_SCAN_ERR: {staging_path}: {e}")
            return f"DVD ISO staged, scan failed: {staging_path} ({e})"

        if dry_run:
            return f"Dry run: would scan DVD ISO: {staging_path}"

        if result.ok:
            log_line(f"DVD_SCAN_OK: {staging_path} layout={result.layout} logs={result.logs_dir}")
        else:
            log_line(f"DVD_SCAN_WARN: {staging_path} layout={result.layout} errors={'; '.join(result.errors)} logs={result.logs_dir}")
        return (
            f"DVD ISO staged/scanned for manual review: {staging_path} | "
            f"{result.layout}: {result.reason} | logs: {result.logs_dir}"
        )

    def _ensure_dvd_scan_state(self) -> None:
        if not hasattr(self, "_dvd_scan_lock") or getattr(self, "_dvd_scan_lock", None) is None:
            self._dvd_scan_lock = threading.RLock()
        if not hasattr(self, "_dvd_scan_jobs"):
            self._dvd_scan_jobs = {}

    def begin_dvd_scan(self, staging_path: str) -> str:
        """lsdvd/HandBrakeCLI scanning can take minutes on a full ISO. Run it on
        a background thread instead of the curses main loop (which previously
        froze -- no render(), no input -- for the whole scan) and surface the
        result later via finish_dvd_scans_if_ready()."""
        self._ensure_dvd_scan_state()
        with self._dvd_scan_lock:
            existing = self._dvd_scan_jobs.get(staging_path)
            if existing is not None and existing.get("status") == "scanning":
                return f"DVD ISO scan already running in background: {staging_path}"
            self._dvd_scan_jobs[staging_path] = {"status": "scanning", "message": ""}

        def worker() -> None:
            message = self.scan_staged_dvd_iso(staging_path)
            with self._dvd_scan_lock:
                self._dvd_scan_jobs[staging_path] = {"status": "done", "message": message}

        threading.Thread(target=worker, daemon=True).start()
        return (
            f"DVD ISO staged: {staging_path} -- scanning with lsdvd/HandBrakeCLI "
            f"in the background, result will appear in the log shortly"
        )

    def finish_dvd_scans_if_ready(self) -> bool:
        self._ensure_dvd_scan_state()
        with self._dvd_scan_lock:
            done_path = next(
                (path for path, info in self._dvd_scan_jobs.items() if info.get("status") == "done"),
                None,
            )
            if done_path is None:
                return False
            message = self._dvd_scan_jobs.pop(done_path).get("message", "")
        self.download_log.insert(0, message)
        self.download_log = self.download_log[:8]
        self.status = message
        return True

    def _completed_download_location(self, identifier: str, filename: str, expected_size: int = 0) -> Optional[str]:
        if self._staged_file_complete(identifier, filename, int(expected_size or 0)):
            path, err = safe_staging_file_path(identifier, filename)
            if not err and path:
                return path
        return self.find_existing_media_file(filename, int(expected_size or 0))

    def _handle_already_complete(
        self,
        identifier: str,
        f: IAFile,
        item_title: str,
        batch: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        existing = self._completed_download_location(identifier, f.name, int(f.size or 0))
        if not existing:
            return None
        if safe_path_under(STAGING_ROOT, existing):
            log_line(f"DL_SKIP_STAGED_COMPLETE: {existing}")
            return self.choose_bucket_and_path(identifier, f.name, item_title, batch=batch)
        log_line(f"DL_SKIP_EXISTING_COMPLETE: {existing}")
        return f"Skipped existing complete file: {existing}"

    def refresh_preview_import_info(self) -> None:
        item = self.preview_item
        if not item:
            self.preview_existing = []
            self.preview_destinations = []
            return
        files = [self.preview_file] if self.preview_file else list(self.preview_files or [])
        existing: List[str] = []
        destinations: List[str] = []
        for f in files[:12]:
            if not f:
                continue
            found = self._completed_download_location(item.identifier, f.name, int(f.size or 0))
            if found:
                existing.append(found)
            destinations.append(self.likely_import_destination(f.name, item.title))
        self.preview_existing = existing
        self.preview_destinations = destinations

    def _ensure_download_state(self) -> None:
        if not hasattr(self, "_download_lock") or getattr(self, "_download_lock", None) is None:
            self._download_lock = threading.RLock()
        if not hasattr(self, "download_queue"):
            self.download_queue = []
        if not hasattr(self, "_download_job_seq"):
            self._download_job_seq = 0
        if not hasattr(self, "_download_worker_thread"):
            self._download_worker_thread = None
        if not hasattr(self, "queue_sel"):
            self.queue_sel = 0
        if not hasattr(self, "queue_return_mode"):
            self.queue_return_mode = "RESULTS"

    def _next_job_id(self) -> int:
        self._ensure_download_state()
        self._download_job_seq += 1
        return self._download_job_seq

    def _queue_display_order(self) -> List[DownloadJob]:
        self._ensure_download_state()
        with self._download_lock:
            jobs = list(self.download_queue)
        # Active/queued/awaiting-import first (in the order they were created),
        # finished ones sink to the bottom -- that's the whole point of the view.
        return sorted(jobs, key=lambda j: (j.status in JOB_TERMINAL_STATUSES, j.job_id))

    def _download_queue_counts(self) -> Dict[str, int]:
        self._ensure_download_state()
        counts: Dict[str, int] = {}
        with self._download_lock:
            for job in self.download_queue:
                counts[job.status] = counts.get(job.status, 0) + 1
        return counts

    def download_queue_summary(self) -> str:
        counts = self._download_queue_counts()
        if not counts:
            return "Queue: empty"
        order = ["downloading", "queued", "awaiting_import", "done", "failed", "canceled"]
        parts = [f"{k}:{counts[k]}" for k in order if k in counts]
        return "Queue: " + " ".join(parts)

    def queue_menu_label(self) -> str:
        self._ensure_download_state()
        return f"Queue ({len(self.download_queue)})" if self.download_queue else "Queue"

    def _downloads_panel_lines(self, right_w: int) -> List[Any]:
        """Compact, always-visible download status for the right-hand panel --
        so progress is visible from RESULTS/FILES without opening the queue."""
        self._ensure_download_state()
        active = None
        with self._download_lock:
            for job in self.download_queue:
                if job.status == "downloading":
                    active = job
                    break

        lines: List[Any] = []
        if active is not None:
            with self._download_lock:
                written, total = active.written, active.total
                speed_bps = active.speed_bps
                title = active.title
                current_file = active.current_file_name
            name = title or current_file or "(untitled)"
            if len(name) > right_w:
                name = name[: max(0, right_w - 1)] + "…"
            lines.append(name)
            bar_w = max(8, min(24, right_w - 4))
            if total > 0:
                pct = int((written * 100) / total)
                lines.append(f"[{shaded_progress_bar(written, total, bar_w)}] {pct}%")
                speed = f"  {human_size(int(speed_bps))}/s" if speed_bps > 0 else ""
                lines.append(f"{human_size(written)}/{human_size(total)}{speed}")
            else:
                lines.append(f"{human_size(written)} downloaded" if written > 0 else "Starting...")
        lines.append((f"{self.download_queue_summary()}  (Q queue)", curses.color_pair(3)))
        return lines

    def open_download_queue(self) -> None:
        self._ensure_download_state()
        if self.mode != "QUEUE":
            self.queue_return_mode = self.mode if self.mode in ("RESULTS", "SEARCH", "FILES", "FAVS") else "RESULTS"
        self.mode = "QUEUE"
        self.focus = "LIST"
        self.queue_sel = 0
        self.status = self.download_queue_summary()

    def selected_queue_job(self) -> Optional[DownloadJob]:
        jobs = self._queue_display_order()
        if not jobs:
            return None
        idx = max(0, min(self.queue_sel, len(jobs) - 1))
        return jobs[idx]

    def cancel_selected_queue_job(self) -> None:
        job = self.selected_queue_job()
        if not job:
            self.status = "Queue is empty."
            return
        if job.status not in ("queued", "downloading"):
            self.status = f"\"{job.title}\" is already {job.status}."
            return
        with self._download_lock:
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "canceled"
                job.error = "Canceled."
                job.finished_at = time.time()
        self.status = f"Canceling \"{job.title}\"..."

    def request_quit(self) -> bool:
        """Cancel and reap queue work before allowing the TUI process to exit."""
        self._ensure_download_state()
        with self._download_lock:
            active = [job for job in self.download_queue if job.status == "downloading"]
            queued = [job for job in self.download_queue if job.status == "queued"]
        if not active and not queued:
            self.exit_requested = True
            return True

        answer = self.prompt(
            f"Cancel {len(active)} active and {len(queued)} queued download(s) before quitting? Type CANCEL: ",
            "",
        )
        if answer != "CANCEL":
            self.status = "Quit canceled; downloads are still active."
            return False

        with self._download_lock:
            for job in active:
                job.cancel_requested = True
            for job in queued:
                job.cancel_requested = True
                job.status = "canceled"
                job.error = "Canceled on exit."
                job.finished_at = time.time()

        worker = self._download_worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        if worker is not None and worker.is_alive():
            self.status = "Still stopping active download; wait and quit again."
            return False

        self.exit_requested = True
        return True

    def remove_selected_queue_job(self) -> None:
        job = self.selected_queue_job()
        if not job:
            self.status = "Queue is empty."
            return
        if job.status == "downloading":
            self.status = "Cancel the active download before removing it."
            return
        with self._download_lock:
            self.download_queue = [j for j in self.download_queue if j.job_id != job.job_id]
        self.queue_sel = max(0, self.queue_sel - 1)
        self.status = f"Removed \"{job.title}\" from the queue."

    def handle_mouse_event(self) -> bool:
        try:
            _mouse_id, _x, _y, _z, button_state = curses.getmouse()
        except Exception:
            return False

        direction = mouse_wheel_direction(button_state)
        if direction == 0:
            return False
        return self.scroll_active_list(direction)

    def scroll_active_list(self, direction: int) -> bool:
        if self.mode in ("RESULTS", "SEARCH"):
            visible = self.get_visible_results()
            if not visible:
                return True
            self.sel_r = scroll_index(self.sel_r, direction, len(visible))
            return True

        if self.mode == "FILES":
            visible = self.get_visible_files()
            if not visible:
                return True
            self.sel_f = scroll_index(self.sel_f, direction, len(visible))
            return True

        if self.mode == "FAVS":
            if self.favs_tab == "ITEMS":
                favs_len = len(self.favs.get("items") or [])
            elif self.favs_tab == "FILES":
                favs_len = len(self.favs.get("files") or [])
            else:
                folders = self.favs.get("folders") or {}
                favs_len = sum(len(folders.get(b) or []) for b in ("TV", "Movies", "Music", "Other"))
            if not favs_len:
                return True
            self.favs_idx = scroll_index(self.favs_idx, direction, favs_len)
            return True

        return False

    def queue_row_attr(self, status: str, active: bool = False) -> int:
        status_l = (status or "").lower()
        if active or status_l in ("downloading", "active"):
            return curses.color_pair(2) | curses.A_BOLD
        if status_l in ("failed", "error", "blocked", "canceled"):
            return curses.color_pair(5) | curses.A_BOLD
        if status_l in ("unclear", "warning", "skipped", "staged"):
            return curses.color_pair(3)
        if status_l in ("marked", "pending", "queued", "awaiting_import"):
            return curses.color_pair(1) | curses.A_BOLD
        return curses.color_pair(6)

    def import_queue_status(self, msg: str) -> str:
        text = str(msg or "")
        if text.startswith("Left in staging:"):
            return "staged"
        if text.startswith("Skipped existing complete file:"):
            return "skipped"
        if text.startswith("Refused:") or text.startswith("Downloaded, but staging file not found:"):
            return "failed"
        return "done"

    def note_import_status(self, status: str) -> None:
        if status == "done":
            self.jellyfin_rescan_needed = True

    def request_jellyfin_rescan_if_needed(self) -> None:
        if not getattr(self, "jellyfin_rescan_needed", False):
            return
        self.status = "Requesting Jellyfin library rescan..."
        self.render()
        ok, msg = ia_jellyfin.request_library_rescan()
        log_line(msg)
        self.download_log.insert(0, msg)
        self.download_log = self.download_log[:8]
        if ok:
            self.jellyfin_rescan_needed = False
        self.status = msg
        self.render()

    def import_left_in_staging(self, msg: str) -> bool:
        return self.import_queue_status(msg) == "staged"

    def job_file_table_rows(self, job: DownloadJob, width: int, limit: int = 8) -> List[Tuple[str, int]]:
        if width <= 0:
            return []
        self._ensure_download_state()
        with self._download_lock:
            file_rows = list(job.file_statuses)
            current_name = job.current_file_name
        rows: List[Tuple[str, int]] = [("STATUS      SIZE       FILE", curses.color_pair(3) | curses.A_BOLD)]
        name_w = max(8, width - 22)
        for row in file_rows[:limit]:
            status = str(row.get("status") or "pending")
            size = display_size(row.get("size"))
            name = str(row.get("name") or "")
            if len(name) > name_w:
                name = name[: max(0, name_w - 1)] + "…"
            active = bool(current_name and row.get("name") == current_name)
            line = f"{status:<11} {size:>8}  {name}"
            rows.append((line[:width], self.queue_row_attr(status, active)))
        if len(file_rows) > limit:
            rows.append((f"... and {len(file_rows) - limit} more", curses.color_pair(6) | curses.A_DIM))
        return rows

    def record_failed_file(
        self,
        f: IAFile,
        err: str,
        owner_item: Optional[SearchResult],
        owner_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        owner = replace(owner_item) if isinstance(owner_item, SearchResult) and owner_item.identifier else None
        metadata = deepcopy(owner_metadata) if isinstance(owner_metadata, dict) else None
        entry = FailedDownload(file=f, owner_item=owner, owner_metadata=metadata, error=err)
        for index, existing in enumerate(self.failed_queue):
            if not isinstance(existing, FailedDownload):
                continue
            existing_owner = existing.owner_item
            if (
                existing.file.name == f.name
                and existing_owner
                and owner
                and existing_owner.identifier == owner.identifier
            ):
                self.failed_queue[index] = entry
                return
        self.failed_queue.append(entry)

    def file_owner(self) -> Tuple[Optional[SearchResult], str]:
        """Return the item that supplied the current FILES list, never its mutable selection."""
        item = getattr(self, "file_owner_item", None)
        if not isinstance(item, SearchResult) or not item.identifier:
            return None, "File list owner is unavailable; reload the item before downloading."
        return item, ""

    def _clear_retry_failed_preview(self) -> None:
        self._retry_failed_entries = []
        self._retry_failed_metadata = None

    def retry_failed_downloads(self) -> None:
        if not self.failed_queue:
            self.status = "No failed files to retry."
            return

        entries = list(self.failed_queue)
        if not all(
            isinstance(entry, FailedDownload)
            and isinstance(entry.owner_item, SearchResult)
            and entry.owner_item.identifier
            for entry in entries
        ):
            self.status = "Failed download owner is unavailable; retry cannot safely choose an item."
            return

        owner = entries[0].owner_item
        retry_entries = [entry for entry in entries if entry.owner_item.identifier == owner.identifier]
        other_count = len(entries) - len(retry_entries)
        self.preview_item = replace(owner)
        self.preview_file = None
        self.preview_files = [entry.file for entry in retry_entries]
        self.preview_prefix = "__SELECTED__"
        self._retry_failed_entries = retry_entries
        self._retry_failed_metadata = deepcopy(retry_entries[0].owner_metadata)
        self.refresh_preview_import_info()
        remaining = f" {other_count} failed file(s) from other items will remain queued for retry." if other_count else ""
        self.preview_msg = f"Retrying {len(retry_entries)} failed file(s) from {owner.identifier}.{remaining}"
        self.mode = "PREVIEW_DL"
        self.focus = "MENU"
        self.menu_idx = 0
        self.status = "Preview retry (no changes)."

    def resume_or_retry_download(self) -> None:
        if getattr(self, "failed_queue", None):
            self.retry_failed_downloads()
            return
        self.resume_pending_download()
    
    def set_preview_for_selected(self) -> None:
        self._clear_retry_failed_preview()
        item, owner_err = self.file_owner()
        if not item:
            self.status = owner_err
            return
        visible = self.get_visible_files()
        if not visible or not (0 <= self.sel_f < len(visible)):
            self.status = "No file selected."
            return
        f = visible[self.sel_f]

        if self.is_youtube_result(item):
            ok, why = True, "YouTube single video via yt-dlp."
        else:
            meta = self.cur_meta or {}
            ok, why = is_openly_licensed(meta) if meta else (False, "No metadata loaded")

        self.preview_item = item
        self.preview_file = f
        self.preview_files = []
        self.preview_prefix = ""
        if self.is_youtube_result(item):
            self.preview_msg = "YouTube single-video download via yt-dlp. You can download after confirmation."
        elif ok:
            self.preview_msg = "Open license detected in metadata. You can download after confirmation."
        else:
            if self.enforce_license_gate:
                self.preview_msg = f"Download blocked. {why}"
            else:
                self.preview_msg = f"Rights unclear. {why}  You can still download if you confirm."
        self.refresh_preview_import_info()
        self.mode = "PREVIEW_DL"
        self.focus = "MENU"
        self.menu_idx = 0
        self.status = "Preview (no changes)."

    def set_preview_for_marked(self) -> None:
        self._clear_retry_failed_preview()
        item, owner_err = self.file_owner()
        if not item:
            self.status = owner_err
            return
        marked = self.get_marked_visible_files()
        if not marked:
            self.set_preview_for_selected()
            return

        if self.is_youtube_result(item):
            ok, why = True, "YouTube single video via yt-dlp."
        else:
            meta = self.cur_meta or {}
            ok, why = is_openly_licensed(meta) if meta else (False, "No metadata loaded")
        total = sum(int(f.size or 0) for f in marked)
        self.preview_item = item
        self.preview_file = None
        self.preview_files = list(marked)
        self.preview_prefix = "__SELECTED__"

        if self.is_youtube_result(item):
            self.preview_msg = f"YouTube single-video download via yt-dlp ({display_size(total)})."
        elif ok:
            self.preview_msg = f"Open license detected. Will download {len(marked)} marked files ({human_size(total)})."
        else:
            if self.enforce_license_gate:
                self.preview_msg = f"Download blocked. {why}"
            else:
                self.preview_msg = f"Rights unclear. {why}  You can still download if you confirm."
        self.refresh_preview_import_info()

        self.mode = "PREVIEW_DL"
        self.focus = "MENU"
        self.menu_idx = 0
        self.status = "Preview (no changes)."

    def set_preview_for_prefix(self) -> None:
        self._clear_retry_failed_preview()
        item, owner_err = self.file_owner()
        if not item:
            self.status = owner_err
            return
        if self.is_youtube_result(item):
            self.status = "YouTube supports single-video downloads only."
            return
        meta = self.cur_meta or {}
        ok, why = is_openly_licensed(meta) if meta else (False, "No metadata loaded")

        visible = self.get_visible_files()
        selected = visible[self.sel_f] if visible and 0 <= self.sel_f < len(visible) else None
        suggestions = self.prefix_suggestions_for_file(selected.name if selected else "")
        prefix = ""
        if suggestions:
            custom_label = "Type custom prefix..."
            pick = self.prompt_list("Choose folder/prefix", suggestions + [custom_label])
            if pick is None:
                self.status = "Canceled."
                return
            if pick == custom_label:
                prefix = self.prompt("Folder/prefix to download (matches start of filename): ", "")
            else:
                prefix = pick
        else:
            prefix = self.prompt("Folder/prefix to download (matches start of filename): ", "")
        if prefix is None:
            self.status = "Canceled."
            return
        prefix = prefix.strip()
        if not prefix:
            self.status = "No prefix provided."
            return

        matches = [f for f in visible if (f.name or "").startswith(prefix)]
        if not matches:
            self.status = f"No files match prefix: {prefix}"
            return

        total = sum(int(f.size or 0) for f in matches)
        self.preview_item = item
        self.preview_file = None
        self.preview_files = matches
        self.preview_prefix = prefix

        if ok:
            self.preview_msg = f"Open license detected. Will download {len(matches)} files ({human_size(total)})."
        else:
            if self.enforce_license_gate:
                self.preview_msg = f"Download blocked. {why}"
            else:
                self.preview_msg = f"Rights unclear. {why}  You can still download if you confirm."
        self.refresh_preview_import_info()

        self.mode = "PREVIEW_DL"
        self.focus = "MENU"
        self.menu_idx = 0
        self.status = "Preview (no changes)."

    def set_preview_for_item(self) -> None:
        self._clear_retry_failed_preview()
        item, owner_err = self.file_owner()
        if not item:
            self.status = owner_err
            return
        if self.is_youtube_result(item):
            self.set_preview_for_selected()
            return
        meta = self.cur_meta or {}
        ok, why = is_openly_licensed(meta) if meta else (False, "No metadata loaded")

        visible = self.get_visible_files()
        if not visible:
            self.status = "No visible files."
            return

        total = sum(int(f.size or 0) for f in visible)
        self.preview_item = item
        self.preview_file = None
        self.preview_files = list(visible)
        self.preview_prefix = "__FULL_ITEM__"

        if ok:
            extra = " Type ALL after Confirm to proceed." if self.requires_strong_bulk_confirm("__FULL_ITEM__", len(visible), total) else ""
            self.preview_msg = f"Open license detected. Will download {len(visible)} visible files ({human_size(total)}).{extra}"
        else:
            if self.enforce_license_gate:
                self.preview_msg = f"Download blocked. {why}"
            else:
                extra = " Type ALL after Confirm to proceed." if self.requires_strong_bulk_confirm("__FULL_ITEM__", len(visible), total) else ""
                self.preview_msg = f"Rights unclear. {why}  You can still download if you confirm.{extra}"
        self.refresh_preview_import_info()

        self.mode = "PREVIEW_DL"
        self.focus = "MENU"
        self.menu_idx = 0
        self.status = "Preview (no changes)."

    def _ia_download_base_args(self) -> List[str]:
        return ia_downloads.download_base_args(IA_NO_CHANGE_TIMESTAMP)

    def _verify_expected_size(self, identifier: str, filename: str, expected_size: int) -> Tuple[bool, str]:
        return ia_downloads.verify_expected_size(identifier, filename, expected_size)

    def _staged_file_complete(self, identifier: str, filename: str, expected_size: int) -> bool:
        if expected_size <= 0:
            return False
        path, err = safe_staging_file_path(identifier, filename)
        if err or not path:
            return False
        return os.path.exists(path) and ia_downloads.safe_getsize(path) == int(expected_size)

    def _is_stall_error(self, msg: str) -> bool:
        return "download stalled" in (msg or "").lower()

    def _background_wait_before_stall_retry(self, job: DownloadJob, attempt_num: int, max_attempts: int) -> bool:
        # No curses access here (background thread): show progress via job.error
        # instead of self.status/self.render(), and poll job.cancel_requested
        # (set by the main thread) instead of reading stdscr directly.
        for remaining in range(STALL_RETRY_DELAY_S, 0, -1):
            with self._download_lock:
                job.error = f"Stalled. Auto-retry {attempt_num}/{max_attempts} in {remaining}s."
                if job.cancel_requested:
                    return False
            time.sleep(1)
        return True

    def _background_download_file(self, job: DownloadJob, identifier: str, filename: str, expected_size: int) -> Tuple[bool, str]:
        """Curses-free rewrite of the old _download_one_with_progress, safe to run
        on the background worker thread: progress/cancel flow through `job`
        (guarded by self._download_lock) instead of self.status/self.render()/stdscr."""
        path, err = safe_staging_file_path(identifier, filename)
        if err or not path:
            return False, err
        os.makedirs(STAGING_ROOT, exist_ok=True)
        target_lock, lock_err = ia_downloads.acquire_target_download_lock(identifier, filename)
        if not target_lock:
            return False, lock_err

        try:
            if job.is_youtube:
                if not job.webpage_url:
                    return False, "YouTube URL is missing."
                os.makedirs(yt_downloads.youtube_staging_dir(identifier), exist_ok=True)
                cmd = yt_downloads.single_video_download_cmd(APP_CONFIG["yt_dlp_path"], job.webpage_url, identifier)
                read_written = lambda: ia_downloads.dir_total_size(yt_downloads.youtube_staging_dir(identifier))
            else:
                cmd = ia_downloads.single_download_cmd(identifier, filename, IA_NO_CHANGE_TIMESTAMP)
                read_written = lambda: ia_downloads.safe_getsize(path)
            log_line(f"DL_CMD: {shlex.join(cmd)}")
            ia_minotaur_events.emit_archive_started(f"{identifier} {filename}")
            log_fh = ia_downloads.open_process_log()
            try:
                with self._download_lock:
                    job.current_file_name = filename
                    job.total = int(expected_size or 0)
                    job.written = 0
                    job.speed_bps = 0.0
                    job.eta_s = 0.0
                    job.error = ""

                def check_cancel() -> bool:
                    with self._download_lock:
                        return job.cancel_requested

                def update_progress(progress: ia_downloads.DownloadProgress) -> None:
                    with self._download_lock:
                        job.written = progress.written
                        job.total = progress.total
                        job.speed_bps = progress.speed_bps
                        job.eta_s = progress.eta_s

                max_stall_retries = STALL_AUTO_RETRIES
                attempt = 0
                while True:
                    if attempt > 0:
                        log_line(f"DL_STALL_RETRY: {filename} attempt {attempt}/{max_stall_retries}")

                    ok, msg = ia_downloads.run_download_with_progress(
                        cmd,
                        target=filename,
                        expected_total=int(expected_size or 0),
                        read_written=read_written,
                        log_fh=log_fh,
                        stall_timeout_s=STALL_TIMEOUT_S,
                        is_cancel_requested=check_cancel,
                        on_progress=update_progress,
                        log_path=LOG_PATH,
                    )
                    if ok:
                        break

                    if msg.startswith("download failed:"):
                        log_line(f"DL_POPEN_ERR: {msg}")
                        ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: {msg}")
                        return False, msg

                    if self._is_stall_error(msg):
                        if self._staged_file_complete(identifier, filename, int(expected_size or 0)):
                            log_line(f"DL_STALL_COMPLETE: {filename}")
                            break
                        if check_cancel():
                            ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: Canceled.")
                            return False, "Canceled."
                        if attempt < max_stall_retries:
                            attempt += 1
                            if not self._background_wait_before_stall_retry(job, attempt, max_stall_retries):
                                ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: Canceled.")
                                return False, "Canceled."
                            continue
                        err = f"{msg} Auto-retry limit reached."
                        ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: {err}")
                        return False, err

                    ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: {msg}")
                    return False, msg

                if not job.is_youtube:
                    ok_target, msg_target = ia_downloads.verify_download_target(identifier, filename)
                    if not ok_target:
                        ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: {msg_target}")
                        return False, msg_target
                    ok_sz, msg_sz = self._verify_expected_size(identifier, filename, int(expected_size or 0))
                    if not ok_sz:
                        ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: {msg_sz}")
                        return False, msg_sz
                ia_minotaur_events.emit_archive_completed(f"{identifier} {filename}")
                return True, ""
            finally:
                log_fh.close()
        finally:
            target_lock.release()

    # ---------- background download queue ----------
    def _ensure_download_worker_running(self) -> None:
        self._ensure_download_state()
        with self._download_lock:
            thread = self._download_worker_thread
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(target=self._download_worker_loop, daemon=True)
            self._download_worker_thread = thread
        thread.start()

    def _next_queued_job(self) -> Optional[DownloadJob]:
        self._ensure_download_state()
        with self._download_lock:
            for job in self.download_queue:
                if job.status == "queued":
                    return job
        return None

    def _download_worker_loop(self) -> None:
        while True:
            job = self._next_queued_job()
            if job is None:
                return
            with self._download_lock:
                job.status = "downloading"
            self._run_job_downloads(job)

    def _run_job_downloads(self, job: DownloadJob) -> None:
        with self._download_lock:
            already_handled = {row["name"] for row in job.file_statuses}
            completed_names = [row["name"] for row in job.file_statuses if row.get("status") in ("done", "skipped")]
        remaining = [f for f in job.files if f.name not in already_handled]

        for f in remaining:
            with self._download_lock:
                if job.cancel_requested:
                    job.set_file_status(f.name, "canceled", "Canceled.")
                    job.status = "canceled"
                    job.finished_at = time.time()
                    self._save_pending(job.identifier, job.title, job.files, job.preview_prefix, "", completed_names)
                    return
                job.set_file_status(f.name, "downloading")

            ok, err = self._background_download_file(job, job.identifier, f.name, int(f.size or 0))
            if not ok:
                status = "canceled" if "cancel" in err.lower() else "failed"
                with self._download_lock:
                    job.set_file_status(f.name, status, err)
                    job.status = status
                    job.error = err
                    job.finished_at = time.time()
                self._save_pending(job.identifier, job.title, job.files, job.preview_prefix, "", completed_names)
                return

            real_name = f.name
            if job.is_youtube:
                found = yt_downloads.find_downloaded_video_file(job.identifier, job.video_id)
                if not found:
                    err = "yt-dlp finished, but downloaded file was not found in staging."
                    with self._download_lock:
                        job.set_file_status(f.name, "failed", err)
                        job.status = "failed"
                        job.error = err
                        job.finished_at = time.time()
                    return
                real_name = found
                found_path, found_err = safe_staging_file_path(job.identifier, found)
                if not found_err and found_path:
                    f.size = ia_downloads.safe_getsize(found_path)

            with self._download_lock:
                job.set_file_status(f.name, "downloaded", size=int(f.size or 0), real_name=real_name)

        with self._download_lock:
            job.status = "awaiting_import"
            job.current_file_name = ""
            job.error = ""
        self._clear_pending()

    def _enqueue_download_job(
        self,
        identifier: str,
        title: str,
        files: List[IAFile],
        *,
        preview_prefix: str = "",
        batch: Optional[Dict[str, str]] = None,
        is_youtube: bool = False,
        webpage_url: str = "",
        video_id: str = "",
        pre_resolved: Optional[List[Tuple[IAFile, str]]] = None,
        owner_item: Optional[SearchResult] = None,
        owner_metadata: Optional[Dict[str, Any]] = None,
    ) -> DownloadJob:
        """Queue a job for the background worker. `pre_resolved` are files the
        main thread already handled via _handle_already_complete (already
        staged/skipped/failed) before the job was built -- they're recorded as
        already-done so the worker only downloads what's actually left."""
        job = DownloadJob(
            job_id=self._next_job_id(),
            identifier=identifier,
            title=title,
            files=list(files),
            preview_prefix=preview_prefix,
            batch=batch,
            is_youtube=is_youtube,
            webpage_url=webpage_url,
            video_id=video_id,
            owner_item=replace(owner_item) if isinstance(owner_item, SearchResult) and owner_item.identifier else None,
            owner_metadata=deepcopy(owner_metadata) if isinstance(owner_metadata, dict) else None,
            created_at=time.time(),
        )
        for f, msg in (pre_resolved or []):
            status = self.import_queue_status(msg)
            self.note_import_status(status)
            job.set_file_status(f.name, status, msg, size=int(f.size or 0))
            if status == "failed":
                self.record_failed_file(f, msg, job.owner_item, job.owner_metadata)
            self.download_log.insert(0, msg)
        if pre_resolved:
            self.download_log = self.download_log[:8]
        with self._download_lock:
            self.download_queue.append(job)
        self._ensure_download_worker_running()
        return job

    def finish_download_progress(self) -> bool:
        """Polled once per main-loop tick, alongside finish_search_load_if_ready/
        finish_file_load_if_ready. Byte transfer already happened in the
        background; this does the interactive "where does this go" placement
        step (and failure/cancel bookkeeping) for whichever job finished
        downloading, one job at a time, without ever running while another
        prompt is open."""
        self._ensure_download_state()
        with self._download_lock:
            target = None
            for job in self.download_queue:
                if job.status in ("awaiting_import", "failed", "canceled") and not job.bookkept:
                    target = job
                    break
        if target is None:
            return False
        self._process_finished_job(target)
        self._prune_download_queue()
        return True

    def _process_finished_job(self, job: DownloadJob) -> None:
        if job.status in ("failed", "canceled"):
            with self._download_lock:
                failed_row = next((r for r in job.file_statuses if r.get("status") in ("failed", "canceled")), None)
                err = job.error
                job.bookkept = True
            if job.status == "failed" and failed_row:
                f = next((x for x in job.files if x.name == failed_row.get("name")), None)
                if f:
                    self.record_failed_file(f, err, job.owner_item, job.owner_metadata)
            suffix = "  (press R to resume)" if job.status == "canceled" else ""
            self.status = f"{err}{suffix}"
            self.download_log.insert(0, f"Error: {err}")
            self.download_log = self.download_log[:8]
            return

        # awaiting_import: place every file whose bytes finished downloading.
        # Non-interactive when job.batch was chosen up front; otherwise this
        # is the same per-file bucket/folder prompt flow as before, just
        # deferred until we're back at the main loop instead of forced the
        # instant bytes land.
        with self._download_lock:
            to_import = list(job.files_needing_import())
        for row in to_import:
            name = str(row["name"])
            real_name = str(row.get("real_name") or name)
            msg = self.choose_bucket_and_path(job.identifier, real_name, job.title, batch=job.batch)
            if job.is_youtube and msg.startswith("Saved: "):
                final_path = msg[len("Saved: ") :].split(" | ", 1)[0]
                captions = yt_downloads.move_downloaded_captions(job.identifier, job.video_id, final_path)
                if captions:
                    msg = f"{msg} | Captions saved: {len(captions)}"
            import_status = self.import_queue_status(msg)
            self.note_import_status(import_status)
            with self._download_lock:
                job.set_file_status(name, import_status, msg)
            if import_status == "failed":
                f = next((x for x in job.files if x.name == name), None)
                if f:
                    self.record_failed_file(f, msg, job.owner_item, job.owner_metadata)
            self.download_log.insert(0, msg)
            self.download_log = self.download_log[:8]

        with self._download_lock:
            staged_count = sum(1 for r in job.file_statuses if r.get("status") == "staged")
            total_files = len(job.files)
            job.status = "done"
            job.finished_at = time.time()
            job.bookkept = True
        if staged_count:
            self.status = f"Downloaded {total_files} file(s); {staged_count} import pending. ({self.download_queue_summary()})"
        else:
            self.status = f"Done. Downloaded {total_files} file(s). ({self.download_queue_summary()})"

    def _prune_download_queue(self, keep_terminal: int = 20) -> None:
        with self._download_lock:
            active = [j for j in self.download_queue if j.status not in JOB_TERMINAL_STATUSES]
            terminal = [j for j in self.download_queue if j.status in JOB_TERMINAL_STATUSES]
            terminal.sort(key=lambda j: j.finished_at)
            self.download_queue = active + terminal[-keep_terminal:]

    def resume_pending_download(self) -> None:
        pending = self._load_pending()
        if not pending:
            self.status = "No pending download to resume."
            return

        identifier = pending.get("identifier", "")
        item_title = pending.get("item_title", identifier)
        if not identifier:
            self.status = "Pending download state is invalid — cleared."
            self._clear_pending()
            return

        files_data = pending.get("files") or []
        completed_names: set = set(pending.get("completed_names") or [])
        glob_pat = pending.get("glob_pat", "")
        preview_prefix = pending.get("preview_prefix", "")

        all_files = [
            IAFile(name=str(fd["name"]), size=int(fd.get("size") or 0), fmt=str(fd.get("fmt") or ""))
            for fd in files_data
            if fd.get("name")
        ]
        staged_ready: List[IAFile] = []
        remaining: List[IAFile] = []
        skipped_existing = 0
        completed_names_list = list(completed_names)

        for f in all_files:
            if f.name in completed_names:
                continue
            existing = self.find_existing_media_file(f.name, int(f.size or 0))
            if existing:
                skipped_existing += 1
                completed_names.add(f.name)
                completed_names_list.append(f.name)
                self.download_log.insert(0, f"Skipped existing complete file: {existing}")
                self.download_log = self.download_log[:8]
                continue
            if self._staged_file_complete(identifier, f.name, int(f.size or 0)):
                staged_ready.append(f)
            else:
                remaining.append(f)

        if not staged_ready and not remaining:
            self.status = "All files from the pending download are already complete."
            self._clear_pending()
            return

        n = len(staged_ready) + len(remaining)
        total_bytes = sum(int(f.size or 0) for f in remaining)
        action = "Import" if staged_ready and not remaining else "Resume"
        confirm = self.prompt(
            f"{action} {n} file(s) ({human_size(total_bytes)} left) for \"{item_title}\"? Enter=yes Esc=no: ", ""
        )
        if confirm is None:
            self.status = "Resume canceled."
            return

        stub_item = SearchResult(identifier=identifier, title=item_title, year="", creator="")

        existing = [i for i, r in enumerate(self.results) if r.identifier == identifier]
        if existing:
            self.sel_r = existing[0]
        else:
            self.results.insert(0, stub_item)
            self.sel_r = 0

        self.mode = "FILES"
        self.focus = "LIST"
        os.makedirs(STAGING_ROOT, exist_ok=True)

        new_completed = completed_names_list

        # Files already downloaded (staged) just need the interactive
        # placement step -- quick, so it still runs right away on the main
        # thread, same as before.
        for f in staged_ready:
            msg = self.choose_bucket_and_path(identifier, f.name, item_title)
            import_status = self.import_queue_status(msg)
            self.note_import_status(import_status)
            if import_status == "done" or import_status == "skipped":
                new_completed.append(f.name)
            self.download_log.insert(0, msg)
            self.download_log = self.download_log[:8]

        if not remaining:
            if skipped_existing:
                self.status = f"Skipped {skipped_existing} existing file(s)."
            self._clear_pending()
            self.status = f"Resume complete. {n} file(s) handled."
            return

        # Anything still needing bytes goes to the background queue so search
        # and browsing stay available while it downloads.
        self._enqueue_download_job(
            identifier,
            item_title,
            remaining,
            preview_prefix=preview_prefix,
            owner_item=stub_item,
        )
        self.status = f"Resuming {len(remaining)} file(s) in the background ({self.download_queue_summary()})."

    def perform_download_plan(self) -> None:
        if not self.preview_item:
            self.status = "Nothing to download."
            self.mode = "FILES"
            self.focus = "LIST"
            return

        retry_entries = list(getattr(self, "_retry_failed_entries", []))
        if retry_entries:
            if not all(
                isinstance(entry, FailedDownload)
                and isinstance(entry.owner_item, SearchResult)
                and entry.owner_item.identifier == self.preview_item.identifier
                for entry in retry_entries
            ):
                self.status = "Failed download owner is inconsistent; retry cannot safely choose an item."
                self.mode = "FILES"
                self.focus = "LIST"
                return
            metadata = getattr(self, "_retry_failed_metadata", None)
        # A normal app instance always has this attribute. Refuse any preview
        # whose item no longer matches the FILES view that supplied its files.
        elif hasattr(self, "file_owner_item"):
            owner, owner_err = self.file_owner()
            if not owner or owner.identifier != self.preview_item.identifier:
                self.status = owner_err or "File list owner does not match this download preview; reload the item."
                self.mode = "FILES"
                self.focus = "LIST"
                return
            metadata = self.cur_meta or {}
        else:
            metadata = self.cur_meta or {}

        is_youtube_plan = self.is_youtube_result(self.preview_item)
        if is_youtube_plan:
            ok, why = True, "YouTube single video via yt-dlp."
        else:
            ok, why = is_openly_licensed(metadata) if metadata else (False, "No metadata loaded")
        if not ok and self.enforce_license_gate:
            self.status = f"Blocked. {why}"
            self.mode = "FILES"
            self.focus = "LIST"
            return

        if not ok and not self.enforce_license_gate:
            s = self.prompt('Rights unclear. Press Enter to proceed, or Esc to cancel: ', "")
            if s is None:
                self.status = "Canceled."
                self.mode = "FILES"
                self.focus = "LIST"
                return

        item = self.preview_item
        if retry_entries:
            retry_entry_ids = {id(entry) for entry in retry_entries}
            self.failed_queue = [entry for entry in self.failed_queue if id(entry) not in retry_entry_ids]
            self._clear_retry_failed_preview()
        else:
            self.failed_queue = []

        # single file
        if self.preview_file:
            f = self.preview_file
            complete_msg = self._handle_already_complete(item.identifier, f, item.title)
            self.preview_item = None
            self.preview_file = None
            self.preview_files = []
            self.preview_prefix = ""
            self.mode = "FILES"
            self.focus = "LIST"

            if complete_msg:
                status = self.import_queue_status(complete_msg)
                self.note_import_status(status)
                self.download_log.insert(0, complete_msg)
                self.download_log = self.download_log[:8]
                self.status = complete_msg
                return

            self._enqueue_download_job(
                item.identifier,
                item.title,
                [f],
                is_youtube=self.is_youtube_result(item),
                webpage_url=getattr(item, "webpage_url", ""),
                video_id=getattr(item, "video_id", ""),
                owner_item=item,
                owner_metadata=metadata,
            )
            self.status = f"Queued: {item.title} — {f.name} ({self.download_queue_summary()})"
            return

        # prefix or full item
        if self.preview_files:
            queue = list(self.preview_files)
            total_expected = sum(int(f.size or 0) for f in queue)
            batch_import = self.choose_batch_import_options(item.title) if len(queue) > 1 else None

            if self.requires_strong_bulk_confirm(self.preview_prefix, len(queue), total_expected):
                s = self.prompt(
                    f"Large all-visible download: {len(queue)} file(s), {human_size(total_expected)}. Type ALL to continue: ",
                    "",
                )
                if s != "ALL":
                    self.status = "Canceled large all-visible download."
                    self.mode = "FILES"
                    self.focus = "LIST"
                    return

            # Already-complete files are resolved here, on the main thread
            # (interactive placement may be involved), before the rest goes
            # to the background worker as a single job.
            pre_resolved: List[Tuple[IAFile, str]] = []
            remaining: List[IAFile] = []
            for f in queue:
                complete_msg = self._handle_already_complete(item.identifier, f, item.title, batch=batch_import)
                if complete_msg:
                    pre_resolved.append((f, complete_msg))
                else:
                    remaining.append(f)

            was_selected_plan = self.preview_prefix == "__SELECTED__"
            preview_prefix = self.preview_prefix
            self.preview_item = None
            self.preview_file = None
            self.preview_files = []
            self.preview_prefix = ""
            if was_selected_plan:
                self.selected_file_names.clear()
                self.save_current_file_view_state()

            self._enqueue_download_job(
                item.identifier,
                item.title,
                queue,
                preview_prefix=preview_prefix,
                batch=batch_import,
                pre_resolved=pre_resolved,
                owner_item=item,
                owner_metadata=metadata,
            )
            self.mode = "FILES"
            self.focus = "LIST"
            if not remaining:
                self.status = f"All {len(queue)} file(s) were already complete."
            else:
                self.status = f"Queued: {item.title} — {len(remaining)}/{len(queue)} file(s) ({self.download_queue_summary()})"
            return

        self.status = "Nothing selected."
        self.mode = "FILES"
        self.focus = "LIST"

    def preview_plan_kind(self) -> str:
        if self.preview_file:
            return "Selected file"
        if self.preview_prefix == "__SELECTED__":
            return "Marked files"
        if self.preview_prefix == "__FULL_ITEM__":
            return "All visible files"
        if self.preview_prefix:
            return f"Folder prefix: {self.preview_prefix}"
        return "None"

    def preview_file_count_and_total(self) -> Tuple[int, int]:
        if self.preview_file:
            return 1, int(self.preview_file.size or 0)
        if self.preview_files:
            return len(self.preview_files), sum(int(f.size or 0) for f in self.preview_files)
        return 0, 0

    def preview_queue_table_rows(self, width: int, limit: int = 8) -> List[Tuple[str, int]]:
        files = [self.preview_file] if self.preview_file else list(self.preview_files or [])
        rows: List[Tuple[str, int]] = [("STATUS      SIZE       FILE", curses.color_pair(3) | curses.A_BOLD)]
        name_w = max(8, width - 22)
        for f in files[:limit]:
            if not f:
                continue
            name = f.name
            if len(name) > name_w:
                name = name[: max(0, name_w - 1)] + "…"
            rows.append((f"{'marked':<11} {human_size(int(f.size or 0)):>8}  {name}"[:width], self.queue_row_attr("marked")))
        if len(files) > limit:
            rows.append((f"... and {len(files) - limit} more", curses.color_pair(6) | curses.A_DIM))
        return rows

    def requires_strong_bulk_confirm(self, prefix: str, count: int, total_bytes: int) -> bool:
        if prefix != "__FULL_ITEM__":
            return False
        return count >= BULK_CONFIRM_FILE_THRESHOLD or total_bytes >= BULK_CONFIRM_BYTES_THRESHOLD

    def current_license_status(self) -> Tuple[str, str]:
        item = getattr(self, "preview_item", None) or getattr(self, "file_owner_item", None) or self.selected_result()
        if self.is_youtube_result(item):
            return "open", "YouTube single video via yt-dlp."
        meta = self.cur_meta or {}
        if not meta:
            return "unknown", "No metadata loaded"
        ok, why = is_openly_licensed(meta)
        if ok:
            return "open", why
        if self.enforce_license_gate:
            return "blocked", why
        return "unclear", why

    # ---------- render ----------
    def draw_help(self, top_y: int) -> None:
        h, w = self.stdscr.getmaxyx()
        y = top_y

        lines = [
            "KEYBOARD SHORTCUTS:",
            "  /  s       Open search bar (works anywhere)",
            "  l          Local filter all search results",
            "  0..9       Jump/select result number",
            "  Tab        Switch MENU <-> LIST focus",
            "  Arrows     Navigate menu items or list",
            "  j / k      Move down / up in list (vim-style)",
            "  g / Home   Jump to first item in list",
            "  G / End    Jump to last item in list",
            "  Enter      Activate menu button / open item or file",
            "  n  ]  PgDn  Next page of results",
            "  p  [  PgUp  Previous page of results",
            "  y          Show library audit summary counts",
            "  r          Show selected result metadata summary",
            "  R          Resume pending/failed download state",
            "  #          Go to specific page number",
            "  Space      Mark/unmark file (in FILES mode)",
            "  A / I / U  Mark all visible / invert visible / clear marks",
            "  d          Preview marked files, or selected file if none marked",
            "  D          Preview/download all visible files",
            "  f          File filter menu: keyword, clear keyword, video-only, show all",
            "  v          Toggle video-only filter (in FILES mode)",
            "  Backspace  Go back (works in FILES, FAVS, HELP, PREVIEW)",
            "  q          Quit",
            "",
            "SEARCH FLOW:",
            "  1) [Search] -> type query -> Enter  (Up/Down recalls history)",
            "  2) Pick result with arrows, Enter/[Open] to view files",
            "  3) Pick file, then [Preview] -> [Confirm] to download",
            "  4) Or mark files with Space, then press d to preview only marked files",
            "  5) Or [Folder] / [Item] for prefix or all-visible bulk downloads",
            "",
            "SEARCH OPTIONS:",
            "  [Filter]     choose: movies / audio / texts / software / any",
            "  [Sort]       choose: relevance / date / title / downloads",
            "  [Title only] search within item titles only",
            "  [Local]      refines all loaded search results; L clears it quickly",
            "  [Attempts]   inspect or re-run the fallback queries for this search",
            "  [Collections] finds IA collection items",
            "  [In Collection] narrows by a collection identifier",
            "  Actions -> Search / fields searches title, creator, subject, date, etc.",
            "  [History]    pick from recent searches",
            "  Changing filter, sort, or title-only refreshes results after selection",
            "  Advanced:    use IA syntax directly, e.g. collection:prelinger",
            "",
            "FAVORITES:",
            "  [Fav] saves a result  |  [Fav File] saves a file",
            "  [Favs] opens the favorites browser (ITEMS / FILES / FOLDERS)",
            "  Use arrows + [Open] or [Download] on a saved favorite",
            "  [Remove] deletes the selected favorite",
            "",
            "DOWNLOADS:",
            "  No download starts without a confirmation step.",
            "  Press c to cancel while downloading.",
            "  Press R from any screen to resume a canceled/stalled download.",
            "  Downloads stalled for 2 min auto-retry twice, then save resume state.",
            "  Files go to staging first, then move to TV / Movies / Music / Other.",
            "  Final folder and filename are editable before import; Esc leaves the file in staging.",
            "  Unclear rights show a warning; [License gate] can block them.",
            f"  {'--no-change-timestamp enabled (mtimes set to now).' if IA_NO_CHANGE_TIMESTAMP else 'Source mtimes preserved.'}",
            f"  Log: {LOG_PATH}",
        ]

        for line in lines:
            if y >= h - 4:
                break
            self.safe_addstr(y, 0, line[: max(0, w - 1)], curses.color_pair(6))
            y += 1

    def draw_help_overlay(self) -> None:
        h, w = self.stdscr.getmaxyx()
        box_w = min(w - 4, 72)
        box_h = min(h - 4, 16)
        if box_w < 40 or box_h < 10:
            return
        top = max(1, (h - box_h) // 2)
        left = max(2, (w - box_w) // 2)

        for y in range(top, top + box_h):
            self.safe_addstr(y, left, " " * box_w, curses.color_pair(6))

        self.safe_addstr(top, left, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(2))
        self.safe_addstr(top + box_h - 1, left, "└" + "─" * (box_w - 2) + "┘", curses.color_pair(2))
        for y in range(top + 1, top + box_h - 1):
            self.safe_addstr(y, left, "│", curses.color_pair(2))
            self.safe_addstr(y, left + box_w - 1, "│", curses.color_pair(2))

        lines = [
            " Help ",
            "",
            "Tab switches MENU/LIST. Enter activates. Backspace goes back. q quits.",
        ]
        if self.mode in ("RESULTS", "SEARCH"):
            lines += [
                "Open Enter/o | Search / | Details r | Local l/f | Clear L/F",
                "Actions a | Page n/p | Filter, sort, title-only in Actions",
            ]
        elif self.mode == "FILES":
            lines += [
                "Preview Enter/p | Folder o | Mark Space/m/A/I/U | Marked d | All visible D",
                "Filter f/F | Video v | Retry R | Bucket in menu",
            ]
        elif self.mode == "FAVS":
            lines += [
                "Open Enter/o | Tab changes saved tabs | Remove Del",
                "Backspace returns to results/files.",
            ]
        elif self.mode == "PREVIEW_DL":
            lines += ["Enter confirms download | Esc or Backspace cancels."]
        else:
            lines += ["Use the menu or arrows to navigate.", "Search with / or s."]
        lines += ["", "Press ? or Esc to close."]

        y = top + 1
        for line in lines:
            if y >= top + box_h - 1:
                break
            attr = curses.color_pair(1) | curses.A_BOLD if line.strip() == "Help" else curses.color_pair(6)
            self.safe_addstr(y, left + 2, line[: max(0, box_w - 4)].ljust(max(0, box_w - 4)), attr)
            y += 1

    def draw_welcome(self, top_y: int) -> None:
        h, w = self.stdscr.getmaxyx()
        lines = [
            "No item selected.",
            "",
            "First steps:",
            "  IA search: press / or choose IA Search",
            "  YouTube search: choose YT Search",
            "  Source: choose Source",
            "  Help: press ?",
            "  Quit: press q",
        ]
        if self.search_history:
            lines += ["", "Recent searches:"]
            for q in self.search_history[:5]:
                lines.append(f"  - {q}")
        center_y = top_y + 2
        for i, line in enumerate(lines):
            y = center_y + i
            if y >= h - 4:
                break
            x = max(0, (w - len(line)) // 2)
            self.safe_addstr(y, x, line[: max(0, w - 1)], curses.color_pair(6))

    def draw_preview(self, top_y: int) -> None:
        h, w = self.stdscr.getmaxyx()
        y = top_y + 1
        item = self.preview_item

        def box(title: str, rows: List[Any], top: int) -> int:
            if top >= h - 4:
                return top
            box_w = max(20, w - 2)
            self.safe_addstr(top, 0, "┌" + f" {title} ".ljust(max(0, box_w - 2), "─")[: max(0, box_w - 2)] + "┐", curses.color_pair(2))
            top += 1
            for row in rows:
                if top >= h - 4:
                    break
                if isinstance(row, tuple):
                    text, attr = row
                else:
                    text, attr = str(row), curses.color_pair(6)
                body_w = max(0, box_w - 4)
                self.safe_addstr(top, 0, "│ ", curses.color_pair(2))
                self.safe_addstr(top, 2, str(text)[:body_w].ljust(body_w), attr)
                self.safe_addstr(top, box_w - 1, "│", curses.color_pair(2))
                top += 1
            if top < h - 4:
                self.safe_addstr(top, 0, "└" + "─" * max(0, box_w - 2) + "┘", curses.color_pair(2))
                top += 1
            return top + 1

        if not item:
            box("Plan", ["Nothing selected.", "Backspace returns to files."], y)
            return

        count, total = self.preview_file_count_and_total()
        license_status, license_reason = self.current_license_status()
        files = [self.preview_file] if self.preview_file else list(self.preview_files or [])
        plan_rows: List[Any] = [
            f"Item: {item.title}",
            f"Identifier: {item.identifier}",
            f"Plan: {self.preview_plan_kind()}",
            f"Files: {count}   Total size: {display_size(total)}",
        ]
        if self.preview_file:
            f = self.preview_file
            plan_rows += [f"File: {f.name}", f"Size: {display_size(f.size)}   Format: {f.fmt or '(unknown)'}"]
        if self.preview_msg:
            plan_rows.append(self.preview_msg)

        y = box("Plan", plan_rows, y)
        lic_attr = self.queue_row_attr("blocked" if license_status == "blocked" else "unclear" if license_status != "open" else "done")
        y = box("License", [(f"{license_status}: {license_reason}", lic_attr)], y)

        dest_rows = [f"Bucket: {self.last_bucket}"]
        if self.preview_destinations:
            dest_rows += self.preview_destinations[:5]
            if len(self.preview_destinations) > 5:
                dest_rows.append(f"... and {len(self.preview_destinations) - 5} more")
        else:
            dest_rows.append("Destination will be chosen after download.")
        y = box("Destination", dest_rows, y)

        existing_rows = self.preview_existing[:5] if self.preview_existing else ["No matching existing media found."]
        if len(self.preview_existing) > 5:
            existing_rows.append(f"... and {len(self.preview_existing) - 5} more")
        y = box("Existing Files", existing_rows, y)

        queue_rows = self.preview_queue_table_rows(w - 6, limit=10)
        if not files:
            queue_rows = ["No files in plan."]
        box("Queue", queue_rows, y)

    def draw_panels(self, top_y: int) -> None:
        h, w = self.stdscr.getmaxyx()
        body_top = top_y
        body_bottom = h - 4
        if body_bottom <= body_top + 2:
            return

        for y2 in range(body_top, body_bottom):
            if y2 % 2 == 0:
                self.safe_addstr(y2, 0, " " * max(0, w - 1), curses.A_DIM)

        left_w = max(30, int(w * 0.70))
        if left_w > w - 2:
            left_w = w - 2
        right_x = left_w + 1
        right_w = max(0, (w - right_x - 1))

        for y in range(body_top, body_bottom):
            self.safe_addstr(y, left_w, "│", curses.color_pair(1))

        left_title = "RESULTS"
        if self.mode == "FILES":
            left_title = "FILES"
        elif self.mode == "FAVS":
            left_title = f"FAVORITES ({self.favs_tab})"
        elif self.mode == "HELP":
            left_title = "HELP"
        elif self.mode == "QUEUE":
            left_title = "DOWNLOAD QUEUE"
        elif self.mode == "ERROR":
            left_title = "ERROR"
        elif self.mode == "PREVIEW_DL":
            left_title = "PREVIEW"

        left_focus = " <FOCUS>" if self.focus == "LIST" else ""
        menu_focus = " <MENU FOCUS>" if self.focus == "MENU" else ""
        self.safe_addstr(body_top, 0, f" {left_title}{left_focus} ".ljust(max(0, left_w - 1), "─"), curses.color_pair(2))
        self.safe_addstr(body_top, right_x, f" DETAILS{menu_focus} ".ljust(max(0, right_w), "─")[: max(0, right_w)], curses.color_pair(2))

        list_top = body_top + 1
        max_rows = body_bottom - list_top
        if max_rows <= 0:
            return

        if self.mode in ("RESULTS", "SEARCH"):
            visible_results = self.get_visible_results()
            with self._search_cache_lock:
                loading_more = self._all_results_loading
                loader_error = self._all_results_loader_error
            if not visible_results:
                if self.results and self.result_filter:
                    progress = self.local_filter_progress_label()
                    if progress:
                        msg = f"No current matches for \"{self.result_filter}\"; {progress}. Press l to change."
                    else:
                        msg = f"No results match local filter \"{self.result_filter}\". Press l to change or clear it."
                else:
                    msg = "No results. First steps: IA search /  |  YT Search menu  |  Source menu  |  Help ?  |  Quit q"
                self.safe_addstr(list_top, 0, msg.ljust(max(0, left_w - 1)), curses.color_pair(6))
            else:
                if self.result_filter:
                    progress = self.local_filter_progress_label()
                    suffix = f"; {progress}" if progress else ""
                    phdr = f" Results 1-{len(visible_results)} of {len(visible_results)}  (local filter{suffix})"
                elif self.total_results > 0:
                    start_n = (self.page - 1) * ROWS_PER_PAGE + 1
                    end_n = (self.page - 1) * ROWS_PER_PAGE + len(self.results)
                    total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
                    phdr = f" Results {start_n}–{end_n} of {self.total_results}  (page {self.page}/{total_pages})  [ ] or n/p to page"
                else:
                    start_n = (self.page - 1) * ROWS_PER_PAGE + 1
                    end_n = (self.page - 1) * ROWS_PER_PAGE + len(self.results)
                    phdr = f" Results {start_n}–{end_n}  (page {self.page})"
                if loading_more:
                    phdr += "  loading more results..."
                elif loader_error:
                    phdr += f"  load paused: {loader_error}"
                self.safe_addstr(list_top, 0, phdr[: max(0, left_w - 1)].ljust(max(0, left_w - 1)), curses.color_pair(3))
                list_top += 1
                max_rows = max(0, max_rows - 1)
                chips = self.results_state_chips()
                if chips and max_rows > 0:
                    chip_line = "  ".join(f"[{chip}]" for chip in chips)
                    self.safe_addstr(list_top, 0, chip_line[: max(0, left_w - 1)].ljust(max(0, left_w - 1)), curses.color_pair(2))
                    list_top += 1
                    max_rows = max(0, max_rows - 1)
                if self.sel_r >= len(visible_results):
                    self.sel_r = max(0, len(visible_results) - 1)
                start = 0
                if self.sel_r >= max_rows:
                    start = self.sel_r - max_rows + 1
                for i in range(start, min(len(visible_results), start + max_rows)):
                    r = visible_results[i]
                    marker = ">" if i == self.sel_r else " "
                    try:
                        raw_idx = visible_results.index(r) if self.result_filter else self.results.index(r)
                    except ValueError:
                        raw_idx = i
                    abs_num = (i + 1) if self.result_filter else ((self.page - 1) * ROWS_PER_PAGE + raw_idx + 1)
                    idx = f"{abs_num:02d}"
                    raw_title = (r.title or "")
                    meta = self.result_meta_summary(r)
                    meta_suffix = f"  [{meta}]" if meta else ""
                    badge = self.result_source_badge(r)
                    max_title = max(12, left_w - 16 - len(badge) - len(meta_suffix))
                    title = (raw_title[:max_title - 1] + "…") if len(raw_title) > max_title else raw_title
                    star = "*" if self.is_fav_item(r.identifier) else " "
                    line = f"{marker} {idx} {star} │ {badge} {title}{meta_suffix}"
                    line = line[: max(0, left_w - 1)].ljust(max(0, left_w - 1))

                    self.safe_addstr(list_top + (i - start), 0, line, self.result_row_attr(r, i == self.sel_r))

        elif self.mode == "FILES":
            visible = self.get_visible_files()
            header = self.selected_item_header()
            self.safe_addstr(list_top, 0, header[: max(0, left_w - 1)].ljust(max(0, left_w - 1)), curses.color_pair(2) | curses.A_BOLD)
            list_top += 1
            max_rows = max(0, max_rows - 1)
            chips = self.file_filter_chips()
            if chips:
                chip_line = "  ".join(f"[{chip}]" for chip in chips)
                self.safe_addstr(list_top, 0, chip_line[: max(0, left_w - 1)].ljust(max(0, left_w - 1)), curses.color_pair(3))
                list_top += 1
                max_rows = max(0, max_rows - 1)
            if not visible:
                loading_files = bool(getattr(self, "_file_load_loading", False))
                if loading_files:
                    msg = "Loading file list..."
                elif self.file_kw:
                    msg = f"No files match \"{self.file_kw}\"  |  f filter menu  |  U clear marks  |  v show all"
                elif self._all_recognized_video_files() and not self._eligible_video_files():
                    msg = (
                        f"No video files meet the {self.min_video_file_size_mb}MB minimum  |  "
                        "toggle 'hide small videos' to show  |  Backspace results"
                    )
                elif self.video_only:
                    msg = "No video files visible  |  v show all  |  f filter menu  |  Backspace results"
                else:
                    msg = "No files found for this item  |  Backspace results  |  / search"
                self.safe_addstr(list_top, 0, msg.ljust(max(0, left_w - 1)), curses.color_pair(6))
            else:
                if self.sel_f >= len(visible):
                    self.sel_f = max(0, len(visible) - 1)
                start = 0
                if self.sel_f >= max_rows:
                    start = self.sel_f - max_rows + 1
                item = self.selected_result()
                for i in range(start, min(len(visible), start + max_rows)):
                    f = visible[i]
                    marker = " "
                    star = " "
                    if item and self.is_fav_file(item.identifier, f.name):
                        star = "*"
                    mark = self.file_marker(i, f.name)
                    line = f"{marker} {mark} {i+1:02d} {star} │ {human_size(f.size):>9}  {f.name}"
                    line = line[: max(0, left_w - 1)].ljust(max(0, left_w - 1))

                    if i == self.sel_f:
                        attr = curses.color_pair(8) if self.focus == "LIST" else curses.color_pair(6)
                        if self.focus == "LIST":
                            attr |= curses.A_BOLD
                        self.safe_addstr(list_top + (i - start), 0, line, attr)
                    elif f.name in self.selected_file_names:
                        self.safe_addstr(list_top + (i - start), 0, line, curses.color_pair(3) | curses.A_BOLD)
                    else:
                        self.safe_addstr(list_top + (i - start), 0, line, curses.color_pair(6))

        elif self.mode == "FAVS":
            if self.favs_tab == "ITEMS":
                items_list = self.favs.get("items") or []
                if not items_list:
                    self.safe_addstr(list_top, 0, "No saved items. Press [Fav] on a result to add one.".ljust(max(0, left_w - 1)), curses.color_pair(6))
                else:
                    if self.favs_idx >= len(items_list):
                        self.favs_idx = max(0, len(items_list) - 1)
                    start = max(0, self.favs_idx - max_rows + 1) if self.favs_idx >= max_rows else 0
                    for i in range(start, min(len(items_list), start + max_rows)):
                        it = items_list[i]
                        marker = ">" if i == self.favs_idx else " "
                        raw = str(it.get("title") or it.get("identifier") or "?")
                        ttl = (raw[:37] + "…") if len(raw) > 38 else raw
                        yr = f" ({it['year']})" if it.get("year") else ""
                        line = f"{marker} {i+1:02d} │ {ttl}{yr}"
                        line = line[: max(0, left_w - 1)].ljust(max(0, left_w - 1))
                        attr = (curses.color_pair(7) | curses.A_BOLD) if i == self.favs_idx else curses.color_pair(6)
                        self.safe_addstr(list_top + (i - start), 0, line, attr)

            elif self.favs_tab == "FILES":
                files_list = self.favs.get("files") or []
                if not files_list:
                    self.safe_addstr(list_top, 0, "No saved files. Press [Fav File] when viewing files.".ljust(max(0, left_w - 1)), curses.color_pair(6))
                else:
                    if self.favs_idx >= len(files_list):
                        self.favs_idx = max(0, len(files_list) - 1)
                    start = max(0, self.favs_idx - max_rows + 1) if self.favs_idx >= max_rows else 0
                    for i in range(start, min(len(files_list), start + max_rows)):
                        it = files_list[i]
                        marker = ">" if i == self.favs_idx else " "
                        ident = str(it.get("identifier") or "")
                        fname = str(it.get("filename") or "")
                        entry = f"{ident}/{fname}"
                        entry = (entry[:37] + "…") if len(entry) > 38 else entry
                        line = f"{marker} {i+1:02d} │ {entry}"
                        line = line[: max(0, left_w - 1)].ljust(max(0, left_w - 1))
                        attr = (curses.color_pair(7) | curses.A_BOLD) if i == self.favs_idx else curses.color_pair(6)
                        self.safe_addstr(list_top + (i - start), 0, line, attr)

            elif self.favs_tab == "FOLDERS":
                folders = self.favs.get("folders") or {}
                flat: List[Tuple[str, str]] = []
                for bucket in ("TV", "Movies", "Music", "Other"):
                    for name in (folders.get(bucket) or []):
                        flat.append((bucket, name))
                if not flat:
                    self.safe_addstr(list_top, 0, "No saved folders.".ljust(max(0, left_w - 1)), curses.color_pair(6))
                else:
                    if self.favs_idx >= len(flat):
                        self.favs_idx = max(0, len(flat) - 1)
                    start = max(0, self.favs_idx - max_rows + 1) if self.favs_idx >= max_rows else 0
                    for i in range(start, min(len(flat), start + max_rows)):
                        bucket, name = flat[i]
                        marker = ">" if i == self.favs_idx else " "
                        line = f"{marker} {i+1:02d} │ [{bucket}] {name}"
                        line = line[: max(0, left_w - 1)].ljust(max(0, left_w - 1))
                        attr = (curses.color_pair(7) | curses.A_BOLD) if i == self.favs_idx else curses.color_pair(6)
                        self.safe_addstr(list_top + (i - start), 0, line, attr)

        elif self.mode == "QUEUE":
            jobs = self._queue_display_order()
            self.safe_addstr(list_top, 0, self.download_queue_summary()[: max(0, left_w - 1)].ljust(max(0, left_w - 1)), curses.color_pair(3))
            list_top += 1
            max_rows = max(0, max_rows - 1)
            if not jobs:
                self.safe_addstr(list_top, 0, "Queue is empty. Download something from Files to add a job.".ljust(max(0, left_w - 1)), curses.color_pair(6))
            else:
                if self.queue_sel >= len(jobs):
                    self.queue_sel = max(0, len(jobs) - 1)
                start = max(0, self.queue_sel - max_rows + 1) if self.queue_sel >= max_rows else 0
                for i in range(start, min(len(jobs), start + max_rows)):
                    job = jobs[i]
                    marker = ">" if i == self.queue_sel else " "
                    title = (job.title[:37] + "…") if len(job.title) > 38 else job.title
                    line = f"{marker} {i+1:02d} │ [{job.status:<14}] {title} — {job.summary_label()}"
                    line = line[: max(0, left_w - 1)].ljust(max(0, left_w - 1))
                    attr = self.queue_row_attr(job.status, active=(job.status == "downloading"))
                    if i == self.queue_sel:
                        attr |= curses.A_REVERSE
                    self.safe_addstr(list_top + (i - start), 0, line, attr)

        ry = list_top
        details: List[str] = []

        if self.mode in ("RESULTS", "SEARCH"):
            sel_item = self.selected_result()
            details = []
            if sel_item and self.is_youtube_result(sel_item):
                details += self.youtube_result_details_lines(sel_item)
            elif sel_item:
                status, status_reason = license_status_from_fields(sel_item.licenseurl, sel_item.rights)
                details += [
                    "Selected:",
                    "  Source: [IA] Internet Archive",
                    f"  {sel_item.title or '(no title)'}",
                    f"  Year:    {sel_item.year or '—'}",
                    f"  Type:    {sel_item.mediatype or '—'}",
                    f"  Downloads: {compact_count(sel_item.downloads) if sel_item.downloads else '—'}",
                    f"  License hint: {status}",
                    f"  Creator: {sel_item.creator or '—'}",
                    f"  ID:      {sel_item.identifier}",
                    "",
                ]
                if sel_item.formats:
                    details += ["Formats:", f"  {sel_item.formats}", ""]
                if sel_item.collection:
                    details += ["Collection:", f"  {sel_item.collection}", ""]
                if sel_item.date or sel_item.publicdate:
                    details += [
                        "Dates:",
                        f"  Date:       {sel_item.date or '—'}",
                        f"  Publicdate: {sel_item.publicdate or '—'}",
                        "",
                    ]
                if status != "unknown":
                    details += ["License reason:", f"  {status_reason}", ""]
                followups = build_sideways_searches(
                    {
                        "metadata": {
                            "identifier": sel_item.identifier,
                            "creator": sel_item.creator,
                            "collection": sel_item.collection,
                            "subject": [],
                        }
                    },
                    self.filter,
                )
                if followups:
                    details += ["Follow-up searches:"]
                    for label, query in followups[:4]:
                        short = query if len(query) <= max(18, right_w - 8) else (query[: max(15, right_w - 11)] + "...")
                        details.append(f"  {label}: {short}")
                    details.append("")
                if sel_item.description:
                    details.append("Description:")
                    wrap_w = max(10, right_w - 2)
                    for wrapped in textwrap.wrap(sel_item.description, width=wrap_w):
                        details.append(f"  {wrapped}")
                    details.append("")
            if not (sel_item and self.is_youtube_result(sel_item)):
                if sel_item:
                    details += [
                        "Enter or [Open] to view files",
                        f"Sort: {self._sort_label()}",
                        f"Local filter: {self.result_filter or '(none)'}",
                        f"Query: {self.query_built or '(none)'}",
                    ]
                else:
                    details += [
                        "No item selected",
                        f"Source: {self.search_source_badge()} {self.search_source_label()}",
                        f"Query: {self.query_built or '(none)'}",
                    ]
            if self.last_search_attempts and not (sel_item and self.is_youtube_result(sel_item)):
                details += [
                    "",
                    "Search debug:",
                    f"  Strategy: {self.last_search_used_label or 'custom'}",
                    f"  Total results: {self.total_results or len(self.results)}",
                ]
                for label, query in self.last_search_attempts[:3]:
                    q = query if len(query) <= max(18, right_w - 8) else (query[: max(15, right_w - 11)] + "...")
                    details.append(f"  {label}: {q}")
            chips = [] if (sel_item and self.is_youtube_result(sel_item)) else self.collection_choices_from_results(limit=4)
            if chips:
                details += ["", "Top collections:"]
                for chip in chips:
                    details.append(f"  [{chip}]")
                details.append("  Search tools -> Result collections to narrow")
        elif self.mode == "FILES":
            item = self.selected_result()
            visible = self.get_visible_files()
            sel = visible[self.sel_f] if (visible and 0 <= self.sel_f < len(visible)) else None
            details = [
                "What happens next:",
                "  Preview -> Confirm -> Download",
                "  Space -> mark/unmark file",
                "  A/I/U -> mark all visible, invert visible, clear marks",
                "  Folder -> prefix bulk download",
                "  Item -> download all visible",
                "",
                f"Save to: {self.last_bucket}",
                f"Keyword: {self.file_kw or '(none)'}",
                f"Video only: {'On' if self.video_only else 'Off'}",
                f"Marked files: {len(self.selected_file_names)}",
                f"Failed files: {len(self.failed_queue)}",
                "",
                "Selected file:",
            ]
            if sel:
                details += [
                    f"  {sel.name}",
                    f"  {human_size(sel.size)} | {sel.fmt or '(unknown)'}",
                ]
            else:
                details += ["  (none)"]

            if item:
                details += [
                    "",
                    "Item:",
                    f"  {item.title}",
                    f"  ID: {item.identifier}",
                ]

            if self.cur_meta:
                ok2, why2 = is_openly_licensed(self.cur_meta)
                details += ["", "License gate:", f"  {'ALLOW' if ok2 else 'BLOCK'}", f"  {why2}"]
                followups = build_sideways_searches(self.cur_meta, self.filter)
                if followups:
                    details += ["", "Follow-up searches:"]
                    for label, query in followups[:4]:
                        short = query if len(query) <= max(18, right_w - 8) else (query[: max(15, right_w - 11)] + "...")
                        details.append(f"  {label}: {short}")

        elif self.mode == "QUEUE":
            job = self.selected_queue_job()
            if not job:
                details = ["No jobs queued.", "", "Download a file or item from Files to add one."]
            else:
                with self._download_lock:
                    written, total = job.written, job.total
                    speed_bps, eta_s = job.speed_bps, job.eta_s
                    current_file = job.current_file_name
                    error = job.error
                details = [f"Job: {job.title}", f"  Status: {job.status}"]
                if job.status == "downloading":
                    details.append(f"  File: {current_file}")
                    bar_w = max(8, min(34, right_w - 4))
                    if total > 0:
                        pct = int((written * 100) / total) if total else 0
                        details += [
                            f"  [{shaded_progress_bar(written, total, bar_w)}]",
                            f"  {pct}%  {human_size(written)}/{human_size(total)}",
                        ]
                    else:
                        details += [
                            f"  [{shaded_progress_bar(written, total, bar_w)}]",
                            f"  {human_size(written)} downloaded" if written > 0 else "  Size unknown",
                        ]
                    if speed_bps > 0:
                        details.append(f"  Speed: {human_size(int(speed_bps))}/s")
                    if eta_s > 0:
                        details.append(f"  ETA: {int(eta_s)}s")
                elif error:
                    details.append(f"  {error}")
                details += ["", "Files:"]
                details += self.job_file_table_rows(job, right_w, limit=8)
            details += ["", "c cancel  |  x remove  |  Backspace/q back"]

        if getattr(self, "last_error_detail", "") and self.mode != "QUEUE":
            wrap_w = max(12, right_w - 2)
            err_rows = ["Last error:"]
            for wrapped in textwrap.wrap(str(self.last_error_detail), width=wrap_w) or [str(self.last_error_detail)]:
                err_rows.append(f"  {wrapped}")
            details = err_rows + [""] + details

        for line in details:
            if ry >= body_bottom:
                break
            if isinstance(line, tuple):
                text, attr = line
            else:
                text, attr = str(line), curses.color_pair(6)
            self.safe_addstr(ry, right_x, text[: max(0, right_w)].ljust(max(0, right_w)), attr)
            ry += 1

        self._ensure_download_state()
        show_downloads_panel = bool(self.download_queue) and self.mode != "QUEUE" and right_w > 10
        activity_reserved = min(8, len(self.download_log) + 1) if (right_w > 10 and self.download_log and self.mode != "FAVS") else 0

        if show_downloads_panel:
            dl_lines = self._downloads_panel_lines(right_w)
            downloads_reserved = min(6, len(dl_lines) + 1)
            ry3 = body_bottom - activity_reserved - downloads_reserved
            if ry3 > list_top + 2:
                self.safe_addstr(ry3, right_x, " DOWNLOADS ".ljust(max(0, right_w), "─")[: max(0, right_w)], curses.color_pair(2))
                ry3 += 1
                bottom_limit = body_bottom - activity_reserved
                for line in dl_lines:
                    if ry3 >= bottom_limit:
                        break
                    if isinstance(line, tuple):
                        text, attr = line
                    else:
                        text, attr = str(line), curses.color_pair(6)
                    self.safe_addstr(ry3, right_x, text[: max(0, right_w)].ljust(max(0, right_w)), attr)
                    ry3 += 1

        if activity_reserved:
            ry2 = body_bottom - activity_reserved
            if ry2 > list_top + 2:
                self.safe_addstr(ry2, right_x, " ACTIVITY ".ljust(max(0, right_w), "─")[: max(0, right_w)], curses.color_pair(2))
                ry2 += 1
                for msg in self.download_log[:7]:
                    if ry2 >= body_bottom:
                        break
                    label = "ERR " if msg.lower().startswith(("error", "resume error")) else "DONE"
                    line = f"{label}  {msg}"
                    attr = curses.color_pair(5) if label.strip() == "ERR" else curses.color_pair(6)
                    self.safe_addstr(ry2, right_x, line[: max(0, right_w)].ljust(max(0, right_w)), attr)
                    ry2 += 1

    def render(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        if self.term_too_small():
            self.safe_addstr(0, 0, "Terminal too small.", curses.color_pair(5) | curses.A_BOLD)
            self.safe_addstr(2, 0, f"Need at least {MIN_W}x{MIN_H}. Current: {w}x{h}", curses.color_pair(6))
            self.safe_addstr(4, 0, "Resize your terminal window.", curses.color_pair(6))
            self.stdscr.refresh()
            return

        y = self.draw_banner(w)
        y = self.draw_top_status(y, w)
        y = self.draw_menu_bar(y, w)

        if self.mode == "ERROR":
            self.safe_addstr(y + 1, 0, ("ERROR: " + self.status)[: max(0, w - 1)], curses.color_pair(5))
        elif self.mode == "HELP":
            self.draw_help(y)
        elif self.mode == "PREVIEW_DL":
            self.draw_preview(y)
        else:
            if self.show_welcome and not self.results and self.mode in ("RESULTS", "SEARCH"):
                self.draw_welcome(y)
            self.draw_panels(y)

        if self.help_overlay:
            self.draw_help_overlay()

        self.draw_footer(h, w)
        self.stdscr.refresh()

    # ---------- menu actions ----------
    def activate_menu_action(self, action: str) -> None:
        if action == "noop":
            return

        if action == "quit":
            self.request_quit()
            return

        if action == "actions":
            self.open_action_palette()
            return

        if action == "search_tools":
            self.open_search_tools()
            return

        if action == "source_switch":
            self.choose_search_source()
            return

        if action == "help":
            self.toggle_help_overlay()
            return

        if action == "theme":
            self.cycle_theme()
            return

        if action == "audit":
            self.show_audit_summary()
            return

        if action == "favs":
            self.mode = "FAVS"
            self.focus = "LIST"
            self.menu_idx = 0
            self.favs_idx = 0
            if self.favs_tab not in ("ITEMS", "FILES", "FOLDERS"):
                self.favs_tab = "ITEMS"
            self.status = "Favorites. Use Tab for menu, or arrows for list."
            return

        if action == "queue_view":
            self.menu_idx = 0
            self.open_download_queue()
            return

        if action == "license_gate":
            self.enforce_license_gate = not self.enforce_license_gate
            self.status = "License gate: ON (blocks unclear rights)" if self.enforce_license_gate else "License gate: OFF (warns only)"
            return

        if action == "back":
            if self.mode == "FILES":
                self.cancel_file_load()
                self.save_current_file_view_state()
                self.mode = "RESULTS"
                self.focus = "LIST"
                self.status = "Back to results"
                return
            if self.mode == "HELP":
                self.mode = "FILES" if self.files else "RESULTS"
                self.focus = "LIST"
                self.status = "Back"
                return
            if self.mode == "FAVS":
                self.mode = "FILES" if self.files else "RESULTS"
                self.focus = "LIST"
                self.status = "Back"
                return
            if self.mode == "QUEUE":
                self.mode = self.queue_return_mode
                self.focus = "LIST"
                self.status = "Back"
                return

        if self.mode == "QUEUE":
            if action == "queue_cancel":
                self.cancel_selected_queue_job()
                return
            if action == "queue_remove":
                self.remove_selected_queue_job()
                return

        if self.mode in ("RESULTS", "SEARCH"):
            if action == "search":
                s = self.prompt("Search: ", self.query_text, history=self.search_history)
                if s is not None:
                    self.query_text = s
                    self.show_welcome = False
                    self.start_search_async(reset_page=True)
                return
            if action == "combined_search":
                s = self.prompt("Combined IA + YouTube search: ", self.query_text, history=self.search_history)
                if s is not None:
                    if s.strip():
                        self.start_combined_search_async(s.strip())
                    else:
                        self.status = "Combined search canceled."
                return
            if action == "youtube_search":
                s = self.prompt("YouTube search: ", self.query_text, history=self.search_history)
                if s is not None:
                    if s.strip():
                        self.start_youtube_search_async(s.strip())
                    else:
                        self.status = "YouTube search canceled."
                return
            if action == "youtube_url":
                s = self.prompt("YouTube URL: ", "", history=self.search_history)
                if s is not None:
                    if s.strip():
                        self.start_youtube_url_async(s.strip())
                    else:
                        self.status = "YouTube URL canceled."
                return
            if action == "search_preset":
                preset_labels = [label for label, _key in archive_query_preset_labels()]
                pick = self.prompt_list("Archive search preset", preset_labels)
                if not pick:
                    self.status = "Search preset canceled."
                    return
                preset = None
                for label, key in archive_query_preset_labels():
                    if label == pick:
                        preset = key
                        break
                if not preset:
                    self.status = "Search preset canceled."
                    return
                extra = self.prompt("Extra search text (blank for preset only): ", getattr(self, "query_text", ""))
                if extra is None:
                    self.status = "Search preset canceled."
                    return
                try:
                    query = build_archive_preset_query(preset, extra or "", getattr(self, "title_only", False))
                except ValueError as e:
                    self.status = str(e)
                    return
                self.set_query_and_search(extra or preset, built_query=query)
                return
            if action == "search_attempts":
                self.choose_search_attempt()
                return
            if action == "collection_search":
                s = self.prompt("Find collections: ", "")
                if s is not None:
                    query = build_collection_search_query(s)
                    self.set_query_and_search(s or "collections", built_query=query)
                return
            if action == "field_search":
                fields = ["title", "creator", "subject", "description", "identifier", "collection", "date", "publicdate", "licenseurl"]
                field = self.prompt_list("IA field", fields)
                if not field:
                    self.status = "Field search canceled."
                    return
                value = self.prompt(f"{field}: ", "")
                if value is not None:
                    query = build_field_query(field, value, self.filter)
                    self.set_query_and_search(f"{field}:{value}", built_query=query)
                return
            if action == "within_collection":
                choices = self.collection_choices_from_results()
                selected = self.selected_result()
                if selected and selected.collection:
                    first = normalize_collection_identifier(selected.collection)
                    if first:
                        first_label = f"{first} (selected)"
                        choices = [first_label] + [c for c in choices if self._collection_from_choice(c) != first]
                if choices:
                    pick = self.prompt_list("Search within collection", choices + ["Type collection identifier"])
                    if not pick:
                        self.status = "Collection search canceled."
                        return
                    coll = self.prompt("Collection identifier: ", "") if pick == "Type collection identifier" else self._collection_from_choice(pick).replace(" (selected)", "")
                else:
                    coll = self.prompt("Collection identifier: ", "")
                if coll is not None:
                    s = self.prompt("Search text inside collection (blank for all): ", self.query_text)
                    if s is not None:
                        query = build_within_collection_query(s, coll, self.filter, self.title_only)
                        self.set_query_and_search(s or f"collection:{coll}", built_query=query)
                return
            if action == "collection_facets":
                choices = self.collection_choices_from_results()
                if not choices:
                    self.status = "No collections found on this result page."
                    return
                pick = self.prompt_list("Collections on this page", choices)
                if pick:
                    coll = self._collection_from_choice(pick)
                    query = build_within_collection_query(self.query_text, coll, self.filter, self.title_only)
                    self.set_query_and_search(self.query_text or f"collection:{coll}", built_query=query)
                return
            if action == "history":
                if not self.search_history:
                    self.status = "No search history yet."
                    return
                pick = self.prompt_list("Search History", self.search_history)
                if pick:
                    self.query_text = pick
                    self.show_welcome = False
                    self.start_search_async(reset_page=True)
                return
            if action == "filter":
                changed = self.choose_filter()
                if changed and self.query_text:
                    self.start_search_async(reset_page=True)
                return
            if action == "sort":
                changed = self.choose_sort()
                if changed and self.query_text:
                    self.start_search_async(reset_page=True)
                return
            if action == "result_filter":
                s = self.prompt("Local result filter (blank clears): ", self.result_filter)
                if s is not None:
                    self.set_result_filter(s)
                return
            if action == "clear_result_filter":
                self.clear_result_filter()
                return
            if action == "title":
                self.title_only = not self.title_only
                self.status = "Search mode: title" if self.title_only else "Search mode: broad"
                if self.query_text:
                    self.start_search_async(reset_page=True)
                return
            if action == "toggle_hide_small_items":
                self.hide_small_items = not self.hide_small_items
                self._save_session()
                state = "on" if self.hide_small_items else "off"
                # The filter is now applied when results are fetched (so a
                # filtered page still comes back full via backfill), so
                # self.results no longer carries small items to reveal
                # locally -- flipping the toggle re-runs the search instead.
                if getattr(self, "query_text", ""):
                    self.status = f"Hide small items: {state} (<{self.min_item_size_mb}MB) — refreshing results..."
                    self.start_search_async(reset_page=True)
                else:
                    n = len(self.get_visible_results())
                    self.status = f"Hide small items: {state} (<{self.min_item_size_mb}MB) — {n} visible"
                return
            if action == "edit_min_item_size":
                s = self.prompt("Minimum item size in MB (0 = no minimum): ", str(self.min_item_size_mb))
                if s is not None:
                    try:
                        mb = max(0, int(str(s).strip() or 0))
                    except ValueError:
                        self.status = "Minimum item size must be a whole number of MB."
                        return
                    self.min_item_size_mb = mb
                    self._save_session()
                    if getattr(self, "query_text", ""):
                        self.status = f"Minimum item size: {mb}MB — refreshing results..."
                        self.start_search_async(reset_page=True)
                    else:
                        n = len(self.get_visible_results())
                        self.status = f"Minimum item size: {mb}MB — {n} visible"
                return
            if action == "next_page":
                self.next_page()
                return
            if action == "prev_page":
                self.prev_page()
                return
            if action == "open":
                self.open_selected_result()
                return
            if action == "details":
                selected = self.selected_result()
                if not selected:
                    self.status = "No result selected."
                    return
                status, reason = license_status_from_fields(selected.licenseurl, selected.rights)
                self.status = f"Details: {selected.identifier} | {selected.mediatype or '?'} | license {status}: {reason}"
                return
            if action == "fav_item":
                selected = self.selected_result()
                if not selected:
                    self.status = "No result selected."
                    return
                self.toggle_fav_item(selected)
                return

        if self.mode == "FILES":
            visible = self.get_visible_files()

            if action == "keyword":
                self.choose_file_filter_action()
                return

            if action == "toggle_file_mark":
                self.toggle_current_file_mark()
                self.focus = "LIST"
                return

            if action == "mark_file_range":
                self.mark_file_range()
                self.focus = "LIST"
                return

            if action == "mark_all_visible":
                self.mark_all_visible_files()
                self.focus = "LIST"
                return

            if action == "invert_visible_marks":
                self.invert_visible_file_marks()
                self.focus = "LIST"
                return

            if action == "clear_file_marks":
                self.clear_file_marks()
                self.focus = "LIST"
                return

            if action == "video_only":
                self.video_only = not self.video_only
                self.sel_f = 0
                self.status = "Video only: ON" if self.video_only else "Video only: OFF (showing all files)"
                self.save_current_file_view_state()
                return

            if action == "toggle_hide_small_video_files":
                self.hide_small_video_files = not self.hide_small_video_files
                self.sel_f = 0
                self._save_session()
                state = "on" if self.hide_small_video_files else "off"
                n = len(self.get_visible_files())
                self.status = f"Hide small videos: {state} (<{self.min_video_file_size_mb}MB) — {n} visible"
                return

            if action == "edit_min_video_file_size":
                s = self.prompt("Minimum video file size in MB (0 = no minimum): ", str(self.min_video_file_size_mb))
                if s is not None:
                    try:
                        mb = max(0, int(str(s).strip() or 0))
                    except ValueError:
                        self.status = "Minimum video file size must be a whole number of MB."
                        return
                    self.min_video_file_size_mb = mb
                    self.sel_f = 0
                    self._save_session()
                    n = len(self.get_visible_files())
                    self.status = f"Minimum video file size: {mb}MB — {n} visible"
                return

            if action == "bucket":
                self.cycle_bucket()
                return

            if action == "preview":
                self.set_preview_for_selected()
                return

            if action == "folder":
                self.set_preview_for_prefix()
                return

            if action == "item":
                self.set_preview_for_item()
                return

            if action == "download":
                self.set_preview_for_marked()
                return

            if action == "retry_failed":
                self.retry_failed_downloads()
                return

            if action == "fav_file":
                item = self.selected_result()
                if not item or not visible:
                    self.status = "No file selected."
                    return
                idx = self.sel_f
                if 0 <= idx < len(visible):
                    self.toggle_fav_file(item, visible[idx])
                else:
                    self.status = "Bad selection."
                return

        if self.mode == "PREVIEW_DL":
            if action == "confirm_download":
                self.perform_download_plan()
                return
            if action == "cancel_preview":
                self.mode = "FILES"
                self.focus = "LIST"
                self.status = "Canceled."
                return

        if self.mode == "FAVS":
            if action == "tab":
                order = ["ITEMS", "FILES", "FOLDERS"]
                try:
                    i = order.index(self.favs_tab)
                except ValueError:
                    i = 0
                self.favs_tab = order[(i + 1) % len(order)]
                self.favs_idx = 0
                self.status = f"Favorites tab: {self.favs_tab}"
                return

            if action == "remove":
                if self.favs_tab == "ITEMS":
                    lst = self.favs.get("items") or []
                    if lst and 0 <= self.favs_idx < len(lst):
                        removed = lst.pop(self.favs_idx)
                        self.favs["items"] = lst
                        self.favs_idx = max(0, min(self.favs_idx, len(lst) - 1))
                        self.save_favs()
                        self.status = f"Removed: {removed.get('title') or removed.get('identifier', '?')}"
                    else:
                        self.status = "Nothing to remove."
                elif self.favs_tab == "FILES":
                    lst = self.favs.get("files") or []
                    if lst and 0 <= self.favs_idx < len(lst):
                        removed = lst.pop(self.favs_idx)
                        self.favs["files"] = lst
                        self.favs_idx = max(0, min(self.favs_idx, len(lst) - 1))
                        self.save_favs()
                        self.status = f"Removed: {removed.get('filename', '?')}"
                    else:
                        self.status = "Nothing to remove."
                elif self.favs_tab == "FOLDERS":
                    folders = self.favs.get("folders") or {}
                    flat: List[Tuple[str, str]] = []
                    for b in ("TV", "Movies", "Music", "Other"):
                        for n in (folders.get(b) or []):
                            flat.append((b, n))
                    if flat and 0 <= self.favs_idx < len(flat):
                        bucket, name = flat[self.favs_idx]
                        self.favs["folders"][bucket] = [n for n in (folders.get(bucket) or []) if n != name]
                        self.favs_idx = max(0, min(self.favs_idx, len(flat) - 2))
                        self.save_favs()
                        self.status = f"Removed folder: {name}"
                    else:
                        self.status = "Nothing to remove."
                return

            if action == "primary":
                if self.favs_tab == "ITEMS":
                    lst = self.favs.get("items") or []
                    if lst and 0 <= self.favs_idx < len(lst):
                        it = lst[self.favs_idx]
                        fav_sr = SearchResult(
                            identifier=it.get("identifier", ""),
                            title=it.get("title", ""),
                            year=it.get("year", ""),
                            creator=it.get("creator", ""),
                        )
                        existing = [i for i, r in enumerate(self.results) if r.identifier == fav_sr.identifier]
                        if existing:
                            self.sel_r = existing[0]
                        else:
                            self.results.insert(0, fav_sr)
                            self.sel_r = 0
                        self.mode = "RESULTS"
                        self.focus = "LIST"
                        self.load_files()
                    else:
                        self.status = "No item selected."
                elif self.favs_tab == "FILES":
                    lst = self.favs.get("files") or []
                    if lst and 0 <= self.favs_idx < len(lst):
                        it = lst[self.favs_idx]
                        ident = it.get("identifier", "")
                        fname = it.get("filename", "")
                        fav_sr = SearchResult(
                            identifier=ident,
                            title=it.get("item_title", ident),
                            year="",
                            creator="",
                        )
                        existing = [i for i, r in enumerate(self.results) if r.identifier == ident]
                        if existing:
                            self.sel_r = existing[0]
                        else:
                            self.results.insert(0, fav_sr)
                            self.sel_r = 0
                        self.mode = "RESULTS"
                        self.focus = "LIST"
                        self.load_files()
                        for i, f in enumerate(self.files):
                            if f.name == fname:
                                self.sel_f = i
                                break
                        self.status = f"Files loaded. Selected: {fname}"
                    else:
                        self.status = "No file selected."
                elif self.favs_tab == "FOLDERS":
                    folders = self.favs.get("folders") or {}
                    flat2: List[Tuple[str, str]] = []
                    for b in ("TV", "Movies", "Music", "Other"):
                        for n in (folders.get(b) or []):
                            flat2.append((b, n))
                    if flat2 and 0 <= self.favs_idx < len(flat2):
                        bucket, name = flat2[self.favs_idx]
                        self.last_bucket = bucket
                        self.status = f"Bucket set to {bucket}. Folder: {name}"
                    else:
                        self.status = "No folder selected."
                return

    # ---------- input loop ----------
    def loop(self) -> None:
        ensure_dirs()
        self.init_colors()
        curses.curs_set(0)
        self.stdscr.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except Exception:
            pass
        try:
            self.stdscr.timeout(100)
        except Exception:
            pass

        if self.ia_present:
            self.status = f"Ready (ia: {self.ia_version}). Choose [Search]."
        else:
            self.status = self.ia_version

        self._restore_session()

        pending = self._load_pending()
        if pending:
            ptitle = pending.get("item_title") or pending.get("identifier") or "unknown"
            n_remaining = len([f for f in (pending.get("files") or [])
                               if f.get("name") not in set(pending.get("completed_names") or [])])
            self.status = f"Pending: \"{ptitle}\" ({n_remaining} file(s) left) — press R to resume"

        while not self.exit_requested:
            self.finish_search_load_if_ready()
            self.finish_file_load_if_ready()
            self.finish_download_progress()
            self.finish_dvd_scans_if_ready()
            self.finish_post_import_integrations_if_ready()
            self.render()
            ch = self.stdscr.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_MOUSE and self.handle_mouse_event():
                continue

            if self.help_overlay:
                if ch in (27, ord('?'), curses.KEY_BACKSPACE, 127, 8):
                    self.help_overlay = False
                    self.status = "Help closed"
                    continue
                if ch in (ord("q"), ord("Q")):
                    self.help_overlay = False
                    self.status = "Help closed"
                    continue
                continue

            if ch == ord("q"):
                if self.mode == "PREVIEW_DL":
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.status = "Canceled."
                    continue
                if self.mode == "QUEUE":
                    self.mode = self.queue_return_mode
                    self.focus = "LIST"
                    self.status = "Back"
                    continue
                self.request_quit()
                continue

            if self.mode == "ERROR" or self.term_too_small():
                continue

            if ch == ord('?'):
                self.toggle_help_overlay()
                continue

            if ch in (ord('T'),):
                self.cycle_theme()
                continue

            if ch == 9:  # Tab
                self.focus = "LIST" if self.focus == "MENU" else "MENU"
                self.status = "Focus: MENU" if self.focus == "MENU" else "Focus: LIST"
                continue

            items = self.get_menu_items()
            if self.focus == "MENU":
                if ch == curses.KEY_LEFT:
                    if items:
                        self.menu_idx = max(0, self.menu_idx - 1)
                    continue
                if ch == curses.KEY_RIGHT:
                    if items:
                        self.menu_idx = min(len(items) - 1, self.menu_idx + 1)
                    continue
                if is_enter_key(ch):
                    if items and 0 <= self.menu_idx < len(items):
                        _label, action = items[self.menu_idx]
                        self.activate_menu_action(action)
                    continue

            if ch == ord('a'):
                self.open_action_palette()
                continue

            if ch in (ord('y'), ord('Y')) and self.mode in ("RESULTS", "SEARCH", "FILES", "FAVS"):
                self.show_audit_summary()
                continue

            if ch in (ord('Q'),) and self.mode in ("RESULTS", "SEARCH", "FILES", "FAVS"):
                self.open_download_queue()
                continue

            if self.mode == "QUEUE" and self.focus == "LIST":
                if ch in (curses.KEY_UP, ord('k')):
                    self.queue_sel = max(0, self.queue_sel - 1)
                    continue
                if ch in (curses.KEY_DOWN, ord('j')):
                    jobs = self._queue_display_order()
                    self.queue_sel = min(max(0, len(jobs) - 1), self.queue_sel + 1)
                    continue
                if ch in (ord('c'), ord('C')):
                    self.cancel_selected_queue_job()
                    continue
                if ch in (ord('x'), ord('X'), curses.KEY_DC):
                    self.remove_selected_queue_job()
                    continue
                if ch in (27, curses.KEY_BACKSPACE, 127, 8):
                    self.mode = self.queue_return_mode
                    self.focus = "LIST"
                    self.status = "Back"
                    continue

            if ch in (ord('/'), ord('s'), ord('S')):
                s = self.prompt("Search: ", self.query_text, history=self.search_history)
                if s is not None:
                    self.query_text = s
                    self.show_welcome = False
                    self.start_search_async(reset_page=True)
                continue

            if ch == ord('R') and self.mode not in ("PREVIEW_DL", "QUEUE"):
                self.resume_or_retry_download()
                continue

            if ch == 27:
                if bool(getattr(self, "_search_load_loading", False)):
                    self.cancel_search_load()
                    self.status = "Search canceled."
                    continue
                if self.mode == "PREVIEW_DL":
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.status = "Canceled."
                continue

            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if self.mode == "FILES":
                    self.cancel_file_load()
                    self.save_current_file_view_state()
                    self.mode = "RESULTS"
                    self.focus = "LIST"
                    self.status = "Back to results"
                elif self.mode == "FAVS":
                    self.mode = "FILES" if self.files else "RESULTS"
                    self.focus = "LIST"
                    self.status = "Back"
                elif self.mode == "HELP":
                    self.mode = "FILES" if self.files else "RESULTS"
                    self.focus = "LIST"
                    self.status = "Back"
                elif self.mode == "PREVIEW_DL":
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.status = "Canceled."
                continue

            if self.handle_files_hotkey(ch):
                continue

            if self.focus == "LIST":
                if self.mode in ("RESULTS", "SEARCH"):
                    visible_results = self.get_visible_results()
                    if ch in (curses.KEY_UP, ord('k')) and visible_results:
                        self.sel_r = max(0, self.sel_r - 1)
                        continue
                    if ch in (curses.KEY_DOWN, ord('j')) and visible_results:
                        self.sel_r = min(len(visible_results) - 1, self.sel_r + 1)
                        continue
                    if ch in (ord('g'), curses.KEY_HOME) and visible_results:
                        self.sel_r = 0
                        continue
                    if ch in (ord('G'), curses.KEY_END) and visible_results:
                        self.sel_r = len(visible_results) - 1
                        continue
                    if self.handle_results_hotkey(ch):
                        continue
                    if ch == ord('r'):
                        self.activate_menu_action("details")
                        continue
                    if ch in (ord('o'), ord('O')) or is_enter_key(ch):
                        self.open_selected_result()
                        continue
                    if ord('0') <= ch <= ord('9') and self.results:
                        first = chr(ch)
                        val = self.prompt("Jump/select result #: ", first)
                        if val is not None and val.strip().isdigit():
                            self.jump_to_result_number(int(val.strip()))
                        else:
                            self.status = "Canceled."
                        continue
                    if ch in (ord('n'), ord(']'), curses.KEY_NPAGE):
                        self.next_page()
                        continue
                    if ch in (ord('p'), ord('['), curses.KEY_PPAGE):
                        self.prev_page()
                        continue
                    if ch == ord('#') and self.total_results > 0:
                        total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
                        val = self.prompt(f"Go to page (1-{total_pages}): ", "")
                        if val is not None and val.strip().isdigit():
                            target = int(val.strip())
                            if 1 <= target <= total_pages:
                                self.page = target
                                self.start_search_async(reset_page=False)
                            else:
                                self.status = f"Page must be 1-{total_pages}."
                        continue

                if self.mode == "FILES":
                    visible = self.get_visible_files()
                    if ch in (curses.KEY_UP, ord('k')) and visible:
                        self.sel_f = max(0, self.sel_f - 1)
                        continue
                    if ch in (curses.KEY_DOWN, ord('j')) and visible:
                        self.sel_f = min(len(visible) - 1, self.sel_f + 1)
                        continue
                    if ch in (ord('g'), curses.KEY_HOME) and visible:
                        self.sel_f = 0
                        continue
                    if ch in (ord('G'), curses.KEY_END) and visible:
                        self.sel_f = len(visible) - 1
                        continue
                    if is_enter_key(ch):
                        self.set_preview_for_selected()
                        continue

                if self.mode == "FAVS":
                    if self.favs_tab == "ITEMS":
                        favs_len = len(self.favs.get("items") or [])
                    elif self.favs_tab == "FILES":
                        favs_len = len(self.favs.get("files") or [])
                    else:
                        folders = self.favs.get("folders") or {}
                        favs_len = sum(len(folders.get(b) or []) for b in ("TV", "Movies", "Music", "Other"))
                    if ch in (curses.KEY_UP, ord('k')) and favs_len:
                        self.favs_idx = max(0, self.favs_idx - 1)
                        continue
                    if ch in (curses.KEY_DOWN, ord('j')) and favs_len:
                        self.favs_idx = min(favs_len - 1, self.favs_idx + 1)
                        continue
                    if ch in (ord('g'), curses.KEY_HOME) and favs_len:
                        self.favs_idx = 0
                        continue
                    if ch in (ord('G'), curses.KEY_END) and favs_len:
                        self.favs_idx = favs_len - 1
                        continue
                    if is_enter_key(ch):
                        self.activate_menu_action("primary")
                        continue

        # exit
        self.request_jellyfin_rescan_if_needed()
        self._save_session()


def main(stdscr):
    app = RetroWaveIA(stdscr)
    app.loop()


def cli_main(argv: Optional[List[str]] = None) -> int:
    set_process_umask()
    parser = argparse.ArgumentParser(
        prog="ia_minotaur",
        description="Full-screen Internet Archive browser/downloader.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check required commands and writable app directories without launching curses.",
    )
    parser.add_argument(
        "--scan-dvd-iso",
        metavar="PATH",
        help="Scan a staged DVD ISO with lsdvd and HandBrakeCLI, then write logs next to the ISO.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview supported non-curses actions without writing scan logs or moving files.",
    )
    args = parser.parse_args(argv)

    if args.check:
        return print_environment_check()
    if args.scan_dvd_iso:
        iso_path = os.path.abspath(os.path.expanduser(args.scan_dvd_iso))
        result = ia_dvd.scan_dvd_iso(iso_path, dry_run=args.dry_run)
        print(f"ISO: {result.iso_path}")
        print(f"Logs: {result.logs_dir}")
        print(f"Layout: {result.layout}")
        print(f"Reason: {result.reason}")
        if result.dry_run:
            print("Dry run: no scan commands were executed and no files were written.")
        else:
            print(f"lsdvd: {result.lsdvd_log}")
            print(f"HandBrakeCLI: {result.handbrake_log}")
            print(f"Analysis: {result.analysis_path}")
            if result.errors:
                for err in result.errors:
                    print(f"Warning: {err}")
        return 0 if result.ok else 1

    curses.wrapper(main)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
