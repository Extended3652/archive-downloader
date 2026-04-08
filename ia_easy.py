#!/usr/bin/env python3
import json
import os
import re
import sys
from typing import List, Optional

from ia_common import (
    IACommandError,
    IAFile,
    IANotInstalled,
    SearchResult,
    human_size,
    is_video_file,
    run,
)

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

def filter_video_files(files: List[IAFile], keyword: Optional[str]) -> List[IAFile]:
    vids = [f for f in files if is_video_file(f.name, f.fmt)]
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

    default_dest = os.environ.get("XDG_DOWNLOAD_DIR") or os.path.expanduser("~/Downloads")
    dest = prompt(f"Download folder (default: {default_dest}): ")
    if not dest:
        dest = default_dest
    else:
        dest = os.path.expanduser(dest)

    while True:
        q = prompt("\nSearch title (example: Test Copy) or full IA query: ")
        if q == "":
            print("\nBye.")
            return 0

        try:
            results = ia_search_simple(q, rows=25)
        except IACommandError as e:
            print(f"Search failed: {e.stderr or e}")
            continue

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
        except IACommandError as e:
            print(f"Could not fetch metadata: {e.stderr or e}")
            continue
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
        try:
            download_file(item.identifier, chosen.name, dest)
        except IACommandError as e:
            print(f"Download failed: {e.stderr or e}")
            continue

        again = prompt("\nDownload another? (y/n): ").lower()
        if again not in ("y", "yes"):
            print("\nBye.")
            return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IANotInstalled:
        print("\nError: 'ia' command not found.")
        print("Install with: pip3 install --user internetarchive\n")
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\nBye.")
        raise SystemExit(0)
