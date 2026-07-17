"""Tests for DVD ISO scan parsing and classification."""
import subprocess

import ia_dvd


def test_parse_lsdvd_titles_and_classifies_episode_titles():
    text = "\n".join(
        [
            "Title: 01, Length: 00:24:10.000 Chapters: 5, Cells: 5, Audio streams: 1, Subpictures: 0",
            "Title: 02, Length: 00:23:50.000 Chapters: 4, Cells: 4, Audio streams: 1, Subpictures: 0",
        ]
    )

    titles = ia_dvd.parse_lsdvd_titles(text)

    assert [t.title for t in titles] == [1, 2]
    assert titles[0].seconds == 1450
    assert ia_dvd.classify_dvd_layout(titles)[0] == "one_title_per_episode"


def test_parse_handbrake_titles_and_classifies_long_chapter_title():
    text = """
+ title 1:
  + duration: 01:45:00
  + chapters: 8
"""

    titles = ia_dvd.parse_handbrake_titles(text)

    assert len(titles) == 1
    assert titles[0].chapters == 8
    assert ia_dvd.classify_dvd_layout(titles)[0] == "one_long_title_split_by_chapters"


def test_scan_dvd_iso_dry_run_does_not_call_runner(tmp_path):
    iso = tmp_path / "Disc One.ISO"
    iso.write_bytes(b"")

    def fail_runner(*_args, **_kwargs):
        raise AssertionError("runner should not be called in dry-run")

    result = ia_dvd.scan_dvd_iso(str(iso), dry_run=True, runner=fail_runner)

    assert result.ok is True
    assert result.dry_run is True
    assert result.layout == "dry_run"
    assert result.logs_dir.endswith("_dvd_logs/Disc_One")


def test_scan_dvd_iso_writes_logs_and_analysis(tmp_path):
    iso = tmp_path / "Disc One.ISO"
    iso.write_bytes(b"iso")

    def runner(cmd, stdout=None, stderr=None, text=None, timeout=None):
        if cmd[0] == "lsdvd":
            return subprocess.CompletedProcess(cmd, 0, "Title: 01, Length: 00:24:00.000 Chapters: 4\n")
        return subprocess.CompletedProcess(cmd, 0, "+ title 1:\n  + duration: 00:24:00\n  + chapters: 4\n")

    result = ia_dvd.scan_dvd_iso(str(iso), runner=runner)

    assert result.ok is True
    assert result.layout == "manual_review"
    assert (tmp_path / "_dvd_logs" / "Disc_One" / "lsdvd.txt").exists()
    assert (tmp_path / "_dvd_logs" / "Disc_One" / "handbrake-scan.txt").exists()
    assert (tmp_path / "_dvd_logs" / "Disc_One" / "analysis.json").exists()
