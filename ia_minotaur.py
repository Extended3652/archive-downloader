#!/usr/bin/env python3
import argparse
import curses
import os
import re
import shutil
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
import ia_config
import ia_downloads
from ia_organize import (
    auto_clean_movie_folder_name,
    build_collection_search_query,
    build_field_query,
    build_query_attempts,
    build_within_collection_query,
    build_query,
    detect_sxxeyy,
    is_openly_licensed,
    license_status_from_fields,
    normalize_collection_identifier,
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
BULK_CONFIRM_FILE_THRESHOLD = 10
BULK_CONFIRM_BYTES_THRESHOLD = 5 * 1024 * 1024 * 1024


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

        self.results: List[SearchResult] = []
        self.sel_r = 0

        self.files: List[IAFile] = []
        self.sel_f = 0
        self.file_kw = ""
        self.video_only = False
        self.selected_file_names: Set[str] = set()
        self.file_view_state: Dict[str, Dict[str, Any]] = {}

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

        self.dl_current_name: str = ""
        self.dl_current_written: int = 0
        self.dl_current_total: int = 0
        self.dl_speed_bps: float = 0.0
        self.dl_eta_s: float = 0.0
        self.dl_overall_written: int = 0
        self.dl_overall_total: int = 0
        self.dl_cancel_requested: bool = False

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
                "query_text": self.query_text,
                "filter": self.filter,
                "title_only": self.title_only,
                "page": self.page,
                "sort_by": self.sort_by,
                "enforce_license_gate": getattr(self, "enforce_license_gate", False),
                "search_history": self.search_history[:MAX_HISTORY],
            },
        )

    def _restore_session(self) -> None:
        try:
            data = ia_state.load_session(SESSION_PATH)
            if not data:
                return
            self.query_text = str(data.get("query_text") or "")
            if data.get("filter") in FILTERS:
                self.filter = data["filter"]
            self.title_only = bool(data.get("title_only", False))
            self.page = max(1, int(data.get("page") or 1))
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
        line1 = f"{header}  |  {focus_info}  |  Filter: {self.filter}  |  Search: {search_mode}{sort_info}  |  {page_info}"
        self.safe_addstr(y, 0, line1[: max(0, w - 1)].ljust(max(0, w - 1)), curses.color_pair(3)); y += 1

        breadcrumb = self.breadcrumb()
        if self.query_built and self.mode in ("RESULTS", "SEARCH"):
            line2 = f"{breadcrumb}  |  Query: {self.query_built[:60]}   Root: {MEDIA_ROOT}"
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
        for label, val in SORT_OPTIONS:
            if val == self.sort_by:
                return label
        return "relevance"

    def result_license_label(self, r: SearchResult) -> str:
        status, _why = license_status_from_fields(r.licenseurl, r.rights)
        return {
            "open": "lic:open",
            "blocked": "lic:block",
            "unclear": "lic:unclear",
            "unknown": "?",
        }.get(status, "?")

    def result_meta_summary(self, r: SearchResult) -> str:
        parts = []
        if r.year:
            parts.append(str(r.year))
        if r.mediatype:
            parts.append(str(r.mediatype))
        if r.downloads:
            parts.append(f"{compact_count(r.downloads)} dl")
        lic = self.result_license_label(r)
        if lic != "?":
            parts.append(lic)
        return " | ".join(parts)

    def result_filter_blob(self, r: SearchResult) -> str:
        status, _why = license_status_from_fields(r.licenseurl, r.rights)
        values = [
            r.identifier,
            r.title,
            r.year,
            r.creator,
            r.description,
            r.mediatype,
            str(r.downloads or ""),
            r.date,
            r.publicdate,
            r.collection,
            status,
            r.rights,
            r.licenseurl,
        ]
        return " ".join(str(v or "") for v in values).lower()

    def get_visible_results(self) -> List[SearchResult]:
        needle = self.result_filter.strip().lower()
        if not needle:
            return list(self.results)
        terms = [t for t in needle.split() if t]
        return [r for r in self.results if all(t in self.result_filter_blob(r) for t in terms)]

    def selected_result(self) -> Optional[SearchResult]:
        visible = self.get_visible_results()
        if not visible:
            return None
        if self.sel_r >= len(visible):
            self.sel_r = max(0, len(visible) - 1)
        return visible[self.sel_r]

    def set_result_filter(self, value: str) -> None:
        self.result_filter = (value or "").strip()
        self.sel_r = 0
        n = len(self.get_visible_results())
        self.status = f"Local result filter: {self.result_filter or '(none)'} ({n} visible)"

    def collection_choices_from_results(self, limit: int = 12) -> List[str]:
        counts: Dict[str, int] = {}
        for r in self.results:
            for raw in str(r.collection or "").split(","):
                coll = normalize_collection_identifier(raw)
                if coll:
                    counts[coll] = counts.get(coll, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        return [f"{coll} ({count})" for coll, count in ordered[:limit]]

    def _collection_from_choice(self, choice: str) -> str:
        return re.sub(r"\s+\(\d+\)\s*$", "", choice or "").strip()

    def set_query_and_search(self, query_text: str, *, built_query: Optional[str] = None) -> None:
        self.query_text = query_text
        self.query_built = built_query or ""
        self.show_welcome = False
        self.do_search(reset_page=True, built_query=built_query)

    def jump_to_result_number(self, target: int) -> None:
        if target < 1:
            self.status = "Result number must be >= 1."
            return
        if self.total_results > 0:
            total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            target_page = ((target - 1) // ROWS_PER_PAGE) + 1
            if target_page > total_pages:
                self.status = f"Result must be 1-{self.total_results}."
                return
            self.page = target_page
            self.do_search(reset_page=False)
            self.sel_r = min(max(0, (target - 1) % ROWS_PER_PAGE), max(0, len(self.get_visible_results()) - 1))
            self.focus = "LIST"
            self.status = f"Selected result {target}."
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
            fav_label = (
                "Unfav" if (selected and self.is_fav_item(selected.identifier))
                else "Fav"
            )
            return [
                ("Actions", "actions"),
                ("Search", "search"),
                ("Collections", "collection_search"),
                ("In Collection", "within_collection"),
                (f"Filter: {self.filter}", "filter"),
                (f"Local: {self.result_filter or 'Off'}", "result_filter"),
                (f"Sort: {self._sort_label()}", "sort"),
                (f"Title only: {'On' if self.title_only else 'Off'}", "title"),
                (f"License gate: {'On' if self.enforce_license_gate else 'Off'}", "license_gate"),
                ("Prev", "prev_page"),
                ("Next", "next_page"),
                ("Open", "open"),
                ("History", "history"),
                (fav_label, "fav_item"),
                (f"Theme: {theme}", "theme"),
                ("Favs", "favs"),
                ("Help", "help"),
                ("Quit", "quit"),
            ]
        if self.mode == "FILES":
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
                (f"Video only: {'On' if self.video_only else 'Off'}", "video_only"),
                ("Keyword", "keyword"),
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

        x = 0
        for i, (label, _action) in enumerate(items):
            pill = f" {label} "
            if x + len(pill) >= w - 1:
                break

            is_sel = (self.focus == "MENU" and i == self.menu_idx)
            attr = curses.color_pair(6) | curses.A_DIM
            if is_sel:
                attr = curses.color_pair(3) | curses.A_BOLD

            self.safe_addstr(y, x, pill, attr)
            x += len(pill)

        if x < w - 1:
            self.safe_addstr(y, x, " " * (w - 1 - x), curses.color_pair(6) | curses.A_DIM)

        return y + 1

    def command_footer(self) -> str:
        if self.mode in ("RESULTS", "SEARCH"):
            return "Search /   Open Enter/o   Mark Fav   Download -   Details r   Actions a   Theme T"
        if self.mode == "FILES":
            return "Search /   Open Enter   Mark Space/A/I/U   Download d   Details r   Actions a   Theme T"
        if self.mode == "PREVIEW_DL":
            return "Search /   Open -   Mark -   Download Enter   Details ?   Actions a   Theme T"
        if self.mode == "FAVS":
            return "Search /   Open Enter/o   Mark Remove   Download -   Details ?   Actions a   Theme T"
        return "Search /   Open Enter   Mark -   Download -   Details ?   Actions a   Theme T"

    def hint_bar(self, include_overlay_state: bool = True) -> str:
        if include_overlay_state and self.help_overlay:
            return "?/Esc closes help  |  Backspace back  |  q quit"
        if self.mode == "DOWNLOADING":
            return "c cancel  |  q quit after cancel  |  progress updates live"
        if self.mode in ("RESULTS", "SEARCH"):
            return "Enter/o open  |  / search  |  l local filter  |  a actions  |  n/p page  |  ? help  |  q quit"
        if self.mode == "FILES":
            return "Space mark  |  A all  |  I invert  |  U clear  |  d marked/selected  |  f keyword  |  ? help"
        if self.mode == "FAVS":
            return "Enter/o open  |  a actions  |  Tab menu/list  |  Backspace back  |  ? help  |  q quit"
        if self.mode == "PREVIEW_DL":
            return "Enter confirm  |  Backspace/Esc cancel  |  ? help  |  q quit"
        return "j/k navigate  |  Enter select  |  a actions  |  Tab menu/list  |  Backspace back  |  ? help  |  q quit"

    def draw_footer(self, h: int, w: int) -> None:
        if h < 4 or w < 2:
            return

        status = (self.status or "")[: max(0, w - 1)]
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
        curses.curs_set(1)
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
            if ch in (10, 13):
                curses.curs_set(0)
                return "".join(buf).strip()
            if ch in (27,):
                curses.curs_set(0)
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
        query = ""

        self.stdscr.nodelay(False)
        while True:
            visible_options = self.filter_options(options, query)
            if not visible_options:
                visible_options = options
                query = ""
            if idx >= len(visible_options):
                idx = max(0, len(visible_options) - 1)

            for y in range(top, top + box_h):
                self.safe_addstr(y, left, " " * max(0, box_w), curses.color_pair(6))

            self.safe_addstr(top, left, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(2))
            self.safe_addstr(top + box_h - 1, left, "└" + "─" * (box_w - 2) + "┘", curses.color_pair(2))
            for y in range(top + 1, top + box_h - 1):
                self.safe_addstr(y, left, "│", curses.color_pair(2))
                self.safe_addstr(y, left + box_w - 1, "│", curses.color_pair(2))

            t = f" {title} "
            self.safe_addstr(top, left + 2, t[: max(0, box_w - 4)], curses.color_pair(1) | curses.A_BOLD)
            if query:
                q = f" find: {query} "
                self.safe_addstr(top + 1, left + 2, q[: max(0, box_w - 4)], curses.color_pair(3))

            body_top = top + 2
            body_bottom = top + box_h - 2
            max_rows = max(1, body_bottom - body_top)

            if idx < start:
                start = idx
            if idx >= start + max_rows:
                start = idx - max_rows + 1

            for i in range(start, min(len(visible_options), start + max_rows)):
                row_y = body_top + (i - start)
                s = visible_options[i]
                line = f" {i+1:02d}. {s}"
                line = line[: max(0, box_w - 2)].ljust(max(0, box_w - 2))
                if i == idx:
                    self.safe_addstr(row_y, left + 1, line, curses.color_pair(9) | curses.A_BOLD)
                else:
                    self.safe_addstr(row_y, left + 1, line, curses.color_pair(6))

            hint = "Type to filter  Up/Down choose  Enter select  Backspace edit  Esc cancel"
            self.safe_addstr(top + box_h - 1, left + 2, hint[: max(0, box_w - 4)], curses.color_pair(3))

            self.stdscr.refresh()
            ch = self.stdscr.getch()

            if ch in (27,):
                return None
            if ch in (10, 13, curses.KEY_ENTER):
                return visible_options[idx]
            if ch == curses.KEY_UP:
                idx = max(0, idx - 1)
            if ch == curses.KEY_DOWN:
                idx = min(len(visible_options) - 1, idx + 1)
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
                idx = 0
                start = 0
            elif 32 <= ch <= 126:
                query += chr(ch)
                idx = 0
                start = 0

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

    def action_palette_options(self) -> List[Tuple[str, str]]:
        if self.mode in ("RESULTS", "SEARCH"):
            return [
                ("Open / selected result (open enter item files)", "open"),
                ("Open / result details (metadata rights description)", "details"),
                ("Search / new query (/ find archive)", "search"),
                ("Search / collections (mediatype collection)", "collection_search"),
                ("Search / fields (title creator subject date collection)", "field_search"),
                ("Search / inside collection (collection identifier)", "within_collection"),
                ("Search / result collections (facet narrow)", "collection_facets"),
                ("Filter / media type (movies audio texts software any)", "filter"),
                ("Filter / local result page (local refine narrow)", "result_filter"),
                ("Filter / title-only mode (title exact)", "title"),
                ("Filter / license gate (rights license block)", "license_gate"),
                ("Sort / result order (date downloads title relevance)", "sort"),
                ("Page / previous (prev older [)", "prev_page"),
                ("Page / next (next more ])", "next_page"),
                ("App / favorites (saved items files folders)", "favs"),
                ("App / theme (retro minimal high contrast)", "theme"),
                ("App / help (? shortcuts)", "help"),
                ("App / quit (exit)", "quit"),
            ]
        if self.mode == "FILES":
            return [
                ("Open / preview selected file", "preview"),
                ("Select / toggle file mark", "toggle_file_mark"),
                ("Select / mark all visible files", "mark_all_visible"),
                ("Select / invert visible marks", "invert_visible_marks"),
                ("Select / clear marked files", "clear_file_marks"),
                ("Download / marked files", "download"),
                ("Download / retry failed files retry failed", "retry_failed"),
                ("Download / folder prefix folder", "folder"),
                ("Download / all visible files all", "item"),
                ("Filter / keyword", "keyword"),
                ("Filter / video only", "video_only"),
                ("Download / save bucket folder", "bucket"),
                ("Filter / rights license", "license_gate"),
                ("Alias / movie video", "video_only"),
                ("Alias / audio keyword", "keyword"),
                ("Alias / all", "item"),
                ("Alias / clear", "clear_file_marks"),
                ("Alias / queue", "download"),
                ("App / theme retro minimal high contrast", "theme"),
                ("App / favorites", "favs"),
                ("App / back", "back"),
                ("App / help", "help"),
                ("App / quit", "quit"),
            ]
        if self.mode == "FAVS":
            return [
                ("Open / selected favorite", "primary"),
                ("Filter / favorites tab", "tab"),
                ("App / remove favorite", "remove"),
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
        if reset_page:
            self.page = 1
        attempts = [("custom", built_query)] if built_query is not None else build_query_attempts(self.query_text, self.filter, self.title_only)
        attempts = [(label, query) for label, query in attempts if query]
        if not attempts:
            self.query_built = ""
            self.status = "Select [Search] in the menu to search."
            return

        self._add_to_history(self.query_text)
        self._save_session()
        self.status = "Searching..."
        self.render()

        used_label = ""
        last_err = ""
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

        self.sel_r = 0
        self.result_filter = ""
        self.mode = "RESULTS"
        self.focus = "LIST"
        search_hint = "" if used_label in ("", "title", "custom", "advanced") else f" ({used_label} match)"
        if self.total_results > 0:
            total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
            self.status = f"Page {self.page}/{total_pages} — {self.total_results} total results{search_hint}. Arrows to select, Enter to open."
        else:
            self.status = f"Page {self.page} — {len(self.results)} results{search_hint}. Arrows to select, Enter to open."

    def next_page(self) -> None:
        if not self.query_text:
            self.status = "No search yet. Choose [Search]."
            return
        saved_focus = self.focus
        saved_menu_idx = self.menu_idx
        saved_page = self.page
        self.page += 1
        self.do_search(reset_page=False)
        if not self.results:
            self.page = saved_page
            self.status = "No more results (rolled back to previous page)."
        # Keep menu focus so the user can immediately paginate again.
        self.focus = saved_focus
        self.menu_idx = saved_menu_idx

    def prev_page(self) -> None:
        if not self.query_text:
            self.status = "No search yet. Choose [Search]."
            return
        if self.page <= 1:
            self.status = "Already on first page."
            return
        saved_focus = self.focus
        saved_menu_idx = self.menu_idx
        saved_page = self.page
        self.page -= 1
        self.do_search(reset_page=False)
        if not self.results:
            self.page = saved_page
            self.status = "No results on that page (rolled back)."
        # Keep menu focus so the user can immediately paginate again.
        self.focus = saved_focus
        self.menu_idx = saved_menu_idx

    def load_files(self) -> None:
        self.save_current_file_view_state()
        item = self.selected_result()
        if not item:
            self.status = "No results to open."
            return
        self.status = f"Loading files for {item.identifier}..."
        self.render()

        files, meta, err = ia_files(item.identifier)
        if err:
            self.status = err
            return

        self.cur_meta = meta
        self.files = files
        self.restore_file_view_state(item.identifier)
        self.mode = "FILES"
        self.focus = "LIST"
        self.status = "Use arrows to choose a file, then [Preview], [Folder], [Item], or [Download]."

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
        self.file_view_state[item.identifier] = {
            "file_kw": self.file_kw,
            "video_only": self.video_only,
            "sel_f": self.sel_f,
            "selected_file_names": sorted(self.selected_file_names),
        }

    def restore_file_view_state(self, identifier: str) -> None:
        state = self.file_view_state.get(identifier, {})
        self.file_kw = str(state.get("file_kw") or "")
        self.video_only = bool(state.get("video_only", False))
        self.sel_f = max(0, int(state.get("sel_f") or 0))
        valid_names = {f.name for f in self.files}
        self.selected_file_names = {
            str(name)
            for name in (state.get("selected_file_names") or [])
            if str(name) in valid_names
        }

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
        if not self.selected_file_names:
            return []
        return [f for f in self.files if f.name in self.selected_file_names]

    def toggle_current_file_mark(self) -> None:
        visible = self.get_visible_files()
        if not visible or not (0 <= self.sel_f < len(visible)):
            self.status = "No file selected."
            return
        name = visible[self.sel_f].name
        if name in self.selected_file_names:
            self.selected_file_names.remove(name)
            self.status = f"Unmarked: {name}"
        else:
            self.selected_file_names.add(name)
            self.status = f"Marked: {name}"
        self.save_current_file_view_state()

    def clear_file_marks(self) -> None:
        n = len(self.selected_file_names)
        self.selected_file_names.clear()
        self.save_current_file_view_state()
        self.status = f"Cleared {n} marked file(s)." if n else "No marked files."

    def mark_all_visible_files(self) -> None:
        visible = self.get_visible_files()
        if not visible:
            self.status = "No visible files to mark."
            return
        before = len(self.selected_file_names)
        self.selected_file_names.update(f.name for f in visible)
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
                self.selected_file_names.remove(f.name)
            else:
                self.selected_file_names.add(f.name)
        self.save_current_file_view_state()
        self.status = f"Inverted visible marks; {len(self.selected_file_names)} marked."

    def file_filter_chips(self) -> List[str]:
        chips = []
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

    def handle_files_hotkey(self, ch: int) -> bool:
        if self.mode != "FILES":
            return False
        if ch in (ord('f'), ord('F')):
            s = self.prompt("Keyword (blank clears): ", self.file_kw)
            if s is not None:
                self.file_kw = s.strip()
                self.sel_f = 0
                self.status = "Keyword updated"
                self.save_current_file_view_state()
            else:
                self.status = "Keyword unchanged."
            self.focus = "LIST"
            return True
        if ch == ord(' '):
            self.toggle_current_file_mark()
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
        if ch in (ord('d'), ord('D')):
            self.set_preview_for_marked()
            return True
        if ch in (ord('r'), ord('R')):
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

    def movie_filename_for_folder(self, movie_folder: str, source_filename: str) -> str:
        movie = sanitize_folder(movie_folder)
        ext = os.path.splitext(os.path.basename(source_filename or ""))[1] or ".mp4"
        return f"{movie}{ext}"

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

        # --- helpers ---
        def has_year_hint(s: str) -> bool:
            if not s:
                return False
            return bool(re.search(r"\((19|20)\d{2}\)", s) or re.search(r"(19|20)\d{2}", s))

        def is_single_large_video(name: str) -> bool:
            try:
                video_files = [f for f in self.files if is_video_file(f.name, f.fmt)]
                large_video_files = [f for f in video_files if int(f.size or 0) >= LARGE_VIDEO_BYTES]
                return (len(large_video_files) == 1 and (large_video_files[0].name or "") == name)
            except Exception:
                return False

        ep = detect_sxxeyy(filename) or detect_sxxeyy(item_title)

        # Auto-detect a suggested bucket
        if ep:
            suggested = "TV"
        elif has_year_hint(filename) or has_year_hint(item_title) or is_single_large_video(filename):
            suggested = "Movies"
        elif self.last_bucket in ("TV", "Movies", "Music", "Other"):
            suggested = self.last_bucket
        else:
            suggested = "Other"

        # Ask user to confirm / choose bucket at download time
        bucket_raw = self.prompt("Save to (TV/Movies/Music/Other): ", suggested)
        if bucket_raw is None:
            return f"Left in staging: {staging_path}"
        bucket = bucket_raw.strip().title()
        if bucket not in ("TV", "Movies", "Music", "Other"):
            bucket = suggested
        self.last_bucket = bucket

        if bucket == "TV":
            show_default = sanitize_folder(item_title)
            show = self.prompt('Show name (Enter default, or type "*" for favorites): ', show_default)
            if show is None:
                return f"Left in staging: {staging_path}"
            if show.strip() == "*":
                pick = self.pick_folder_fav_if_requested("TV")
                show = pick if pick else show_default
            show = sanitize_folder(show)
            self.add_folder_fav("TV", show)

            if ep:
                season, episode = ep
                episode_override: Optional[int] = None
            else:
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

            season_dir = os.path.join(BUCKET_TV, show, f"Season {season:02d}")
            os.makedirs(season_dir, exist_ok=True)

            new_name = filename
            if ep or episode_override is not None:
                ext = os.path.splitext(filename)[1] or ".mp4"
                ep_num = ep[1] if ep else (episode_override if episode_override is not None else 1)
                new_name = f"{show} - S{season:02d}E{ep_num:02d}{ext}"

            final_path = os.path.join(season_dir, new_name)

        elif bucket == "Movies":
            title_default = auto_clean_movie_folder_name(item_title, filename)
            movie = self.prompt('Movie folder (Enter default, or type "*" for favorites): ', title_default)
            if movie is None:
                return f"Left in staging: {staging_path}"
            if movie.strip() == "*":
                pick = self.pick_folder_fav_if_requested("Movies")
                movie = pick if pick else title_default
            movie = sanitize_folder(movie)
            self.add_folder_fav("Movies", movie)

            movie_dir = os.path.join(BUCKET_MOVIES, movie)
            final_path = os.path.join(movie_dir, self.movie_filename_for_folder(movie, filename))

        elif bucket == "Music":
            artist_default = sanitize_folder(item_title)
            artist = self.prompt('Artist/album folder (Enter default, or type "*" for favorites): ', artist_default)
            if artist is None:
                return f"Left in staging: {staging_path}"
            if artist.strip() == "*":
                pick = self.pick_folder_fav_if_requested("Music")
                artist = pick if pick else artist_default
            artist = sanitize_folder(artist)
            self.add_folder_fav("Music", artist)

            music_dir = os.path.join(BUCKET_MUSIC, artist)
            final_path = os.path.join(music_dir, filename)

        else:
            sub = self.prompt('Other subfolder (Enter "Misc", or type "*" for favorites): ', "Misc")
            if sub is None:
                return f"Left in staging: {staging_path}"
            if sub.strip() == "*":
                pick = self.pick_folder_fav_if_requested("Other")
                sub = pick if pick else "Misc"
            sub = sanitize_folder(sub)
            self.add_folder_fav("Other", sub)

            other_dir = os.path.join(BUCKET_OTHER, sub)
            final_path = os.path.join(other_dir, filename)

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
        bucket_raw = self.prompt("Queue save to (TV/Movies/Music/Other): ", self.last_bucket)
        if bucket_raw is None:
            return None
        bucket = bucket_raw.strip().title()
        if bucket not in ("TV", "Movies", "Music", "Other"):
            bucket = self.last_bucket if self.last_bucket in ("TV", "Movies", "Music", "Other") else "Other"
        self.last_bucket = bucket
        if bucket == "TV":
            folder = self.prompt("Queue show/folder: ", sanitize_folder(item_title))
            season = self.prompt("Queue season number: ", "01")
            return {"bucket": bucket, "folder": sanitize_folder(folder or item_title), "season": season or "01"}
        if bucket == "Movies":
            folder = self.prompt("Queue movie/folder: ", auto_clean_movie_folder_name(item_title, ""))
            return {"bucket": bucket, "folder": sanitize_folder(folder or item_title)}
        if bucket == "Music":
            folder = self.prompt("Queue artist/album folder: ", sanitize_folder(item_title))
            return {"bucket": bucket, "folder": sanitize_folder(folder or item_title)}
        folder = self.prompt("Queue other subfolder: ", "Misc")
        return {"bucket": bucket, "folder": sanitize_folder(folder or "Misc")}

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
        ep = detect_sxxeyy(filename) or detect_sxxeyy(item_title)
        bucket = getattr(self, "last_bucket", "Other")
        if ep:
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
            found = self.find_existing_media_file(f.name, int(f.size or 0))
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

    def set_queue_status(self, filename: str, status: str, detail: str = "") -> None:
        for row in self.queue_status:
            if row.get("name") == filename:
                row["status"] = status
                row["detail"] = detail
                return
        self.queue_status.append({"name": filename, "status": status, "detail": detail, "size": 0})

    def queue_summary(self) -> str:
        counts: Dict[str, int] = {}
        for row in self.queue_status:
            status = str(row.get("status") or "pending")
            counts[status] = counts.get(status, 0) + 1
        if not counts:
            return "Queue: empty"
        order = ["pending", "downloading", "done", "skipped", "failed", "canceled"]
        parts = [f"{k}:{counts[k]}" for k in order if k in counts]
        return "Queue: " + " ".join(parts)

    def queue_row_attr(self, status: str, active: bool = False) -> int:
        status_l = (status or "").lower()
        if active or status_l in ("downloading", "active"):
            return curses.color_pair(2) | curses.A_BOLD
        if status_l in ("failed", "error", "blocked"):
            return curses.color_pair(5) | curses.A_BOLD
        if status_l in ("unclear", "warning", "skipped"):
            return curses.color_pair(3)
        if status_l in ("marked", "pending"):
            return curses.color_pair(1) | curses.A_BOLD
        return curses.color_pair(6)

    def queue_table_rows(self, width: int, limit: int = 8) -> List[Tuple[str, int]]:
        if width <= 0:
            return []
        rows: List[Tuple[str, int]] = [("STATUS      SIZE       FILE", curses.color_pair(3) | curses.A_BOLD)]
        name_w = max(8, width - 22)
        for row in self.queue_status[:limit]:
            status = str(row.get("status") or "pending")
            size = human_size(int(row.get("size") or 0))
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

        meta = self.cur_meta or {}
        ok, why = is_openly_licensed(meta) if meta else (False, "No metadata loaded")

        self.preview_item = item
        self.preview_file = f
        self.preview_files = []
        self.preview_prefix = ""
        if ok:
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

        meta = self.cur_meta or {}
        ok, why = is_openly_licensed(meta) if meta else (False, "No metadata loaded")
        total = sum(int(f.size or 0) for f in marked)
        self.preview_item = item
        self.preview_file = None
        self.preview_files = list(marked)
        self.preview_prefix = "__SELECTED__"

        if ok:
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

    def _download_one_with_progress(self, identifier: str, filename: str, expected_size: int) -> Tuple[bool, str]:
        path, err = safe_staging_file_path(identifier, filename)
        if err or not path:
            return False, err
        os.makedirs(STAGING_ROOT, exist_ok=True)

        cmd = ia_downloads.single_download_cmd(identifier, filename, IA_NO_CHANGE_TIMESTAMP)
        log_line(f"DL_CMD: {' '.join(cmd)}")
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
                else:
                    self.status = f"{filename}  {human_size(progress.written)} downloaded  (c cancels)"
                self.render()

            ok, msg = ia_downloads.run_download_with_progress(
                cmd,
                target=filename,
                expected_total=int(expected_size or 0),
                read_written=lambda: ia_downloads.safe_getsize(path),
                log_fh=log_fh,
                stall_timeout_s=STALL_TIMEOUT_S,
                is_cancel_requested=check_cancel,
                on_progress=update_progress,
                log_path=LOG_PATH,
            )
            if not ok:
                if msg.startswith("download failed:"):
                    log_line(f"DL_POPEN_ERR: {msg}")
                return False, msg

            ok_sz, msg_sz = self._verify_expected_size(identifier, filename, int(expected_size or 0))
            if not ok_sz:
                return False, msg_sz
            return True, ""
        finally:
            log_fh.close()

    def _download_glob_with_progress(self, identifier: str, glob_pat: str, expected_total: int) -> Tuple[bool, str]:
        os.makedirs(STAGING_ROOT, exist_ok=True)
        os.makedirs(ia_downloads.staging_dir_for_identifier(identifier), exist_ok=True)

        cmd = ia_downloads.glob_download_cmd(identifier, glob_pat, IA_NO_CHANGE_TIMESTAMP)
        log_line(f"DL_GLOB_CMD: {' '.join(cmd)}")
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
                else:
                    self.status = f"{identifier}  {human_size(progress.written)} downloaded  (c cancels)"
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
            return ok, msg
        finally:
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
                self.download_log.insert(0, f"Skipped existing: {existing}")
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
            existing = self.find_existing_media_file(f.name, int(f.size or 0))
            if existing:
                self.set_queue_status(f.name, "skipped", existing)
                self.mode = "FILES"
                self.focus = "LIST"
                self.preview_item = None
                self.preview_file = None
                self.preview_files = []
                self.preview_prefix = ""
                self.status = f"Skipped existing file: {existing}"
                self.download_log.insert(0, f"Skipped existing: {existing}")
                self.download_log = self.download_log[:8]
                return
            self.set_queue_status(f.name, "downloading")
            self.status = f"Downloading: {f.name}"
            self.render()

            ok2, err = self._download_one_with_progress(item.identifier, f.name, int(f.size or 0))
            if not ok2:
                status = "canceled" if "cancel" in err.lower() else "failed"
                self.set_queue_status(f.name, status, err)
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

            msg = self.choose_bucket_and_path(item.identifier, f.name, item.title)
            self.set_queue_status(f.name, "done", msg)
            self.download_log.insert(0, msg)
            self.download_log = self.download_log[:8]
            self.status = msg
            self.render()

            self.mode = "FILES"
            self.focus = "LIST"
            self.preview_item = None
            self.preview_file = None
            self.preview_files = []
            self.preview_prefix = ""
            self.status = "Done. Downloaded 1 file."
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
                for f in queue:
                    existing = self.find_existing_media_file(f.name, int(f.size or 0))
                    if existing:
                        self.set_queue_status(f.name, "skipped", existing)
                        self.download_log.insert(0, f"Skipped existing: {existing}")
                        self.download_log = self.download_log[:8]
                    else:
                        remaining_for_glob.append(f)
                if not remaining_for_glob:
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.status = f"Skipped {len(queue)} existing file(s)."
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
                                       self.preview_prefix, glob_pat, [])
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
                completed_names: List[str] = []
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
                    self.set_queue_status(f.name, "done", msg)
                    completed_names.append(f.name)
                    self.download_log.insert(0, msg)
                    self.download_log = self.download_log[:8]
                    self.status = msg
                    self.render()

                self._clear_pending()
                self.mode = "FILES"
                self.focus = "LIST"
                was_selected_plan = self.preview_prefix == "__SELECTED__"
                self.preview_item = None
                self.preview_file = None
                self.preview_files = []
                self.preview_prefix = ""
                if was_selected_plan:
                    self.selected_file_names.clear()
                    self.save_current_file_view_state()
                self.status = f"Done. Downloaded {len(queue)} file(s)."
                return

            # Full item (visible set). Sequential download keeps progress accurate per-file and imports cleanly.
            seq_completed: List[str] = []
            for idx, f in enumerate(queue):
                existing = self.find_existing_media_file(f.name, int(f.size or 0))
                if existing:
                    self.set_queue_status(f.name, "skipped", existing)
                    seq_completed.append(f.name)
                    self.download_log.insert(0, f"Skipped existing: {existing}")
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
                self.set_queue_status(f.name, "done", msg)
                seq_completed.append(f.name)
                self.download_log.insert(0, msg)
                self.download_log = self.download_log[:8]
                self.status = msg
                self.render()

            self._clear_pending()
            self.mode = "FILES"
            self.focus = "LIST"
            was_selected_plan = self.preview_prefix == "__SELECTED__"
            self.preview_item = None
            self.preview_file = None
            self.preview_files = []
            self.preview_prefix = ""
            if was_selected_plan:
                self.selected_file_names.clear()
                self.save_current_file_view_state()
            self.status = f"Done. Downloaded {len(queue)} file(s)."
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
            "  l          Local filter current result page",
            "  0..9       Jump/select result number",
            "  Tab        Switch MENU <-> LIST focus",
            "  Arrows     Navigate menu items or list",
            "  j / k      Move down / up in list (vim-style)",
            "  g / Home   Jump to first item in list",
            "  G / End    Jump to last item in list",
            "  Enter      Activate menu button / open item or file",
            "  n  ]  PgDn  Next page of results",
            "  p  [  PgUp  Previous page of results",
            "  r          Show selected result metadata summary",
            "  R          Resume pending/failed download state",
            "  #          Go to specific page number",
            "  Space      Mark/unmark file (in FILES mode)",
            "  A / I / U  Mark all visible / invert visible / clear marks",
            "  d          Preview marked files, or selected file if none marked",
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
            "  Downloads stalled for 2 min are killed automatically and can be resumed.",
            "  Files go to staging first, then move to TV / Movies / Music / Other.",
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
        box_h = min(h - 4, 18)
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
            self.hint_bar(include_overlay_state=False),
            "",
            "Universal:  / search   a actions   Tab focus   Backspace back   q quit",
        ]
        if self.mode in ("RESULTS", "SEARCH"):
            lines += [
                "Results:    Enter/o open   l local filter   digits jump   n/p page",
                "Options:    filter, sort, title-only, license gate are in Actions",
            ]
        elif self.mode == "FILES":
            lines += [
                "Files:      Space mark   A all   I invert   U clear   d marked/selected",
                "Filters:    f keyword   v video-only   blank keyword clears",
            ]
        elif self.mode == "FAVS":
            lines += [
                "Favorites:  Enter/o open   Tab changes focus   Actions changes tab/removes",
            ]
        elif self.mode == "PREVIEW_DL":
            lines += ["Preview:    Enter confirms   Backspace/Esc cancels"]
        else:
            lines += ["Navigate:   j/k or arrows   Enter selects"]
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
            "Welcome.",
            "",
            "Press  /  to search, or choose [Search] in the menu.",
            "Use  j/k  or arrows to navigate,  g/G  to jump to start/end.",
            "",
            "Tab switches between MENU and LIST focus.",
            "n / p  or  [ / ]  pages through results.",
            "[Favs] opens your saved items and files.",
            "[Help] shows all shortcuts.",
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
            f"Files: {count}   Total size: {human_size(total)}",
        ]
        if self.preview_file:
            f = self.preview_file
            plan_rows += [f"File: {f.name}", f"Size: {human_size(f.size)}   Format: {f.fmt or '(unknown)'}"]
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
            if not visible_results:
                msg = (
                    f"No results match local filter \"{self.result_filter}\". Press l to change or clear it."
                    if self.results and self.result_filter
                    else "Choose [Search] or press / to begin."
                )
                self.safe_addstr(list_top, 0, msg.ljust(max(0, left_w - 1)), curses.color_pair(6))
            else:
                start_n = (self.page - 1) * ROWS_PER_PAGE + 1
                end_n = (self.page - 1) * ROWS_PER_PAGE + len(self.results)
                if self.total_results > 0:
                    total_pages = max(1, (self.total_results + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)
                    phdr = f" Results {start_n}–{end_n} of {self.total_results}  (page {self.page}/{total_pages})  [ ] or n/p to page"
                else:
                    phdr = f" Results {start_n}–{end_n}  (page {self.page})"
                if self.result_filter:
                    phdr += f"  local: {len(visible_results)}/{len(self.results)}"
                self.safe_addstr(list_top, 0, phdr[: max(0, left_w - 1)].ljust(max(0, left_w - 1)), curses.color_pair(3))
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
                        raw_idx = self.results.index(r)
                    except ValueError:
                        raw_idx = i
                    abs_num = (self.page - 1) * ROWS_PER_PAGE + raw_idx + 1
                    idx = f"{abs_num:02d}"
                    raw_title = (r.title or "")
                    meta = self.result_meta_summary(r)
                    meta_suffix = f"  [{meta}]" if meta else ""
                    max_title = max(12, left_w - 15 - len(meta_suffix))
                    title = (raw_title[:max_title - 1] + "…") if len(raw_title) > max_title else raw_title
                    star = "*" if self.is_fav_item(r.identifier) else " "
                    line = f"{marker} {idx} {star} │ {title}{meta_suffix}"
                    line = line[: max(0, left_w - 1)].ljust(max(0, left_w - 1))

                    if i == self.sel_r:
                        attr = curses.color_pair(7) if self.focus == "LIST" else curses.color_pair(6)
                        if self.focus == "LIST":
                            attr |= curses.A_BOLD
                        self.safe_addstr(list_top + (i - start), 0, line, attr)
                    else:
                        self.safe_addstr(list_top + (i - start), 0, line, curses.color_pair(6))

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
                if self.file_kw:
                    msg = f"No files match \"{self.file_kw}\"  |  f change keyword  |  U clear marks  |  v show all"
                elif self.video_only:
                    msg = "No video files visible  |  v show all  |  f keyword  |  Backspace results"
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
            if sel_item:
                status, status_reason = license_status_from_fields(sel_item.licenseurl, sel_item.rights)
                details += [
                    "Selected:",
                    f"  {sel_item.title or '(no title)'}",
                    f"  Year:    {sel_item.year or '—'}",
                    f"  Type:    {sel_item.mediatype or '—'}",
                    f"  Downloads: {compact_count(sel_item.downloads) if sel_item.downloads else '—'}",
                    f"  License hint: {status}",
                    f"  Creator: {sel_item.creator or '—'}",
                    f"  ID:      {sel_item.identifier}",
                    "",
                ]
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
                if sel_item.description:
                    details.append("Description:")
                    wrap_w = max(10, right_w - 2)
                    for wrapped in textwrap.wrap(sel_item.description, width=wrap_w):
                        details.append(f"  {wrapped}")
                    details.append("")
            details += [
                "Enter or [Open] to view files",
                f"Sort: {self._sort_label()}",
                f"Local filter: {self.result_filter or '(none)'}",
                f"Query: {self.query_built or '(none)'}",
            ]
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

        elif self.mode == "DOWNLOADING":
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
                    f"  {human_size(self.dl_current_written)} downloaded",
                ]
            if self.dl_speed_bps > 0:
                details += [f"  Speed: {human_size(int(self.dl_speed_bps))}/s"]
            if self.dl_eta_s > 0:
                details += [f"  ETA: {int(self.dl_eta_s)}s"]
            details += ["", "Press c to cancel"]

            if self.queue_status:
                details += ["", "Queue:"]
                details += self.queue_table_rows(right_w, limit=8)

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
        if action == "quit":
            self.exit_requested = True
            return

        if action == "actions":
            self.open_action_palette()
            return

        if action == "help":
            self.toggle_help_overlay()
            return

        if action == "theme":
            self.cycle_theme()
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
                    self.do_search(reset_page=True)
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
                    self.do_search(reset_page=True)
                return
            if action == "filter":
                changed = self.choose_filter()
                if changed and self.query_text:
                    self.do_search(reset_page=True)
                return
            if action == "sort":
                changed = self.choose_sort()
                if changed and self.query_text:
                    self.do_search(reset_page=True)
                return
            if action == "result_filter":
                s = self.prompt("Local result filter (blank clears): ", self.result_filter)
                if s is not None:
                    self.set_result_filter(s)
                return
            if action == "title":
                self.title_only = not self.title_only
                self.status = "Search mode: title" if self.title_only else "Search mode: broad"
                if self.query_text:
                    self.do_search(reset_page=True)
                return
            if action == "next_page":
                self.next_page()
                return
            if action == "prev_page":
                self.prev_page()
                return
            if action == "open":
                self.show_welcome = False
                self.load_files()
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
                s = self.prompt("Keyword (blank clears): ", self.file_kw)
                if s is not None:
                    self.file_kw = s.strip()
                    self.sel_f = 0
                    self.status = "Keyword updated"
                    self.save_current_file_view_state()
                else:
                    self.status = "Keyword unchanged."
                self.focus = "LIST"
                return

            if action == "toggle_file_mark":
                self.toggle_current_file_mark()
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

        if self.ia_present:
            self.status = f"Ready (ia: {self.ia_version}). Choose [Search]."
        else:
            self.status = self.ia_version

        self._restore_session()
        if self.query_text:
            self.show_welcome = False
            self.do_search(reset_page=False)

        pending = self._load_pending()
        if pending:
            ptitle = pending.get("item_title") or pending.get("identifier") or "unknown"
            n_remaining = len([f for f in (pending.get("files") or [])
                               if f.get("name") not in set(pending.get("completed_names") or [])])
            self.status = f"Pending: \"{ptitle}\" ({n_remaining} file(s) left) — press R to resume"

        while not self.exit_requested:
            self.render()
            ch = self.stdscr.getch()

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
                if ch in (10, 13, curses.KEY_ENTER):
                    if items and 0 <= self.menu_idx < len(items):
                        _label, action = items[self.menu_idx]
                        self.activate_menu_action(action)
                    continue

            if ch == ord('a'):
                self.open_action_palette()
                continue

            if ch in (ord('/'), ord('s'), ord('S')):
                s = self.prompt("Search: ", self.query_text, history=self.search_history)
                if s is not None:
                    self.query_text = s
                    self.show_welcome = False
                    self.do_search(reset_page=True)
                continue

            if ch == ord('R') and self.mode not in ("DOWNLOADING", "PREVIEW_DL"):
                self.resume_pending_download()
                continue

            if ch == 27 and self.mode == "PREVIEW_DL":
                self.mode = "FILES"
                self.focus = "LIST"
                self.status = "Canceled."
                continue

            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if self.mode == "FILES":
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
                    if ch in (ord('l'), ord('L')) and self.results:
                        s = self.prompt("Local result filter (blank clears): ", self.result_filter)
                        if s is not None:
                            self.set_result_filter(s)
                        continue
                    if ch == ord('r'):
                        self.activate_menu_action("details")
                        continue
                    if ch in (ord('o'), ord('O')):
                        self.show_welcome = False
                        self.load_files()
                        continue
                    if ord('0') <= ch <= ord('9') and self.results:
                        first = chr(ch)
                        val = self.prompt("Jump/select result #: ", first)
                        if val is not None and val.strip().isdigit():
                            self.jump_to_result_number(int(val.strip()))
                        else:
                            self.status = "Canceled."
                        continue
                    if ch in (10, 13, curses.KEY_ENTER):
                        self.show_welcome = False
                        self.load_files()
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
                                self.do_search(reset_page=False)
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
                    if ch in (10, 13, curses.KEY_ENTER):
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
                    if ch in (10, 13, curses.KEY_ENTER):
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
    args = parser.parse_args(argv)

    if args.check:
        return print_environment_check()

    curses.wrapper(main)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main(sys.argv[1:]))
