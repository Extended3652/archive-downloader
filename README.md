# archive-downloader

Three Python wrappers around the [`internetarchive`](https://archive.org/developers/internetarchive/) (`ia`) CLI. Pick the one that fits your workflow.

## The tools

**`ia_dl.py`** — scriptable argparse CLI. Subcommands: `search`, `list`, `download`. Good for shell scripts and one-off invocations.

**`ia_easy.py`** — interactive `input()`-based flow optimized for finding and grabbing a single movie file. Defaults to `$IA_MEDIA_ROOT` or `/mnt/ssd/media`, and prompts for another folder.

**`ia_minotaur.py`** — full-screen curses TUI with favorites, open-license gating, staged downloads, live progress, and automatic bucket organization (`TV/`, `Movies/`, `Music/`, `Other/`). The heaviest of the three.

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

# Interactive (movies)
ia-easy

# Full TUI
ia-minotaur --check
ia-minotaur
```

## Media root

The download tools default to `/mnt/ssd/media`. Set `IA_MEDIA_ROOT` to choose a different default for all three wrappers:

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
  "rows_per_page": 30
}
```

Environment variables override the config file: `IA_MEDIA_ROOT`, `IA_DEFAULT_BUCKET`, `IA_DEFAULT_FILTER`, `IA_DEFAULT_SORT`, `IA_TITLE_ONLY`, `IA_LICENSE_GATE`, `IA_NO_CHANGE_TIMESTAMP`, and `IA_ROWS_PER_PAGE`.

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

## Running tests

```
python3 -m pip install -e ".[dev]"
pytest tests/ -v
```

The suite covers pure helpers in `ia_common.py` and `ia_minotaur.py` (filename parsing, query building, license gating, path safety). Tests do not hit the real Internet Archive API and do not require a TTY.

## License gating disclaimer

`ia_minotaur.py` checks item metadata before downloads. By default, unclear rights show a warning and require an extra confirmation; turning on **License gate** blocks downloads unless metadata clearly indicates an open license (Creative Commons, public domain, CC-BY, etc.). **This is a best-effort heuristic based on the `licenseurl` and `rights` fields of Internet Archive metadata — it is not legal advice.** You are responsible for confirming the actual rights status of anything you download and for respecting those rights.

The other two tools (`ia_dl.py`, `ia_easy.py`) do not perform any license check. Use them accordingly.
