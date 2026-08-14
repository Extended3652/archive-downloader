"""Tests for download command and staging-size helpers."""
import io
import os
import stat

import ia_downloads
import ia_paths


def test_download_base_args():
    assert ia_downloads.download_base_args(True) == ["--no-change-timestamp"]
    assert ia_downloads.download_base_args(False) == []


def test_single_and_glob_download_cmd_use_staging_root(monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    monkeypatch.setattr(ia_downloads, "STAGING_ROOT", str(staging))

    assert ia_downloads.single_download_cmd("item", "file.mp4", True) == [
        "ia",
        "download",
        "item",
        "file.mp4",
        "--destdir",
        str(staging),
        "--no-change-timestamp",
    ]
    assert ia_downloads.glob_download_cmd("item", "*.mp4", False) == [
        "ia",
        "download",
        "item",
        "--destdir",
        str(staging),
        "--glob",
        "*.mp4",
    ]


def test_safe_getsize_and_dir_total_size(tmp_path):
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a.bin").write_bytes(b"123")
    (nested / "b.bin").write_bytes(b"12345")

    assert ia_downloads.safe_getsize(str(root / "a.bin")) == 3
    assert ia_downloads.safe_getsize(str(root / "missing.bin")) == 0
    assert ia_downloads.dir_total_size(str(root)) == 8


def test_verify_expected_size_accepts_unknown_size():
    assert ia_downloads.verify_expected_size("item", "file.mp4", 0) == (True, "")


def test_verify_expected_size_matches_and_mismatches(monkeypatch, tmp_path):
    root = tmp_path / "media"
    for module in (ia_paths, ia_downloads):
        monkeypatch.setattr(module, "STAGING_ROOT", str(root / ".ia_staging"))

    path = ia_paths.staging_file_path("item", "file.mp4")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"1234")

    assert ia_downloads.verify_expected_size("item", "file.mp4", 4) == (True, "")
    ok, msg = ia_downloads.verify_expected_size("item", "file.mp4", 5)
    assert not ok
    assert "Size mismatch" in msg


def test_verify_expected_size_rejects_escaping_staging_path(monkeypatch, tmp_path):
    root = tmp_path / "media"
    for module in (ia_paths, ia_downloads):
        monkeypatch.setattr(module, "STAGING_ROOT", str(root / ".ia_staging"))

    ok, msg = ia_downloads.verify_expected_size("item", "../../../escape.mp4", 1)

    assert not ok
    assert msg.startswith("Refused: staging path escapes item staging dir:")


def test_normalize_media_permissions_adds_group_write_and_skips_symlinks(tmp_path):
    root = tmp_path / "media"
    media_dir = root / "Movies" / "Movie (1999)"
    media_dir.mkdir(parents=True)
    media_file = media_dir / "Movie (1999).mp4"
    media_file.write_bytes(b"x")
    link = media_dir / "linked.mp4"
    link.symlink_to(media_file)
    media_dir.chmod(0o2755)
    media_file.chmod(0o644)

    ia_paths.normalize_media_permissions(str(media_dir), media_root=str(root), recursive=True)

    assert stat.S_IMODE(media_dir.stat().st_mode) == 0o2775
    assert stat.S_IMODE(media_file.stat().st_mode) == 0o664
    assert link.is_symlink()


def test_normalize_media_permissions_preserves_existing_world_write(tmp_path):
    root = tmp_path / "media"
    media_dir = root / "Movies" / "Writable Movie"
    media_dir.mkdir(parents=True)
    media_file = media_dir / "movie.mp4"
    media_file.write_bytes(b"x")
    media_dir.chmod(0o777)
    media_file.chmod(0o666)

    ia_paths.normalize_media_permissions(str(media_dir), media_root=str(root), recursive=True)

    assert stat.S_IMODE(media_dir.stat().st_mode) == 0o2777
    assert stat.S_IMODE(media_file.stat().st_mode) == 0o666


def test_normalize_media_permissions_refuses_outside_media_root(tmp_path):
    root = tmp_path / "media"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.write_text("x", encoding="utf-8")

    try:
        ia_paths.normalize_media_permissions(str(outside), media_root=str(root))
    except ValueError as exc:
        assert "escapes media root" in str(exc)
    else:
        raise AssertionError("expected outside normalization to be refused")


class FakeProcess:
    def __init__(self, poll_values):
        self.poll_values = list(poll_values)
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        if len(self.poll_values) > 1:
            return self.poll_values.pop(0)
        return self.poll_values[0]

    def wait(self, timeout=2):
        return self.poll()

    def kill(self):
        self.killed = True


def test_run_download_with_progress_reports_success_and_progress():
    proc = FakeProcess([None, 0])
    sizes = iter([5, 10])
    times = iter([0.0, 0.6, 0.7])
    progress = []

    ok, msg = ia_downloads.run_download_with_progress(
        ["ia", "download"],
        target="file.mp4",
        expected_total=10,
        read_written=lambda: next(sizes),
        log_fh=io.StringIO(),
        stall_timeout_s=120,
        is_cancel_requested=lambda: False,
        on_progress=progress.append,
        popen=lambda *args, **kwargs: proc,
        now=lambda: next(times),
        sleep=lambda _seconds: None,
    )

    assert (ok, msg) == (True, "")
    assert [p.written for p in progress] == [5, 10]
    assert progress[-1].target == "file.mp4"
    assert progress[-1].total == 10


def test_run_download_with_progress_reports_nonzero_exit():
    proc = FakeProcess([2])

    ok, msg = ia_downloads.run_download_with_progress(
        ["ia", "download"],
        target="file.mp4",
        expected_total=0,
        read_written=lambda: 0,
        log_fh=io.StringIO(),
        stall_timeout_s=120,
        is_cancel_requested=lambda: False,
        popen=lambda *args, **kwargs: proc,
        now=lambda: 0.0,
        sleep=lambda _seconds: None,
        log_path="/tmp/test.log",
    )

    assert not ok
    assert msg == "download failed (code 2); see log: /tmp/test.log"


def test_run_download_with_progress_can_cancel_running_process():
    proc = FakeProcess([None, None])
    cancel_calls = iter([True, True])

    ok, msg = ia_downloads.run_download_with_progress(
        ["ia", "download"],
        target="file.mp4",
        expected_total=0,
        read_written=lambda: 0,
        log_fh=io.StringIO(),
        stall_timeout_s=120,
        is_cancel_requested=lambda: next(cancel_calls),
        popen=lambda *args, **kwargs: proc,
        now=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert (ok, msg) == (False, "Canceled.")
    assert proc.killed


def test_run_download_with_progress_stops_stalled_download():
    proc = FakeProcess([None])
    times = iter([0.0, 10.0])

    ok, msg = ia_downloads.run_download_with_progress(
        ["ia", "download"],
        target="file.mp4",
        expected_total=10,
        read_written=lambda: 0,
        log_fh=io.StringIO(),
        stall_timeout_s=5,
        is_cancel_requested=lambda: False,
        popen=lambda *args, **kwargs: proc,
        now=lambda: next(times),
        sleep=lambda _seconds: None,
    )

    assert not ok
    assert msg == "Download stalled — no progress for 5s. Try again."
    assert proc.killed
