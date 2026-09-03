"""Tests for ia_common — the shared helpers module."""
import os
import subprocess
import tempfile

import pytest

from ia_common import (
    DEFAULT_MEDIA_ROOT,
    IACommandError,
    IAFile,
    IANotInstalled,
    SearchResult,
    compact_count,
    deduplicate_file_variants,
    default_media_root,
    human_size,
    is_video_file,
    run,
    safe_path_under,
)


def test_deduplicate_file_variants_prefers_explicit_original():
    original = IAFile(
        "foo.mp4", 10, "MPEG4", source="original", sha1="original-hash"
    )
    derivative = IAFile(
        "foo.ia.mp4",
        10,
        "h.264 IA",
        source="derivative",
        original="foo.mp4",
        sha1="derivative-hash",
    )

    logical = deduplicate_file_variants([derivative, original])

    assert [f.name for f in logical] == ["foo.mp4"]
    assert logical[0].variant_names == ("foo.mp4", "foo.ia.mp4")
    assert [m.get("source") for m in logical[0].variant_metadata] == ["original", "derivative"]


def test_deduplicate_file_variants_keeps_uncertain_hash_mismatch():
    files = [
        IAFile("foo.mp4", 10, "MPEG4", sha1="original-hash"),
        IAFile("foo.ia.mp4", 10, "h.264 IA", sha1="different-hash"),
    ]

    assert [f.name for f in deduplicate_file_variants(files)] == ["foo.mp4", "foo.ia.mp4"]


def test_deduplicate_file_variants_keeps_unpaired_extra():
    files = [
        IAFile("foo.mp4", 10, "MPEG4"),
        IAFile("The Critic Webisodes.mp4", 20, "MPEG4"),
    ]

    assert [f.name for f in deduplicate_file_variants(files)] == [
        "foo.mp4", "The Critic Webisodes.mp4"
    ]


# ---------------------------------------------------------------- human_size
class TestDefaultMediaRoot:
    def test_default_without_env(self, monkeypatch):
        monkeypatch.delenv("IA_MEDIA_ROOT", raising=False)
        monkeypatch.setenv("IA_CONFIG_PATH", "/__missing_archive_downloader_config__.json")
        assert default_media_root() == DEFAULT_MEDIA_ROOT

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("IA_CONFIG_PATH", "/__missing_archive_downloader_config__.json")
        monkeypatch.setenv("IA_MEDIA_ROOT", "/tmp/custom-media")
        assert default_media_root() == "/tmp/custom-media"

    def test_env_override_expands_user(self, monkeypatch):
        monkeypatch.setenv("IA_CONFIG_PATH", "/__missing_archive_downloader_config__.json")
        monkeypatch.setenv("IA_MEDIA_ROOT", "~/custom-media")
        assert default_media_root() == os.path.expanduser("~/custom-media")


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


class TestCompactCount:
    def test_small_count(self):
        assert compact_count(999) == "999"

    def test_thousands(self):
        assert compact_count(1200) == "1.2K"
        assert compact_count(12000) == "12K"

    def test_millions(self):
        assert compact_count(1_250_000) == "1.2M"

    def test_invalid(self):
        assert compact_count("bad") == "?"


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
        assert sr.mediatype == ""
        assert sr.downloads == 0

    def test_iafile_defaults(self):
        f = IAFile(name="a.mp4", size=100)
        assert f.fmt == ""
