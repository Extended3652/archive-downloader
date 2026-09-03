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
    "radarr_enabled": False,
    "radarr_url": "http://192.168.86.70:7878",
    "radarr_api_key": "",
    "radarr_local_movie_root": os.path.join(DEFAULT_MEDIA_ROOT, "Movies"),
    "radarr_root_folder": "",
    "radarr_quality_profile_id": 0,
    "radarr_monitor_movie": True,
    "radarr_search_on_add": False,
    "radarr_timeout_s": 10,
    "bazarr_enabled": False,
    "bazarr_url": "http://localhost:6767",
    "bazarr_api_key": "",
    "bazarr_timeout_s": 10,
    "bazarr_wait_timeout_s": 120,
    "bazarr_poll_interval_s": 3,
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


def non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def clean_string(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    return default


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
    cfg["radarr_enabled"] = parse_bool(data.get("radarr_enabled"), cfg["radarr_enabled"])
    cfg["radarr_url"] = clean_string(data.get("radarr_url"), cfg["radarr_url"]) or cfg["radarr_url"]
    cfg["radarr_api_key"] = clean_string(data.get("radarr_api_key"), cfg["radarr_api_key"])
    cfg["radarr_local_movie_root"] = os.path.expanduser(
        clean_string(data.get("radarr_local_movie_root"), cfg["radarr_local_movie_root"])
    )
    cfg["radarr_root_folder"] = os.path.expanduser(clean_string(data.get("radarr_root_folder"), cfg["radarr_root_folder"]))
    cfg["radarr_quality_profile_id"] = non_negative_int(
        data.get("radarr_quality_profile_id"),
        cfg["radarr_quality_profile_id"],
    )
    cfg["radarr_monitor_movie"] = parse_bool(data.get("radarr_monitor_movie"), cfg["radarr_monitor_movie"])
    cfg["radarr_search_on_add"] = False
    cfg["radarr_timeout_s"] = positive_float(data.get("radarr_timeout_s"), cfg["radarr_timeout_s"])
    cfg["bazarr_enabled"] = parse_bool(data.get("bazarr_enabled"), cfg["bazarr_enabled"])
    cfg["bazarr_url"] = clean_string(data.get("bazarr_url"), cfg["bazarr_url"]) or cfg["bazarr_url"]
    cfg["bazarr_api_key"] = clean_string(data.get("bazarr_api_key"), cfg["bazarr_api_key"])
    cfg["bazarr_timeout_s"] = positive_float(data.get("bazarr_timeout_s"), cfg["bazarr_timeout_s"])
    cfg["bazarr_wait_timeout_s"] = positive_float(data.get("bazarr_wait_timeout_s"), cfg["bazarr_wait_timeout_s"])
    cfg["bazarr_poll_interval_s"] = positive_float(data.get("bazarr_poll_interval_s"), cfg["bazarr_poll_interval_s"])

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
    if env.get("IA_RADARR_ENABLED"):
        cfg["radarr_enabled"] = parse_bool(env["IA_RADARR_ENABLED"], cfg["radarr_enabled"])
    if env.get("IA_RADARR_URL"):
        cfg["radarr_url"] = clean_string(env["IA_RADARR_URL"], cfg["radarr_url"]) or cfg["radarr_url"]
    if env.get("IA_RADARR_API_KEY"):
        cfg["radarr_api_key"] = clean_string(env["IA_RADARR_API_KEY"], cfg["radarr_api_key"])
    elif env.get("RADARR_API_KEY"):
        cfg["radarr_api_key"] = clean_string(env["RADARR_API_KEY"], cfg["radarr_api_key"])
    if env.get("IA_RADARR_LOCAL_MOVIE_ROOT"):
        cfg["radarr_local_movie_root"] = os.path.expanduser(clean_string(env["IA_RADARR_LOCAL_MOVIE_ROOT"]))
    if env.get("IA_RADARR_ROOT_FOLDER"):
        cfg["radarr_root_folder"] = os.path.expanduser(clean_string(env["IA_RADARR_ROOT_FOLDER"]))
    if env.get("IA_RADARR_QUALITY_PROFILE_ID"):
        cfg["radarr_quality_profile_id"] = non_negative_int(
            env["IA_RADARR_QUALITY_PROFILE_ID"],
            cfg["radarr_quality_profile_id"],
        )
    if env.get("IA_RADARR_MONITOR_MOVIE"):
        cfg["radarr_monitor_movie"] = parse_bool(env["IA_RADARR_MONITOR_MOVIE"], cfg["radarr_monitor_movie"])
    if env.get("IA_RADARR_TIMEOUT_S"):
        cfg["radarr_timeout_s"] = positive_float(env["IA_RADARR_TIMEOUT_S"], cfg["radarr_timeout_s"])
    if env.get("IA_BAZARR_ENABLED"):
        cfg["bazarr_enabled"] = parse_bool(env["IA_BAZARR_ENABLED"], cfg["bazarr_enabled"])
    if env.get("IA_BAZARR_URL"):
        cfg["bazarr_url"] = clean_string(env["IA_BAZARR_URL"], cfg["bazarr_url"]) or cfg["bazarr_url"]
    if env.get("IA_BAZARR_API_KEY"):
        cfg["bazarr_api_key"] = clean_string(env["IA_BAZARR_API_KEY"], cfg["bazarr_api_key"])
    elif env.get("BAZARR_API_KEY"):
        cfg["bazarr_api_key"] = clean_string(env["BAZARR_API_KEY"], cfg["bazarr_api_key"])
    if env.get("IA_BAZARR_TIMEOUT_S"):
        cfg["bazarr_timeout_s"] = positive_float(env["IA_BAZARR_TIMEOUT_S"], cfg["bazarr_timeout_s"])
    if env.get("IA_BAZARR_WAIT_TIMEOUT_S"):
        cfg["bazarr_wait_timeout_s"] = positive_float(env["IA_BAZARR_WAIT_TIMEOUT_S"], cfg["bazarr_wait_timeout_s"])
    if env.get("IA_BAZARR_POLL_INTERVAL_S"):
        cfg["bazarr_poll_interval_s"] = positive_float(env["IA_BAZARR_POLL_INTERVAL_S"], cfg["bazarr_poll_interval_s"])

    if not cfg["radarr_local_movie_root"]:
        cfg["radarr_local_movie_root"] = os.path.join(cfg["media_root"], "Movies")

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
    if key == "radarr_enabled":
        return parse_bool(value, False), "radarr_enabled"
    if key == "radarr_url":
        cleaned = clean_string(value)
        if not cleaned:
            raise ValueError("radarr_url must not be empty")
        return cleaned, "radarr_url"
    if key == "radarr_api_key":
        return clean_string(value), "radarr_api_key"
    if key == "radarr_local_movie_root":
        cleaned = clean_string(value)
        if not cleaned:
            raise ValueError("radarr_local_movie_root must not be empty")
        return os.path.expanduser(cleaned), "radarr_local_movie_root"
    if key == "radarr_root_folder":
        cleaned = clean_string(value)
        if not cleaned:
            raise ValueError("radarr_root_folder must not be empty")
        return os.path.expanduser(cleaned), "radarr_root_folder"
    if key == "radarr_quality_profile_id":
        parsed = positive_int(value, 0)
        if parsed < 1:
            raise ValueError("radarr_quality_profile_id must be >= 1")
        return parsed, "radarr_quality_profile_id"
    if key == "radarr_monitor_movie":
        return parse_bool(value, True), "radarr_monitor_movie"
    if key == "radarr_search_on_add":
        return False, "radarr_search_on_add"
    if key == "radarr_timeout_s":
        parsed = positive_float(value, 0)
        if parsed <= 0:
            raise ValueError("radarr_timeout_s must be > 0")
        return parsed, "radarr_timeout_s"
    if key == "bazarr_enabled":
        return parse_bool(value, False), "bazarr_enabled"
    if key == "bazarr_url":
        cleaned = clean_string(value)
        if not cleaned:
            raise ValueError("bazarr_url must not be empty")
        return cleaned, "bazarr_url"
    if key == "bazarr_api_key":
        return clean_string(value), "bazarr_api_key"
    if key == "bazarr_timeout_s":
        parsed = positive_float(value, 0)
        if parsed <= 0:
            raise ValueError("bazarr_timeout_s must be > 0")
        return parsed, "bazarr_timeout_s"
    if key == "bazarr_wait_timeout_s":
        parsed = positive_float(value, 0)
        if parsed <= 0:
            raise ValueError("bazarr_wait_timeout_s must be > 0")
        return parsed, "bazarr_wait_timeout_s"
    if key == "bazarr_poll_interval_s":
        parsed = positive_float(value, 0)
        if parsed <= 0:
            raise ValueError("bazarr_poll_interval_s must be > 0")
        return parsed, "bazarr_poll_interval_s"
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
