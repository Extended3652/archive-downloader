#!/usr/bin/env python3
import curses
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Tuple, Optional, Dict, Any


MEDIA_ROOT = "/mnt/ssd/media"
STAGING_ROOT = os.path.join(MEDIA_ROOT, ".ia_staging")

BUCKET_TV = os.path.join(MEDIA_ROOT, "TV")
BUCKET_MOVIES = os.path.join(MEDIA_ROOT, "Movies")
BUCKET_OTHER = os.path.join(MEDIA_ROOT, "Other")

FAVS_PATH = os.path.join(MEDIA_ROOT, ".ia_favorites.json")
LOG_PATH = os.path.join(MEDIA_ROOT, ".ia_dl.log")

FILTERS = ["movies", "audio", "texts", "software", "any"]
ROWS_PER_PAGE = 30

MIN_H = 18
MIN_W = 70

# Keep downloaded file mtimes as "now" so normal tools like find -mmin work as expected.
# This also reduces confusion when verifying "new downloads" by timestamp.
IA_NO_CHANGE_TIMESTAMP = True

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}
VIDEO_FORMAT_HINTS = (
    "h.264",
    "h264",
    "mpeg4",
    "mp4",
    "matroska",
    "webm",
    "quicktime",
    "avi",
)
LARGE_VIDEO_BYTES = 500 * 1024 * 1024


@dataclass
class SearchResult:
    identifier: str
    title: str
    year: str
    creator: str


@dataclass
class IAFile:
    name: str
    size: int
    fmt: str


def log_line(msg: str) -> None:
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def run_cmd(cmd: List[str], timeout: int = 60) -> Tuple[int, str, str]:
    try:
        log_line(f"CMD: {' '.join(cmd)}")
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        log_line(f"RC: {p.returncode}")
        if p.stderr:
            log_line(f"STDERR: {p.stderr.strip()[:2000]}")
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        log_line("RC: 127 (command not found)")
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        log_line(f"RC: 124 (timeout {timeout}s)")
        return 124, "", "command timed out"


def human_size(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)}{units[i]}"
    return f"{f:.2f}{units[i]}"


def ensure_dirs() -> None:
    os.makedirs(STAGING_ROOT, exist_ok=True)
    os.makedirs(BUCKET_TV, exist_ok=True)
    os.makedirs(BUCKET_MOVIES, exist_ok=True)
    os.makedirs(BUCKET_OTHER, exist_ok=True)


def sanitize_folder(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[\/\\:\*\?\"<>\|]+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Unknown"


def detect_sxxeyy(text: str) -> Optional[Tuple[int, int]]:
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def is_video_file(name: str, fmt: str = "") -> bool:
    ext = os.path.splitext((name or "").lower())[1]
    if ext in VIDEO_EXTS:
        return True
    fmt_l = (fmt or "").lower()
    return any(h in fmt_l for h in VIDEO_FORMAT_HINTS)


def auto_clean_movie_folder_name(item_title: str, filename: str) -> str:
    raw = (item_title or "").strip() or (filename or "").strip()
    raw = os.path.basename(raw)
    raw = os.path.splitext(raw)[0]
    raw = re.sub(r"[\[\](){}]", " ", raw)
    raw = re.sub(r"[._]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()

    year = ""
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", raw)
    if year_match:
        year = year_match.group(1)
        title_part = raw[: year_match.start()]
    else:
        title_part = raw

    scene_rx = re.compile(
        r"\b(?:"
        r"2160p|1080p|720p|480p|"
        r"bluray|brrip|bdrip|webrip|web-dl|hdrip|dvdrip|"
        r"x264|x265|h264|h265|hevc|av1|"
        r"aac2?\.0|aac|dts(?:-?hd)?|ddp?5?\.1|ac3|"
        r"proper|repack|extended|remastered|unrated|"
        r"yify|yts|rarbg"
        r")\b",
        re.IGNORECASE,
    )
    title_part = scene_rx.sub(" ", title_part)
    title_part = re.sub(r"\s+", " ", title_part).strip(" -._")

    cleaned_title = sanitize_folder(title_part or raw)
    if year:
        return sanitize_folder(f"{cleaned_title} ({year})")
    return cleaned_title


def build_query(user_text: str, media_filter: str, title_only: bool) -> str:
    s = (user_text or "").strip()
    if not s:
        return ""

    up = s.upper()
    looks_advanced = (
        "mediatype:" in s
        or "title:" in s
        or "collection:" in s
        or "identifier:" in s
        or "licenseurl:" in s
        or "rights:" in s
        or " AND " in up
        or " OR " in up
    )
    if looks_advanced:
        return s

    base = s
    if title_only:
        base = f'title:("{s}")'
    if media_filter and media_filter != "any":
        base = f"{base} AND mediatype:{media_filter}"
    return base


def ia_ok() -> Tuple[bool, str]:
    code, out, err = run_cmd(["ia", "--version"], timeout=10)
    if code == 0:
        return True, out.strip()
    msg = (err or out).strip()
    return False, msg or "ia not available"


def ia_search_via_curl(query: str, rows: int, page: int) -> Tuple[List[SearchResult], str]:
    start = max(0, (page - 1) * rows)

    cmd = [
        "curl",
        "-sS",
        "-G",
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
        "output=json",
        "--data-urlencode",
        f"rows={rows}",
        "--data-urlencode",
        f"start={start}",
    ]

    code, out, err = run_cmd(cmd, timeout=60)
    if code != 0:
        msg = (err or out).strip()
        return [], msg or f"search failed (code {code})"

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], "search returned non-JSON"

    docs = (((data or {}).get("response") or {}).get("docs") or [])
    results: List[SearchResult] = []
    for d in docs:
        ident = str(d.get("identifier", "")).strip()
        if not ident:
            continue
        title = str(d.get("title", "")).strip() or "(no title)"
        year = str(d.get("year", "")).strip()
        creator = str(d.get("creator", "")).strip()
        results.append(SearchResult(ident, title, year, creator))

    return results, ""


def ia_metadata_json(identifier: str) -> Tuple[Optional[Dict[str, Any]], str]:
    code, out, err = run_cmd(["ia", "metadata", identifier], timeout=60)
    if code != 0:
        msg = (err or out).strip()
        return None, msg or f"metadata failed (code {code})"
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


def ia_files(identifier: str) -> Tuple[List[IAFile], Optional[Dict[str, Any]], str]:
    meta, err = ia_metadata_json(identifier)
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


def is_openly_licensed(meta: Dict[str, Any]) -> Tuple[bool, str]:
    m = meta.get("metadata", {}) or {}
    licenseurl = str(m.get("licenseurl", "") or "").lower()
    rights = str(m.get("rights", "") or "").lower()
    possible = [licenseurl, rights]

    allow_markers = [
        "creativecommons.org",
        "cc-by",
        "cc0",
        "public domain",
        "publicdomain",
        "no known copyright",
    ]
    deny_markers = [
        "all rights reserved",
        "copyright",
        "no redistribution",
        "permission required",
    ]

    joined = " | ".join([p for p in possible if p])
    for d in deny_markers:
        if d in joined:
            return False, f"Blocked by rights metadata: {d}"

    for a in allow_markers:
        if a in joined:
            return True, "Open license detected"

    return False, "No clear open license in metadata (licenseurl/rights)."


def staging_file_path(identifier: str, filename: str) -> str:
    return os.path.join(STAGING_ROOT, identifier, filename)


def staging_identifier_dir(identifier: str) -> str:
    return os.path.join(STAGING_ROOT, identifier)


def safe_getsize(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except Exception:
        return 0


def dir_total_size(root: str) -> int:
    total = 0
    try:
        for base, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(base, fn)
                total += safe_getsize(p)
    except Exception:
        return 0
    return total


class RetroWaveIA:
    def __init__(self, stdscr):
        self.stdscr = stdscr

        self.ia_present, self.ia_version = ia_ok()
        self.status = "Ready"
        self.mode = "RESULTS"  # RESULTS / FILES / FAVS / HELP / ERROR / DOWNLOADING / TOO_SMALL / PREVIEW_DL

        self.query_text = ""
        self.query_built = ""
        self.filter = "movies"
        self.title_only = False
        self.enforce_license_gate = False
        self.page = 1

        self.results: List[SearchResult] = []
        self.sel_r = 0

        self.files: List[IAFile] = []
        self.sel_f = 0
        self.file_kw = ""

        self.last_bucket = "TV"  # TV/Movies/Other
        self.download_log: List[str] = []
        self.show_welcome = True

        self.focus = "MENU"  # MENU or LIST
        self.menu_idx = 0

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
        base = {
            "items": [],
            "files": [],
            "folders": {"TV": [], "Movies": [], "Other": []},
        }
        try:
            if os.path.exists(FAVS_PATH):
                with open(FAVS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    base["items"] = data.get("items", []) if isinstance(data.get("items", []), list) else []
                    base["files"] = data.get("files", []) if isinstance(data.get("files", []), list) else []
                    folders = data.get("folders", {})
                    if isinstance(folders, dict):
                        for k in ("TV", "Movies", "Other"):
                            v = folders.get(k, [])
                            base["folders"][k] = v if isinstance(v, list) else []
        except Exception:
            pass
        return base

    def save_favs(self) -> None:
        try:
            tmp = FAVS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.favs, f, indent=2)
            os.replace(tmp, FAVS_PATH)
        except Exception:
            pass

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
        bucket = bucket if bucket in ("TV", "Movies", "Other") else "Other"
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
        curses.init_pair(1, curses.COLOR_MAGENTA, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
        curses.init_pair(5, curses.COLOR_RED, -1)
        curses.init_pair(6, curses.COLOR_WHITE, -1)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_MAGENTA)
        curses.init_pair(9, curses.COLOR_BLACK, curses.COLOR_YELLOW)

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
            item = self.results[self.sel_r] if self.results else None
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

        line1 = f"{header}  |  Filter: {self.filter}  |  Search: {search_mode}  |  Page: {self.page}"
        self.safe_addstr(y, 0, line1[: max(0, w - 1)].ljust(max(0, w - 1)), curses.color_pair(3)); y += 1

        line2 = f"Root: {MEDIA_ROOT}   Staging: {STAGING_ROOT}"
        self.safe_addstr(y, 0, line2[: max(0, w - 1)].ljust(max(0, w - 1)), curses.color_pair(3)); y += 1
        return y

    def get_menu_items(self) -> List[Tuple[str, str]]:
        if self.mode in ("RESULTS", "SEARCH"):
            fav_label = (
                "Unfav" if (self.results and 0 <= self.sel_r < len(self.results)
                            and self.is_fav_item(self.results[self.sel_r].identifier))
                else "Fav"
            )
            return [
                ("Search", "search"),
                (f"Filter: {self.filter}", "filter"),
                (f"Title only: {'On' if self.title_only else 'Off'}", "title"),
                (f"License gate: {'On' if self.enforce_license_gate else 'Off'}", "license_gate"),
                ("Prev", "prev_page"),
                ("Next", "next_page"),
                ("Open", "open"),
                (fav_label, "fav_item"),
                ("Favs", "favs"),
                ("Help", "help"),
                ("Quit", "quit"),
            ]
        if self.mode == "FILES":
            item = self.results[self.sel_r] if self.results else None
            visible = self.get_visible_files()
            sel = visible[self.sel_f] if (visible and 0 <= self.sel_f < len(visible)) else None
            is_f = False
            if item and sel:
                is_f = self.is_fav_file(item.identifier, sel.name)
            fav_file_label = "Fav File" if not is_f else "Unfav File"
            return [
                ("Back", "back"),
                ("Keyword", "keyword"),
                ("Preview", "preview"),
                ("Folder", "folder"),
                ("Item", "item"),
                ("Download", "download"),
                (f"Save to: {self.last_bucket}", "bucket"),
                (fav_file_label, "fav_file"),
                ("Favs", "favs"),
                ("Help", "help"),
                ("Quit", "quit"),
            ]
        if self.mode == "PREVIEW_DL":
            return [("Confirm", "confirm_download"), ("Cancel", "cancel_preview")]
        if self.mode == "FAVS":
            tab = self.favs_tab
            tab_label = f"Tab: {tab}"
            primary = "Open" if tab == "ITEMS" else ("Download" if tab == "FILES" else "Use")
            return [
                ("Back", "back"),
                (tab_label, "tab"),
                (primary, "primary"),
                ("Remove", "remove"),
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
            pill = f"[ {label} ]"
            if x + len(pill) >= w - 1:
                break

            is_sel = (self.focus == "MENU" and i == self.menu_idx)
            attr = curses.color_pair(2)
            if is_sel:
                attr = curses.color_pair(9) | curses.A_BOLD

            self.safe_addstr(y, x, pill, attr)
            x += len(pill) + 1

        if x < w - 1:
            self.safe_addstr(y, x, " " * (w - 1 - x), curses.color_pair(2))

        return y + 1

    def draw_footer(self, h: int, w: int) -> None:
        if h < 4 or w < 2:
            return

        status = (self.status or "")[: max(0, w - 1)]
        self.safe_addstr(h - 3, 0, status.ljust(max(0, w - 1)), curses.color_pair(6))

        if self.mode == "DOWNLOADING":
            keybar = "c cancels  |  q quits after cancel  |  (progress updates live)"
        else:
            keybar = "Arrows move  |  Tab switches menu/list  |  Enter selects  |  q quits"
        self.safe_addstr(h - 2, 0, keybar[: max(0, w - 1)].ljust(max(0, w - 1)), curses.color_pair(2))
        self.safe_addstr(h - 1, 0, ("═" * max(0, w - 1)), curses.color_pair(1))

    def prompt(self, label: str, default: str = "") -> Optional[str]:
        h, w = self.stdscr.getmaxyx()
        if h < 6 or w < 10:
            return None

        y = h - 5
        buf = default
        curses.curs_set(1)
        while True:
            bar = f"{label}{buf}"
            self.safe_addstr(y, 0, " " * max(0, w - 1), curses.color_pair(8))
            self.safe_addstr(y, 0, bar[: max(0, w - 1)], curses.color_pair(8))
            try:
                self.stdscr.move(y, min(w - 2, len(label) + len(buf)))
            except curses.error:
                pass
            self.stdscr.refresh()

            ch = self.stdscr.getch()
            if ch in (10, 13):
                curses.curs_set(0)
                return buf.strip()
            if ch in (27,):
                curses.curs_set(0)
                return None
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
                continue
            if 0 <= ch <= 255:
                c = chr(ch)
                if c.isprintable():
                    buf += c

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

        self.stdscr.nodelay(False)
        while True:
            for y in range(top, top + box_h):
                self.safe_addstr(y, left, " " * max(0, box_w), curses.color_pair(6))

            self.safe_addstr(top, left, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(2))
            self.safe_addstr(top + box_h - 1, left, "└" + "─" * (box_w - 2) + "┘", curses.color_pair(2))
            for y in range(top + 1, top + box_h - 1):
                self.safe_addstr(y, left, "│", curses.color_pair(2))
                self.safe_addstr(y, left + box_w - 1, "│", curses.color_pair(2))

            t = f" {title} "
            self.safe_addstr(top, left + 2, t[: max(0, box_w - 4)], curses.color_pair(1) | curses.A_BOLD)

            body_top = top + 2
            body_bottom = top + box_h - 2
            max_rows = max(1, body_bottom - body_top)

            if idx < start:
                start = idx
            if idx >= start + max_rows:
                start = idx - max_rows + 1

            for i in range(start, min(len(options), start + max_rows)):
                row_y = body_top + (i - start)
                s = options[i]
                line = f" {i+1:02d}. {s}"
                line = line[: max(0, box_w - 2)].ljust(max(0, box_w - 2))
                if i == idx:
                    self.safe_addstr(row_y, left + 1, line, curses.color_pair(9) | curses.A_BOLD)
                else:
                    self.safe_addstr(row_y, left + 1, line, curses.color_pair(6))

            hint = "Up/Down choose  Enter select  Esc cancel"
            self.safe_addstr(top + box_h - 1, left + 2, hint[: max(0, box_w - 4)], curses.color_pair(3))

            self.stdscr.refresh()
            ch = self.stdscr.getch()

            if ch in (27,):
                return None
            if ch in (10, 13, curses.KEY_ENTER):
                return options[idx]
            if ch == curses.KEY_UP:
                idx = max(0, idx - 1)
            if ch == curses.KEY_DOWN:
                idx = min(len(options) - 1, idx + 1)

    # ---------- logic ----------
    def cycle_filter(self) -> None:
        idx = FILTERS.index(self.filter) if self.filter in FILTERS else 0
        self.filter = FILTERS[(idx + 1) % len(FILTERS)]
        self.status = f"Filter set to: {self.filter}"

    def do_search(self, reset_page: bool = True) -> None:
        if reset_page:
            self.page = 1
        self.query_built = build_query(self.query_text, self.filter, self.title_only)
        if not self.query_built:
            self.status = "Select [Search] in the menu to search."
            return

        self.status = "Searching..."
        self.render()

        self.results, err = ia_search_via_curl(self.query_built, rows=ROWS_PER_PAGE, page=self.page)
        if err:
            self.status = err
            return

        self.sel_r = 0
        self.mode = "RESULTS"
        self.focus = "LIST"
        self.status = f"Found {len(self.results)} results. Use arrows, then [Open]."

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
        if not self.results:
            self.status = "No results to open."
            return
        item = self.results[self.sel_r]
        self.status = f"Loading files for {item.identifier}..."
        self.render()

        files, meta, err = ia_files(item.identifier)
        if err:
            self.status = err
            return

        self.cur_meta = meta
        self.files = files
        self.file_kw = ""
        self.sel_f = 0
        self.mode = "FILES"
        self.focus = "LIST"
        self.status = "Use arrows to choose a file, then [Preview], [Folder], [Item], or [Download]."

    def get_visible_files(self) -> List[IAFile]:
        files = list(self.files)
        kw = self.file_kw.strip()
        if kw:
            rx = re.compile(re.escape(kw), re.IGNORECASE)
            files = [f for f in files if rx.search(f.name) or rx.search(f.fmt)]
        return files

    def cycle_bucket(self) -> None:
        order = ["TV", "Movies", "Other"]
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

    def choose_bucket_and_path(self, identifier: str, filename: str, item_title: str) -> str:
        staging_path = staging_file_path(identifier, filename)
        if not os.path.exists(staging_path):
            return f"Downloaded, but staging file not found: {staging_path}"
    
        # --- helpers (local, minimal impact) ---
        def has_year_hint(s: str) -> bool:
            if not s:
                return False
            # common patterns: "(1993)", "1993", "- 1993 -"
            return bool(re.search(r"\((19|20)\d{2}\)", s) or re.search(r"(19|20)\d{2}", s))
    
        def is_single_large_video(name: str) -> bool:
            try:
                video_files = [f for f in self.files if is_video_file(f.name, f.fmt)]
                large_video_files = [f for f in video_files if int(f.size or 0) >= LARGE_VIDEO_BYTES]
                return (len(large_video_files) == 1 and (large_video_files[0].name or "") == name)
            except Exception:
                return False
    
        ep = detect_sxxeyy(filename) or detect_sxxeyy(item_title)
    
        # Start from last bucket, but allow smart overrides
        bucket = self.last_bucket if self.last_bucket in ("TV", "Movies", "Other") else "Other"
    
        # If it clearly looks episodic, force TV regardless of last choice
        if ep:
            bucket = "TV"
        else:
            # If it looks like a movie, force Movies regardless of last choice
            # (year hint OR single large video file with no SxxEyy)
            if has_year_hint(filename) or has_year_hint(item_title) or is_single_large_video(filename):
                bucket = "Movies"
    
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
    
            # If we detected SxxEyy, do not ask season/episode questions
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
            os.makedirs(movie_dir, exist_ok=True)
            final_path = os.path.join(movie_dir, filename)
    
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
            os.makedirs(other_dir, exist_ok=True)
            final_path = os.path.join(other_dir, filename)
    
        if os.path.exists(final_path):
            base, ext = os.path.splitext(final_path)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            final_path = f"{base}_{stamp}{ext}"
    
        shutil.move(staging_path, final_path)
        return f"Saved: {final_path}"
    
    def set_preview_for_selected(self) -> None:
        if not self.results:
            self.status = "No item selected."
            return
        item = self.results[self.sel_r]
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
        self.mode = "PREVIEW_DL"
        self.focus = "MENU"
        self.menu_idx = 0
        self.status = "Preview (no changes)."

    def set_preview_for_prefix(self) -> None:
        if not self.results:
            self.status = "No item selected."
            return
        item = self.results[self.sel_r]
        meta = self.cur_meta or {}
        ok, why = is_openly_licensed(meta) if meta else (False, "No metadata loaded")

        prefix = self.prompt("Folder/prefix to download (matches start of filename): ", "")
        if prefix is None:
            self.status = "Canceled."
            return
        prefix = prefix.strip()
        if not prefix:
            self.status = "No prefix provided."
            return

        visible = self.get_visible_files()
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

        self.mode = "PREVIEW_DL"
        self.focus = "MENU"
        self.menu_idx = 0
        self.status = "Preview (no changes)."

    def set_preview_for_item(self) -> None:
        if not self.results:
            self.status = "No item selected."
            return
        item = self.results[self.sel_r]
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
            self.preview_msg = f"Open license detected. Will download {len(visible)} visible files ({human_size(total)})."
        else:
            if self.enforce_license_gate:
                self.preview_msg = f"Download blocked. {why}"
            else:
                self.preview_msg = f"Rights unclear. {why}  You can still download if you confirm."

        self.mode = "PREVIEW_DL"
        self.focus = "MENU"
        self.menu_idx = 0
        self.status = "Preview (no changes)."

    def _ia_download_base_args(self) -> List[str]:
        args: List[str] = []
        if IA_NO_CHANGE_TIMESTAMP:
            args.append("--no-change-timestamp")
        return args

    def _verify_expected_size(self, identifier: str, filename: str, expected_size: int) -> Tuple[bool, str]:
        if expected_size <= 0:
            return True, ""
        p = staging_file_path(identifier, filename)
        actual = safe_getsize(p)
        if actual != int(expected_size):
            return False, f"Size mismatch for {filename}: got {human_size(actual)} expected {human_size(int(expected_size))}"
        return True, ""

    def _download_one_with_progress(self, identifier: str, filename: str, expected_size: int) -> Tuple[bool, str]:
        os.makedirs(STAGING_ROOT, exist_ok=True)

        cmd = ["ia", "download", identifier, filename, "--destdir", STAGING_ROOT] + self._ia_download_base_args()
        log_line(f"DL_CMD: {' '.join(cmd)}")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            log_line(f"DL_POPEN_ERR: {e}")
            return False, f"download failed: {e}"

        start_t = time.time()
        last_t = start_t
        last_bytes = 0
        self.dl_cancel_requested = False

        self.dl_current_name = filename
        self.dl_current_total = int(expected_size or 0)
        self.dl_current_written = 0
        self.dl_speed_bps = 0.0
        self.dl_eta_s = 0.0

        self.stdscr.nodelay(True)

        while True:
            rc = p.poll()
            path = staging_file_path(identifier, filename)

            written = safe_getsize(path)
            self.dl_current_written = written

            now = time.time()
            dt = now - last_t
            if dt >= 0.5:
                delta = max(0, written - last_bytes)
                self.dl_speed_bps = float(delta) / float(dt) if dt > 0 else 0.0
                if self.dl_current_total > 0 and self.dl_speed_bps > 0:
                    remain = max(0, self.dl_current_total - written)
                    self.dl_eta_s = float(remain) / float(self.dl_speed_bps)
                else:
                    self.dl_eta_s = 0.0
                last_t = now
                last_bytes = written

            ch = self.stdscr.getch()
            if ch in (ord("c"), ord("C")):
                self.dl_cancel_requested = True
                try:
                    p.terminate()
                except Exception:
                    pass

            if self.dl_current_total > 0:
                pct = int((written * 100) / self.dl_current_total) if self.dl_current_total else 0
                sp = human_size(int(self.dl_speed_bps)) + "/s" if self.dl_speed_bps > 0 else "?/s"
                eta = f"{int(self.dl_eta_s)}s" if self.dl_eta_s > 0 else "?"
                self.status = f"{filename}  {pct}%  {human_size(written)}/{human_size(self.dl_current_total)}  {sp}  ETA {eta}  (c cancels)"
            else:
                self.status = f"{filename}  {human_size(written)} downloaded  (c cancels)"

            self.render()

            if rc is not None:
                out, err = p.communicate(timeout=2)
                if err:
                    log_line(f"DL_STDERR: {err.strip()[:2000]}")
                if out:
                    log_line(f"DL_STDOUT: {out.strip()[:2000]}")

                if self.dl_cancel_requested:
                    return False, "Canceled."
                if rc != 0:
                    msg = (err or out or "").strip()
                    return False, msg or f"download failed (code {rc})"

                ok_sz, msg_sz = self._verify_expected_size(identifier, filename, int(expected_size or 0))
                if not ok_sz:
                    return False, msg_sz
                return True, ""

            time.sleep(0.1)

    def _download_glob_with_progress(self, identifier: str, glob_pat: str, expected_total: int) -> Tuple[bool, str]:
        os.makedirs(STAGING_ROOT, exist_ok=True)
        os.makedirs(staging_identifier_dir(identifier), exist_ok=True)

        cmd = ["ia", "download", identifier, "--destdir", STAGING_ROOT, "--glob", glob_pat] + self._ia_download_base_args()
        log_line(f"DL_GLOB_CMD: {' '.join(cmd)}")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            log_line(f"DL_GLOB_POPEN_ERR: {e}")
            return False, f"download failed: {e}"

        start_t = time.time()
        last_t = start_t
        last_bytes = 0
        self.dl_cancel_requested = False

        self.dl_current_name = f"--glob {glob_pat}"
        self.dl_current_total = int(expected_total or 0)
        self.dl_current_written = 0
        self.dl_speed_bps = 0.0
        self.dl_eta_s = 0.0

        self.stdscr.nodelay(True)

        base_dir = staging_identifier_dir(identifier)

        while True:
            rc = p.poll()

            written = dir_total_size(base_dir)
            self.dl_current_written = written

            now = time.time()
            dt = now - last_t
            if dt >= 0.5:
                delta = max(0, written - last_bytes)
                self.dl_speed_bps = float(delta) / float(dt) if dt > 0 else 0.0
                if self.dl_current_total > 0 and self.dl_speed_bps > 0:
                    remain = max(0, self.dl_current_total - written)
                    self.dl_eta_s = float(remain) / float(self.dl_speed_bps)
                else:
                    self.dl_eta_s = 0.0
                last_t = now
                last_bytes = written

            ch = self.stdscr.getch()
            if ch in (ord("c"), ord("C")):
                self.dl_cancel_requested = True
                try:
                    p.terminate()
                except Exception:
                    pass

            if self.dl_current_total > 0:
                pct = int((written * 100) / self.dl_current_total) if self.dl_current_total else 0
                sp = human_size(int(self.dl_speed_bps)) + "/s" if self.dl_speed_bps > 0 else "?/s"
                eta = f"{int(self.dl_eta_s)}s" if self.dl_eta_s > 0 else "?"
                self.status = f"{identifier}  {pct}%  {human_size(written)}/{human_size(self.dl_current_total)}  {sp}  ETA {eta}  (c cancels)"
            else:
                self.status = f"{identifier}  {human_size(written)} downloaded  (c cancels)"

            self.render()

            if rc is not None:
                out, err = p.communicate(timeout=2)
                if err:
                    log_line(f"DL_GLOB_STDERR: {err.strip()[:2000]}")
                if out:
                    log_line(f"DL_GLOB_STDOUT: {out.strip()[:2000]}")

                if self.dl_cancel_requested:
                    return False, "Canceled."
                if rc != 0:
                    msg = (err or out or "").strip()
                    return False, msg or f"download failed (code {rc})"
                return True, ""

            time.sleep(0.1)

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
            s = self.prompt('Rights unclear. Type "DOWNLOAD" to proceed, or Esc to cancel: ', "")
            if s is None or s.strip().upper() != "DOWNLOAD":
                self.status = "Canceled."
                self.mode = "FILES"
                self.focus = "LIST"
                return

        item = self.preview_item

        # single file
        if self.preview_file:
            queue = [self.preview_file]
            self.dl_overall_total = sum(int(f.size or 0) for f in queue)
            self.dl_overall_written = 0

            self.mode = "DOWNLOADING"
            self.focus = "MENU"

            f = queue[0]
            self.status = f"Downloading: {f.name}"
            self.render()

            ok2, err = self._download_one_with_progress(item.identifier, f.name, int(f.size or 0))
            if not ok2:
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
            total_expected = sum(int(f.size or 0) for f in queue)

            self.mode = "DOWNLOADING"
            self.focus = "MENU"

            if self.preview_prefix and self.preview_prefix != "__FULL_ITEM__":
                # Use ia --glob for prefix downloads.
                # NOTE: IA globs are matched against the "name" field (including folder paths).
                # Using prefix* matches "prefix..." including subpaths if prefix includes a folder/ path.
                glob_pat = f"{self.preview_prefix}*"
                self.status = f"Downloading prefix via --glob: {glob_pat}"
                self.render()

                ok2, err = self._download_glob_with_progress(item.identifier, glob_pat, int(total_expected))
                if not ok2:
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

                # Import each expected file (now that the glob run finished).
                for f in queue:
                    ok_sz, msg_sz = self._verify_expected_size(item.identifier, f.name, int(f.size or 0))
                    if not ok_sz:
                        self.mode = "FILES"
                        self.focus = "LIST"
                        self.preview_item = None
                        self.preview_file = None
                        self.preview_files = []
                        self.preview_prefix = ""
                        self.status = msg_sz
                        self.download_log.insert(0, f"Error: {msg_sz}")
                        self.download_log = self.download_log[:8]
                        return

                    msg = self.choose_bucket_and_path(item.identifier, f.name, item.title)
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
                self.status = f"Done. Downloaded {len(queue)} file(s)."
                return

            # Full item (visible set). Sequential download keeps progress accurate per-file and imports cleanly.
            for idx, f in enumerate(queue):
                self.dl_current_name = f.name
                self.dl_current_total = int(f.size or 0)
                self.dl_current_written = 0

                self.status = f"Downloading {idx+1}/{len(queue)}: {f.name}"
                self.render()

                ok2, err = self._download_one_with_progress(item.identifier, f.name, int(f.size or 0))
                if not ok2:
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
            self.status = f"Done. Downloaded {len(queue)} file(s)."
            return

        self.status = "Nothing selected."
        self.mode = "FILES"
        self.focus = "LIST"

    # ---------- render ----------
    def draw_help(self, top_y: int) -> None:
        h, w = self.stdscr.getmaxyx()
        y = top_y

        lines = [
            "Flow:",
            "  1) MENU -> [Search]",
            "  2) LIST -> pick result",
            "  3) MENU -> [Open]",
            "  4) LIST -> pick file",
            "  5) MENU -> [Preview] then Confirm to download (if allowed)",
            "  6) Or MENU -> [Folder] to download all files that share a prefix (uses ia --glob)",
            "  7) Or MENU -> [Item] to download all visible files (keyword filter applies)",
            "",
            "Download behavior:",
            "  - No download starts without confirmation.",
            "  - Progress is shown by tracking staged bytes on disk.",
            "  - Press c to cancel while downloading.",
            "",
            "Notes:",
            "  - Downloads are blocked unless metadata indicates an open license (CC/PD).",
            "  - Staging is used first, then files are moved into TV/Movies/Other.",
            f"  - {'--no-change-timestamp is enabled (mtimes set to now).' if IA_NO_CHANGE_TIMESTAMP else 'Source mtimes are preserved.'}",
            "  - Log file: /mnt/ssd/media/.ia_dl.log",
        ]

        for line in lines:
            if y >= h - 4:
                break
            self.safe_addstr(y, 0, line[: max(0, w - 1)], curses.color_pair(6))
            y += 1

    def draw_welcome(self, top_y: int) -> None:
        h, w = self.stdscr.getmaxyx()
        lines = [
            "Welcome.",
            "",
            "Use the menu at the top.",
            "Choose [Search] to begin.",
            "",
            "Tip: Tab switches MENU and LIST.",
        ]
        center_y = top_y + 3
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

        lines: List[str] = []
        if item and self.preview_file:
            f = self.preview_file
            lines += [
                "Preview (no changes)",
                "",
                f"Item: {item.title}",
                f"Identifier: {item.identifier}",
                "",
                f"File: {f.name}",
                f"Size: {human_size(f.size)}   Format: {f.fmt or '(unknown)'}",
                "",
                f"Save bucket: {self.last_bucket}",
                "",
                self.preview_msg or "",
                "",
                "Confirm will download into staging, then prompt for naming and move into media folders.",
            ]
        elif item and self.preview_files:
            total = sum(int(f.size or 0) for f in self.preview_files)
            mode_label = "Full item (visible files)" if self.preview_prefix == "__FULL_ITEM__" else f"Folder/prefix: {self.preview_prefix}"
            lines += [
                "Preview (no changes)",
                "",
                f"Item: {item.title}",
                f"Identifier: {item.identifier}",
                "",
                mode_label,
                f"Files: {len(self.preview_files)}   Total size: {human_size(total)}",
                "",
                self.preview_msg or "",
                "",
                "First matches:",
            ]
            for f in self.preview_files[:10]:
                lines.append(f"  {human_size(int(f.size or 0)):>9}  {f.name}")
            if len(self.preview_files) > 10:
                lines.append(f"  ... and {len(self.preview_files) - 10} more")
            lines += ["", "Confirm will download, then import files into media folders."]
        else:
            lines = ["Nothing selected."]

        for line in lines:
            if y >= h - 4:
                break
            self.safe_addstr(y, 0, line[: max(0, w - 1)], curses.color_pair(6))
            y += 1

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

        self.safe_addstr(body_top, 0, f" {left_title} ".ljust(max(0, left_w - 1), "─"), curses.color_pair(2))
        self.safe_addstr(body_top, right_x, " DETAILS ".ljust(max(0, right_w), "─")[: max(0, right_w)], curses.color_pair(2))

        list_top = body_top + 1
        max_rows = body_bottom - list_top
        if max_rows <= 0:
            return

        if self.mode in ("RESULTS", "SEARCH"):
            if not self.results:
                self.safe_addstr(list_top, 0, "Choose [Search] in the menu to begin.".ljust(max(0, left_w - 1)), curses.color_pair(6))
            else:
                start = 0
                if self.sel_r >= max_rows:
                    start = self.sel_r - max_rows + 1
                for i in range(start, min(len(self.results), start + max_rows)):
                    r = self.results[i]
                    marker = ">" if i == self.sel_r else " "
                    abs_num = (self.page - 1) * ROWS_PER_PAGE + i + 1
                    idx = f"{abs_num:02d}"
                    title = (r.title or "")[:40]
                    year = f" ({r.year})" if r.year else ""
                    star = "*" if self.is_fav_item(r.identifier) else " "
                    line = f"{marker} {idx} {star} │ {title}{year}"
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
            if not visible:
                self.safe_addstr(list_top, 0, "No matching files. Use [Keyword].".ljust(max(0, left_w - 1)), curses.color_pair(6))
            else:
                if self.sel_f >= len(visible):
                    self.sel_f = max(0, len(visible) - 1)
                start = 0
                if self.sel_f >= max_rows:
                    start = self.sel_f - max_rows + 1
                item = self.results[self.sel_r] if self.results else None
                for i in range(start, min(len(visible), start + max_rows)):
                    f = visible[i]
                    marker = ">" if i == self.sel_f else " "
                    star = " "
                    if item and self.is_fav_file(item.identifier, f.name):
                        star = "*"
                    line = f"{marker} {i+1:02d} {star} │ {human_size(f.size):>9}  {f.name}"
                    line = line[: max(0, left_w - 1)].ljust(max(0, left_w - 1))

                    if i == self.sel_f:
                        attr = curses.color_pair(8) if self.focus == "LIST" else curses.color_pair(6)
                        if self.focus == "LIST":
                            attr |= curses.A_BOLD
                        self.safe_addstr(list_top + (i - start), 0, line, attr)
                    else:
                        self.safe_addstr(list_top + (i - start), 0, line, curses.color_pair(6))

        ry = list_top
        details: List[str] = []

        if self.mode in ("RESULTS", "SEARCH"):
            details = [
                "What happens next:",
                "  Select item, then Open to view files",
                "",
                "MENU:",
                "  Search -> Filter -> Open",
                "  Fav -> save this item",
                "",
                f"Query: {self.query_built or '(none)'}",
            ]
        elif self.mode == "FILES":
            item = self.results[self.sel_r] if self.results else None
            visible = self.get_visible_files()
            sel = visible[self.sel_f] if (visible and 0 <= self.sel_f < len(visible)) else None
            details = [
                "What happens next:",
                "  Preview -> Confirm -> Download",
                "  Folder -> prefix bulk download",
                "  Item -> download all visible",
                "",
                f"Save to: {self.last_bucket}",
                f"Keyword: {self.file_kw or '(none)'}",
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
            ]
            if self.dl_current_total > 0:
                pct = int((self.dl_current_written * 100) / self.dl_current_total) if self.dl_current_total else 0
                details += [
                    f"  {pct}%  {human_size(self.dl_current_written)}/{human_size(self.dl_current_total)}",
                ]
            else:
                details += [f"  {human_size(self.dl_current_written)} downloaded"]
            if self.dl_speed_bps > 0:
                details += [f"  Speed: {human_size(int(self.dl_speed_bps))}/s"]
            if self.dl_eta_s > 0:
                details += [f"  ETA: {int(self.dl_eta_s)}s"]
            details += ["", "Press c to cancel"]

        for line in details:
            if ry >= body_bottom:
                break
            self.safe_addstr(ry, right_x, line[: max(0, right_w)].ljust(max(0, right_w)), curses.color_pair(6))
            ry += 1

        if right_w > 10 and self.download_log and self.mode != "FAVS":
            ry2 = body_bottom - min(6, len(self.download_log) + 1)
            if ry2 > list_top + 2:
                self.safe_addstr(ry2, right_x, " RECENT ".ljust(max(0, right_w), "─")[: max(0, right_w)], curses.color_pair(2))
                ry2 += 1
                for msg in self.download_log[:5]:
                    if ry2 >= body_bottom:
                        break
                    self.safe_addstr(ry2, right_x, msg[: max(0, right_w)].ljust(max(0, right_w)), curses.color_pair(6))
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

        self.draw_footer(h, w)
        self.stdscr.refresh()

    # ---------- menu actions ----------
    def activate_menu_action(self, action: str) -> None:
        if action == "quit":
            self.exit_requested = True
            return

        if action == "help":
            self.mode = "HELP" if self.mode != "HELP" else ("FILES" if self.files else "RESULTS")
            self.focus = "MENU"
            self.menu_idx = 0
            self.status = "Help" if self.mode == "HELP" else "Back"
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
                s = self.prompt("Search: ", self.query_text)
                if s is not None:
                    self.query_text = s
                    self.show_welcome = False
                    self.do_search(reset_page=True)
                return
            if action == "filter":
                self.cycle_filter()
                return
            if action == "title":
                self.title_only = not self.title_only
                self.status = "Search mode: title" if self.title_only else "Search mode: broad"
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
            if action == "fav_item":
                if not self.results:
                    self.status = "No result selected."
                    return
                self.toggle_fav_item(self.results[self.sel_r])
                return

        if self.mode == "FILES":
            visible = self.get_visible_files()

            if action == "keyword":
                s = self.prompt("Keyword (blank clears): ", self.file_kw)
                if s is not None:
                    self.file_kw = s.strip()
                    self.sel_f = 0
                    self.status = "Keyword updated"
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
                self.set_preview_for_selected()
                return

            if action == "fav_file":
                item = self.results[self.sel_r] if self.results else None
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
                except Exception:
                    i = 0
                self.favs_tab = order[(i + 1) % len(order)]
                self.favs_idx = 0
                self.status = f"Favorites tab: {self.favs_tab}"
                return

            if action == "remove":
                self.status = "Remove not implemented in this build."
                return

            if action == "primary":
                self.status = "Primary not implemented in this build."
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

        while not self.exit_requested:
            self.render()
            ch = self.stdscr.getch()

            if ch in (ord("q"), ord("Q")):
                if self.mode == "PREVIEW_DL":
                    self.mode = "FILES"
                    self.focus = "LIST"
                    self.status = "Canceled."
                    continue
                break

            if self.mode == "ERROR" or self.term_too_small():
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

            if ch == ord('/'):
                s = self.prompt("Search: ", self.query_text)
                if s is not None:
                    self.query_text = s
                    self.show_welcome = False
                    self.do_search(reset_page=True)
                continue

            if self.focus == "LIST":
                if self.mode in ("RESULTS", "SEARCH"):
                    if ch == curses.KEY_UP and self.results:
                        self.sel_r = max(0, self.sel_r - 1)
                        continue
                    if ch == curses.KEY_DOWN and self.results:
                        self.sel_r = min(len(self.results) - 1, self.sel_r + 1)
                        continue
                    if ch in (10, 13, curses.KEY_ENTER):
                        self.show_welcome = False
                        self.load_files()
                        continue

                if self.mode == "FILES":
                    visible = self.get_visible_files()
                    if ch == curses.KEY_UP and visible:
                        self.sel_f = max(0, self.sel_f - 1)
                        continue
                    if ch == curses.KEY_DOWN and visible:
                        self.sel_f = min(len(visible) - 1, self.sel_f + 1)
                        continue
                    if ch in (10, 13, curses.KEY_ENTER):
                        self.set_preview_for_selected()
                        continue
                    if ch in (curses.KEY_BACKSPACE, 127, 8):
                        self.mode = "RESULTS"
                        self.focus = "LIST"
                        self.status = "Back to results"
                        continue

        # exit


def main(stdscr):
    app = RetroWaveIA(stdscr)
    app.loop()


if __name__ == "__main__":
    curses.wrapper(main)
