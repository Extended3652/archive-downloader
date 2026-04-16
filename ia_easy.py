#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class SearchResult:
    identifier: str
    title: str
    year: str

@dataclass
class IAFile:
    name: str
    size: int
    fmt: str

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}
VIDEO_FORMAT_HINTS = (
    "h.264", "h264", "mpeg4", "mp4", "matroska", "webm", "quicktime", "avi"
)

def run(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)
    except FileNotFoundError:
        print("\nError: 'ia' command not found.")
        print("Install with: pip3 install --user internetarchive\n")
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or e.stdout or "").strip()
        print("\nThat command failed:")
        print(" ".join(cmd))
        if msg:
            print("\n" + msg + "\n")
        sys.exit(e.returncode)

def human_size(n: int) -> str:
    try:
        n = int(n)
    except Exception:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)}{units[i]}"
    return f"{f:.2f}{units[i]}"

def prompt(msg: str) -> str:
    return input(msg).strip()

def prompt_int(msg: str, lo: int, hi: int) -> Optional[int]:
    while True:
        s = prompt(msg)
        if s == "":
            return None
        if s.isdigit():
            v = int(s)
            if lo <= v <= hi:
                return v
        print(f"Enter a number {lo}-{hi}, or press Enter to cancel.")

def ia_search_simple(q: str, rows: int = 20) -> List[SearchResult]:
    # If user types just words, we'll search those in title and restrict to movies.
    # You can still type full IA query syntax if you want.
    q = q.strip()
    if not q:
        return []

    if ("mediatype:" not in q) and ("title:" not in q) and ("AND" not in q) and ("OR" not in q):
        query = f'title:("{q}") AND mediatype:movies'
    else:
        query = q

    p = run(["ia", "search", query, "--rows", str(rows), "--json"])
    out: List[SearchResult] = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ident = str(obj.get("identifier", "")).strip()
        title = str(obj.get("title", "")).strip() or "(no title)"
        year = str(obj.get("year", "")).strip()
        if ident:
            out.append(SearchResult(identifier=ident, title=title, year=year))
    return out

def ia_metadata_files(identifier: str) -> List[IAFile]:
    p = run(["ia", "metadata", identifier, "--json"])
    meta = json.loads(p.stdout)
    files = []
    for f in meta.get("files", []) or []:
        name = str(f.get("name", "")).strip()
        if not name:
            continue
        size_raw = f.get("size", 0)
        try:
            size = int(size_raw) if size_raw is not None else 0
        except Exception:
            size = 0
        fmt = str(f.get("format", "")).strip()
        files.append(IAFile(name=name, size=size, fmt=fmt))
    return files

def is_video_file(f: IAFile) -> bool:
    ext = os.path.splitext(f.name.lower())[1]
    if ext in VIDEO_EXTS:
        return True
    fmt_l = (f.fmt or "").lower()
    return any(h in fmt_l for h in VIDEO_FORMAT_HINTS)

def filter_video_files(files: List[IAFile], keyword: Optional[str]) -> List[IAFile]:
    vids = [f for f in files if is_video_file(f)]
    if keyword:
        rx = re.compile(re.escape(keyword), re.IGNORECASE)
        vids = [f for f in vids if rx.search(f.name) or rx.search(f.fmt)]
    # Sort biggest first, usually the main video is the largest
    vids.sort(key=lambda x: x.size or 0, reverse=True)
    return vids

def download_file(identifier: str, filename: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    cmd = ["ia", "download", identifier, "--destdir", dest, "--files", filename]
    print("\nDownloading:")
    print("  " + " ".join(cmd))
    run(cmd, check=True)
    print("\nDone.")
    print(f"Saved to: {os.path.join(dest, identifier, filename)}")

def main() -> int:
    print("\nInternet Archive Downloader (easy mode)")
    print("-------------------------------------")
    print("Tips:")
    print("- Press Enter on any prompt to cancel/back out.")
    print("- Search is limited to mediatype:movies by default.\n")

    dest = prompt("Download folder (default: /mnt/ssd/media): ")
    if not dest:
        dest = "/mnt/ssd/media"
    else:
        dest = os.path.expanduser(dest)

    while True:
        q = prompt("\nSearch title (example: Test Copy) or full IA query: ")
        if q == "":
            print("\nBye.")
            return 0

        results = ia_search_simple(q, rows=25)
        if not results:
            print("No results. Try different words.")
            continue

        print("\nResults:")
        for i, r in enumerate(results, start=1):
            y = f" ({r.year})" if r.year else ""
            title = (r.title[:80] + "...") if len(r.title) > 80 else r.title
            print(f"{i:2d}. {title}{y}")
            print(f"    id: {r.identifier}")

        idx = prompt_int("\nPick an item number to view files: ", 1, len(results))
        if idx is None:
            continue

        item = results[idx - 1]
        try:
            files = ia_metadata_files(item.identifier)
        except json.JSONDecodeError:
            print("Could not read metadata for that item.")
            continue

        keyword = prompt("Optional filter keyword for files (example: mp4, h.264, 720p). Enter to skip: ")
        vids = filter_video_files(files, keyword if keyword else None)

        if not vids:
            print("No video-like files found for that item.")
            continue

        print("\nVideo files (biggest first):")
        for i, f in enumerate(vids, start=1):
            fmt = f.fmt if f.fmt else ""
            print(f"{i:2d}. {human_size(f.size):>10}  {fmt:<22}  {f.name}")

        fidx = prompt_int("\nPick a file number to download: ", 1, len(vids))
        if fidx is None:
            continue

        chosen = vids[fidx - 1]
        download_file(item.identifier, chosen.name, dest)

        again = prompt("\nDownload another? (y/n): ").lower()
        if again not in ("y", "yes"):
            print("\nBye.")
            return 0

if __name__ == "__main__":
    raise SystemExit(main())
