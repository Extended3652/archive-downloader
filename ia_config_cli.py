#!/usr/bin/env python3
"""Command-line config management for archive-downloader."""
import argparse
import json
import sys
from typing import List, Optional

import ia_config


def print_json(data: dict) -> None:
    safe = dict(data)
    if safe.get("radarr_api_key"):
        safe["radarr_api_key"] = "[redacted]"
    print(json.dumps(safe, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ia-config",
        description="Manage archive-downloader JSON configuration.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("path", help="Print the config file path.")
    sub.add_parser("show", help="Print the effective config as JSON.")

    set_parser = sub.add_parser("set", help="Set one config value.")
    set_parser.add_argument(
        "key",
        choices=[
            "media_root",
            "default_bucket",
            "default_filter",
            "default_sort",
            "title_only",
            "license_gate",
            "no_change_timestamp",
            "rows_per_page",
            "radarr_enabled",
            "radarr_url",
            "radarr_api_key",
            "radarr_local_movie_root",
            "radarr_root_folder",
            "radarr_quality_profile_id",
            "radarr_monitor_movie",
            "radarr_search_on_add",
            "radarr_timeout_s",
        ],
    )
    set_parser.add_argument("value")

    args = parser.parse_args(argv)

    if args.cmd == "path":
        print(ia_config.config_path())
        return 0

    if args.cmd == "show":
        print_json(ia_config.load_config())
        return 0

    if args.cmd == "set":
        try:
            cfg = ia_config.set_config_value(args.key, args.value)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        print_json(cfg)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
