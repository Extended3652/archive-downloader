"""Optional Radarr registration for completed movie imports."""
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib import error, parse, request

import ia_config
from ia_organize import split_title_year


API_KEY_ENVS = ("IA_RADARR_API_KEY", "RADARR_API_KEY")
RADARR_MOVIE_ROOT_DEFAULT = "/mnt/ssd/media/Movies"


@dataclass
class RadarrSettings:
    enabled: bool
    url: str
    api_key: str
    local_movie_root: str
    root_folder: str
    quality_profile_id: int
    monitor_movie: bool
    search_on_add: bool
    timeout_s: float


@dataclass
class RadarrResult:
    ok: bool
    status: str
    message: str
    changed: bool = False
    movie_id: int = 0
    radarr_path: str = ""


class RadarrError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0):
        self.status = status
        super().__init__(message)


def load_settings(
    cfg: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> RadarrSettings:
    data = ia_config.load_config(environ=environ) if cfg is None else dict(cfg)
    env = os.environ if environ is None else environ
    api_key = str(data.get("radarr_api_key") or "").strip()
    for key in API_KEY_ENVS:
        if env.get(key):
            api_key = str(env[key]).strip()
            break
    local_root = str(data.get("radarr_local_movie_root") or "").strip()
    if not local_root:
        local_root = os.path.join(str(data.get("media_root") or ia_config.DEFAULT_MEDIA_ROOT), "Movies")
    return RadarrSettings(
        enabled=ia_config.parse_bool(data.get("radarr_enabled"), False),
        url=str(data.get("radarr_url") or "http://192.168.86.70:7878").strip(),
        api_key=api_key,
        local_movie_root=os.path.expanduser(local_root),
        root_folder=os.path.expanduser(str(data.get("radarr_root_folder") or "").strip()),
        quality_profile_id=int(data.get("radarr_quality_profile_id") or 0),
        monitor_movie=ia_config.parse_bool(data.get("radarr_monitor_movie"), True),
        search_on_add=False,
        timeout_s=float(data.get("radarr_timeout_s") or 10),
    )


def normalize_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def normalize_title(value: str) -> str:
    text = str(value or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def extract_year(*values: str) -> str:
    for value in values:
        _title, year = split_title_year(str(value or ""))
        if year:
            return year
        match = re.search(r"\b(19\d{2}|20\d{2})\b", str(value or ""))
        if match:
            return match.group(1)
    return ""


def extract_tmdb_id(meta: Optional[Mapping[str, Any]] = None, explicit: Any = None) -> int:
    if explicit:
        try:
            return int(str(explicit).strip())
        except (TypeError, ValueError):
            pass
    values: List[Any] = []
    if isinstance(meta, Mapping):
        values.extend(meta.get(k) for k in ("tmdbId", "tmdb_id", "themoviedb_id"))
        inner = meta.get("metadata")
        if isinstance(inner, Mapping):
            values.extend(inner.get(k) for k in ("tmdbId", "tmdb_id", "themoviedb_id", "external-identifier"))
        values.append(meta.get("external-identifier"))
    for raw in _flatten(values):
        text = str(raw or "").strip()
        if not text:
            continue
        match = re.search(r"(?:tmdb|themoviedb)(?:[:/_ -]|movie[:/_ -])*(\d+)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        if text.isdigit() and len(text) <= 8:
            return int(text)
    return 0


def _flatten(values: Iterable[Any]) -> Iterable[Any]:
    for value in values:
        if isinstance(value, (list, tuple, set)):
            for inner in _flatten(value):
                yield inner
        else:
            yield value


def movie_title_year_from_path(path: str, fallback_title: str = "", fallback_year: str = "") -> Tuple[str, str]:
    folder = os.path.basename(os.path.abspath(path))
    title, year = split_title_year(folder)
    if not year:
        ft, fy = split_title_year(fallback_title)
        title = title if title and title != folder else ft or fallback_title
        year = fallback_year or fy
    return (title or fallback_title or folder).strip(), str(year or fallback_year or "").strip()


def strong_movie_match(candidate: Mapping[str, Any], title: str, year: str) -> bool:
    expected_title = normalize_title(title)
    expected_year = str(year or "").strip()
    if not expected_title or not expected_year:
        return False
    candidate_titles = [
        str(candidate.get("title") or ""),
        str(candidate.get("originalTitle") or ""),
        str(candidate.get("sortTitle") or ""),
    ]
    candidate_year = str(candidate.get("year") or "")
    return candidate_year == expected_year and any(normalize_title(t) == expected_title for t in candidate_titles)


def map_local_to_radarr_path(local_path: str, settings: RadarrSettings) -> Tuple[str, str]:
    local_root = os.path.realpath(settings.local_movie_root)
    radarr_root = settings.root_folder.rstrip("/\\")
    if not local_root or not radarr_root:
        return "", "Radarr path mapping missing local movie root or Radarr root folder."
    candidate = os.path.realpath(local_path)
    try:
        rel = os.path.relpath(candidate, local_root)
    except ValueError:
        return "", f"Radarr path mapping failed: {local_path} is not under {settings.local_movie_root}."
    if rel == ".." or rel.startswith(".." + os.sep):
        return "", f"Radarr path mapping failed: {local_path} is not under {settings.local_movie_root}."
    if rel == ".":
        return radarr_root, ""
    return radarr_root.rstrip("/\\") + "/" + rel.replace(os.sep, "/"), ""


class RadarrClient:
    def __init__(
        self,
        settings: RadarrSettings,
        *,
        opener: Callable[..., object] = request.urlopen,
    ):
        self.settings = settings
        self.base_url = normalize_url(settings.url)
        self.opener = opener

    def request_json(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None) -> Any:
        if not self.base_url:
            raise RadarrError("Radarr URL is not configured.")
        if not self.settings.api_key:
            raise RadarrError("Radarr API key is not configured.")
        url = self.base_url + "/api/v3/" + path.lstrip("/")
        body = None
        headers = {"X-Api-Key": self.settings.api_key}
        if payload is not None:
            body = json.dumps(dict(payload)).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=body, method=method.upper(), headers=headers)
        try:
            with self.opener(req, timeout=self.settings.timeout_s) as resp:
                raw = resp.read().decode("utf-8") if hasattr(resp, "read") else ""
                status = int(getattr(resp, "status", 0) or 0)
        except error.HTTPError as exc:
            status = int(exc.code or 0)
            if status in (401, 403):
                raise RadarrError("Radarr authentication failed.", status=status) from exc
            raise RadarrError(f"Radarr request failed: HTTP {status}.", status=status) from exc
        except Exception as exc:
            raise RadarrError(f"Radarr unavailable: {exc}.") from exc
        if not 200 <= status < 300:
            raise RadarrError(f"Radarr request failed: HTTP {status}.", status=status)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RadarrError("Radarr returned invalid JSON.") from exc

    def root_folders(self) -> List[Dict[str, Any]]:
        return list(self.request_json("GET", "rootfolder") or [])

    def quality_profiles(self) -> List[Dict[str, Any]]:
        return list(self.request_json("GET", "qualityprofile") or [])

    def movies(self) -> List[Dict[str, Any]]:
        return list(self.request_json("GET", "movie") or [])

    def lookup_tmdb(self, tmdb_id: int) -> List[Dict[str, Any]]:
        return list(self.request_json("GET", f"movie/lookup/tmdb?tmdbId={int(tmdb_id)}") or [])

    def lookup(self, title: str, year: str = "") -> List[Dict[str, Any]]:
        term = title if not year else f"{title} {year}"
        return list(self.request_json("GET", "movie/lookup?term=" + parse.quote(term)) or [])

    def add_movie(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return dict(self.request_json("POST", "movie", payload) or {})

    def update_movie(self, movie_id: int, payload: Mapping[str, Any]) -> Dict[str, Any]:
        return dict(self.request_json("PUT", f"movie/{int(movie_id)}?moveFiles=false", payload) or {})

    def refresh_movie(self, movie_id: int) -> Dict[str, Any]:
        return dict(self.request_json("POST", "command", {"name": "RefreshMovie", "movieId": int(movie_id)}) or {})


def redact_message(message: str, settings: RadarrSettings) -> str:
    text = str(message or "")
    if settings.api_key:
        text = text.replace(settings.api_key, "[redacted]")
    return text


def validate_settings(settings: RadarrSettings) -> Optional[RadarrResult]:
    if not settings.enabled:
        return RadarrResult(True, "disabled", "Radarr registration disabled.")
    if not settings.api_key:
        return RadarrResult(False, "missing_config", "Radarr registration skipped: API key is not configured.")
    if not settings.root_folder:
        return RadarrResult(False, "missing_config", "Radarr registration skipped: radarr_root_folder is not configured.")
    if int(settings.quality_profile_id or 0) <= 0:
        return RadarrResult(
            False,
            "missing_config",
            "Radarr registration skipped: radarr_quality_profile_id is not configured.",
        )
    return None


def validate_remote_choices(client: Any, settings: RadarrSettings) -> Optional[RadarrResult]:
    roots = client.root_folders()
    root_paths = {str(root.get("path") or "").rstrip("/\\") for root in roots}
    if settings.root_folder.rstrip("/\\") not in root_paths:
        return RadarrResult(
            False,
            "missing_config",
            f"Radarr registration skipped: configured root folder is not available in Radarr: {settings.root_folder}.",
        )
    profiles = client.quality_profiles()
    profile_ids = {int(profile.get("id") or 0) for profile in profiles}
    if int(settings.quality_profile_id) not in profile_ids:
        return RadarrResult(
            False,
            "missing_config",
            f"Radarr registration skipped: quality profile id is not available in Radarr: {settings.quality_profile_id}.",
        )
    return None


def select_lookup_result(results: Iterable[Mapping[str, Any]], title: str, year: str) -> Tuple[Optional[Dict[str, Any]], str]:
    matches = [dict(item) for item in results if strong_movie_match(item, title, year)]
    unique: Dict[int, Dict[str, Any]] = {}
    for item in matches:
        tmdb_id = int(item.get("tmdbId") or 0)
        if tmdb_id:
            unique[tmdb_id] = item
    if len(unique) == 1:
        return next(iter(unique.values())), ""
    if len(unique) > 1:
        return None, f"Radarr lookup ambiguous for {title} ({year}); {len(unique)} strong matches."
    return None, f"Radarr lookup found no strong title/year match for {title} ({year})."


def find_existing_movie(
    movies: Iterable[Mapping[str, Any]],
    *,
    tmdb_id: int = 0,
    title: str = "",
    year: str = "",
) -> Optional[Dict[str, Any]]:
    for movie in movies:
        if tmdb_id and int(movie.get("tmdbId") or 0) == int(tmdb_id):
            return dict(movie)
    for movie in movies:
        if strong_movie_match(movie, title, year):
            return dict(movie)
    return None


def build_add_payload(
    lookup_movie: Mapping[str, Any],
    settings: RadarrSettings,
    radarr_path: str,
) -> Dict[str, Any]:
    payload = dict(lookup_movie)
    payload["qualityProfileId"] = int(settings.quality_profile_id)
    payload["rootFolderPath"] = settings.root_folder
    payload["path"] = radarr_path
    payload["monitored"] = bool(settings.monitor_movie)
    payload["addOptions"] = {"searchForMovie": False}
    return payload


def register_completed_movie(
    local_movie_file: str,
    *,
    item_title: str = "",
    item_year: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
    tmdb_id: int = 0,
    settings: Optional[RadarrSettings] = None,
    client: Optional[Any] = None,
    dry_run: bool = False,
    logger: Optional[Callable[[str], None]] = None,
) -> RadarrResult:
    settings = settings or load_settings()

    def log(message: str) -> None:
        safe = redact_message(message, settings)
        if logger:
            logger(safe)

    validation = validate_settings(settings)
    if validation:
        log(validation.message)
        return validation

    if not os.path.exists(local_movie_file):
        result = RadarrResult(False, "not_found", f"Radarr registration skipped: final file missing: {local_movie_file}")
        log(result.message)
        return result

    local_movie_dir = os.path.dirname(os.path.abspath(local_movie_file))
    radarr_path, map_err = map_local_to_radarr_path(local_movie_dir, settings)
    if map_err:
        result = RadarrResult(False, "path_mapping_failed", map_err)
        log(result.message)
        return result

    title, year = movie_title_year_from_path(local_movie_dir, item_title, item_year)
    tmdb = extract_tmdb_id(metadata, tmdb_id)
    client = client or RadarrClient(settings)

    try:
        remote_validation = validate_remote_choices(client, settings)
        if remote_validation:
            log(remote_validation.message)
            return remote_validation
        existing_movies = client.movies()
        existing = find_existing_movie(existing_movies, tmdb_id=tmdb, title=title, year=year)

        if existing:
            movie_id = int(existing.get("id") or 0)
            existing_path = str(existing.get("path") or "").rstrip("/\\")
            if existing_path == radarr_path.rstrip("/\\"):
                if not dry_run:
                    client.refresh_movie(movie_id)
                message = (
                    "Radarr movie already registered; no changes made."
                    if dry_run
                    else "Radarr movie already registered; refresh requested."
                )
                result = RadarrResult(True, "already_registered", message, False, movie_id, radarr_path)
                log(result.message)
                return result
            if dry_run:
                result = RadarrResult(True, "would_update_path", f"Radarr movie path would update: {existing_path} -> {radarr_path}.", False, movie_id, radarr_path)
                log(result.message)
                return result
            payload = dict(existing)
            payload["path"] = radarr_path
            payload["rootFolderPath"] = settings.root_folder
            payload["monitored"] = bool(settings.monitor_movie)
            payload["qualityProfileId"] = int(settings.quality_profile_id)
            updated = client.update_movie(movie_id, payload)
            updated_id = int(updated.get("id") or movie_id)
            client.refresh_movie(updated_id)
            result = RadarrResult(True, "path_updated", "Radarr movie path updated; refresh requested.", True, updated_id, radarr_path)
            log(result.message)
            return result

        lookup_results = client.lookup_tmdb(tmdb) if tmdb else client.lookup(title, year)
        lookup_movie, lookup_err = select_lookup_result(lookup_results, title, year)
        if lookup_err:
            result = RadarrResult(False, "lookup_failed", lookup_err)
            log(result.message)
            return result
        if dry_run:
            result = RadarrResult(True, "would_add", f"Radarr movie would be added: {title} ({year}) at {radarr_path}.", False, 0, radarr_path)
            log(result.message)
            return result
        added = client.add_movie(build_add_payload(lookup_movie or {}, settings, radarr_path))
        movie_id = int(added.get("id") or 0)
        if movie_id:
            client.refresh_movie(movie_id)
        result = RadarrResult(True, "added", "Radarr movie added; refresh requested.", True, movie_id, radarr_path)
        log(result.message)
        return result
    except RadarrError as exc:
        result = RadarrResult(False, "radarr_failed", redact_message(str(exc), settings), False, 0, radarr_path)
        log(result.message)
        return result
