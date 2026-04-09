# archive-downloader

Three Python wrappers around the [`internetarchive`](https://archive.org/developers/internetarchive/) (`ia`) CLI. Pick the one that fits your workflow.

## The tools

**`ia_dl.py`** — scriptable argparse CLI. Subcommands: `search`, `list`, `download`. Good for shell scripts and one-off invocations.

**`ia_easy.py`** — interactive `input()`-based flow optimized for finding and grabbing a single movie file. Honors `$XDG_DOWNLOAD_DIR` (falls back to `~/Downloads`).

**`ia_minotaur.py`** — full-screen curses TUI with favorites, open-license gating, staged downloads, live progress, and automatic bucket organization (`TV/`, `Movies/`, `Music/`, `Other/`). The heaviest of the three.

All three share `ia_common.py` for subprocess handling, the `SearchResult` / `IAFile` dataclasses, and small utilities.

## Install

```
pip install -r requirements.txt
```

You also need two binaries on `PATH`:

- `ia` — installed by `pip install internetarchive` (listed in `requirements.txt`). Run `ia configure` once if you want to use an Internet Archive account.
- `curl` — used by `ia_minotaur.py` to hit `advancedsearch.php` for paginated search. Usually pre-installed on Linux / macOS.

## Quick start

```
# Scriptable
python3 ia_dl.py search 'title:"The Big Movie" AND mediatype:movies' --rows 5
python3 ia_dl.py list <identifier> --ext mp4
python3 ia_dl.py download <identifier> --biggest --dest ./out

# Interactive (movies)
python3 ia_easy.py

# Full TUI
python3 ia_minotaur.py
```

## `ia_minotaur.py` media root

`ia_minotaur.py` organizes downloads into buckets under a single media root. The location is configurable via the `IA_MEDIA_ROOT` environment variable, defaulting to `~/Media`:

```
export IA_MEDIA_ROOT=/mnt/ssd/media
python3 ia_minotaur.py
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
pip install -r requirements-dev.txt
pytest tests/ -v
```

The suite covers pure helpers in `ia_common.py` and `ia_minotaur.py` (filename parsing, query building, license gating, path safety). Tests do not hit the real Internet Archive API and do not require a TTY.

## License gating disclaimer

`ia_minotaur.py` will refuse to download items whose metadata doesn't clearly indicate an open license (Creative Commons, public domain, CC-BY, etc.). **This is a best-effort heuristic based on the `licenseurl` and `rights` fields of Internet Archive metadata — it is not legal advice.** You are responsible for confirming the actual rights status of anything you download and for respecting those rights.

The other two tools (`ia_dl.py`, `ia_easy.py`) do not perform any license check. Use them accordingly.
