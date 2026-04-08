#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
from typing import List, Optional

from ia_common import IACommandError, IAFile, IANotInstalled, SearchResult, human_size, run

def sanitize_query(q: str) -> str:
    q = q.strip()
    q = re.sub(r"\s+", " ", q)
    return q

def ia_search(query: str, rows: int) -> List[SearchResult]:
    # Use ia CLI search and JSON output for robust parsing.
    # Query example: title:"Test Copy" AND mediatype:movies
    cmd = ["ia", "search", query, "--rows", str(rows), "--json"]
    p = run(cmd)
    results = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        identifier = str(obj.get("identifier", "")).strip()
        title = str(obj.get("title", "")).strip()
        year = str(obj.get("year", "")).strip()
        creator = str(obj.get("creator", "")).strip()
        if identifier:
            results.append(SearchResult(identifier=identifier, title=title or "(no title)", year=year or "", creator=creator))
    return results

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
    # ia metadata ITEM --json gives a JSON blob with files.
    cmd = ["ia", "metadata", identifier, "--json"]
    p = run(cmd)
    try:
        meta = json.loads(p.stdout)
    except json.JSONDecodeError:
        print("Could not parse metadata JSON.", file=sys.stderr)
        sys.exit(1)

    files = []
    for f in meta.get("files", []) or []:
        name = str(f.get("name", "")).strip()
        if not name:
            continue
        size_raw = f.get("size")
        try:
            size = int(size_raw) if size_raw is not None else 0
        except (TypeError, ValueError):
            size = 0
        fmt = str(f.get("format", "")).strip()
        files.append(IAFile(name=name, size=size, fmt=fmt))
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
            print(f"Bad regex: {e}", file=sys.stderr)
            sys.exit(2)
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
    return sorted(files, key=lambda f: f.size or 0, reverse=True)[0]

def ia_download(identifier: str, dest: str, glob_pat: Optional[str], exact_file: Optional[str]) -> None:
    os.makedirs(dest, exist_ok=True)

    cmd = ["ia", "download", identifier, "--destdir", dest]
    if exact_file:
        cmd += ["--files", exact_file]
    elif glob_pat:
        cmd += ["--glob", glob_pat]

    print("Running:", " ".join(cmd))
    run(cmd, check=True)
    print("Done.")

def positive_int(s: str) -> int:
    v = int(s)
    if v < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return v

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ia_dl",
        description="Helper CLI for searching and downloading from Internet Archive using the 'ia' tool."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="Search items and print results.")
    sp.add_argument("query", help='Search query. Example: \'title:"Test Copy" AND mediatype:movies\'')
    sp.add_argument("--rows", type=positive_int, default=20, help="Max results (default 20).")

    lp = sub.add_parser("list", help="List files for an item identifier.")
    lp.add_argument("identifier", help="Internet Archive identifier.")
    lp.add_argument("--ext", action="append", help="Filter by extension (repeatable), e.g. --ext mp4 --ext mkv")
    lp.add_argument("--regex", help="Filter by filename regex (case-insensitive).")

    dp = sub.add_parser("download", help="Download a file (interactive or automatic).")
    dp.add_argument("identifier", nargs="?", help="Identifier. If omitted, you can use --search to find one.")
    dp.add_argument("--search", help='Search query to pick an identifier interactively.')
    dp.add_argument("--rows", type=positive_int, default=20, help="Max search results (default 20).")
    dp.add_argument("--dest", default=".", help="Destination directory (default current dir).")
    dp.add_argument("--ext", action="append", help="Filter by extension (repeatable), e.g. --ext mp4")
    dp.add_argument("--regex", help="Filter by filename regex (case-insensitive).")
    dp.add_argument("--biggest", action="store_true", help="Auto-pick biggest matching file (no prompt).")
    dp.add_argument("--glob", help="Download using ia --glob (advanced), e.g. '*.mp4'")
    dp.add_argument("--file", help="Download one exact file by name (advanced).")

    args = ap.parse_args()

    if args.cmd == "search":
        q = sanitize_query(args.query)
        results = ia_search(q, args.rows)
        if not results:
            print("No results.")
            return 1
        for r in results:
            y = f" ({r.year})" if r.year else ""
            print(f"{r.identifier}\t{r.title}{y}")
        return 0

    if args.cmd == "list":
        files = ia_list_files(args.identifier)
        files = filter_files(files, args.ext, args.regex)
        print_files(files)
        return 0

    if args.cmd == "download":
        identifier = args.identifier

        if args.search:
            q = sanitize_query(args.search)
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
        files = filter_files(files, args.ext, args.regex)

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
