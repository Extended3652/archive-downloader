"""Path configuration and filesystem guards for the IA downloader tools."""
import os
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
    path = staging_file_path(identifier, filename)
    if not safe_path_under(staging_identifier_dir(identifier), path):
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
