"""Tests for ia_common — the shared helpers module."""
import os
import subprocess
import tempfile

import pytest

from ia_common import (
    IACommandError,
    IAFile,
    IANotInstalled,
    SearchResult,
    human_size,
    is_video_file,
    run,
    safe_path_under,
)


# ---------------------------------------------------------------- human_size
class TestHumanSize:
    def test_zero(self):
        assert human_size(0) == "0B"

    def test_just_under_kb(self):
        assert human_size(1023) == "1023B"

    def test_exactly_kb(self):
        assert human_size(1024) == "1.00KB"

    def test_mb(self):
        assert human_size(5 * 1024 * 1024) == "5.00MB"

    def test_gb(self):
        assert human_size(1024 ** 3) == "1.00GB"

    def test_gb_fractional(self):
        assert human_size(int(1.5 * 1024 ** 3)) == "1.50GB"

    def test_tb(self):
        assert human_size(1024 ** 4) == "1.00TB"

    def test_past_tb_stays_in_tb(self):
        # Current behavior: units table tops out at TB, so anything larger
        # just shows as an ever-growing "N.NNTB". Pin this so a later refactor
        # that adds PB/EB has to update the test deliberately.
        assert human_size(1024 ** 5).endswith("TB")

    def test_none(self):
        assert human_size(None) == "?"

    def test_non_numeric_string(self):
        assert human_size("garbage") == "?"

    def test_numeric_string(self):
        # int("1024") works, so a numeric string goes through the normal path.
        assert human_size("1024") == "1.00KB"

    def test_negative_stays_in_bytes(self):
        # The scaling loop only steps up while f >= 1024, so negatives
        # never leave the B unit. Document current behavior.
        assert human_size(-1) == "-1B"
        assert human_size(-2048) == "-2048B"


# ------------------------------------------------------------- is_video_file
class TestIsVideoFile:
    def test_mp4_extension(self):
        assert is_video_file("movie.mp4")

    def test_mkv_uppercase(self):
        assert is_video_file("movie.MKV")

    def test_webm(self):
        assert is_video_file("clip.webm")

    def test_text_file(self):
        assert not is_video_file("readme.txt")

    def test_no_extension_no_format(self):
        assert not is_video_file("some_random_file", "")

    def test_format_hint_matroska(self):
        assert is_video_file("data.bin", "Matroska")

    def test_format_hint_h264(self):
        assert is_video_file("data.bin", "h.264")

    def test_unrelated_format(self):
        assert not is_video_file("data.bin", "text")

    def test_empty_everything(self):
        assert not is_video_file("", "")

    def test_none_safe(self):
        # Defensive: the helper is called on metadata that may have None values.
        assert not is_video_file(None, None)


# ------------------------------------------------------------ safe_path_under
class TestSafePathUnder:
    def test_nested_is_under(self):
        with tempfile.TemporaryDirectory() as root:
            assert safe_path_under(root, os.path.join(root, "a", "b"))

    def test_dotdot_resolves_inside(self):
        with tempfile.TemporaryDirectory() as root:
            assert safe_path_under(root, os.path.join(root, "a", "..", "b"))

    def test_dotdot_escapes(self):
        with tempfile.TemporaryDirectory() as root:
            escape = os.path.join(root, "..", "etc", "passwd")
            assert not safe_path_under(root, escape)

    def test_absolute_outside(self):
        with tempfile.TemporaryDirectory() as root:
            assert not safe_path_under(root, "/etc/passwd")

    def test_same_dir_is_under_itself(self):
        with tempfile.TemporaryDirectory() as root:
            assert safe_path_under(root, root)

    def test_nonexistent_candidate_under_nonexistent_root(self):
        # realpath on a nonexistent path passes it through unchanged, so
        # lexically nested paths are still "under" the root.
        assert safe_path_under("/__no_such_root__", "/__no_such_root__/x")

    def test_different_roots(self):
        with tempfile.TemporaryDirectory() as root_a:
            with tempfile.TemporaryDirectory() as root_b:
                assert not safe_path_under(root_a, os.path.join(root_b, "file"))


# ------------------------------------------------------------------ run()
class TestRun:
    def test_echo_succeeds(self):
        r = run(["echo", "hi"])
        assert isinstance(r, subprocess.CompletedProcess)
        assert r.returncode == 0
        assert "hi" in r.stdout

    def test_false_raises_iacommanderror(self):
        with pytest.raises(IACommandError) as excinfo:
            run(["false"])
        assert excinfo.value.returncode == 1
        assert excinfo.value.cmd == ["false"]

    def test_missing_binary_raises_ianotinstalled(self):
        with pytest.raises(IANotInstalled) as excinfo:
            run(["__definitely_not_a_real_binary_xyz__"])
        assert excinfo.value.returncode == 127

    def test_check_false_returns_nonzero(self):
        # With check=False, a non-zero exit should not raise.
        r = run(["false"], check=False)
        assert r.returncode == 1


# ----------------------------------------------------- exception hierarchy
class TestExceptions:
    def test_ianotinstalled_is_iacommanderror_subclass(self):
        assert issubclass(IANotInstalled, IACommandError)

    def test_iacommanderror_stores_fields(self):
        e = IACommandError(["ia", "search", "foo"], 2, "boom")
        assert e.cmd == ["ia", "search", "foo"]
        assert e.returncode == 2
        assert e.stderr == "boom"
        # __str__ should include the joined command and the message.
        s = str(e)
        assert "ia search foo" in s
        assert "boom" in s


# ------------------------------------------------------- dataclass defaults
class TestDataclassDefaults:
    def test_searchresult_defaults(self):
        sr = SearchResult(identifier="x", title="y")
        assert sr.year == ""
        assert sr.creator == ""

    def test_iafile_defaults(self):
        f = IAFile(name="a.mp4", size=100)
        assert f.fmt == ""
