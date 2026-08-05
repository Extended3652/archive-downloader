#!/usr/bin/env python3
"""Read-only Radarr diagnostics for archive-downloader."""
import argparse
import json
import os
import sys
from typing import Any, List, Optional

import ia_radarr


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ia-radarr",
        description="Diagnose optional Radarr registration without changing media by default.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Verify Radarr connectivity.")
    sub.add_parser("roots", help="List Radarr root folders.")
    sub.add_parser("profiles", help="List Radarr quality profiles.")
    map_parser = sub.add_parser("map", help="Show the Radarr-visible path for a local movie folder.")
    map_parser.add_argument("local_movie_folder")
    lookup = sub.add_parser("lookup", help="Look up one movie using Radarr.")
    lookup.add_argument("--title", required=True)
    lookup.add_argument("--year", default="")
    lookup.add_argument("--tmdb-id", type=int, default=0)
    register = sub.add_parser("register", help="Dry-run or apply Radarr registration for one completed movie file.")
    register.add_argument("local_movie_file")
    register.add_argument("--title", default="")
    register.add_argument("--year", default="")
    register.add_argument("--tmdb-id", type=int, default=0)
    register.add_argument("--apply", action="store_true", help="Actually add/update Radarr. Without this, no changes are made.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = ia_radarr.load_settings()
    client = ia_radarr.RadarrClient(settings)

    try:
        if args.cmd == "check":
            roots = client.root_folders()
            profiles = client.quality_profiles()
            print_json(
                {
                    "ok": True,
                    "url": ia_radarr.normalize_url(settings.url),
                    "root_folders": len(roots),
                    "quality_profiles": len(profiles),
                    "enabled": settings.enabled,
                }
            )
            return 0

        if args.cmd == "roots":
            print_json(client.root_folders())
            return 0

        if args.cmd == "profiles":
            print_json(client.quality_profiles())
            return 0

        if args.cmd == "map":
            radarr_path, err = ia_radarr.map_local_to_radarr_path(os.path.abspath(args.local_movie_folder), settings)
            print_json(
                {
                    "ok": not bool(err),
                    "local_path": os.path.abspath(args.local_movie_folder),
                    "radarr_path": radarr_path,
                    "error": err,
                    "local_movie_root": settings.local_movie_root,
                    "radarr_root_folder": settings.root_folder,
                }
            )
            return 0 if not err else 1

        if args.cmd == "lookup":
            results = client.lookup_tmdb(args.tmdb_id) if args.tmdb_id else client.lookup(args.title, args.year)
            match, err = ia_radarr.select_lookup_result(results, args.title, args.year)
            print_json({"ok": match is not None, "match": match, "error": err, "results": results})
            return 0 if match is not None else 1

        if args.cmd == "register":
            result = ia_radarr.register_completed_movie(
                os.path.abspath(args.local_movie_file),
                item_title=args.title,
                item_year=args.year,
                tmdb_id=args.tmdb_id,
                settings=settings,
                client=client,
                dry_run=not args.apply,
            )
            print_json(result.__dict__)
            return 0 if result.ok else 1
    except ia_radarr.RadarrError as exc:
        print_json({"ok": False, "error": ia_radarr.redact_message(str(exc), settings), "status": exc.status})
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
