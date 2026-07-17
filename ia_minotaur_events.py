"""Best-effort Minotaur Core event emission for archive_downloader."""
from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional


DEFAULT_WRAPPER = "/mnt/ssd/home-pi/projects/minotaur_core/scripts/minotaur-event"
WRAPPER_ENV = "MINOTAUR_EVENT_WRAPPER"
SOURCE = "archive_downloader"


def safe_text(value: object, max_chars: int = 240) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    if len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def emit_minotaur_event(
    event_type: str,
    title: str,
    message: str = "",
    *,
    severity: str = "info",
    tags: str = "archive,download",
    wrapper_path: Optional[str] = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Emit an event without making Minotaur Core a dependency.

    Returns True only when the wrapper ran successfully. Any failure is swallowed
    so download flows keep their existing behavior.
    """
    try:
        wrapper_path = wrapper_path or os.environ.get(WRAPPER_ENV, DEFAULT_WRAPPER)
        if not os.path.exists(wrapper_path) or not os.access(wrapper_path, os.X_OK):
            return False
        runner(
            [
                wrapper_path,
                SOURCE,
                event_type,
                safe_text(title, 120),
                safe_text(message),
                severity,
                tags,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return True
    except Exception:
        return False


def emit_archive_started(message: str) -> bool:
    return emit_minotaur_event("archive.started", "Archive started", message)


def emit_archive_completed(message: str) -> bool:
    return emit_minotaur_event("archive.completed", "Archive complete", message)


def emit_archive_failed(message: str) -> bool:
    return emit_minotaur_event(
        "archive.failed",
        "Archive failed",
        message,
        severity="error",
        tags="archive,download,error",
    )
