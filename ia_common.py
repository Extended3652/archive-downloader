"""Shared helpers for ia_dl / ia_easy / ia_minotaur.

Centralizes the dataclasses, subprocess wrapper, and small utilities that
were previously duplicated across the three scripts.
"""
import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional

import ia_config


# ---- shared dataclasses (unify on `fmt`; ia_dl previously used `format`) ----
@dataclass
class SearchResult:
    identifier: str
    title: str
    year: str = ""
    creator: str = ""
    description: str = ""
    mediatype: str = ""
    formats: str = ""
    downloads: int = 0
    date: str = ""
    publicdate: str = ""
    collection: str = ""
    licenseurl: str = ""
    rights: str = ""
    source: str = "ia"
    webpage_url: str = ""
    video_id: str = ""
    uploader: str = ""
    duration: int = 0
    upload_date: str = ""


@dataclass
class IAFile:
    name: str
    size: int
    fmt: str = ""


# ---- shared constants ----
DEFAULT_MEDIA_ROOT = ia_config.DEFAULT_MEDIA_ROOT
DVD_IMAGE_EXTS = {".iso"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"} | DVD_IMAGE_EXTS
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
TORRENT_FORMAT_HINTS = (
    "archive bittorrent",
    "bittorrent",
    "torrent",
)


# ---- exceptions (replace sys.exit() in run()) ----
class IACommandError(RuntimeError):
    """Raised when an `ia` (or other) subprocess fails."""

    def __init__(self, cmd: List[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(cmd)} -> rc={returncode}: {stderr}")


class IANotInstalled(IACommandError):
    """Raised when the `ia` binary isn't on PATH."""


# ---- shared functions ----
def default_media_root() -> str:
    """Return the configured media root shared by all command wrappers."""
    return ia_config.load_config()["media_root"]


def run(cmd: List[str], *, check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run a subprocess and return its CompletedProcess.

    Raises IANotInstalled if the binary isn't on PATH, or IACommandError on
    a non-zero exit when check=True. Callers decide what to do with errors
    instead of having the library exit the process.
    """
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=check,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise IANotInstalled(cmd, 127, str(e)) from e
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or "").strip()
        raise IACommandError(cmd, e.returncode, msg) from e


def human_size(n) -> str:
    """Format a byte count as a short human-readable string (e.g. 1.50MB)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)}{units[i]}"
    return f"{f:.2f}{units[i]}"


def compact_count(n) -> str:
    """Format a count compactly for list columns (e.g. 1.2K, 3.4M)."""
    try:
        value = int(n)
    except (TypeError, ValueError):
        return "?"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value < 1000:
        return f"{sign}{value}"
    units = [(1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")]
    for factor, suffix in units:
        if value >= factor:
            scaled = value / factor
            if scaled >= 10:
                return f"{sign}{scaled:.0f}{suffix}"
            return f"{sign}{scaled:.1f}{suffix}"
    return f"{sign}{value}"


def is_video_file(name: str, fmt: str = "") -> bool:
    """Return True if a filename or IA format string looks like video."""
    ext = os.path.splitext((name or "").lower())[1]
    if ext in VIDEO_EXTS:
        return True
    fmt_l = (fmt or "").lower()
    return any(h in fmt_l for h in VIDEO_FORMAT_HINTS)


def is_dvd_iso_file(name: str, fmt: str = "") -> bool:
    """Return True if a filename or IA format string looks like a DVD ISO image."""
    ext = os.path.splitext((name or "").lower())[1]
    if ext in DVD_IMAGE_EXTS:
        return True
    fmt_l = (fmt or "").lower()
    name_l = (name or "").lower()
    return "dvd" in fmt_l and ("iso" in fmt_l or name_l.endswith(".iso"))


def is_archive_torrent_format(fmt: str) -> bool:
    """Return True if an IA format string indicates torrent availability."""
    fmt_l = (fmt or "").lower()
    return any(hint in fmt_l for hint in TORRENT_FORMAT_HINTS)


def safe_path_under(root: str, candidate: str) -> bool:
    """Return True iff `candidate` (after symlink/.. resolution) lives under `root`."""
    try:
        root_abs = os.path.realpath(root)
        cand_abs = os.path.realpath(candidate)
        return os.path.commonpath([root_abs, cand_abs]) == root_abs
    except (ValueError, OSError):
        return False
