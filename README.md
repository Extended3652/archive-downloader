# archive-downloader

Python tools around the [`internetarchive`](https://archive.org/developers/internetarchive/) (`ia`) CLI, with a curses TUI, helper CLIs, and library-audit utilities. Pick the one that fits your workflow.

There is also **`ia_audit.py` / `ia-audit`** for scanning an existing media library for weird filenames, duplicate episodes/movies, metadata gaps, rename suggestions, stale leftovers, and optional codec/bitrate reporting.

## The tools

**`ia_dl.py`** — scriptable argparse CLI. Subcommands: `search`, `list`, `download`. Good for shell scripts and one-off invocations.

**`ia_easy.py`** — interactive `input()`-based flow optimized for finding and grabbing a single movie file. Defaults to `$IA_MEDIA_ROOT` or `/mnt/ssd/media`, and prompts for another folder.

**`ia_minotaur.py`** — full-screen curses TUI with favorites, open-license gating, staged downloads, live progress, and automatic bucket organization (`TV/`, `Movies/`, `Music/`, `Other/`). The heaviest of the three.

**`ia_audit.py`** — report-oriented library auditor for the media root. Flags suspicious filenames, suggests cleaner filenames, detects duplicate episodes and movie folders, finds likely metadata cleanup work, reports stale `.ia_staging` / `.part` / `.torrent` leftovers, and can optionally produce `ffprobe`-based codec + bitrate summaries.

All three share `ia_common.py` for subprocess handling, the `SearchResult` / `IAFile` dataclasses, and small utilities. `ia_api.py` wraps Internet Archive search and metadata access for Minotaur. `ia_downloads.py` handles download command construction and staging-size checks. `ia_organize.py` handles naming, query building, and license heuristics. `ia_paths.py` centralizes media-root paths and staging path guards. `ia_state.py` handles JSON persistence for favorites, sessions, and pending downloads.

## Install

```
python3 -m pip install -e .
```

You also need two binaries on `PATH`:

- `ia` — installed by the `internetarchive` Python dependency. Run `ia configure` once if you want to use an Internet Archive account.
- `curl` — used by `ia-minotaur` to hit `advancedsearch.php` for paginated search. Usually pre-installed on Linux / macOS.

## Quick start

```
# Scriptable
ia-dl search 'title:"The Big Movie" AND mediatype:movies' --rows 5
ia-dl list <identifier> --ext mp4
ia-dl download <identifier> --biggest --dest ./out

# Audit an existing library
ia-audit
ia-audit --probe --max-probe 300
ia-audit --json > audit.json
ia-audit --rename-plan rename-plan.json
ia-audit --apply-rename-plan rename-plan.json
ia-audit --apply-rename-plan rename-plan.json --execute
ia-audit --triage-plan triage-plan.json
ia-audit --manual-triage-plan manual-triage.json
ia-audit --apply-triage-plan triage-plan.json
ia-audit --apply-triage-plan triage-plan.json --execute

# Interactive (movies)
ia-easy

# Full TUI
ia-minotaur --check
ia-minotaur
```

Inside `ia-minotaur`, press `y` for a lightweight library audit summary in the status bar. For full details or fixes, use `ia-audit`.

## Media root

The download tools default to `/mnt/ssd/media`. Set `IA_MEDIA_ROOT` to choose a different default for the tools:

```
export IA_MEDIA_ROOT=/mnt/ssd/media
```

You can also create a read-only JSON config at `~/.config/archive-downloader/config.json`, or set `IA_CONFIG_PATH` to another file:

```
{
  "media_root": "/mnt/ssd/media",
  "default_bucket": "TV",
  "default_filter": "movies",
  "default_sort": "",
  "title_only": false,
  "license_gate": false,
  "no_change_timestamp": true,
  "rows_per_page": 30,
  "radarr_enabled": false,
  "radarr_url": "http://192.168.86.70:7878",
  "radarr_local_movie_root": "/mnt/ssd/media/Movies",
  "radarr_root_folder": "/path/radarr/sees/as/movies",
  "radarr_quality_profile_id": 1,
  "radarr_monitor_movie": true,
  "radarr_search_on_add": false,
  "radarr_timeout_s": 10
}
```

Environment variables override the config file: `IA_MEDIA_ROOT`, `IA_DEFAULT_BUCKET`, `IA_DEFAULT_FILTER`, `IA_DEFAULT_SORT`, `IA_TITLE_ONLY`, `IA_LICENSE_GATE`, `IA_NO_CHANGE_TIMESTAMP`, `IA_ROWS_PER_PAGE`, `IA_RADARR_ENABLED`, `IA_RADARR_URL`, `IA_RADARR_API_KEY`, `RADARR_API_KEY`, `IA_RADARR_LOCAL_MOVIE_ROOT`, `IA_RADARR_ROOT_FOLDER`, `IA_RADARR_QUALITY_PROFILE_ID`, `IA_RADARR_MONITOR_MOVIE`, and `IA_RADARR_TIMEOUT_S`.

`ia-minotaur` can ask Jellyfin to rescan libraries when you quit after a successful import. Set `JELLYFIN_URL` and `JELLYFIN_API_KEY` in the environment that launches the downloader:

```
export JELLYFIN_URL=http://127.0.0.1:8096
export JELLYFIN_API_KEY=your-api-key
```

Use `ia-config` to manage the same file without hand-editing JSON:

```
ia-config path
ia-config show
ia-config set media_root /mnt/ssd/media
ia-config set default_bucket Movies
ia-config set default_filter movies
ia-config set default_sort "downloads desc"
ia-config set title_only false
ia-config set license_gate true
ia-config set no_change_timestamp true
ia-config set rows_per_page 30
```

## Radarr registration

`ia-minotaur` can optionally register completed movie imports with Radarr so Bazarr can see them through Radarr synchronization. This runs only after a movie file has been renamed and moved into its final `Movies/Title (Year)/` folder. It does not run for TV, Music, Other, staged files, or failed imports.

Radarr registration is disabled by default. Enable it only after checking the Radarr-visible path and quality profile:

```
ia-config set radarr_enabled true
ia-config set radarr_url http://192.168.86.70:7878
ia-config set radarr_local_movie_root /mnt/ssd/media/Movies
ia-config set radarr_root_folder /path/radarr/sees/as/movies
ia-config set radarr_quality_profile_id 1
export RADARR_API_KEY=your-api-key
```

Find the API key in Radarr under Settings -> General -> Security. Find valid root folders and quality profiles with:

```
ia-radarr roots
ia-radarr profiles
```

Because Radarr runs on kassny while downloads run on minotaur, paths must be mapped explicitly. `radarr_local_movie_root` is the minotaur path, usually `/mnt/ssd/media/Movies`. `radarr_root_folder` must be the matching Movies root exactly as Radarr reports it. Check a mapping without changing Radarr:

```
ia-radarr map "/mnt/ssd/media/Movies/Metropolis (1927)"
```

Safe read-only checks:

```
ia-radarr check
ia-radarr lookup --title "Metropolis" --year 1927
```

Controlled write test for one already imported movie requires `--apply`; without it the command is a dry run:

```
ia-radarr register "/mnt/ssd/media/Movies/Metropolis (1927)/Metropolis (1927).mp4" --title "Metropolis" --year 1927 --apply
```

Radarr search-on-add remains disabled. The feature adds existing media as monitored for subtitle management, updates stale Radarr paths when safe, and requests a Radarr refresh. It never asks Radarr to download, replace, delete, move, or rename media. Bazarr is not called directly; it should pick up the movie from its normal Radarr sync. If a movie does not appear in Bazarr, verify Radarr shows the movie at the expected path, run `ia-radarr check`, confirm Bazarr's Radarr connection, then trigger Bazarr's Radarr sync from Bazarr.

`ia-minotaur` organizes downloads into buckets under the shared media root:

```
ia-minotaur
```

On-disk layout:

```
$IA_MEDIA_ROOT/
  TV/               # organized by show name
  Movies/           # organized by "Title (Year)"
  Music/            # organized by artist
  Other/            # everything else
  .ia_staging/      # partial / in-progress downloads
  .ia_favorites.json
  .ia_dl.log
```

Destination paths are validated against the media root before any file is moved, so user-entered folder names containing `..` are rejected.

If a Minotaur file download stalls, the stalled `ia` process is killed and retried automatically up to two times. Completed staged files are imported without redownloading; after the retry limit, pending state is saved so `R` can resume later.

## Running tests

```
python3 -m pip install -e ".[dev]"
pytest tests/ -v
```

The suite covers pure helpers in `ia_common.py` and `ia_minotaur.py` (filename parsing, query building, license gating, path safety). Tests do not hit the real Internet Archive API and do not require a TTY.

`ia-audit --probe` uses `ffprobe` if available; without it, codec and bitrate sections stay empty and the rest of the audit still works.

The cleanup section is conservative by design and does not treat normal subtitle sidecars such as `.srt` as junk.

`ia-audit --rename-plan <file>` writes a JSON array of proposed in-place renames with source path, destination path, and collision flags. It does not rename anything.

`ia-audit --apply-rename-plan <file>` reads a previously reviewed rename plan and validates it against the current filesystem. By default it emits a JSON dry-run report; add `--execute` to perform only the non-blocked renames.

`ia-audit --triage-plan <file>` writes a JSON move plan for higher-confidence `Other` and hidden-bucket reclassification suggestions. Low-confidence items stay in the text/JSON report for manual review but are not added to the move plan.

`ia-audit --manual-triage-plan <file>` interactively walks the low-confidence triage items, lets you choose bucket/folder/name, and writes a reviewable move plan JSON. It does not move anything by itself.

`ia-audit --apply-triage-plan <file>` reads a previously reviewed triage move plan and validates it against the current filesystem. By default it emits a JSON dry-run report; add `--execute` to perform only the non-blocked moves.

## License gating disclaimer

`ia_minotaur.py` checks item metadata before downloads. By default, unclear rights show a warning and require an extra confirmation; turning on **License gate** blocks downloads unless metadata clearly indicates an open license (Creative Commons, public domain, CC-BY, etc.). **This is a best-effort heuristic based on the `licenseurl` and `rights` fields of Internet Archive metadata — it is not legal advice.** You are responsible for confirming the actual rights status of anything you download and for respecting those rights.

The other two tools (`ia_dl.py`, `ia_easy.py`) do not perform any license check. Use them accordingly.
