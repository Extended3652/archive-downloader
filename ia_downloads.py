"""Download command, staging-size, and progress helpers."""
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from ia_common import human_size
from ia_paths import (
    LOG_PATH,
    STAGING_ROOT,
    safe_staging_file_path,
    staging_identifier_dir,
)


@dataclass
class DownloadProgress:
    target: str
    written: int
    total: int
    speed_bps: float
    eta_s: float


def download_base_args(no_change_timestamp: bool) -> List[str]:
    return ["--no-change-timestamp"] if no_change_timestamp else []


def single_download_cmd(identifier: str, filename: str, no_change_timestamp: bool) -> List[str]:
    return [
        "ia",
        "download",
        identifier,
        filename,
        "--destdir",
        STAGING_ROOT,
    ] + download_base_args(no_change_timestamp)


def glob_download_cmd(identifier: str, glob_pat: str, no_change_timestamp: bool) -> List[str]:
    return [
        "ia",
        "download",
        identifier,
        "--destdir",
        STAGING_ROOT,
        "--glob",
        glob_pat,
    ] + download_base_args(no_change_timestamp)


def open_process_log():
    """Open the shared log for subprocess output, or discard output if unavailable."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        return open(LOG_PATH, "a", encoding="utf-8")
    except Exception:
        return open(os.devnull, "w", encoding="utf-8")


def safe_getsize(path: str) -> int:
    try:
        return int(os.path.getsize(path))
    except Exception:
        return 0


def dir_total_size(root: str, getsize: Callable[[str], int] = safe_getsize) -> int:
    total = 0
    try:
        for base, _dirs, files in os.walk(root):
            for fn in files:
                p = os.path.join(base, fn)
                total += getsize(p)
    except Exception:
        return 0
    return total


def run_download_with_progress(
    cmd: List[str],
    *,
    target: str,
    expected_total: int,
    read_written: Callable[[], int],
    log_fh,
    stall_timeout_s: int,
    is_cancel_requested: Callable[[], bool],
    on_progress: Optional[Callable[[DownloadProgress], None]] = None,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_s: float = 0.1,
    progress_interval_s: float = 0.5,
    log_path: str = LOG_PATH,
) -> Tuple[bool, str]:
    """Run a download subprocess and report file/dir-size based progress.

    This helper deliberately knows nothing about curses. Callers provide a
    byte counter, cancel predicate, and optional progress callback.
    """
    try:
        proc = popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, text=True)
    except Exception as e:
        return False, f"download failed: {e}"

    start_t = now()
    last_t = start_t
    last_bytes = 0
    last_progress_t = start_t
    last_progress_bytes = 0
    speed_bps = 0.0
    eta_s = 0.0
    total = int(expected_total or 0)
    canceled = False

    while True:
        rc = proc.poll()
        written = int(read_written() or 0)

        current_t = now()
        if written > last_progress_bytes:
            last_progress_t = current_t
            last_progress_bytes = written
        elif rc is None and current_t - last_progress_t > stall_timeout_s:
            try:
                proc.kill()
            except Exception:
                pass
            return False, f"Download stalled — no progress for {stall_timeout_s}s. Try again."

        dt = current_t - last_t
        if dt >= progress_interval_s:
            delta = max(0, written - last_bytes)
            speed_bps = float(delta) / float(dt) if dt > 0 else 0.0
            if total > 0 and speed_bps > 0:
                remain = max(0, total - written)
                eta_s = float(remain) / float(speed_bps)
            else:
                eta_s = 0.0
            last_t = current_t
            last_bytes = written

        if is_cancel_requested():
            canceled = True
            try:
                proc.kill()
            except Exception:
                pass

        if on_progress:
            on_progress(DownloadProgress(target, written, total, speed_bps, eta_s))

        if rc is not None:
            proc.wait(timeout=2)
            if canceled:
                return False, "Canceled."
            if rc != 0:
                return False, f"download failed (code {rc}); see log: {log_path}"
            return True, ""

        sleep(poll_interval_s)


def verify_expected_size(identifier: str, filename: str, expected_size: int) -> Tuple[bool, str]:
    if expected_size <= 0:
        return True, ""
    p, err = safe_staging_file_path(identifier, filename)
    if err or not p:
        return False, err
    actual = safe_getsize(p)
    if actual != int(expected_size):
        return False, f"Size mismatch for {filename}: got {human_size(actual)} expected {human_size(int(expected_size))}"
    return True, ""


def staging_dir_for_identifier(identifier: str) -> str:
    return staging_identifier_dir(identifier)
