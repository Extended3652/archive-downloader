"""DVD ISO staging and scan helpers."""
import json
import os
import re
import subprocess
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List, Optional, Tuple

from ia_common import is_dvd_iso_file


@dataclass
class DvdTitle:
    title: int
    seconds: int = 0
    chapters: int = 0


@dataclass
class DvdScanResult:
    iso_path: str
    logs_dir: str
    lsdvd_log: str
    handbrake_log: str
    analysis_path: str
    layout: str
    reason: str
    titles: List[DvdTitle]
    ok: bool
    errors: List[str]
    dry_run: bool = False


def dvd_logs_dir_for_iso(iso_path: str) -> str:
    parent = os.path.dirname(os.path.abspath(iso_path))
    stem = os.path.splitext(os.path.basename(iso_path))[0] or "dvd"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "dvd"
    return os.path.join(parent, "_dvd_logs", safe_stem)


def parse_hms_to_seconds(value: str) -> int:
    m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", value or "")
    if not m:
        return 0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def parse_lsdvd_titles(text: str) -> List[DvdTitle]:
    titles: List[DvdTitle] = []
    for line in (text or "").splitlines():
        m = re.search(r"Title:\s*(\d+),\s*Length:\s*([0-9:]+).*?Chapters:\s*(\d+)", line, re.IGNORECASE)
        if not m:
            continue
        titles.append(DvdTitle(title=int(m.group(1)), seconds=parse_hms_to_seconds(m.group(2)), chapters=int(m.group(3))))
    return titles


def parse_handbrake_titles(text: str) -> List[DvdTitle]:
    titles: List[DvdTitle] = []
    current: Optional[DvdTitle] = None
    for line in (text or "").splitlines():
        title_match = re.search(r"\+\s*title\s+(\d+):", line, re.IGNORECASE)
        if title_match:
            current = DvdTitle(title=int(title_match.group(1)))
            titles.append(current)
            continue
        if current is None:
            continue
        duration_match = re.search(r"\+\s*duration:\s*([0-9:]+)", line, re.IGNORECASE)
        if duration_match:
            current.seconds = parse_hms_to_seconds(duration_match.group(1))
            continue
        chapters_match = re.search(r"\+\s*chapters:\s*(\d+)", line, re.IGNORECASE)
        if chapters_match:
            current.chapters = int(chapters_match.group(1))
    return titles


def classify_dvd_layout(titles: List[DvdTitle]) -> Tuple[str, str]:
    episode_like = [t for t in titles if 15 * 60 <= t.seconds <= 45 * 60]
    long_titles = [t for t in titles if t.seconds >= 60 * 60]

    if len(episode_like) >= 2:
        nums = ", ".join(str(t.title) for t in episode_like[:12])
        return "one_title_per_episode", f"{len(episode_like)} episode-length title(s): {nums}"
    if len(long_titles) == 1 and long_titles[0].chapters >= 2:
        return "one_long_title_split_by_chapters", (
            f"title {long_titles[0].title} is {long_titles[0].seconds // 60} min "
            f"with {long_titles[0].chapters} chapter(s)"
        )
    if long_titles:
        nums = ", ".join(str(t.title) for t in long_titles[:12])
        return "multiple_long_titles_manual_review", f"{len(long_titles)} long title(s): {nums}"
    return "manual_review", "No clear episode-title or long-title chapter pattern detected"


def _run_scan_command(
    cmd: List[str],
    output_path: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout: int = 900,
) -> Tuple[bool, str]:
    try:
        proc = runner(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        output = proc.stdout or ""
        ok = proc.returncode == 0
        if not ok:
            output += f"\n[exit {proc.returncode}]\n"
    except FileNotFoundError as e:
        output = f"{cmd[0]} not found: {e}\n"
        ok = False
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or "") + f"\n{cmd[0]} timed out after {timeout}s\n"
        ok = False
    except Exception as e:
        output = f"{cmd[0]} failed: {e}\n"
        ok = False

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(output)
    return ok, output


def scan_dvd_iso(
    iso_path: str,
    *,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> DvdScanResult:
    if not is_dvd_iso_file(iso_path):
        raise ValueError(f"Not a DVD ISO path: {iso_path}")

    logs_dir = dvd_logs_dir_for_iso(iso_path)
    lsdvd_log = os.path.join(logs_dir, "lsdvd.txt")
    handbrake_log = os.path.join(logs_dir, "handbrake-scan.txt")
    analysis_path = os.path.join(logs_dir, "analysis.json")
    errors: List[str] = []

    if dry_run:
        return DvdScanResult(
            iso_path=iso_path,
            logs_dir=logs_dir,
            lsdvd_log=lsdvd_log,
            handbrake_log=handbrake_log,
            analysis_path=analysis_path,
            layout="dry_run",
            reason="Would run lsdvd and HandBrakeCLI scan",
            titles=[],
            ok=True,
            errors=[],
            dry_run=True,
        )

    os.makedirs(logs_dir, exist_ok=True)
    lsdvd_ok, lsdvd_out = _run_scan_command(["lsdvd", iso_path], lsdvd_log, runner=runner)
    hb_ok, hb_out = _run_scan_command(["HandBrakeCLI", "-i", iso_path, "-t", "0", "--scan"], handbrake_log, runner=runner)

    if not lsdvd_ok:
        errors.append("lsdvd scan failed")
    if not hb_ok:
        errors.append("HandBrakeCLI scan failed")

    titles = parse_lsdvd_titles(lsdvd_out)
    if not titles:
        titles = parse_handbrake_titles(hb_out)
    layout, reason = classify_dvd_layout(titles)

    result = DvdScanResult(
        iso_path=iso_path,
        logs_dir=logs_dir,
        lsdvd_log=lsdvd_log,
        handbrake_log=handbrake_log,
        analysis_path=analysis_path,
        layout=layout,
        reason=reason,
        titles=titles,
        ok=not errors,
        errors=errors,
    )
    payload: Dict[str, object] = asdict(result)
    payload["titles"] = [asdict(t) for t in titles]
    with open(analysis_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return result
