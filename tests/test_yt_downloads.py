"""Tests for yt-dlp download command construction."""
import yt_downloads


def test_single_video_download_cmd_uses_staging_and_mp4_preferences(monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    monkeypatch.setattr(yt_downloads, "STAGING_ROOT", str(staging))

    cmd = yt_downloads.single_video_download_cmd(
        "/custom/yt-dlp",
        "https://www.youtube.com/watch?v=abc123",
        "yt-abc123",
    )

    assert cmd[0] == "/custom/yt-dlp"
    assert cmd == [
        "/custom/yt-dlp",
        "-f",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "-P",
        str(staging),
        "-o",
        "yt-abc123/%(title).180B [%(id)s].%(ext)s",
        "--no-playlist",
        "https://www.youtube.com/watch?v=abc123",
    ]


def test_display_filename_includes_video_id_and_mp4_extension():
    assert yt_downloads.display_filename("A/B Video", "abc123") == "A B Video [abc123].mp4"
