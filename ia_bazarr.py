"""Best-effort Bazarr subtitle handoff for completed imports."""
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple
from urllib import error, parse, request

import ia_config


API_KEY_ENVS = ("IA_BAZARR_API_KEY", "BAZARR_API_KEY")
SUBTITLE_EXTENSIONS = (".srt", ".sub", ".smi", ".txt", ".ssa", ".ass", ".mpl", ".vtt")
ENGLISH_CODES = ("en", "eng", "english")


@dataclass
class BazarrSettings:
    enabled: bool
    url: str
    api_key: str
    timeout_s: float
    wait_timeout_s: float
    poll_interval_s: float


@dataclass
class BazarrResult:
    ok: bool
    status: str
    message: str
    subtitle_path: str = ""


class BazarrError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0):
        self.status = status
        super().__init__(message)


def load_settings(
    cfg: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> BazarrSettings:
    data = ia_config.load_config(environ=environ) if cfg is None else dict(cfg)
    env = os.environ if environ is None else environ
    api_key = str(data.get("bazarr_api_key") or "").strip()
    for key in API_KEY_ENVS:
        if env.get(key):
            api_key = str(env[key]).strip()
            break
    return BazarrSettings(
        enabled=ia_config.parse_bool(data.get("bazarr_enabled"), False),
        url=str(data.get("bazarr_url") or "http://localhost:6767").strip(),
        api_key=api_key,
        timeout_s=float(data.get("bazarr_timeout_s") or 10),
        wait_timeout_s=float(data.get("bazarr_wait_timeout_s") or 120),
        poll_interval_s=float(data.get("bazarr_poll_interval_s") or 3),
    )


def normalize_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def redact_message(message: str, settings: BazarrSettings) -> str:
    text = str(message or "")
    if settings.api_key:
        text = text.replace(settings.api_key, "[redacted]")
    return text


def _looks_english_subtitle(media_path: str, candidate: str) -> bool:
    media_base = os.path.splitext(os.path.basename(media_path))[0].casefold()
    name = os.path.basename(candidate).casefold()
    stem, ext = os.path.splitext(name)
    if ext not in SUBTITLE_EXTENSIONS:
        return False
    if stem == media_base:
        return False
    if not stem.startswith(media_base + "."):
        return False
    tags = [part for part in stem[len(media_base) + 1 :].split(".") if part]
    if "forced" in tags:
        return False
    return any(tag in ENGLISH_CODES for tag in tags)


def find_english_subtitles(media_path: str) -> Tuple[str, ...]:
    directory = os.path.dirname(os.path.abspath(media_path))
    try:
        names = os.listdir(directory)
    except OSError:
        return ()
    paths = [
        os.path.join(directory, name)
        for name in names
        if _looks_english_subtitle(media_path, os.path.join(directory, name))
    ]
    return tuple(sorted(paths))


class BazarrClient:
    def __init__(
        self,
        settings: BazarrSettings,
        *,
        opener: Callable[..., object] = request.urlopen,
    ):
        self.settings = settings
        self.base_url = normalize_url(settings.url)
        self.opener = opener

    def request(
        self,
        method: str,
        path: str,
        fields: Optional[Mapping[str, Any]] = None,
        *,
        expect_json: bool = False,
    ) -> Any:
        if not self.base_url:
            raise BazarrError("Bazarr URL is not configured.")
        if not self.settings.api_key:
            raise BazarrError("Bazarr API key is not configured.")
        url = self.base_url + "/api/" + path.lstrip("/")
        body = None
        headers = {"X-API-KEY": self.settings.api_key}
        if fields is not None:
            body = parse.urlencode(dict(fields), doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = request.Request(url, data=body, method=method.upper(), headers=headers)
        try:
            with self.opener(req, timeout=self.settings.timeout_s) as resp:
                raw = resp.read().decode("utf-8") if hasattr(resp, "read") else ""
                status = int(getattr(resp, "status", 0) or 0)
        except error.HTTPError as exc:
            status = int(exc.code or 0)
            if status in (401, 403):
                raise BazarrError("Bazarr authentication failed.", status=status) from exc
            raise BazarrError(f"Bazarr request failed: HTTP {status}.", status=status) from exc
        except Exception as exc:
            raise BazarrError(f"Bazarr unavailable: {exc}.") from exc
        if not 200 <= status < 300:
            raise BazarrError(f"Bazarr request failed: HTTP {status}.", status=status)
        if not expect_json:
            return {}
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BazarrError("Bazarr returned invalid JSON.") from exc

    def movie_action(self, radarr_id: int, action: str) -> None:
        self.request("PATCH", "movies", {"radarrid": int(radarr_id), "action": action})

    def series_action(self, series_id: int, action: str) -> None:
        self.request("PATCH", "series", {"seriesid": int(series_id), "action": action})

    def movie(self, radarr_id: int) -> Dict[str, Any]:
        data = self.request("GET", f"movies?radarrid%5B%5D={int(radarr_id)}", expect_json=True)
        rows = data.get("data") if isinstance(data, dict) else None
        if isinstance(rows, list) and rows:
            return dict(rows[0])
        return {}


def _has_missing_english(movie: Mapping[str, Any]) -> bool:
    missing = movie.get("missing_subtitles")
    if isinstance(missing, Mapping):
        values: Iterable[Any] = list(missing.keys()) + list(missing.values())
    elif isinstance(missing, list):
        values = missing
    else:
        values = ()
    for value in values:
        text = str(value or "").casefold()
        if any(code in text for code in ENGLISH_CODES):
            return True
    return False


def wait_for_new_english_subtitle(
    media_path: str,
    previous: Iterable[str],
    *,
    timeout_s: float,
    poll_interval_s: float,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Tuple[str, bool]:
    previous_set = {os.path.abspath(path) for path in previous}
    deadline = monotonic() + max(0.0, timeout_s)
    stable_seen: Dict[str, Tuple[int, int, int]] = {}
    while monotonic() <= deadline:
        current = [path for path in find_english_subtitles(media_path) if os.path.abspath(path) not in previous_set]
        for path in current:
            try:
                st = os.stat(path)
            except OSError:
                continue
            signature = (int(st.st_size), int(st.st_mtime_ns), int(st.st_ino))
            if stable_seen.get(path) == signature:
                return path, True
            stable_seen[path] = signature
        sleeper(max(0.1, poll_interval_s))
    current = [path for path in find_english_subtitles(media_path) if os.path.abspath(path) not in previous_set]
    return (current[0], False) if current else ("", False)


def handoff_movie(
    media_path: str,
    radarr_id: int,
    *,
    settings: Optional[BazarrSettings] = None,
    client: Optional[BazarrClient] = None,
    logger: Optional[Callable[[str], None]] = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> BazarrResult:
    settings = settings or load_settings()

    def log(message: str) -> None:
        safe = redact_message(message, settings)
        if logger:
            logger(safe)

    if not settings.enabled:
        result = BazarrResult(True, "disabled", "Bazarr subtitle automation disabled.")
        log(result.message)
        return result
    if not settings.api_key:
        result = BazarrResult(False, "missing_config", "Bazarr subtitle automation skipped: API key is not configured.")
        log(result.message)
        return result
    if not radarr_id:
        result = BazarrResult(False, "missing_identity", "Bazarr subtitle automation skipped: Radarr movie id is unavailable.")
        log(result.message)
        return result
    if not os.path.exists(media_path):
        result = BazarrResult(False, "not_found", f"Bazarr subtitle automation skipped: final file missing: {media_path}")
        log(result.message)
        return result

    existing = find_english_subtitles(media_path)
    client = client or BazarrClient(settings)
    try:
        log(f"Bazarr notified for movie {radarr_id}.")
        client.movie_action(radarr_id, "sync")
        client.movie_action(radarr_id, "scan-disk")
        if existing:
            result = BazarrResult(
                True,
                "existing_subtitle",
                "Bazarr notified; existing English subtitle found, provider search skipped.",
                existing[0],
            )
            log(result.message)
            return result

        movie = client.movie(radarr_id)
        if movie and not _has_missing_english(movie):
            result = BazarrResult(True, "no_missing", "Bazarr notified; no missing English subtitle is reported.")
            log(result.message)
            return result

        client.movie_action(radarr_id, "search-missing")
        subtitle_path, stable = wait_for_new_english_subtitle(
            media_path,
            existing,
            timeout_s=settings.wait_timeout_s,
            poll_interval_s=settings.poll_interval_s,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        if subtitle_path:
            message = (
                "Bazarr subtitle found/downloaded; synchronization completed or delegated."
                if stable
                else "Bazarr subtitle found/downloaded; synchronization may still be finishing."
            )
            result = BazarrResult(True, "downloaded", message, subtitle_path)
            log(result.message)
            return result
        result = BazarrResult(True, "not_found", "Bazarr search completed/requested; no qualifying English subtitle found yet.")
        log(result.message)
        return result
    except BazarrError as exc:
        result = BazarrResult(False, "bazarr_failed", redact_message(str(exc), settings))
        log(result.message)
        return result


def handoff_series(
    media_path: str,
    series_id: int,
    *,
    settings: Optional[BazarrSettings] = None,
    client: Optional[BazarrClient] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> BazarrResult:
    settings = settings or load_settings()

    def log(message: str) -> None:
        safe = redact_message(message, settings)
        if logger:
            logger(safe)

    if not settings.enabled:
        result = BazarrResult(True, "disabled", "Bazarr subtitle automation disabled.")
        log(result.message)
        return result
    if not settings.api_key:
        result = BazarrResult(False, "missing_config", "Bazarr subtitle automation skipped: API key is not configured.")
        log(result.message)
        return result
    if not series_id:
        result = BazarrResult(False, "missing_identity", "Bazarr subtitle automation skipped: Sonarr series id is unavailable.")
        log(result.message)
        return result
    if find_english_subtitles(media_path):
        result = BazarrResult(True, "existing_subtitle", "Bazarr skipped; existing English subtitle found.")
        log(result.message)
        return result
    client = client or BazarrClient(settings)
    try:
        log(f"Bazarr notified for series {series_id}.")
        client.series_action(series_id, "sync")
        client.series_action(series_id, "scan-disk")
        client.series_action(series_id, "search-missing")
        result = BazarrResult(True, "delegated", "Bazarr series subtitle search delegated.")
        log(result.message)
        return result
    except BazarrError as exc:
        result = BazarrResult(False, "bazarr_failed", redact_message(str(exc), settings))
        log(result.message)
        return result
