#!/usr/bin/env python3
import argparse
import os
import re
import sys
from typing import List, Optional

from ia_common import IACommandError, IAFile, IANotInstalled, SearchResult, compact_count, default_media_root, human_size, is_archive_torrent_format, is_video_file, run
import ia_api
from ia_minotaur_events import emit_archive_completed, emit_archive_failed, emit_archive_started, safe_text
from ia_organize import archive_query_preset_labels, build_archive_preset_query, build_query, license_status_from_fields


class BadFileRegex(ValueError):
    """Raised when a user-provided filename regex cannot be compiled."""

def sanitize_query(q: str) -> str:
    q = q.strip()
    q = re.sub(r"\s+", " ", q)
    return q

def curl_runner(cmd, timeout=60):
    p = run(cmd, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def ia_search(query: str, rows: int, *, page: int = 1, sort: str = "") -> List[SearchResult]:
    results, _total, err = ia_api.ia_search_via_curl(query, rows, page, sort, runner=curl_runner)
    if err:
        raise IACommandError(["curl", "https://archive.org/advancedsearch.php"], 1, err)
    return results


def search_result_line(r: SearchResult) -> str:
    y = f" ({r.year})" if r.year else ""
    bits = []
    if r.mediatype:
        bits.append(r.mediatype)
    if r.downloads:
        bits.append(f"{compact_count(r.downloads)} dl")
    if r.formats and is_archive_torrent_format(r.formats):
        bits.append("torrent")
    status, _why = license_status_from_fields(r.licenseurl, r.rights)
    if status != "unknown":
        bits.append(f"lic:{status}")
    suffix = f"\t{' | '.join(bits)}" if bits else ""
    return f"{r.identifier}\t{r.title}{y}{suffix}"

def choose_result(results: List[SearchResult]) -> Optional[SearchResult]:
    if not results:
        return None
    for i, r in enumerate(results, start=1):
        y = f" ({r.year})" if r.year else ""
        print(f"{i:2d}. {r.identifier}  |  {r.title}{y}")
    while True:
        s = input("Pick a number (or blank to cancel): ").strip()
        if s == "":
            return None
        if s.isdigit():
            idx = int(s)
            if 1 <= idx <= len(results):
                return results[idx - 1]
        print("Invalid selection.")

def ia_list_files(identifier: str) -> List[IAFile]:
    def runner(cmd, timeout=60):
        p = run(cmd, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    files, _meta, err = ia_api.ia_files(identifier, runner=runner)
    if err:
        raise IACommandError(["ia", "metadata", identifier], 1, err)
    return files

def filter_files(files: List[IAFile], exts: Optional[List[str]], regex: Optional[str]) -> List[IAFile]:
    out = files[:]
    if exts:
        norm_exts = set()
        for e in exts:
            e = (e or "").strip().lower()
            if not e or e == ".":
                continue
            if not e.startswith("."):
                e = "." + e
            norm_exts.add(e)
        if norm_exts:
            out = [f for f in out if os.path.splitext(f.name.lower())[1] in norm_exts]
    if regex:
        try:
            rx = re.compile(regex, re.IGNORECASE)
        except re.error as e:
            raise BadFileRegex(str(e)) from e
        out = [f for f in out if rx.search(f.name)]
    return out

def print_files(files: List[IAFile]) -> None:
    if not files:
        print("(no matching files)")
        return
    for i, f in enumerate(files, start=1):
        fmt = f.fmt if f.fmt else ""
        print(f"{i:2d}. {human_size(f.size):>10}  {fmt:<20}  {f.name}")

def choose_file(files: List[IAFile]) -> Optional[IAFile]:
    if not files:
        return None
    while True:
        s = input("Pick a file number (or blank to cancel): ").strip()
        if s == "":
            return None
        if s.isdigit():
            idx = int(s)
            if 1 <= idx <= len(files):
                return files[idx - 1]
        print("Invalid selection.")

def biggest_file(files: List[IAFile]) -> Optional[IAFile]:
    if not files:
        return None

    def file_rank(f: IAFile) -> tuple:
        name = (f.name or "").lower()
        fmt = (f.fmt or "").lower()
        ext = os.path.splitext(name)[1]

        garbage_hits = (
            ext in {".xml", ".json", ".txt", ".nfo", ".sql", ".sqlite", ".db", ".jpg", ".jpeg", ".png", ".gif", ".pdf", ".torrent"}
            or "thumb" in name
            or "preview" in name
            or "metadata" in name
            or "manifest" in name
            or "derivative" in name
        )
        if is_video_file(name, fmt):
            media_score = 4
        elif ext in {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac"}:
            media_score = 3
        elif ext in {".iso"}:
            media_score = 2
        elif garbage_hits:
            media_score = -2
        else:
            media_score = 0
        return (media_score, int(f.size or 0))

    return sorted(files, key=file_rank, reverse=True)[0]

def ia_download(identifier: str, dest: str, glob_pat: Optional[str], exact_file: Optional[str]) -> None:
    os.makedirs(dest, exist_ok=True)

    cmd = ["ia", "download", identifier, "--destdir", dest]
    if exact_file:
        cmd += ["--files", exact_file]
    elif glob_pat:
        cmd += ["--glob", glob_pat]

    print("Running:", " ".join(cmd))
    target = exact_file or glob_pat or identifier
    emit_archive_started(f"{identifier} {target}")
    try:
        run(cmd, check=True)
    except Exception as exc:
        emit_archive_failed(f"{identifier} {target}: {safe_text(exc, 180)}")
        raise
    print("Done.")
    output = os.path.join(dest, identifier, exact_file) if exact_file else os.path.join(dest, identifier)
    emit_archive_completed(output)

def positive_int(s: str) -> int:
    v = int(s)
    if v < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return v

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ia_dl",
        description="Helper CLI for searching and downloading from Internet Archive using the 'ia' tool."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="Search items and print results.")
    sp.add_argument("query", nargs="?", default="", help='Search query or extra text for a preset. Example: \'title:"Test Copy" AND mediatype:movies\'')
    sp.add_argument("--rows", type=positive_int, default=20, help="Max results (default 20).")
    sp.add_argument("--page", type=positive_int, default=1, help="Search results page (default 1).")
    sp.add_argument(
        "--filter",
        choices=["movies", "audio", "texts", "software", "any"],
        default="any",
        help="Media type filter for simple queries (default any). Advanced queries pass through unchanged.",
    )
    sp.add_argument("--title-only", action="store_true", help="Search title field only for simple queries.")
    sp.add_argument(
        "--sort",
        choices=["relevance", "date-desc", "date-asc", "title", "downloads"],
        default="relevance",
        help="Sort order (default relevance).",
    )
    sp.add_argument(
        "--preset",
        choices=[key for _label, key in archive_query_preset_labels()],
        help="Archive search preset that targets common hidden-media collections.",
    )

    lp = sub.add_parser("list", help="List files for an item identifier.")
    lp.add_argument("identifier", help="Internet Archive identifier.")
    lp.add_argument("--ext", action="append", help="Filter by extension (repeatable), e.g. --ext mp4 --ext mkv")
    lp.add_argument("--regex", help="Filter by filename regex (case-insensitive).")

    dp = sub.add_parser("download", help="Download a file (interactive or automatic).")
    dp.add_argument("identifier", nargs="?", help="Identifier. If omitted, you can use --search to find one.")
    dp.add_argument("--search", help='Search query to pick an identifier interactively.')
    dp.add_argument("--rows", type=positive_int, default=20, help="Max search results (default 20).")
    default_dest = default_media_root()
    dp.add_argument("--dest", default=default_dest, help=f"Destination directory (default {default_dest}).")
    dp.add_argument("--ext", action="append", help="Filter by extension (repeatable), e.g. --ext mp4")
    dp.add_argument("--regex", help="Filter by filename regex (case-insensitive).")
    dp.add_argument("--biggest", action="store_true", help="Auto-pick biggest matching file (no prompt).")
    dp.add_argument("--glob", help="Download using ia --glob (advanced), e.g. '*.mp4'")
    dp.add_argument("--file", help="Download one exact file by name (advanced).")

    args = ap.parse_args(argv)

    if args.cmd == "search":
        sort_map = {
            "relevance": "",
            "date-desc": "date desc",
            "date-asc": "date asc",
            "title": "titleSorter asc",
            "downloads": "downloads desc",
        }
        query_text = sanitize_query(args.query or "")
        if getattr(args, "preset", None):
            q = build_archive_preset_query(args.preset, query_text, args.title_only)
        elif not query_text:
            print("No query or preset provided.", file=sys.stderr)
            return 2
        else:
            q = build_query(query_text, args.filter, args.title_only)
        results = ia_search(q, args.rows, page=args.page, sort=sort_map[args.sort])
        if not results:
            print("No results.")
            return 1
        for r in results:
            print(search_result_line(r))
        return 0

    if args.cmd == "list":
        files = ia_list_files(args.identifier)
        try:
            files = filter_files(files, args.ext, args.regex)
        except BadFileRegex as e:
            print(f"Bad regex: {e}", file=sys.stderr)
            return 2
        print_files(files)
        return 0

    if args.cmd == "download":
        identifier = args.identifier

        if args.search:
            q = build_query(sanitize_query(args.search), "any", False)
            results = ia_search(q, args.rows)
            if not results:
                print("No search results.")
                return 1
            chosen = choose_result(results)
            if not chosen:
                print("Canceled.")
                return 1
            identifier = chosen.identifier

        if not identifier:
            print("Error: provide an identifier or use --search.", file=sys.stderr)
            return 2

        # If user provided --glob or --file, skip listing/picking.
        if args.file or args.glob:
            ia_download(identifier, args.dest, args.glob, args.file)
            return 0

        files = ia_list_files(identifier)
        try:
            files = filter_files(files, args.ext, args.regex)
        except BadFileRegex as e:
            print(f"Bad regex: {e}", file=sys.stderr)
            return 2

        # Default behavior: if no filters, show all files.
        if not files:
            print("No matching files to download.")
            return 1

        print_files(files)

        if args.biggest:
            f = biggest_file(files)
            if not f:
                print("No file selected.")
                return 1
            print(f"Auto-selecting biggest: {f.name} ({human_size(f.size)})")
            ia_download(identifier, args.dest, None, f.name)
            return 0

        f = choose_file(files)
        if not f:
            print("Canceled.")
            return 1
        ia_download(identifier, args.dest, None, f.name)
        return 0

    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IANotInstalled:
        print("Error: 'ia' command not found. Install it with: pip3 install --user internetarchive", file=sys.stderr)
        raise SystemExit(2)
    except IACommandError as e:
        print(f"Command failed: {' '.join(e.cmd)}", file=sys.stderr)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        raise SystemExit(e.returncode or 1)
