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

DEFAULT_MEDIA_ROOT = os.environ.get("XDG_DOWNLOAD_DIR") or os.path.expanduser("~/Downloads")
DEST_BUCKETS = {
    "tv": os.path.join(DEFAULT_MEDIA_ROOT, "TV"),
    "movie": os.path.join(DEFAULT_MEDIA_ROOT, "Movies"),
    "other": os.path.join(DEFAULT_MEDIA_ROOT, "Other"),
}

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
        # Better default search:
        # - prioritize title matches
        # - still allow metadata-only hits from description/subject/creator
        # - include videos and audio by default (many items are not tagged as movies)
        query = (
            f'((title:("{q}")^3) OR description:("{q}") OR subject:("{q}") OR creator:("{q}")) '
            f'AND (mediatype:movies OR mediatype:video OR mediatype:audio)'
        )
    else:
        query = q

    p = run(["ia", "search", query, "--rows", str(rows), "--json"])
    tokens = [t.lower() for t in re.findall(r"[\w']+", q)]
    scored: List[tuple[int, SearchResult]] = []
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
            item = SearchResult(identifier=ident, title=title, year=year)
            score = 0
            title_l = title.lower()
            ident_l = ident.lower()
            for tok in tokens:
                if tok in title_l:
                    score += 3
                if tok in ident_l:
                    score += 1
            if year and year.isdigit():
                score += 1
            scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [i for _, i in scored]

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


def infer_destination_bucket(identifier: str, filename: str) -> str:
    check = f"{identifier} {filename}".lower()
    if re.search(r"\bs\d{1,2}e\d{1,2}\b", check):
        return "tv"
    return "movie"


def choose_destination(identifier: str, filename: str) -> str:
    default_bucket = infer_destination_bucket(identifier, filename)
    print("\nWhere should this download go?")
    print(f" 1) TV     -> {DEST_BUCKETS['tv']}")
    print(f" 2) Movie  -> {DEST_BUCKETS['movie']}")
    print(f" 3) Other  -> {DEST_BUCKETS['other']}")
    print(" 4) Custom folder")

    default_choice = {"tv": "1", "movie": "2", "other": "3"}[default_bucket]
    while True:
        choice = prompt(f"Choose destination [{default_choice}]: ")
        if not choice:
            choice = default_choice

        if choice == "1":
            return DEST_BUCKETS["tv"]
        if choice == "2":
            return DEST_BUCKETS["movie"]
        if choice == "3":
            return DEST_BUCKETS["other"]
        if choice == "4":
            custom = prompt("Custom path: ")
            if custom:
                return os.path.expanduser(custom)
            print("Please enter a custom path.")
            continue

        print("Choose 1, 2, 3, or 4.")


def main() -> int:
    print("\nInternet Archive Downloader (easy mode)")
    print("-------------------------------------")
    print("Tips:")
    print("- Press Enter on any prompt to cancel/back out.")
    print("- Search prioritizes title matches and checks common media types.\n")

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
        dest = choose_destination(item.identifier, chosen.name)
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
