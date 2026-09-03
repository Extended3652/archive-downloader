"""Path configuration and filesystem guards for the IA downloader tools."""
import os
import stat
import tempfile
from typing import Optional, Tuple

from ia_common import default_media_root, safe_path_under


# MEDIA_ROOT is configurable via the IA_MEDIA_ROOT env var.
MEDIA_ROOT = default_media_root()
STAGING_ROOT = os.path.join(MEDIA_ROOT, ".ia_staging")

BUCKET_TV = os.path.join(MEDIA_ROOT, "TV")
BUCKET_MOVIES = os.path.join(MEDIA_ROOT, "Movies")
BUCKET_MUSIC = os.path.join(MEDIA_ROOT, "Music")
BUCKET_OTHER = os.path.join(MEDIA_ROOT, "Other")

FAVS_PATH = os.path.join(MEDIA_ROOT, ".ia_favorites.json")
LOG_PATH = os.path.join(MEDIA_ROOT, ".ia_dl.log")
SESSION_PATH = os.path.expanduser("~/.ia_minotaur_session.json")
PENDING_PATH = os.path.expanduser("~/.ia_minotaur_pending.json")


def staging_file_path(identifier: str, filename: str) -> str:
    return os.path.join(STAGING_ROOT, identifier, filename)


def staging_identifier_dir(identifier: str) -> str:
    return os.path.join(STAGING_ROOT, identifier)


def safe_staging_file_path(identifier: str, filename: str) -> Tuple[Optional[str], str]:
    item_dir = staging_identifier_dir(identifier)
    if not safe_path_under(STAGING_ROOT, item_dir):
        return None, f"Refused: staging item dir escapes staging root: {item_dir}"
    path = staging_file_path(identifier, filename)
    if not safe_path_under(item_dir, path):
        return None, f"Refused: staging path escapes item staging dir: {path}"
    return path, ""


def check_writable_dir(path: str) -> Tuple[bool, str]:
    try:
        os.makedirs(path, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".ia_check_", dir=path):
            pass
        return True, path
    except Exception as e:
        return False, f"{path}: {e}"


def set_process_umask() -> int:
    """Set the downloader process umask for shared media writes."""
    return os.umask(0o002)


def _normalized_mode(mode: int, *, is_dir: bool) -> int:
    mode |= stat.S_IWGRP
    if is_dir:
        mode |= stat.S_ISGID
    return mode


def _normalize_one(path: str) -> None:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        return
    if stat.S_ISDIR(st.st_mode):
        os.chmod(path, _normalized_mode(stat.S_IMODE(st.st_mode), is_dir=True))
    elif stat.S_ISREG(st.st_mode):
        os.chmod(path, _normalized_mode(stat.S_IMODE(st.st_mode), is_dir=False))


def _normalize_existing_parents(path: str, root: str) -> None:
    current = path
    if not os.path.isdir(current) or os.path.islink(current):
        current = os.path.dirname(current)
    ancestors = []
    while current and safe_path_under(root, current):
        if os.path.lexists(current):
            ancestors.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for ancestor in reversed(ancestors):
        _normalize_one(ancestor)


def normalize_media_permissions(
    path: str,
    *,
    media_root: Optional[str] = None,
    recursive: bool = False,
    include_parents: bool = False,
) -> None:
    """Normalize finalized media permissions without changing owner/group."""
    root = media_root or MEDIA_ROOT
    if not safe_path_under(root, path):
        raise ValueError(f"Refused: permission normalization path escapes media root: {path}")
    if include_parents:
        _normalize_existing_parents(path, root)
    if not os.path.lexists(path):
        return

    _normalize_one(path)
    if not recursive or not os.path.isdir(path) or os.path.islink(path):
        return

    for base, dirs, files in os.walk(path, topdown=True, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
        for name in dirs:
            _normalize_one(os.path.join(base, name))
        for name in files:
            child = os.path.join(base, name)
            if not os.path.islink(child):
                _normalize_one(child)
