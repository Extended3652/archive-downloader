"""Read-only user configuration for archive-downloader."""
import json
import os
from typing import Any, Mapping, Optional, Tuple


DEFAULT_MEDIA_ROOT = "/mnt/ssd/media"
DEFAULT_YT_DLP_PATH = "/home/pi/.local/bin/yt-dlp"
DEFAULT_CONFIG = {
    "media_root": DEFAULT_MEDIA_ROOT,
    "yt_dlp_path": DEFAULT_YT_DLP_PATH,
    "default_bucket": "Movies",
    "default_filter": "movies",
    "default_sort": "",
    "title_only": False,
    "license_gate": False,
    "no_change_timestamp": True,
    "rows_per_page": 30,
}
VALID_BUCKETS = {"TV", "Movies", "Music", "Other"}
VALID_FILTERS = {"movies", "audio", "texts", "software", "any"}
VALID_SORTS = {"", "date desc", "date asc", "titleSorter asc", "downloads desc"}


def config_path(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    override = env.get("IA_CONFIG_PATH")
    if override:
        return os.path.expanduser(override)
    config_home = env.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(os.path.expanduser(config_home), "archive-downloader", "config.json")


def config_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    return os.path.dirname(config_path(environ))


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def normalize_bucket(value: Any, default: str = "Movies") -> str:
    candidate = str(value or "").strip().title()
    return candidate if candidate in VALID_BUCKETS else default


def normalize_filter(value: Any, default: str = "movies") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in VALID_FILTERS else default


def normalize_sort(value: Any, default: str = "") -> str:
    candidate = str(value or "").strip()
    if candidate.lower() == "relevance":
        return ""
    return candidate if candidate in VALID_SORTS else default


def positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def load_config(
    path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    env = os.environ if environ is None else environ
    data = {}
    cfg_path = path if path is not None else config_path(env)

    try:
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}

    cfg = dict(DEFAULT_CONFIG)
    if isinstance(data.get("media_root"), str) and data["media_root"].strip():
        cfg["media_root"] = os.path.expanduser(data["media_root"].strip())
    if isinstance(data.get("yt_dlp_path"), str) and data["yt_dlp_path"].strip():
        cfg["yt_dlp_path"] = os.path.expanduser(data["yt_dlp_path"].strip())
    cfg["default_bucket"] = normalize_bucket(data.get("default_bucket"), cfg["default_bucket"])
    cfg["default_filter"] = normalize_filter(data.get("default_filter"), cfg["default_filter"])
    cfg["default_sort"] = normalize_sort(data.get("default_sort"), cfg["default_sort"])
    cfg["title_only"] = parse_bool(data.get("title_only"), cfg["title_only"])
    cfg["license_gate"] = parse_bool(data.get("license_gate"), cfg["license_gate"])
    cfg["no_change_timestamp"] = parse_bool(data.get("no_change_timestamp"), cfg["no_change_timestamp"])
    cfg["rows_per_page"] = positive_int(data.get("rows_per_page"), cfg["rows_per_page"])

    if env.get("IA_MEDIA_ROOT"):
        cfg["media_root"] = os.path.expanduser(str(env["IA_MEDIA_ROOT"]))
    if env.get("IA_DEFAULT_BUCKET"):
        cfg["default_bucket"] = normalize_bucket(env["IA_DEFAULT_BUCKET"], cfg["default_bucket"])
    if env.get("IA_DEFAULT_FILTER"):
        cfg["default_filter"] = normalize_filter(env["IA_DEFAULT_FILTER"], cfg["default_filter"])
    if env.get("IA_DEFAULT_SORT"):
        cfg["default_sort"] = normalize_sort(env["IA_DEFAULT_SORT"], cfg["default_sort"])
    if env.get("IA_TITLE_ONLY"):
        cfg["title_only"] = parse_bool(env["IA_TITLE_ONLY"], cfg["title_only"])
    if env.get("IA_LICENSE_GATE"):
        cfg["license_gate"] = parse_bool(env["IA_LICENSE_GATE"], cfg["license_gate"])
    if env.get("IA_NO_CHANGE_TIMESTAMP"):
        cfg["no_change_timestamp"] = parse_bool(env["IA_NO_CHANGE_TIMESTAMP"], cfg["no_change_timestamp"])
    if env.get("IA_ROWS_PER_PAGE"):
        cfg["rows_per_page"] = positive_int(env["IA_ROWS_PER_PAGE"], cfg["rows_per_page"])

    return cfg


def load_raw_config(path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> dict:
    cfg_path = path if path is not None else config_path(environ)
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def save_config(data: Mapping[str, Any], path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> None:
    cfg_path = path if path is not None else config_path(environ)
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(data), fh, indent=2)
        fh.write("\n")
    os.replace(tmp, cfg_path)


def normalize_config_value(key: str, value: str) -> Tuple[Any, str]:
    if key == "media_root":
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("media_root must not be empty")
        return os.path.expanduser(cleaned), "media_root"
    if key == "default_bucket":
        bucket = normalize_bucket(value, "")
        if not bucket:
            raise ValueError("default_bucket must be one of TV, Movies, Music, Other")
        return bucket, "default_bucket"
    if key == "default_filter":
        filter_value = normalize_filter(value, "")
        if not filter_value:
            raise ValueError("default_filter must be one of movies, audio, texts, software, any")
        return filter_value, "default_filter"
    if key == "default_sort":
        sort_value = normalize_sort(value, "__invalid__")
        if sort_value == "__invalid__":
            raise ValueError("default_sort must be one of: relevance, date desc, date asc, titleSorter asc, downloads desc")
        return sort_value, "default_sort"
    if key == "title_only":
        return parse_bool(value, False), "title_only"
    if key == "license_gate":
        return parse_bool(value, False), "license_gate"
    if key == "no_change_timestamp":
        return parse_bool(value, True), "no_change_timestamp"
    if key == "rows_per_page":
        rows = positive_int(value, 0)
        if rows < 1:
            raise ValueError("rows_per_page must be >= 1")
        return rows, "rows_per_page"
    raise ValueError(f"unknown config key: {key}")


def set_config_value(
    key: str,
    value: str,
    path: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    normalized, normalized_key = normalize_config_value(key, value)
    data = load_raw_config(path, environ)
    data[normalized_key] = normalized
    save_config(data, path, environ)
    return load_config(path, environ)
