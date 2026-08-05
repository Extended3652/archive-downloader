"""Best-effort Jellyfin library refresh helpers."""
import os
from typing import Callable, Mapping, Optional, Tuple
from urllib import error, request
from urllib.parse import urljoin


URL_ENV = "JELLYFIN_URL"
TOKEN_ENVS = ("JELLYFIN_API_KEY", "JELLYFIN_TOKEN", "JELLYFIN_API_TOKEN")


def jellyfin_configured(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return bool((env.get(URL_ENV) or "").strip() and jellyfin_token(env))


def jellyfin_token(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    for key in TOKEN_ENVS:
        token = (env.get(key) or "").strip()
        if token:
            return token
    return ""


def request_library_rescan(
    *,
    jellyfin_url: Optional[str] = None,
    token: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    opener: Callable[..., object] = request.urlopen,
    timeout: float = 5.0,
) -> Tuple[bool, str]:
    """Ask Jellyfin to refresh all libraries.

    This is best effort. Callers should report failures but not let them break
    completed archive downloads.
    """
    env = os.environ if environ is None else environ
    base_url = (jellyfin_url or env.get(URL_ENV) or "").strip()
    api_token = (token or jellyfin_token(env)).strip()
    if not base_url or not api_token:
        return False, f"Jellyfin rescan skipped; set {URL_ENV} and {TOKEN_ENVS[0]}."

    endpoint = urljoin(base_url.rstrip("/") + "/", "Library/Refresh")
    req = request.Request(
        endpoint,
        method="POST",
        headers={
            "X-Emby-Token": api_token,
            "Content-Length": "0",
        },
    )
    try:
        with opener(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 0) or 0)
        if 200 <= status < 300:
            return True, "Jellyfin library rescan requested."
        return False, f"Jellyfin rescan failed: HTTP {status}."
    except error.HTTPError as exc:
        return False, f"Jellyfin rescan failed: HTTP {exc.code}."
    except Exception as exc:
        return False, f"Jellyfin rescan failed: {exc}."
