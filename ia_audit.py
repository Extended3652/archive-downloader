#!/usr/bin/env python3
"""Library audit CLI for media downloaded via archive-downloader."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import ia_config
from ia_common import human_size
from ia_organize import auto_clean_movie_folder_name, detect_sxxeyy, sanitize_folder
from ia_paths import STAGING_ROOT, normalize_media_permissions, set_process_umask


MEDIA_EXTS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".m4v",
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
}
VIDEO_CONTAINERS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}
AUDIO_CONTAINERS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
TEMP_EXTS = {".part", ".tmp", ".crdownload", ".aria2", ".torrent", ".nfo"}
KNOWN_BUCKETS = {"TV", "Movies", "Music", "Other", "Podcasts"}
STAGING_BUCKETS = {".ia_staging"}
RISKY_VIDEO_CODECS = {"mpeg2video", "mpeg4", "vp8", "wmv3", "rv40", "theora"}
RISKY_AUDIO_CODECS = {"dca", "truehd", "pcm_s16le", "pcm_s24le", "wmapro"}


@dataclass
class MediaEntry:
    path: str
    rel_path: str
    bucket: str
    size: int
    ext: str
    show: str = ""
    season: int = 0
    episode: int = 0
    movie_title: str = ""
    movie_year: str = ""


@dataclass
class ProbeInfo:
    container: str = ""
    duration_s: float = 0.0
    bit_rate: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0


@dataclass
class TriageSuggestion:
    path: str
    suggested_bucket: str
    suggested_folder: str
    suggested_name: str
    confidence: str
    reason: str


def is_known_bucket(bucket: str) -> bool:
    return bucket in KNOWN_BUCKETS or bucket in STAGING_BUCKETS


def split_top_bucket(rel_path: str) -> str:
    rel = rel_path.strip(os.sep)
    if not rel:
        return "root"
    return rel.split(os.sep, 1)[0]


def looks_weird_filename(name: str) -> List[str]:
    reasons: List[str] = []
    base = os.path.basename(name or "")
    stem, _ext = os.path.splitext(base)
    if not stem:
        return ["empty stem"]
    if re.search(r"[{}\[\]]", stem):
        reasons.append("brackets in stem")
    if re.search(r"(?:^|[._-])(sample|trailer|rarbg|yify|etrg)(?:[._-]|$)", stem, re.IGNORECASE):
        reasons.append("release/sample tag")
    if stem.count(".") >= 4 or stem.count("_") >= 4:
        reasons.append("dense separators")
    if re.search(r"[A-Za-z]{18,}\d{0,2}$", stem) and " " not in stem and "." not in stem and "_" not in stem:
        reasons.append("long unbroken token")
    if re.fullmatch(r"(video|movie|track|file)[ _.-]*\d{1,3}", stem, re.IGNORECASE):
        reasons.append("generic stem")
    if re.search(r"\b(1080p|720p|2160p|x264|x265|h264|h265|bluray|webrip)\b", stem, re.IGNORECASE):
        reasons.append("scene tags remain")
    return reasons


def cleaned_movie_basename(name: str) -> str:
    ext = os.path.splitext(name)[1]
    cleaned = auto_clean_movie_folder_name("", name)
    if not cleaned:
        return name
    return f"{cleaned}{ext}"


def should_prefer_folder_title(entry: MediaEntry, name: str, cleaned: str) -> bool:
    if not entry.movie_title or not entry.movie_year:
        return False
    weird = looks_weird_filename(name)
    if weird:
        return True
    stem = os.path.splitext(name)[0]
    if re.fullmatch(r"\d[\d._-]*", stem):
        return True
    if re.match(r"^[a-z0-9]{2,6}[-._]", stem):
        return True
    if " (" not in cleaned:
        return True
    return False


def rename_suggestion(entry: MediaEntry) -> Optional[str]:
    name = os.path.basename(entry.path)
    if entry.bucket == "Movies":
        cleaned = cleaned_movie_basename(name)
        if should_prefer_folder_title(entry, name, cleaned):
            preferred = f"{entry.movie_title}{entry.ext or os.path.splitext(name)[1]}"
            if preferred != name:
                return preferred
        if cleaned != name:
            return cleaned
    elif entry.bucket == "TV" and entry.show and entry.season and entry.episode:
        ext = entry.ext or os.path.splitext(name)[1]
        target = f"{entry.show} - S{entry.season:02d}E{entry.episode:02d}{ext}"
        if target != name:
            return target
    return None


def build_rename_plan(root: str, rename_suggestions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []
    seen_targets = set()
    for item in rename_suggestions:
        rel_path = str(item.get("path") or "")
        suggested_name = str(item.get("suggested_name") or "")
        if not rel_path or not suggested_name:
            continue
        src_abs = os.path.join(root, rel_path)
        dst_abs = os.path.join(os.path.dirname(src_abs), suggested_name)
        dst_rel = os.path.relpath(dst_abs, root)
        collides = os.path.exists(dst_abs) and os.path.realpath(dst_abs) != os.path.realpath(src_abs)
        duplicate_target = dst_rel in seen_targets
        seen_targets.add(dst_rel)
        plan.append(
            {
                "from": rel_path,
                "to": dst_rel,
                "from_abs": src_abs,
                "to_abs": dst_abs,
                "collides": collides,
                "duplicate_target": duplicate_target,
            }
        )
    return plan


def load_rename_plan(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("rename plan must be a JSON array")
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("rename plan entries must be JSON objects")
        out.append(item)
    return out


def validate_rename_plan(root: str, plan: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    validated: List[Dict[str, Any]] = []
    seen_targets = set()
    root_abs = os.path.realpath(root)
    for item in plan:
        src_rel = str(item.get("from") or "")
        dst_rel = str(item.get("to") or "")
        src_abs = os.path.realpath(os.path.join(root, src_rel))
        dst_abs = os.path.realpath(os.path.join(root, dst_rel))
        status = "ready"
        reasons: List[str] = []

        if not src_rel or not dst_rel:
            status = "blocked"
            reasons.append("missing from/to path")
        if os.path.commonpath([root_abs, src_abs]) != root_abs:
            status = "blocked"
            reasons.append("source escapes root")
        if os.path.commonpath([root_abs, dst_abs]) != root_abs:
            status = "blocked"
            reasons.append("target escapes root")
        if src_abs == dst_abs:
            status = "blocked"
            reasons.append("source and target are identical")
        if not os.path.exists(src_abs):
            status = "blocked"
            reasons.append("source missing")
        if os.path.exists(dst_abs) and src_abs != dst_abs:
            status = "blocked"
            reasons.append("target already exists")
        if dst_abs in seen_targets:
            status = "blocked"
            reasons.append("duplicate target in plan")
        seen_targets.add(dst_abs)

        validated.append(
            {
                "from": src_rel,
                "to": dst_rel,
                "from_abs": src_abs,
                "to_abs": dst_abs,
                "status": status,
                "reasons": reasons,
            }
        )
    return validated


def apply_rename_plan(root: str, plan: Sequence[Dict[str, Any]], *, execute: bool = False) -> Dict[str, Any]:
    validated = validate_rename_plan(root, plan)
    results: List[Dict[str, Any]] = []
    renamed = 0
    blocked = 0
    for item in validated:
        if item["status"] != "ready":
            blocked += 1
            results.append(dict(item))
            continue

        result = dict(item)
        if not execute:
            result["status"] = "dry-run"
            results.append(result)
            continue

        os.makedirs(os.path.dirname(item["to_abs"]), exist_ok=True)
        os.replace(item["from_abs"], item["to_abs"])
        normalize_media_permissions(item["to_abs"], media_root=root, include_parents=True)
        result["status"] = "renamed"
        results.append(result)
        renamed += 1

    return {
        "root": root,
        "execute": execute,
        "renamed": renamed,
        "blocked": blocked,
        "results": results,
    }


def load_plan(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("plan must be a JSON array")
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("plan entries must be JSON objects")
        out.append(item)
    return out


def extract_movie_year(text: str) -> str:
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text or "")
    return match.group(1) if match else ""


def canonical_movie_title_from_folder(folder: str) -> str:
    folder = sanitize_folder(folder)
    if extract_movie_year(folder):
        return folder
    return auto_clean_movie_folder_name(folder or "", "")


def build_plan(root: str, items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []
    seen_targets = set()
    for item in items:
        src_rel = str(item.get("from") or item.get("path") or "")
        dst_rel = str(item.get("to") or "")
        if not src_rel or not dst_rel:
            continue
        src_abs = os.path.join(root, src_rel)
        dst_abs = os.path.join(root, dst_rel)
        collides = os.path.exists(dst_abs) and os.path.realpath(dst_abs) != os.path.realpath(src_abs)
        duplicate_target = dst_rel in seen_targets
        seen_targets.add(dst_rel)
        plan.append(
            {
                "from": src_rel,
                "to": dst_rel,
                "from_abs": src_abs,
                "to_abs": dst_abs,
                "collides": collides,
                "duplicate_target": duplicate_target,
            }
        )
    return plan


def parse_media_entry(root: str, path: str) -> MediaEntry:
    rel_path = os.path.relpath(path, root)
    bucket = split_top_bucket(rel_path)
    ext = os.path.splitext(path)[1].lower()
    size = 0
    try:
        size = int(os.path.getsize(path))
    except OSError:
        size = 0
    entry = MediaEntry(path=path, rel_path=rel_path, bucket=bucket, size=size, ext=ext)

    parts = rel_path.split(os.sep)
    filename = os.path.basename(path)
    episode = detect_sxxeyy(filename)
    if bucket == "TV":
        if len(parts) >= 2:
            entry.show = sanitize_folder(parts[1])
        if len(parts) >= 3:
            season_match = re.search(r"(\d{1,2})", parts[2])
            if season_match:
                entry.season = int(season_match.group(1))
        if episode:
            entry.season, entry.episode = episode
    elif bucket == "Movies":
        folder = parts[1] if len(parts) >= 2 else ""
        cleaned = canonical_movie_title_from_folder(folder) if folder else auto_clean_movie_folder_name("", filename)
        entry.movie_title = cleaned
        entry.movie_year = extract_movie_year(cleaned)

    return entry


def existing_subfolder_after_bucket(entry: MediaEntry) -> str:
    parts = entry.rel_path.split(os.sep)
    if entry.bucket == "Other" and len(parts) >= 4 and parts[1].lower() in {"video", "misc", "riffs"}:
        return sanitize_folder(parts[2])
    if len(parts) >= 3:
        return sanitize_folder(parts[1])
    return "Misc"


def triage_suggestion_for_entry(entry: MediaEntry) -> Optional[TriageSuggestion]:
    name = os.path.basename(entry.path)
    parent_folder = existing_subfolder_after_bucket(entry)

    if entry.bucket.startswith(".") and entry.bucket not in STAGING_BUCKETS:
        return TriageSuggestion(
            path=entry.rel_path,
            suggested_bucket="Other",
            suggested_folder=sanitize_folder(f"Recovered {entry.bucket.lstrip('.') or 'Hidden'}"),
            suggested_name=name,
            confidence="medium",
            reason="hidden top-level bucket",
        )

    if entry.bucket != "Other":
        return None

    if entry.ext in AUDIO_CONTAINERS:
        return TriageSuggestion(
            path=entry.rel_path,
            suggested_bucket="Podcasts" if "podcast" in entry.rel_path.lower() else "Music",
            suggested_folder=parent_folder,
            suggested_name=name,
            confidence="low",
            reason="audio file under Other",
        )

    episode = detect_sxxeyy(name)
    if episode:
        show = parent_folder if parent_folder != "Misc" else sanitize_folder(os.path.splitext(name)[0])
        return TriageSuggestion(
            path=entry.rel_path,
            suggested_bucket="TV",
            suggested_folder=os.path.join(show, f"Season {episode[0]:02d}"),
            suggested_name=f"{show} - S{episode[0]:02d}E{episode[1]:02d}{entry.ext}",
            confidence="medium",
            reason="episode pattern found",
        )

    movie_folder = canonical_movie_title_from_folder(parent_folder)
    movie_year = extract_movie_year(movie_folder or name)
    if entry.ext in VIDEO_CONTAINERS and movie_year:
        clean_name = f"{movie_folder}{entry.ext}" if movie_folder else cleaned_movie_basename(name)
        return TriageSuggestion(
            path=entry.rel_path,
            suggested_bucket="Movies",
            suggested_folder=movie_folder or "Misc",
            suggested_name=clean_name,
            confidence="high",
            reason="video file with movie-style year/title",
        )

    return TriageSuggestion(
        path=entry.rel_path,
        suggested_bucket="Other",
        suggested_folder=parent_folder,
        suggested_name=name,
        confidence="low",
        reason="manual classification needed",
    )


def build_triage_suggestions(entries: Sequence[MediaEntry]) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []
    for entry in entries:
        suggestion = triage_suggestion_for_entry(entry)
        if suggestion:
            suggestions.append(
                {
                    "path": suggestion.path,
                    "suggested_bucket": suggestion.suggested_bucket,
                    "suggested_folder": suggestion.suggested_folder,
                    "suggested_name": suggestion.suggested_name,
                    "confidence": suggestion.confidence,
                    "reason": suggestion.reason,
                }
            )
    return suggestions


def build_triage_move_plan(root: str, suggestions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    movable: List[Dict[str, Any]] = []
    for item in suggestions:
        if str(item.get("confidence") or "") not in {"high", "medium"}:
            continue
        bucket = sanitize_folder(str(item.get("suggested_bucket") or "Other"))
        folder = sanitize_folder(str(item.get("suggested_folder") or "Misc"))
        name = str(item.get("suggested_name") or "")
        if not name:
            continue
        dst_rel = os.path.join(bucket, folder, name)
        movable.append({"from": str(item.get("path") or ""), "to": dst_rel})
    return build_plan(root, movable)


def unresolved_triage_suggestions(suggestions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in suggestions if str(item.get("confidence") or "") == "low"]


def bucket_choice_map() -> Dict[str, str]:
    return {
        "m": "Movies",
        "t": "TV",
        "u": "Music",
        "p": "Podcasts",
        "o": "Other",
        "k": "KEEP",
        "s": "SKIP",
    }


def prompt_manual_triage_plan(
    root: str,
    suggestions: Sequence[Dict[str, Any]],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> List[Dict[str, Any]]:
    candidates = unresolved_triage_suggestions(suggestions)
    plan_items: List[Dict[str, Any]] = []
    total = len(candidates)
    if total == 0:
        output_fn("No low-confidence triage suggestions.")
        return []

    output_fn(f"Manual triage candidates: {total}")
    for idx, item in enumerate(candidates, start=1):
        path = str(item.get("path") or "")
        default_bucket = str(item.get("suggested_bucket") or "Other")
        default_folder = str(item.get("suggested_folder") or "Misc")
        default_name = str(item.get("suggested_name") or os.path.basename(path))
        reason = str(item.get("reason") or "")

        output_fn("")
        output_fn(f"[{idx}/{total}] {path}")
        output_fn(f"Reason: {reason}")
        output_fn(f"Suggested: {default_bucket}/{default_folder}/{default_name}")
        choice = input_fn("Action [m movie / t tv / u music / p podcasts / o other / k keep-suggested / s skip] (default s): ").strip().lower() or "s"
        bucket = bucket_choice_map().get(choice)
        if bucket == "SKIP" or bucket is None:
            continue
        if bucket == "KEEP":
            bucket = default_bucket
        folder = input_fn(f"Folder [{default_folder}]: ").strip() or default_folder
        name = input_fn(f"Filename [{default_name}]: ").strip() or default_name
        dst_rel = os.path.join(sanitize_folder(bucket), folder.strip() or "Misc", name.strip() or default_name)
        plan_items.append({"from": path, "to": dst_rel})

    return build_plan(root, plan_items)


def validate_move_plan(root: str, plan: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return validate_rename_plan(root, plan)


def apply_move_plan(root: str, plan: Sequence[Dict[str, Any]], *, execute: bool = False) -> Dict[str, Any]:
    validated = validate_move_plan(root, plan)
    results: List[Dict[str, Any]] = []
    moved = 0
    blocked = 0
    for item in validated:
        if item["status"] != "ready":
            blocked += 1
            results.append(dict(item))
            continue
        result = dict(item)
        if not execute:
            result["status"] = "dry-run"
            results.append(result)
            continue
        os.makedirs(os.path.dirname(item["to_abs"]), exist_ok=True)
        os.replace(item["from_abs"], item["to_abs"])
        normalize_media_permissions(item["to_abs"], media_root=root, include_parents=True)
        result["status"] = "moved"
        results.append(result)
        moved += 1
    return {"root": root, "execute": execute, "moved": moved, "blocked": blocked, "results": results}


def iter_library_files(root: str) -> Iterable[Tuple[str, str]]:
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            path = os.path.join(base, name)
            ext = os.path.splitext(name)[1].lower()
            yield path, ext


def ffprobe_path(path: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> ProbeInfo:
    try:
        proc = runner(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,bit_rate,format_name:stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ProbeInfo()

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ProbeInfo()

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    info = ProbeInfo(
        container=str(fmt.get("format_name", "") or ""),
        duration_s=float(fmt.get("duration") or 0.0),
        bit_rate=int(fmt.get("bit_rate") or 0),
    )
    for stream in streams:
        codec_type = str(stream.get("codec_type", "") or "")
        codec_name = str(stream.get("codec_name", "") or "")
        if codec_type == "video" and not info.video_codec:
            info.video_codec = codec_name
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
        elif codec_type == "audio" and not info.audio_codec:
            info.audio_codec = codec_name
    return info


def bitrate_bucket(bit_rate: int) -> str:
    mbps = float(bit_rate or 0) / 1_000_000.0
    if mbps <= 0:
        return "unknown"
    if mbps < 1:
        return "<1 Mbps"
    if mbps < 2:
        return "1-2 Mbps"
    if mbps < 4:
        return "2-4 Mbps"
    if mbps < 8:
        return "4-8 Mbps"
    if mbps < 16:
        return "8-16 Mbps"
    return "16+ Mbps"


def detect_metadata_issues(entry: MediaEntry) -> List[str]:
    issues: List[str] = []
    name = os.path.basename(entry.path)
    if entry.bucket == "TV":
        if not entry.show:
            issues.append("missing show folder")
        if not entry.season:
            issues.append("missing season folder")
        if not entry.episode:
            issues.append("missing SxxEyy pattern")
    elif entry.bucket == "Movies":
        if not entry.movie_year:
            issues.append("missing movie year")
        clean_name = auto_clean_movie_folder_name("", name)
        if " (" not in clean_name:
            issues.append("filename lacks year/title cleanup")
    elif entry.bucket == "Other":
        issues.append("unclassified media in Other")
    elif entry.bucket.startswith(".") and entry.bucket not in STAGING_BUCKETS:
        issues.append("hidden top-level bucket")
    elif not is_known_bucket(entry.bucket):
        issues.append(f"nonstandard top-level bucket: {entry.bucket}")
    elif entry.bucket == "root":
        issues.append("media file at library root")
    return issues


def build_duplicate_keys(entries: Sequence[MediaEntry]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    episode_map: Dict[str, List[str]] = defaultdict(list)
    movie_map: Dict[str, List[str]] = defaultdict(list)
    for entry in entries:
        if entry.bucket == "TV" and entry.show and entry.season and entry.episode:
            key = f"{entry.show.lower()}|S{entry.season:02d}E{entry.episode:02d}"
            episode_map[key].append(entry.rel_path)
        if entry.bucket == "Movies" and entry.movie_title:
            normalized = re.sub(r"\s+", " ", entry.movie_title).strip().lower()
            movie_map[normalized].append(entry.rel_path)
    return episode_map, movie_map


def find_cleanup_candidates(
    root: str,
    stale_days: int,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    current = time.time() if now is None else now
    cutoff = current - (stale_days * 86400)
    out: List[Dict[str, Any]] = []
    for path, ext in iter_library_files(root):
        if ext not in TEMP_EXTS and os.path.basename(path) not in {"Thumbs.db", ".DS_Store"}:
            continue
        try:
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
        except OSError:
            continue
        if mtime > cutoff:
            continue
        reason = "stale temp sidecar"
        if path.startswith(STAGING_ROOT):
            reason = "stale staging file"
        elif ext == ".torrent":
            reason = "stale torrent"
        out.append(
            {
                "path": os.path.relpath(path, root),
                "size": size,
                "age_days": int((current - mtime) // 86400),
                "reason": reason,
            }
        )
    out.sort(key=lambda item: (-item["age_days"], item["path"]))
    return out


def analyze_library(
    root: str,
    *,
    probe: bool = False,
    max_probe: int = 200,
    stale_days: int = 14,
    probe_runner: Callable[[str], ProbeInfo] = ffprobe_path,
) -> Dict[str, Any]:
    media_entries: List[MediaEntry] = []
    weird_names: List[Dict[str, Any]] = []
    metadata_issues: List[Dict[str, Any]] = []
    rename_suggestions: List[Dict[str, Any]] = []
    codec_counter: Counter[str] = Counter()
    audio_codec_counter: Counter[str] = Counter()
    bitrate_counter: Counter[str] = Counter()
    transcode_risks: List[Dict[str, Any]] = []
    probed = 0

    for path, ext in iter_library_files(root):
        if ext not in MEDIA_EXTS:
            continue
        entry = parse_media_entry(root, path)
        media_entries.append(entry)

        weird_reasons = looks_weird_filename(os.path.basename(path))
        if weird_reasons:
            weird_names.append({"path": entry.rel_path, "reasons": weird_reasons})
            suggestion = rename_suggestion(entry)
            if suggestion:
                rename_suggestions.append({"path": entry.rel_path, "suggested_name": suggestion})

        issues = detect_metadata_issues(entry)
        if issues:
            metadata_issues.append({"path": entry.rel_path, "issues": issues})

        if probe and ext in VIDEO_CONTAINERS.union(AUDIO_CONTAINERS) and probed < max_probe:
            info = probe_runner(path)
            probed += 1
            if info.video_codec:
                codec_counter[info.video_codec] += 1
            if info.audio_codec:
                audio_codec_counter[info.audio_codec] += 1
            if info.bit_rate:
                bitrate_counter[bitrate_bucket(info.bit_rate)] += 1
            risky = False
            risk_reasons: List[str] = []
            if info.video_codec in RISKY_VIDEO_CODECS:
                risky = True
                risk_reasons.append(f"video codec {info.video_codec}")
            if info.audio_codec in RISKY_AUDIO_CODECS:
                risky = True
                risk_reasons.append(f"audio codec {info.audio_codec}")
            if ext == ".avi":
                risky = True
                risk_reasons.append("AVI container")
            if risky:
                transcode_risks.append(
                    {
                        "path": entry.rel_path,
                        "video_codec": info.video_codec,
                        "audio_codec": info.audio_codec,
                        "bit_rate": info.bit_rate,
                        "reasons": risk_reasons,
                    }
                )

    episode_map, movie_map = build_duplicate_keys(media_entries)
    duplicate_episodes = [{"key": key, "paths": paths} for key, paths in episode_map.items() if len(paths) > 1]
    duplicate_movies = [{"key": key, "paths": paths} for key, paths in movie_map.items() if len(paths) > 1]
    cleanup_candidates = find_cleanup_candidates(root, stale_days)
    triage_suggestions = build_triage_suggestions(media_entries)
    triage_move_plan = build_triage_move_plan(root, triage_suggestions)

    bucket_counts = Counter(entry.bucket for entry in media_entries)
    return {
        "root": root,
        "summary": {
            "media_files": len(media_entries),
            "bucket_counts": dict(sorted(bucket_counts.items())),
            "weird_filenames": len(weird_names),
            "duplicate_episodes": len(duplicate_episodes),
            "duplicate_movies": len(duplicate_movies),
            "metadata_issues": len(metadata_issues),
            "cleanup_candidates": len(cleanup_candidates),
            "rename_suggestions": len(rename_suggestions),
            "triage_suggestions": len(triage_suggestions),
            "triage_move_plan": len(triage_move_plan),
            "probed_files": probed,
        },
        "weird_filenames": weird_names,
        "rename_suggestions": rename_suggestions,
        "rename_plan": build_rename_plan(root, rename_suggestions),
        "triage_suggestions": triage_suggestions,
        "triage_move_plan": triage_move_plan,
        "duplicate_episodes": duplicate_episodes,
        "duplicate_movies": duplicate_movies,
        "metadata_issues": metadata_issues,
        "cleanup_candidates": cleanup_candidates,
        "codec_report": {
            "video_codecs": dict(codec_counter.most_common()),
            "audio_codecs": dict(audio_codec_counter.most_common()),
            "transcode_risks": transcode_risks,
        },
        "bitrate_heatmap": dict(bitrate_counter),
    }


def print_text_report(report: Dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"Library root: {report['root']}")
    print(
        "Files: {media_files} | Weird names: {weird_filenames} | Duplicate episodes: {duplicate_episodes} | "
        "Duplicate movies: {duplicate_movies} | Metadata issues: {metadata_issues} | "
        "Rename suggestions: {rename_suggestions} | Triage: {triage_suggestions} | Cleanup: {cleanup_candidates}".format(
            **summary
        )
    )
    if summary["bucket_counts"]:
        bucket_bits = ", ".join(f"{bucket}={count}" for bucket, count in summary["bucket_counts"].items())
        print(f"Buckets: {bucket_bits}")

    if report["weird_filenames"]:
        print("\nWeird filenames:")
        for item in report["weird_filenames"][:10]:
            print(f"- {item['path']} [{', '.join(item['reasons'])}]")

    if report["duplicate_episodes"]:
        print("\nDuplicate episodes:")
        for item in report["duplicate_episodes"][:10]:
            print(f"- {item['key']}: {len(item['paths'])} copies")

    if report["duplicate_movies"]:
        print("\nDuplicate movies:")
        for item in report["duplicate_movies"][:10]:
            print(f"- {item['key']}: {len(item['paths'])} copies")

    if report["rename_suggestions"]:
        print("\nRename suggestions:")
        for item in report["rename_suggestions"][:10]:
            print(f"- {item['path']} -> {item['suggested_name']}")
    if report["rename_plan"]:
        blocked = sum(1 for item in report["rename_plan"] if item["collides"] or item["duplicate_target"])
        print(f"Rename plan entries: {len(report['rename_plan'])} ({blocked} blocked by collisions/duplicate targets)")
    if report["triage_suggestions"]:
        print("\nTriage suggestions:")
        for item in report["triage_suggestions"][:10]:
            print(
                f"- {item['path']} -> {item['suggested_bucket']}/{item['suggested_folder']}/{item['suggested_name']} "
                f"[{item['confidence']}, {item['reason']}]"
            )
    if report["triage_move_plan"]:
        blocked = sum(1 for item in report["triage_move_plan"] if item["collides"] or item["duplicate_target"])
        print(f"Triage move plan entries: {len(report['triage_move_plan'])} ({blocked} blocked by collisions/duplicate targets)")

    if report["metadata_issues"]:
        print("\nMetadata issues:")
        for item in report["metadata_issues"][:10]:
            print(f"- {item['path']} [{', '.join(item['issues'])}]")

    if report["cleanup_candidates"]:
        print("\nCleanup candidates:")
        for item in report["cleanup_candidates"][:10]:
            print(f"- {item['path']} ({human_size(item['size'])}, {item['age_days']}d, {item['reason']})")

    codec_report = report["codec_report"]
    if codec_report["video_codecs"] or codec_report["audio_codecs"]:
        print("\nCodec report:")
        if codec_report["video_codecs"]:
            print("Video codecs: " + ", ".join(f"{k}={v}" for k, v in codec_report["video_codecs"].items()))
        if codec_report["audio_codecs"]:
            print("Audio codecs: " + ", ".join(f"{k}={v}" for k, v in codec_report["audio_codecs"].items()))
    if report["bitrate_heatmap"]:
        print("Bitrates: " + ", ".join(f"{k}={v}" for k, v in report["bitrate_heatmap"].items()))


def main(argv: Optional[List[str]] = None) -> int:
    set_process_umask()
    parser = argparse.ArgumentParser(
        prog="ia-audit",
        description="Audit a local media library for naming, duplicate, metadata, cleanup, and codec issues.",
    )
    parser.add_argument("--root", default=ia_config.load_config()["media_root"], help="Library root to scan.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--probe", action="store_true", help="Probe media files with ffprobe for codec and bitrate reporting.")
    parser.add_argument("--max-probe", type=int, default=200, help="Maximum number of media files to ffprobe.")
    parser.add_argument("--stale-days", type=int, default=14, help="Age threshold for cleanup candidates.")
    parser.add_argument(
        "--rename-plan",
        help="Write rename plan JSON to this path, or '-' for stdout only.",
    )
    parser.add_argument(
        "--triage-plan",
        help="Write triage move plan JSON to this path, or '-' for stdout only.",
    )
    parser.add_argument(
        "--manual-triage-plan",
        help="Interactively review low-confidence triage items and write a move plan JSON to this path.",
    )
    parser.add_argument(
        "--apply-rename-plan",
        help="Read a rename plan JSON file and preview or execute its renames.",
    )
    parser.add_argument(
        "--apply-triage-plan",
        help="Read a triage move plan JSON file and preview or execute its moves.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="With --apply-rename-plan, perform the renames instead of only previewing them.",
    )
    args = parser.parse_args(argv)

    root = os.path.expanduser(args.root)
    if args.rename_plan and args.apply_rename_plan:
        parser.error("--rename-plan and --apply-rename-plan cannot be used together")
    if args.triage_plan and args.apply_triage_plan:
        parser.error("--triage-plan and --apply-triage-plan cannot be used together")
    if args.manual_triage_plan and (args.triage_plan or args.apply_triage_plan or args.rename_plan or args.apply_rename_plan):
        parser.error("--manual-triage-plan cannot be combined with other plan actions")

    if args.apply_rename_plan:
        plan = load_plan(os.path.expanduser(args.apply_rename_plan))
        result = apply_rename_plan(root, plan, execute=args.execute)
        print(json.dumps(result, indent=2))
        return 0
    if args.apply_triage_plan:
        plan = load_plan(os.path.expanduser(args.apply_triage_plan))
        result = apply_move_plan(root, plan, execute=args.execute)
        print(json.dumps(result, indent=2))
        return 0

    report = analyze_library(root, probe=args.probe, max_probe=max(0, args.max_probe), stale_days=max(1, args.stale_days))
    if args.manual_triage_plan:
        plan = prompt_manual_triage_plan(root, report["triage_suggestions"])
        payload = json.dumps(plan, indent=2)
        out_path = os.path.expanduser(args.manual_triage_plan)
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
        print(f"Wrote manual triage plan: {out_path} ({len(plan)} entries)")
        return 0
    if args.rename_plan:
        payload = json.dumps(report["rename_plan"], indent=2)
        if args.rename_plan == "-":
            print(payload)
            return 0
        out_path = os.path.expanduser(args.rename_plan)
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")
    if args.triage_plan:
        payload = json.dumps(report["triage_move_plan"], indent=2)
        if args.triage_plan == "-":
            print(payload)
            return 0
        out_path = os.path.expanduser(args.triage_plan)
        out_dir = os.path.dirname(out_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.write("\n")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_text_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
