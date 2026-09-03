"""Shared helpers for ia_dl / ia_easy / ia_minotaur.

Centralizes the dataclasses, subprocess wrapper, and small utilities that
were previously duplicated across the three scripts.
"""
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
    source: str = ""
    original: str = ""
    md5: str = ""
    sha1: str = ""
    crc32: str = ""
    raw_metadata: Dict[str, Any] = None
    variant_names: Tuple[str, ...] = ()
    variant_metadata: Tuple[Dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.raw_metadata is None:
            self.raw_metadata = {}


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


def terminal_ia_variant_name(name: str) -> Optional[str]:
    """Return the normal filename for ``name.ia.ext``, if it has that shape."""
    match = re.match(r"^(.*)\.ia(\.[^/]+)$", str(name or ""), re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1)}{match.group(2)}"


def _metadata_value(file: IAFile, key: str) -> str:
    value = getattr(file, key, "") or (file.raw_metadata or {}).get(key, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _explicit_derivative_pair(normal: IAFile, variant: IAFile) -> bool:
    source = _metadata_value(variant, "source").lower()
    original = _metadata_value(variant, "original")
    return source == "derivative" and original == normal.name


def _same_trusted_hash(normal: IAFile, variant: IAFile) -> bool:
    if int(normal.size or 0) != int(variant.size or 0):
        return False
    for key in ("sha1", "md5"):
        left = _metadata_value(normal, key).lower()
        right = _metadata_value(variant, key).lower()
        if left and right and left == right:
            return True
    return False


def _metadata_snapshot(file: IAFile) -> Dict[str, Any]:
    snapshot = dict(file.raw_metadata or {})
    for key in ("source", "original", "md5", "sha1", "crc32"):
        value = getattr(file, key, "")
        if value and key not in snapshot:
            snapshot[key] = value
    return snapshot


def deduplicate_file_variants(files: List[IAFile]) -> List[IAFile]:
    """Collapse only proven terminal ``.ia``/normal filename variants.

    The returned preferred file retains the complete variant name list. Files
    with only filename similarity, or conflicting/insufficient metadata, stay
    as separate logical files.
    """
    by_name = {f.name: f for f in files}
    hidden_names = set()
    result: List[IAFile] = []
    for file in files:
        if file.name in hidden_names:
            continue
        normal_name = terminal_ia_variant_name(file.name)
        normal = by_name.get(normal_name or "")
        if normal is None:
            result.append(file)
            continue

        variant = file
        if normal.name == file.name:
            variant_name = terminal_ia_variant_name(normal.name)
            variant = by_name.get(variant_name or "")
        if variant is None:
            result.append(file)
            continue

        proven = _explicit_derivative_pair(normal, variant) or _same_trusted_hash(normal, variant)
        if not proven:
            result.append(file)
            continue

        preferred = normal
        preferred.variant_names = tuple(dict.fromkeys([normal.name, variant.name]))
        preferred.variant_metadata = tuple(
            _metadata_snapshot(f) for f in (normal, variant)
        )
        hidden_names.update(preferred.variant_names)
        result.append(preferred)
    return result


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
