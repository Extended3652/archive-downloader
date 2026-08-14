import os
import stat

import ia_easy


def test_download_file_normalizes_direct_download_tree(monkeypatch, tmp_path):
    def fake_run(_cmd, check=True):
        item_dir = tmp_path / "item1"
        item_dir.mkdir()
        output = item_dir / "movie.mp4"
        output.write_bytes(b"movie")
        item_dir.chmod(0o2755)
        output.chmod(0o644)

    monkeypatch.setattr(ia_easy, "run", fake_run)
    monkeypatch.setattr(ia_easy, "emit_archive_started", lambda _message: None)
    monkeypatch.setattr(ia_easy, "emit_archive_completed", lambda _message: None)
    monkeypatch.setattr(ia_easy, "emit_archive_failed", lambda _message: None)

    ia_easy.download_file("item1", "movie.mp4", str(tmp_path))

    assert stat.S_IMODE(os.stat(tmp_path / "item1").st_mode) == 0o2775
    assert stat.S_IMODE(os.stat(tmp_path / "item1" / "movie.mp4").st_mode) == 0o664


def test_cli_main_sets_process_umask(monkeypatch):
    calls = []
    monkeypatch.setattr(ia_easy, "set_process_umask", lambda: calls.append("umask"))
    monkeypatch.setattr(ia_easy, "main", lambda: 0)

    assert ia_easy.cli_main([]) == 0
    assert calls == ["umask"]
