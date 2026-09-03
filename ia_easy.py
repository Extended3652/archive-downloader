#!/usr/bin/env python3
import argparse
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
    default_media_root,
    human_size,
    is_video_file,
    run,
)
from ia_paths import normalize_media_permissions, set_process_umask
from ia_minotaur_events import emit_archive_completed, emit_archive_failed, emit_archive_started, safe_text

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
TEXT = "\033[38;5;253m"
HEADER = "\033[38;5;250m"
CYAN = "\033[38;5;178m"
GREEN = "\033[38;5;114m"
GOLD = "\033[38;5;178m"
MAGENTA = "\033[38;5;208m"
YELLOW = "\033[38;5;220m"
RED = "\033[38;5;203m"
KEYS = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def getch() -> str:
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        if sys.stdin.isatty():
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                return sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass
    return sys.stdin.read(1)


def key_for_index(index: int) -> str:
    return KEYS[index - 1]


def index_for_key(key: str, count: int) -> Optional[int]:
    key = key.upper()
    if key in KEYS[:count]:
        return KEYS.index(key) + 1
    return None


def menu_key(key: str, label: str) -> None:
    print(f"  {BOLD}{GOLD}[{TEXT}{key}{MAGENTA}]{RESET} {label}")


def prompt(msg: str) -> str:
    return input(f"{CYAN}{msg}{RESET}").strip()

def prompt_int(msg: str, lo: int, hi: int) -> Optional[int]:
    while True:
        s = prompt(msg)
        if s == "":
            return None
        if s.isdigit():
            v = int(s)
            if lo <= v <= hi:
                return v
        print(f"{YELLOW}Enter a number {lo}-{hi}, or press Enter to cancel.{RESET}")

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
    normalize_media_permissions(dest, media_root=dest)
    cmd = ["ia", "download", identifier, "--destdir", dest, "--files", filename]
    print(f"\n{MAGENTA}Downloading:{RESET}")
    print(f"  {TEXT}{' '.join(cmd)}{RESET}")
    emit_archive_started(f"{identifier} {filename}")
    try:
        run(cmd, check=True)
    except Exception as exc:
        emit_archive_failed(f"{identifier} {filename}: {safe_text(exc, 180)}")
        raise
    print(f"\n{GREEN}Done.{RESET}")
    output = os.path.join(dest, identifier, filename)
    item_dir = os.path.join(dest, identifier)
    normalize_media_permissions(item_dir, media_root=dest, recursive=True, include_parents=True)
    print(f"{CYAN}Saved to:{RESET} {TEXT}{output}{RESET}")
    emit_archive_completed(output)

def main() -> int:
    print(f"\n{HEADER}{'-' * 40}{RESET}")
    print(f"{BOLD}{MAGENTA}Internet Archive Downloader{RESET} {DIM}(easy mode){RESET}")
    print(f"{HEADER}{'-' * 40}{RESET}")
    print(f"{DIM}Typed prompts still use Enter; result and file selections are one key.{RESET}\n")

    default_dest = default_media_root()
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
            print(f"{RED}Search failed:{RESET} {e.stderr or e}")
            continue

        if not results:
            print(f"{YELLOW}No results. Try different words.{RESET}")
            continue

        print(f"\n{CYAN}Results{RESET}")
        keyed_results = results[:len(KEYS)]
        for i, r in enumerate(keyed_results, start=1):
            y = f" ({r.year})" if r.year else ""
            title = (r.title[:80] + "...") if len(r.title) > 80 else r.title
            menu_key(key_for_index(i), f"{title}{y}")
            print(f"    {DIM}id:{RESET} {r.identifier}")

        menu_key("0", "Back")
        print(f"\n{CYAN}Pick item:{RESET} ", end="", flush=True)
        choice = getch().upper()
        print(choice)
        if choice == "0":
            continue
        idx = index_for_key(choice, len(keyed_results))
        if idx is None:
            continue

        item = results[idx - 1]
        try:
            files = ia_metadata_files(item.identifier)
        except IACommandError as e:
            print(f"{RED}Could not fetch metadata:{RESET} {e.stderr or e}")
            continue
        except json.JSONDecodeError:
            print(f"{RED}Could not read metadata for that item.{RESET}")
            continue

        keyword = prompt("Optional filter keyword for files (example: mp4, h.264, 720p). Enter to skip: ")
        vids = filter_video_files(files, keyword if keyword else None)

        if not vids:
            print(f"{YELLOW}No video-like files found for that item.{RESET}")
            continue

        print(f"\n{CYAN}Video files{RESET} {DIM}(biggest first){RESET}")
        keyed_vids = vids[:len(KEYS)]
        for i, f in enumerate(keyed_vids, start=1):
            fmt = f.fmt if f.fmt else ""
            menu_key(key_for_index(i), f"{human_size(f.size):>10}  {fmt:<22}  {f.name}")

        menu_key("0", "Back")
        print(f"\n{CYAN}Pick file:{RESET} ", end="", flush=True)
        choice = getch().upper()
        print(choice)
        if choice == "0":
            continue
        fidx = index_for_key(choice, len(keyed_vids))
        if fidx is None:
            continue

        chosen = keyed_vids[fidx - 1]
        try:
            download_file(item.identifier, chosen.name, dest)
        except IACommandError as e:
            print(f"{RED}Download failed:{RESET} {e.stderr or e}")
            continue

        print(f"\n{CYAN}Download another?{RESET} {BOLD}{GOLD}[{TEXT}Y{MAGENTA}]{RESET} yes  {BOLD}{GOLD}[{TEXT}N{MAGENTA}]{RESET} no: ", end="", flush=True)
        again = getch().lower()
        print(again)
        if again != "y":
            print(f"\n{DIM}Bye.{RESET}")
            return 0


def cli_main(argv: Optional[List[str]] = None) -> int:
    set_process_umask()
    parser = argparse.ArgumentParser(
        prog="ia_easy",
        description="Interactive Internet Archive movie downloader.",
    )
    parser.parse_args(argv)
    return main()


if __name__ == "__main__":
    try:
        raise SystemExit(cli_main(sys.argv[1:]))
    except IANotInstalled:
        print(f"\n{RED}Error:{RESET} 'ia' command not found.")
        print(f"{DIM}Install with: pip3 install --user internetarchive{RESET}\n")
        raise SystemExit(2)
    except KeyboardInterrupt:
        print(f"\n{DIM}Bye.{RESET}")
        raise SystemExit(0)
