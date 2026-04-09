"""Tests for the pure, importable helpers in ia_minotaur.py.

We only test functions that can run without a real curses terminal and
without touching the user's media root. conftest.py sets IA_MEDIA_ROOT
to a temp path before this module is imported.
"""
import ia_minotaur
from ia_minotaur import (
    auto_clean_movie_folder_name,
    build_query,
    detect_sxxeyy,
    is_openly_licensed,
    sanitize_folder,
)


# -------------------------------------------------------- env override smoke
def test_media_root_respects_env():
    # conftest sets IA_MEDIA_ROOT=/tmp/ia-test-root before import.
    assert ia_minotaur.MEDIA_ROOT == "/tmp/ia-test-root"
    assert ia_minotaur.STAGING_ROOT == "/tmp/ia-test-root/.ia_staging"
    assert ia_minotaur.BUCKET_TV == "/tmp/ia-test-root/TV"
    assert ia_minotaur.BUCKET_MOVIES == "/tmp/ia-test-root/Movies"
    assert ia_minotaur.LOG_PATH == "/tmp/ia-test-root/.ia_dl.log"


# --------------------------------------------------------- sanitize_folder
class TestSanitizeFolder:
    def test_normal_title_passthrough(self):
        assert sanitize_folder("Normal Title") == "Normal Title"

    def test_strips_path_separators(self):
        # Every char in /\:*?"<>| should be removed.
        assert sanitize_folder('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"

    def test_collapses_whitespace(self):
        assert sanitize_folder("  hello   world  ") == "hello world"

    def test_empty_becomes_unknown(self):
        assert sanitize_folder("") == "Unknown"

    def test_none_becomes_unknown(self):
        assert sanitize_folder(None) == "Unknown"

    def test_dotdot_survives_sanitization(self):
        # Pinning: sanitize_folder does NOT strip "..". The path traversal
        # guard in choose_bucket_and_path (using safe_path_under) is what
        # blocks escape. If a future refactor moves the check here, this
        # test will catch it and should be updated deliberately.
        assert sanitize_folder("..") == ".."

    def test_only_separators_becomes_unknown(self):
        # After stripping, nothing is left, so the empty-path branch fires.
        assert sanitize_folder("////\\\\") == "Unknown"


# ------------------------------------------------------------- detect_sxxeyy
class TestDetectSxxEyy:
    def test_s01e05(self):
        assert detect_sxxeyy("Show.S01E05.mkv") == (1, 5)

    def test_lowercase(self):
        assert detect_sxxeyy("s1e1") == (1, 1)

    def test_two_digit(self):
        assert detect_sxxeyy("S99E99") == (99, 99)

    def test_no_match(self):
        assert detect_sxxeyy("random filename") is None

    def test_empty(self):
        assert detect_sxxeyy("") is None

    def test_none_safe(self):
        assert detect_sxxeyy(None) is None

    def test_incomplete_pattern(self):
        assert detect_sxxeyy("S1E") is None

    def test_placeholder_letters(self):
        assert detect_sxxeyy("SxxEyy") is None


# ------------------------------------------------ auto_clean_movie_folder_name
class TestAutoCleanMovieFolderName:
    def test_title_with_year_passthrough(self):
        assert (
            auto_clean_movie_folder_name("The Big Movie (1999)", "bigmovie.mp4")
            == "The Big Movie (1999)"
        )

    def test_strips_scene_tags_from_filename(self):
        # Falls back to filename when title is blank, then strips scene tags
        # and extracts the year.
        result = auto_clean_movie_folder_name(
            "", "The.Big.Movie.1999.1080p.BluRay.x264-YIFY.mkv"
        )
        assert result == "The Big Movie (1999)"

    def test_plain_filename_no_year(self):
        assert (
            auto_clean_movie_folder_name("", "Plain Filename.mp4") == "Plain Filename"
        )

    def test_title_only(self):
        assert auto_clean_movie_folder_name("A Title", "") == "A Title"

    def test_inception_golden(self):
        assert (
            auto_clean_movie_folder_name("", "Inception.2010.1080p.mkv")
            == "Inception (2010)"
        )


# ---------------------------------------------------------------- build_query
class TestBuildQuery:
    def test_simple_words_with_media_filter(self):
        assert build_query("foo", "movies", False) == "foo AND mediatype:movies"

    def test_title_only_wraps_in_title_clause(self):
        assert (
            build_query("foo", "movies", True)
            == 'title:("foo") AND mediatype:movies'
        )

    def test_any_filter_adds_nothing(self):
        assert build_query("foo", "any", False) == "foo"

    def test_advanced_syntax_passthrough(self):
        # If the query already looks advanced (uses title:/mediatype:/AND/OR),
        # build_query must not wrap or append — the user knows what they want.
        q = 'title:"foo" AND mediatype:audio'
        assert build_query(q, "movies", True) == q

    def test_empty_input(self):
        assert build_query("", "movies", False) == ""


# ----------------------------------------------------------- is_openly_licensed
class TestIsOpenlyLicensed:
    def test_cc_by_url_allows(self):
        meta = {
            "metadata": {
                "licenseurl": "https://creativecommons.org/licenses/by/4.0/",
                "rights": "",
            }
        }
        ok, _ = is_openly_licensed(meta)
        assert ok

    def test_public_domain_allows(self):
        ok, _ = is_openly_licensed({"metadata": {"licenseurl": "", "rights": "Public Domain"}})
        assert ok

    def test_cc_by_rights_allows(self):
        ok, _ = is_openly_licensed({"metadata": {"licenseurl": "", "rights": "CC-BY"}})
        assert ok

    def test_negated_public_domain_denies(self):
        # The iteration-1 fix: "not in the public domain" must NOT allow
        # just because the phrase "public domain" appears.
        ok, why = is_openly_licensed(
            {"metadata": {"licenseurl": "", "rights": "Not in the public domain"}}
        )
        assert not ok
        assert "negated" in why.lower() or "not in the public domain" in why.lower()

    def test_all_rights_reserved_denies(self):
        ok, _ = is_openly_licensed(
            {"metadata": {"licenseurl": "", "rights": "All rights reserved"}}
        )
        assert not ok

    def test_empty_rights_denies(self):
        ok, _ = is_openly_licensed({"metadata": {"licenseurl": "", "rights": ""}})
        assert not ok

    def test_empty_metadata_denies(self):
        ok, _ = is_openly_licensed({"metadata": {}})
        assert not ok

    def test_missing_metadata_key_denies(self):
        # Defensive: feed an entirely empty dict (no "metadata" key at all).
        # Must not crash, must deny.
        ok, _ = is_openly_licensed({})
        assert not ok
