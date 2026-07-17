#!/usr/bin/env python3
import argparse
import curses
import os
import re
import shlex
import shutil
import threading
import sys
import textwrap
import time
from typing import List, Tuple, Optional, Dict, Any, Set

from ia_common import (
    IAFile,
    SearchResult,
    VIDEO_EXTS,
    VIDEO_FORMAT_HINTS,
    compact_count,
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
    safe_staging_file_path,
    staging_file_path,
)
import ia_api
import ia_audit
import ia_config
import ia_downloads
import ia_dvd
import ia_minotaur_events
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
    build_within_collection_query,
    build_query,
    detect_sxxeyy,
    infer_bucket,
    is_openly_licensed,
    license_status_from_fields,
    normalize_collection_identifier,
    replace_mediatype_filter,
    sanitize_folder,
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

MIN_H = 18
MIN_W = 70

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


def ia_search_via_curl(query: str, rows: int, page: int, sort: str = "") -> Tuple[List[SearchResult], int, str]:
    return ia_api.ia_search_via_curl(query, rows, page, sort, runner=run_cmd)


def ia_files(identifier: str) -> Tuple[List[IAFile], Optional[Dict[str, Any]], str]:
    return ia_api.ia_files(identifier, runner=run_cmd)


def yt_search(query: str, rows: int = 10) -> Tuple[List[SearchResult], int, str]:
    return yt_api.yt_search(query, rows, yt_dlp_path=APP_CONFIG["yt_dlp_path"], runner=run_cmd)


def yt_metadata_url(url: str) -> Tuple[Optional[SearchResult], str]:
    return yt_api.yt_metadata_url(url, yt_dlp_path=APP_CONFIG["yt_dlp_path"], runner=run_cmd)


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
        self.queue_status: List[Dict[str, Any]] = []
        self.failed_queue: List[IAFile] = []
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

        self.dl_current_name: str = ""
        self.dl_current_written: int = 0
        self.dl_current_total: int = 0
        self.dl_speed_bps: float = 0.0
        self.dl_eta_s: float = 0.0
        self.dl_overall_written: int = 0
        self.dl_overall_total: int = 0
        self.dl_cancel_requested: bool = False
        self.dl_complete_notice: str = ""

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
        if self.theme_name == "Minimal":
            colors = (
                curses.COLOR_WHITE,
                curses.COLOR_CYAN,
                curses.COLOR_WHITE,
                curses.COLOR_GREEN,
                curses.COLOR_RED,
                curses.COLOR_WHITE,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
            )
            backs = (-1, -1, -1, -1, -1, -1, curses.COLOR_WHITE, curses.COLOR_CYAN, curses.COLOR_YELLOW)
        elif self.theme_name == "High contrast":
            colors = (
                curses.COLOR_YELLOW,
                curses.COLOR_CYAN,
                curses.COLOR_YELLOW,
                curses.COLOR_GREEN,
                curses.COLOR_RED,
                curses.COLOR_WHITE,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
            )
            backs = (-1, -1, -1, -1, -1, -1, curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_YELLOW)
        else:
            colors = (
                curses.COLOR_MAGENTA,
                curses.COLOR_CYAN,
                curses.COLOR_YELLOW,
                curses.COLOR_GREEN,
                curses.COLOR_RED,
                curses.COLOR_WHITE,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
                curses.COLOR_BLACK,
            )
            backs = (-1, -1, -1, -1, -1, -1, curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_YELLOW)
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
        elif self.mode == "DOWNLOADING":
            header = "Downloading..."
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
        elif self.mode == "DOWNLOADING":
            crumbs.append("Downloading")
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

    def _start_search_prefetch(self, query: str, total_pages: int, sort_by: str, current_page: int) -> None:
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
                page_results, _page_total, err = ia_search_via_curl(query, rows=ROWS_PER_PAGE, page=page, sort=sort_by)
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
            self._start_search_prefetch(query, total_pages, getattr(self, "sort_by", ""), current_page)
            return

        if not loading and total_pages > loaded_pages:
            self._start_search_prefetch(query, total_pages, getattr(self, "sort_by", ""), current_page)

    def _load_all_search_results(self) -> List[SearchResult]:
        self._ensure_search_cache_state()
        with self._search_cache_lock:
            if self._all_results_cache_key == self._search_cache_key() and self._all_results_cache:
                return list(self._all_results_cache)
        return list(getattr(self, "results", []) or [])

    def get_visible_results(self) -> List[SearchResult]:
        needle = self.result_filter.strip().lower()
        results = getattr(self, "results", [])
        if not needle:
            return list(results)
        terms = [t for t in needle.split() if t]
        scope = self._load_all_search_results()
        return [r for r in scope if all(t in self.result_filter_blob(r) for t in terms)]

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
        else:
            self.cancel_result_prefetch()
        n = len(self.get_visible_results())
        progress = self.local_filter_progress_label()
        suffix = f"; {progress}" if progress else ""
        self.status = f"Local result filter: {self.result_filter or '(none)'} ({n} visible{suffix})"
        self._save_session()

    def clear_result_filter(self) -> None:
        if not self.result_filter:
            self.status = "Local result filter already clear."
            return
        self.result_filter = ""
        self.sel_r = 0
        self.cancel_result_prefetch()
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
        return chips

    def effective_search_total(self, page: int, results: List[SearchResult], reported_total: int) -> int:
        page_num = max(1, int(page or 1))
        visible_count = len(results or [])
        reported = int(reported_total or 0)
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
                ("Help", "help"),
                ("Quit", "quit"),
            ]
        if self.mode == "PREVIEW_DL":
            return [("Confirm", "confirm_download"), ("Cancel", "cancel_preview"), (f"Theme: {theme}", "theme")]
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
        return "j/k navigate   Enter select   a actions   ? help   q quit"

    def hint_bar(self, include_overlay_state: bool = True) -> str:
        if include_overlay_state and self.help_overlay:
            return "?/Esc closes help  |  Backspace back  |  q quit"
        if self.mode == "DOWNLOADING":
            return "c cancel  |  q quit after cancel  |  progress updates live"
        if self.mode in ("RESULTS", "SEARCH"):
            return "Enter/o open  |  / search  |  l/f local filter  |  L/F clear local  |  a actions  |  n/p page  |  ? help  |  q quit"
        if self.mode == "FILES":
            return "Enter/p preview  |  o folder  |  Space mark+next  |  m range  |  d marked  |  D all visible  |  f filter  |  ? help"
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
        if h < 6 or w < 10:
            return None

        y = h - 5
        buf = list(default)
        pos = len(buf)
        hist_idx = -1
        saved_buf = ""
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
                bar = f"{label}{text}"
                hint = ""
                if history:
                    hint = "  (Up/Down for history)"
                self.safe_addstr(y, 0, " " * max(0, w - 1), curses.color_pair(8))
                self.safe_addstr(y, 0, (bar + hint)[: max(0, w - 1)], curses.color_pair(8))
                try:
                    self.stdscr.move(y, min(w - 2, len(label) + pos))
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
            ("Local / clear filter", "clear_result_filter", "L/F"),
            ("Search / collections (mediatype collection)", "collection_search", None),
            ("Search / fields (title creator subject date collection)", "field_search", None),
            ("Search / inside collection (collection identifier)", "within_collection", None),
            ("Search / result collections (facet narrow)", "collection_facets", None),
            ("Filter / media type (movies audio texts software any)", "filter", None),
            ("Filter / local result refine (loaded results)", "result_filter", "l/f"),
            ("Filter / title-only mode (title exact)", "title", None),
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
            ("Download / marked files", "download", "d"),
            ("Download / retry failed files retry failed", "retry_failed", "R"),
            ("Download / folder prefix folder", "folder", "o"),
            ("Download / all visible files all", "item", "D"),
            ("Filter / file filter menu", "keyword", "f/F"),
            ("Filter / video only", "video_only", "v"),
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

    def do_search(self, reset_page: bool = True, built_query: Optional[str] = None) -> None:
        self.cancel_file_load()
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
        for label, query in attempts:
            self.query_built = query
            self.results, self.total_results, err = ia_search_via_curl(query, rows=ROWS_PER_PAGE, page=self.page, sort=self.sort_by)
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
        search_hint = "" if used_label in ("", "title", "custom", "advanced") else f" ({used_label} match)"
        if self.total_results > 0:
            total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            self.status = f"Page {self.page}/{total_pages} — {self.total_results} total results{search_hint}. Arrows to select, Enter to open."
        else:
            self.status = f"Page {self.page} — {len(self.results)} results{search_hint}. Arrows to select, Enter to open."

    def start_search_async(self, reset_page: bool = True, built_query: Optional[str] = None) -> None:
        self._ensure_search_load_state()
        self.cancel_file_load()
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
        self.query_built = attempts[0][1]
        self._add_to_history(self.query_text)
        self._save_session()
        self.show_welcome = False
        self.status = "Searching... press Esc to cancel waiting."
        self.last_search_attempts = list(attempts)
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

        def worker() -> None:
            used_label = ""
            last_err = ""
            final_results: List[SearchResult] = []
            final_total = 0
            final_query = ""
            for label, query in attempts:
                results, total, err = ia_search_via_curl(query, rows=ROWS_PER_PAGE, page=page, sort=sort_by)
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
                "attempts": list(attempts),
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

        def worker() -> None:
            ia_results: List[SearchResult] = []
            ia_total = 0
            ia_query = ""
            ia_label = ""
            ia_err = ""
            for label, query in attempts:
                results, total, err = ia_search_via_curl(query, rows=ROWS_PER_PAGE, page=1, sort=sort_by)
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
            search_hint = "" if self.last_search_used_label in ("", "title", "custom", "advanced") else f" ({self.last_search_used_label} match)"
            if self.total_results > 0:
                self.status = f"Page {self.page}/{total_pages} — {self.total_results} total results{search_hint}. Arrows to select, Enter to open."
            else:
                self.status = f"Page {self.page} — {len(self.results)} results{search_hint}. Arrows to select, Enter to open."
        elif source == "youtube":
            self.result_filter = ""
            self.status = f"YouTube — {len(self.results)} result(s). [YT] rows open as single-video downloads."
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
        self.status = f"YouTube — {len(results)} result(s). [YT] rows open as single-video downloads."

    def do_youtube_url(self, url: str) -> None:
        self.cancel_file_load()
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
            self._file_load_result = {"identifier": ident, "title": title, "pending": True}

        self.cur_meta = None
        self.files = []
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
                    "files": files,
                    "meta": meta,
                    "err": err,
                    "exc": "",
                }
            except Exception as e:
                result = {
                    "identifier": ident,
                    "title": title,
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
        self.restore_file_view_state(ident)
        self.mode = "FILES"
        self.focus = "LIST"
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
        try:
            item = self.selected_result()
        except Exception:
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

    def get_visible_files(self) -> List[IAFile]:
        files = list(self.files)
        if self.video_only:
            files = [f for f in files if is_video_file(f.name, f.fmt)]
        kw = self.file_kw.strip()
        if kw:
            rx = re.compile(re.escape(kw), re.IGNORECASE)
            files = [f for f in files if rx.search(f.name) or rx.search(f.fmt)]
        return files

    def get_marked_visible_files(self) -> List[IAFile]:
        ordered_names = self._ordered_selected_file_names()
        if not ordered_names:
            return []
        visible_by_name = {f.name: f for f in self.files}
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
        chips = []
        if bool(getattr(self, "_file_load_loading", False)):
            chips.append("Opening item")
        if self.file_kw.strip():
            chips.append(f"Keyword: {self.file_kw.strip()}")
        if self.video_only:
            chips.append("Video only: On")
        if self.selected_file_names:
            chips.append(f"Marked: {len(self.selected_file_names)}")
        return chips

    def selected_item_header(self) -> str:
        item = self.selected_result()
        if not item:
            return "No item selected"
        if self.is_youtube_result(item):
            title = item.title or "(no title)"
            channel = item.uploader or item.creator or "unknown channel"
            return f"[YT] {title} | {channel} | {item.video_id or item.identifier} | single video"
        if bool(getattr(self, "_file_load_loading", False)):
            return f"Opening item | {item.title or '(no title)'} | {item.identifier} | waiting for IA file metadata"
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
            return self.scan_staged_dvd_iso(staging_path)

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
            return f"Saved: {final_path}"

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
        return f"Saved: {final_path}"

    def choose_batch_import_options(self, item_title: str) -> Optional[Dict[str, str]]:
        use_batch = self.prompt("Use one destination for this queue? Enter=yes, type n=no: ", "")
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
            season = self.prompt("Queue season number: ", "01")
            if season is None:
                return None
            return {"bucket": bucket, "folder": folder, "season": season or "01"}
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

    def init_queue_status(self, queue: List[IAFile]) -> None:
        self.queue_status = [
            {"name": f.name, "status": "pending", "detail": "", "size": int(f.size or 0)}
            for f in queue
        ]

    def set_queue_status(self, filename: str, status: str, detail: str = "", size: Optional[int] = None) -> None:
        for row in self.queue_status:
            if row.get("name") == filename:
                row["status"] = status
                row["detail"] = detail
                if size is not None:
                    row["size"] = int(size or 0)
                return
        self.queue_status.append({"name": filename, "status": status, "detail": detail, "size": int(size or 0)})

    def queue_summary(self) -> str:
        counts: Dict[str, int] = {}
        for row in self.queue_status:
            status = str(row.get("status") or "pending")
            counts[status] = counts.get(status, 0) + 1
        if not counts:
            return "Queue: empty"
        order = ["pending", "downloading", "done", "staged", "skipped", "failed", "canceled"]
        parts = [f"{k}:{counts[k]}" for k in order if k in counts]
        return "Queue: " + " ".join(parts)

    def show_download_complete(self, message: str) -> None:
        self.dl_complete_notice = message
        self.mode = "DOWNLOADING"
        self.focus = "LIST"
        self.status = message
        self.render()

    def clear_download_complete(self) -> None:
        self.dl_complete_notice = ""

    def handle_download_complete_key(self, ch: int) -> bool:
        if self.mode != "DOWNLOADING" or not self.dl_complete_notice:
            return False
        if is_enter_key(ch) or ch in (curses.KEY_BACKSPACE, 127, 8, 27, ord(" ")):
            self.clear_download_complete()
            self.mode = "FILES"
            self.focus = "LIST"
            self.status = "Back to files"
            return True
        return True

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
        if status_l in ("failed", "error", "blocked"):
            return curses.color_pair(5) | curses.A_BOLD
        if status_l in ("unclear", "warning", "skipped", "staged"):
            return curses.color_pair(3)
        if status_l in ("marked", "pending"):
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

    def import_left_in_staging(self, msg: str) -> bool:
        return self.import_queue_status(msg) == "staged"

    def queue_table_rows(self, width: int, limit: int = 8) -> List[Tuple[str, int]]:
        if width <= 0:
            return []
        rows: List[Tuple[str, int]] = [("STATUS      SIZE       FILE", curses.color_pair(3) | curses.A_BOLD)]
        name_w = max(8, width - 22)
        for row in self.queue_status[:limit]:
            status = str(row.get("status") or "pending")
            size = display_size(row.get("size"))
            name = str(row.get("name") or "")
            if len(name) > name_w:
                name = name[: max(0, name_w - 1)] + "…"
            active = bool(self.dl_current_name and row.get("name") == self.dl_current_name)
            line = f"{status:<11} {size:>8}  {name}"
            rows.append((line[:width], self.queue_row_attr(status, active)))
        if len(self.queue_status) > limit:
            rows.append((f"... and {len(self.queue_status) - limit} more", curses.color_pair(6) | curses.A_DIM))
        return rows

    def record_failed_file(self, f: IAFile, err: str) -> None:
        if all(existing.name != f.name for existing in self.failed_queue):
            self.failed_queue.append(f)
        self.set_queue_status(f.name, "failed", err)

    def retry_failed_downloads(self) -> None:
        if not self.failed_queue:
            self.status = "No failed files to retry."
            return
        item = self.selected_result() or self.preview_item
        if not item:
            self.status = "No item selected for retry."
            return
        self.preview_item = item
        self.preview_file = None
        self.preview_files = list(self.failed_queue)
        self.preview_prefix = "__SELECTED__"
        self.refresh_preview_import_info()
        self.preview_msg = f"Retrying {len(self.failed_queue)} failed file(s)."
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
        item = self.selected_result()
        if not item:
            self.status = "No item selected."
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
        item = self.selected_result()
        if not item:
            self.status = "No item selected."
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
        item = self.selected_result()
        if not item:
            self.status = "No item selected."
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
        item = self.selected_result()
        if not item:
            self.status = "No item selected."
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

    def _wait_before_stall_retry(self, attempt_num: int, max_attempts: int) -> bool:
        for remaining in range(STALL_RETRY_DELAY_S, 0, -1):
            self.status = f"Download stalled. Auto-retry {attempt_num}/{max_attempts} in {remaining}s. Press c to cancel."
            self.render()
            try:
                if self.stdscr.getch() in (ord("c"), ord("C")):
                    self.dl_cancel_requested = True
                    return False
            except Exception:
                pass
            time.sleep(1)
        return True

    def _download_one_with_progress(self, identifier: str, filename: str, expected_size: int) -> Tuple[bool, str]:
        path, err = safe_staging_file_path(identifier, filename)
        if err or not path:
            return False, err
        os.makedirs(STAGING_ROOT, exist_ok=True)

        item = getattr(self, "preview_item", None)
        is_youtube = self.is_youtube_result(item)
        if is_youtube:
            if not item or not getattr(item, "webpage_url", ""):
                return False, "YouTube URL is missing."
            os.makedirs(yt_downloads.youtube_staging_dir(identifier), exist_ok=True)
            cmd = yt_downloads.single_video_download_cmd(APP_CONFIG["yt_dlp_path"], item.webpage_url, identifier)
            read_written = lambda: ia_downloads.dir_total_size(yt_downloads.youtube_staging_dir(identifier))
        else:
            cmd = ia_downloads.single_download_cmd(identifier, filename, IA_NO_CHANGE_TIMESTAMP)
            read_written = lambda: ia_downloads.safe_getsize(path)
        log_line(f"DL_CMD: {shlex.join(cmd)}")
        ia_minotaur_events.emit_archive_started(f"{identifier} {filename}")
        log_fh = ia_downloads.open_process_log()
        try:
            self.dl_cancel_requested = False
            self.dl_current_name = filename
            self.dl_current_total = int(expected_size or 0)
            self.dl_current_written = 0
            self.dl_speed_bps = 0.0
            self.dl_eta_s = 0.0
            self.stdscr.nodelay(True)

            def check_cancel() -> bool:
                ch = self.stdscr.getch()
                if ch in (ord("c"), ord("C")):
                    self.dl_cancel_requested = True
                return self.dl_cancel_requested

            def update_progress(progress: ia_downloads.DownloadProgress) -> None:
                self.dl_current_written = progress.written
                self.dl_current_total = progress.total
                self.dl_speed_bps = progress.speed_bps
                self.dl_eta_s = progress.eta_s
                if progress.total > 0:
                    pct = int((progress.written * 100) / progress.total) if progress.total else 0
                    sp = human_size(int(progress.speed_bps)) + "/s" if progress.speed_bps > 0 else "?/s"
                    eta = f"{int(progress.eta_s)}s" if progress.eta_s > 0 else "?"
                    self.status = f"{filename}  {pct}%  {human_size(progress.written)}/{human_size(progress.total)}  {sp}  ETA {eta}  (c cancels)"
                elif progress.written > 0:
                    self.status = f"{filename}  {human_size(progress.written)} downloaded  (c cancels)"
                else:
                    self.status = f"{filename}  downloaded size unknown  (c cancels)"
                self.render()

            max_stall_retries = STALL_AUTO_RETRIES
            attempt = 0
            while True:
                if attempt > 0:
                    self.status = f"Retrying stalled download {attempt}/{max_stall_retries}: {filename}"
                    self.render()
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
                    if self.dl_cancel_requested:
                        ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: Canceled.")
                        return False, "Canceled."
                    if attempt < max_stall_retries:
                        attempt += 1
                        if not self._wait_before_stall_retry(attempt, max_stall_retries):
                            ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: Canceled.")
                            return False, "Canceled."
                        continue
                    err = f"{msg} Auto-retry limit reached."
                    ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: {err}")
                    return False, err

                ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: {msg}")
                return False, msg

            if not is_youtube:
                ok_sz, msg_sz = self._verify_expected_size(identifier, filename, int(expected_size or 0))
                if not ok_sz:
                    ia_minotaur_events.emit_archive_failed(f"{identifier} {filename}: {msg_sz}")
                    return False, msg_sz
            ia_minotaur_events.emit_archive_completed(f"{identifier} {filename}")
            return True, ""
        finally:
            self.stdscr.nodelay(False)
            log_fh.close()

    def _download_glob_with_progress(self, identifier: str, glob_pat: str, expected_total: int) -> Tuple[bool, str]:
        os.makedirs(STAGING_ROOT, exist_ok=True)
        os.makedirs(ia_downloads.staging_dir_for_identifier(identifier), exist_ok=True)

        cmd = ia_downloads.glob_download_cmd(identifier, glob_pat, IA_NO_CHANGE_TIMESTAMP)
        log_line(f"DL_GLOB_CMD: {shlex.join(cmd)}")
        ia_minotaur_events.emit_archive_started(f"{identifier} {glob_pat}")
        log_fh = ia_downloads.open_process_log()
        try:
            self.dl_cancel_requested = False
            self.dl_current_name = f"--glob {glob_pat}"
            self.dl_current_total = int(expected_total or 0)
            self.dl_current_written = 0
            self.dl_speed_bps = 0.0
            self.dl_eta_s = 0.0
            self.stdscr.nodelay(True)

            base_dir = ia_downloads.staging_dir_for_identifier(identifier)

            def check_cancel() -> bool:
                ch = self.stdscr.getch()
                if ch in (ord("c"), ord("C")):
                    self.dl_cancel_requested = True
                return self.dl_cancel_requested

            def update_progress(progress: ia_downloads.DownloadProgress) -> None:
                self.dl_current_written = progress.written
                self.dl_current_total = progress.total
                self.dl_speed_bps = progress.speed_bps
                self.dl_eta_s = progress.eta_s
                if progress.total > 0:
                    pct = int((progress.written * 100) / progress.total) if progress.total else 0
                    sp = human_size(int(progress.speed_bps)) + "/s" if progress.speed_bps > 0 else "?/s"
                    eta = f"{int(progress.eta_s)}s" if progress.eta_s > 0 else "?"
                    self.status = f"{identifier}  {pct}%  {human_size(progress.written)}/{human_size(progress.total)}  {sp}  ETA {eta}  (c cancels)"
                elif progress.written > 0:
                    self.status = f"{identifier}  {human_size(progress.written)} downloaded  (c cancels)"
                else:
                    self.status = f"{identifier}  downloaded size unknown  (c cancels)"
                self.render()

            ok, msg = ia_downloads.run_download_with_progress(
                cmd,
                target=f"--glob {glob_pat}",
                expected_total=int(expected_total or 0),
                read_written=lambda: ia_downloads.dir_total_size(base_dir),
                log_fh=log_fh,
                stall_timeout_s=STALL_TIMEOUT_S,
                is_cancel_requested=check_cancel,
                on_progress=update_progress,
                log_path=LOG_PATH,
            )
            if not ok and msg.startswith("download failed:"):
                log_line(f"DL_GLOB_POPEN_ERR: {msg}")
            if ok:
                ia_minotaur_events.emit_archive_completed(f"{identifier} {glob_pat}")
            else:
                ia_minotaur_events.emit_archive_failed(f"{identifier} {glob_pat}: {msg}")
            return ok, msg
        finally:
            self.stdscr.nodelay(False)
            log_fh.close()

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

        self.mode = "DOWNLOADING"
        self.focus = "MENU"
        os.makedirs(STAGING_ROOT, exist_ok=True)

        new_completed = completed_names_list

        for f in staged_ready:
            msg = self.choose_bucket_and_path(identifier, f.name, item_title)
            import_status = self.import_queue_status(msg)
            if import_status == "done" or import_status == "skipped":
                new_completed.append(f.name)
            self.download_log.insert(0, msg)
            self.download_log = self.download_log[:8]
            self.status = msg
            self.render()

        if remaining:
            # Resume per file, even for original glob/prefix downloads, so already
            # complete staged files are imported without being downloaded again.
            for idx, f in enumerate(remaining):
                self.status = f"Resuming {idx+1}/{len(remaining)}: {f.name}"
                self.render()
                ok2, err = self._download_one_with_progress(identifier, f.name, int(f.size or 0))
                if not ok2:
                    self._save_pending(identifier, item_title, all_files, preview_prefix, glob_pat, new_completed)
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.status = f"{err}  (press R to retry)"
                    self.download_log.insert(0, f"Resume error: {err}")
                    self.download_log = self.download_log[:8]
                    return
                msg = self.choose_bucket_and_path(identifier, f.name, item_title)
                import_status = self.import_queue_status(msg)
                if import_status == "done" or import_status == "skipped":
                    new_completed.append(f.name)
                self.download_log.insert(0, msg)
                self.download_log = self.download_log[:8]
                self.status = msg
                self.render()

        if len(new_completed) < len(all_files):
            self._save_pending(identifier, item_title, all_files, preview_prefix, glob_pat, new_completed)
            self.mode = "FILES"
            self.focus = "LIST"
            self.status = "Resume paused with files left."
            return

        if skipped_existing:
            self.status = f"Skipped {skipped_existing} existing file(s)."

        self._clear_pending()
        self.mode = "FILES"
        self.focus = "LIST"
        self.status = f"Resume complete. {n} file(s) handled."

    def perform_download_plan(self) -> None:
        if not self.preview_item:
            self.status = "Nothing to download."
            self.mode = "FILES"
            self.focus = "LIST"
            return

        is_youtube_plan = self.is_youtube_result(self.preview_item)
        if is_youtube_plan:
            ok, why = True, "YouTube single video via yt-dlp."
        else:
            meta = self.cur_meta or {}
            ok, why = is_openly_licensed(meta) if meta else (False, "No metadata loaded")
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
        self.failed_queue = []

        # single file
        if self.preview_file:
            queue = [self.preview_file]
            self.init_queue_status(queue)
            self.dl_overall_total = sum(int(f.size or 0) for f in queue)
            self.dl_overall_written = 0

            self.mode = "DOWNLOADING"
            self.focus = "MENU"

            f = queue[0]
            complete_msg = self._handle_already_complete(item.identifier, f, item.title)
            if complete_msg:
                status = self.import_queue_status(complete_msg)
                self.set_queue_status(f.name, status, complete_msg)
                if status == "staged":
                    self._save_pending(item.identifier, item.title, queue, self.preview_prefix, "", [])
                self.mode = "FILES"
                self.focus = "LIST"
                self.preview_item = None
                self.preview_file = None
                self.preview_files = []
                self.preview_prefix = ""
                self.status = complete_msg
                self.download_log.insert(0, complete_msg)
                self.download_log = self.download_log[:8]
                return
            self.set_queue_status(f.name, "downloading")
            self.status = f"Downloading: {f.name}"
            self.render()

            ok2, err = self._download_one_with_progress(item.identifier, f.name, int(f.size or 0))
            if not ok2:
                status = "canceled" if "cancel" in err.lower() else "failed"
                self.set_queue_status(f.name, status, err)
                if "cancel" in err.lower() and not self.is_youtube_result(item):
                    self._save_pending(item.identifier, item.title, queue, self.preview_prefix, "", [])
                if not self.is_youtube_result(item):
                    self.record_failed_file(f, err)
                self.mode = "FILES"
                self.focus = "LIST"
                self.preview_item = None
                self.preview_file = None
                self.preview_files = []
                self.preview_prefix = ""
                self.status = f"{err}  (press R to resume)" if "cancel" in err.lower() else err
                self.download_log.insert(0, f"Error: {err}")
                self.download_log = self.download_log[:8]
                return

            import_name = f.name
            downloaded_size = int(f.size or 0)
            if self.is_youtube_result(item):
                found = yt_downloads.find_downloaded_video_file(item.identifier, item.video_id)
                if not found:
                    err = "yt-dlp finished, but downloaded file was not found in staging."
                    self.record_failed_file(f, err)
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.preview_item = None
                    self.preview_file = None
                    self.preview_files = []
                    self.preview_prefix = ""
                    self.status = err
                    self.download_log.insert(0, f"Error: {err}")
                    self.download_log = self.download_log[:8]
                    return
                import_name = found
                found_path, found_err = safe_staging_file_path(item.identifier, found)
                if not found_err and found_path:
                    downloaded_size = ia_downloads.safe_getsize(found_path)
                    f.size = downloaded_size
            msg = self.choose_bucket_and_path(item.identifier, import_name, item.title)
            import_status = self.import_queue_status(msg)
            self.set_queue_status(f.name, import_status, msg, size=downloaded_size)
            if import_status == "staged":
                self._save_pending(item.identifier, item.title, queue, self.preview_prefix, "", [])
            self.download_log.insert(0, msg)
            self.download_log = self.download_log[:8]
            self.preview_item = None
            self.preview_file = None
            self.preview_files = []
            self.preview_prefix = ""
            if import_status == "staged":
                self.show_download_complete("Downloaded 1 file; import pending. Press R after returning to import from staging.")
            elif import_status == "failed":
                self.show_download_complete("Downloaded 1 file; import failed.")
            else:
                size_note = f" ({display_size(downloaded_size)})" if self.is_youtube_result(item) and downloaded_size > 0 else ""
                self.show_download_complete(f"Done. Downloaded 1 file{size_note}.")
            return

        # prefix or full item
        if self.preview_files:
            queue = list(self.preview_files)
            self.init_queue_status(queue)
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

            self.mode = "DOWNLOADING"
            self.focus = "MENU"

            if self.preview_prefix and self.preview_prefix not in ("__FULL_ITEM__", "__SELECTED__"):
                remaining_for_glob: List[IAFile] = []
                completed_names: List[str] = []
                for f in queue:
                    complete_msg = self._handle_already_complete(item.identifier, f, item.title, batch=batch_import)
                    if complete_msg:
                        status = self.import_queue_status(complete_msg)
                        self.set_queue_status(f.name, status, complete_msg)
                        if status == "done" or status == "skipped":
                            completed_names.append(f.name)
                        self.download_log.insert(0, complete_msg)
                        self.download_log = self.download_log[:8]
                    else:
                        remaining_for_glob.append(f)
                if not remaining_for_glob:
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.status = f"All {len(queue)} file(s) were already complete."
                    return
                queue = remaining_for_glob
                # Use ia --glob for prefix downloads.
                # NOTE: IA globs are matched against the "name" field (including folder paths).
                # Using prefix* matches "prefix..." including subpaths if prefix includes a folder/ path.
                glob_pat = f"{self.preview_prefix}*"
                self.status = f"Downloading prefix via --glob: {glob_pat}"
                self.render()

                ok2, err = self._download_glob_with_progress(item.identifier, glob_pat, int(total_expected))
                if not ok2:
                    for f in queue:
                        self.record_failed_file(f, err)
                    self._save_pending(item.identifier, item.title, queue,
                                       self.preview_prefix, glob_pat, completed_names)
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.preview_item = None
                    self.preview_file = None
                    self.preview_files = []
                    self.preview_prefix = ""
                    self.status = f"{err}  (press R to resume)"
                    self.download_log.insert(0, f"Error: {err}")
                    self.download_log = self.download_log[:8]
                    return

                # Import each expected file (now that the glob run finished).
                for f in queue:
                    ok_sz, msg_sz = self._verify_expected_size(item.identifier, f.name, int(f.size or 0))
                    if not ok_sz:
                        self._save_pending(item.identifier, item.title, queue,
                                           self.preview_prefix, glob_pat, completed_names)
                        self.record_failed_file(f, msg_sz)
                        self.mode = "FILES"
                        self.focus = "LIST"
                        self.preview_item = None
                        self.preview_file = None
                        self.preview_files = []
                        self.preview_prefix = ""
                        self.status = f"{msg_sz}  (press R to resume)"
                        self.download_log.insert(0, f"Error: {msg_sz}")
                        self.download_log = self.download_log[:8]
                        return

                    msg = self.choose_bucket_and_path(item.identifier, f.name, item.title, batch=batch_import)
                    import_status = self.import_queue_status(msg)
                    self.set_queue_status(f.name, import_status, msg)
                    if import_status == "done" or import_status == "skipped":
                        completed_names.append(f.name)
                    self.download_log.insert(0, msg)
                    self.download_log = self.download_log[:8]
                    self.status = msg
                    self.render()

                self._clear_pending()
                was_selected_plan = self.preview_prefix == "__SELECTED__"
                self.preview_item = None
                self.preview_file = None
                self.preview_files = []
                self.preview_prefix = ""
                if was_selected_plan:
                    self.selected_file_names.clear()
                    self.save_current_file_view_state()
                staged_count = sum(1 for row in self.queue_status if row.get("status") == "staged")
                if staged_count:
                    self._save_pending(item.identifier, item.title, queue, self.preview_prefix, glob_pat, completed_names)
                    self.show_download_complete(f"Downloaded {len(queue)} file(s); {staged_count} import pending.")
                else:
                    self.show_download_complete(f"Done. Downloaded {len(queue)} file(s).")
                return

            # Full item (visible set). Sequential download keeps progress accurate per-file and imports cleanly.
            seq_completed: List[str] = []
            for idx, f in enumerate(queue):
                complete_msg = self._handle_already_complete(item.identifier, f, item.title, batch=batch_import)
                if complete_msg:
                    status = self.import_queue_status(complete_msg)
                    self.set_queue_status(f.name, status, complete_msg)
                    if status == "done" or status == "skipped":
                        seq_completed.append(f.name)
                    self.download_log.insert(0, complete_msg)
                    self.download_log = self.download_log[:8]
                    continue
                self.dl_current_name = f.name
                self.dl_current_total = int(f.size or 0)
                self.dl_current_written = 0

                self.status = f"Downloading {idx+1}/{len(queue)}: {f.name}"
                self.set_queue_status(f.name, "downloading")
                self.render()

                ok2, err = self._download_one_with_progress(item.identifier, f.name, int(f.size or 0))
                if not ok2:
                    status = "canceled" if "cancel" in err.lower() else "failed"
                    self.set_queue_status(f.name, status, err)
                    self.record_failed_file(f, err)
                    self._save_pending(item.identifier, item.title, queue,
                                       self.preview_prefix, "", seq_completed)
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.preview_item = None
                    self.preview_file = None
                    self.preview_files = []
                    self.preview_prefix = ""
                    self.status = f"{err}  (press R to resume)"
                    self.download_log.insert(0, f"Error: {err}")
                    self.download_log = self.download_log[:8]
                    return

                msg = self.choose_bucket_and_path(item.identifier, f.name, item.title, batch=batch_import)
                import_status = self.import_queue_status(msg)
                self.set_queue_status(f.name, import_status, msg)
                if import_status == "done" or import_status == "skipped":
                    seq_completed.append(f.name)
                self.download_log.insert(0, msg)
                self.download_log = self.download_log[:8]
                self.status = msg
                self.render()

            self._clear_pending()
            was_selected_plan = self.preview_prefix == "__SELECTED__"
            self.preview_item = None
            self.preview_file = None
            self.preview_files = []
            self.preview_prefix = ""
            if was_selected_plan:
                self.selected_file_names.clear()
                self.save_current_file_view_state()
            staged_count = sum(1 for row in self.queue_status if row.get("status") == "staged")
            if staged_count:
                self._save_pending(item.identifier, item.title, queue, self.preview_prefix, "", seq_completed)
                self.show_download_complete(f"Downloaded {len(queue)} file(s); {staged_count} import pending.")
            else:
                self.show_download_complete(f"Done. Downloaded {len(queue)} file(s).")
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
        item = getattr(self, "preview_item", None) or self.selected_result()
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

        queue_rows = self.queue_table_rows(w - 6, limit=10) if self.queue_status else self.preview_queue_table_rows(w - 6, limit=10)
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
        elif self.mode == "DOWNLOADING":
            left_title = "DOWNLOADING"
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

        elif self.mode == "DOWNLOADING":
            if self.dl_complete_notice:
                details = [
                    "Download complete:",
                    f"  {self.dl_complete_notice}",
                    "",
                    "Press Enter or Backspace to return.",
                ]
            else:
                details = [
                    "Download progress:",
                    f"  Target: {self.dl_current_name}",
                    self.queue_summary(),
                ]
                if self.dl_current_total > 0:
                    pct = int((self.dl_current_written * 100) / self.dl_current_total) if self.dl_current_total else 0
                    bar_w = max(8, min(34, right_w - 4))
                    details += [
                        f"  [{shaded_progress_bar(self.dl_current_written, self.dl_current_total, bar_w)}]",
                        f"  {pct}%  {human_size(self.dl_current_written)}/{human_size(self.dl_current_total)}",
                    ]
                else:
                    bar_w = max(8, min(34, right_w - 4))
                    details += [
                        f"  [{shaded_progress_bar(self.dl_current_written, self.dl_current_total, bar_w)}]",
                        f"  {human_size(self.dl_current_written)} downloaded" if self.dl_current_written > 0 else "  Size unknown",
                    ]
                if self.dl_speed_bps > 0:
                    details += [f"  Speed: {human_size(int(self.dl_speed_bps))}/s"]
                if self.dl_eta_s > 0:
                    details += [f"  ETA: {int(self.dl_eta_s)}s"]
                details += ["", "Press c to cancel"]

            if self.queue_status:
                details += ["", "Queue:"]
                details += self.queue_table_rows(right_w, limit=8)

        if getattr(self, "last_error_detail", "") and self.mode != "DOWNLOADING":
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

        if right_w > 10 and self.download_log and self.mode != "FAVS":
            ry2 = body_bottom - min(8, len(self.download_log) + 1)
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
            self.exit_requested = True
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

            if ch in (ord("q"), ord("Q")):
                if self.mode == "PREVIEW_DL":
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.status = "Canceled."
                    continue
                break

            if self.mode == "ERROR" or self.term_too_small():
                continue

            if ch == ord('?'):
                self.toggle_help_overlay()
                continue

            if self.handle_download_complete_key(ch):
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

            if ch in (ord('/'), ord('s'), ord('S')):
                s = self.prompt("Search: ", self.query_text, history=self.search_history)
                if s is not None:
                    self.query_text = s
                    self.show_welcome = False
                    self.start_search_async(reset_page=True)
                continue

            if ch == ord('R') and self.mode not in ("DOWNLOADING", "PREVIEW_DL"):
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
        self._save_session()


def main(stdscr):
    app = RetroWaveIA(stdscr)
    app.loop()


def cli_main(argv: Optional[List[str]] = None) -> int:
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
