"""Tests for pure organizing, query, and license helpers."""
from ia_organize import (
    auto_clean_movie_folder_name,
    build_collection_search_query,
    build_field_query,
    build_fielded_query,
    build_query,
    build_query_attempts,
    build_within_collection_query,
    detect_sxxeyy,
    is_openly_licensed,
    license_status_from_fields,
    looks_like_identifier,
    looks_like_advanced_query,
    normalize_collection_identifier,
    sanitize_folder,
    split_title_year,
)


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
        # The path traversal guard in ia_paths blocks escape.
        assert sanitize_folder("..") == ".."

    def test_only_separators_becomes_unknown(self):
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
        assert build_query("foo", "movies", False) == '(title:("foo") OR foo) AND mediatype:movies'

    def test_title_only_wraps_in_title_clause(self):
        assert (
            build_query("foo", "movies", True)
            == 'title:("foo") AND mediatype:movies'
        )

    def test_any_filter_adds_nothing(self):
        assert build_query("foo", "any", False) == '(title:("foo") OR foo)'

    def test_advanced_syntax_passthrough(self):
        q = 'title:"foo" AND mediatype:audio'
        assert build_query(q, "movies", True) == q

    def test_creator_advanced_syntax_passthrough(self):
        q = "creator:(Chaplin)"
        assert build_query(q, "movies", False) == q

    def test_year_advanced_syntax_passthrough(self):
        q = 'title:"foo" AND year:1930'
        assert build_query(q, "movies", False) == q

    def test_title_year_at_end(self):
        assert (
            build_query("Metropolis 1927", "movies", False)
            == '(title:("Metropolis") OR Metropolis) AND year:1927 AND mediatype:movies'
        )

    def test_title_year_in_parentheses(self):
        assert (
            build_query("The General (1926)", "movies", False)
            == '(title:("The General") OR The General) AND year:1926 AND mediatype:movies'
        )

    def test_title_year_at_start(self):
        assert (
            build_query("1927 Metropolis", "movies", False)
            == '(title:("Metropolis") OR Metropolis) AND year:1927 AND mediatype:movies'
        )

    def test_title_only_with_year(self):
        assert (
            build_query("Metropolis 1927", "movies", True)
            == 'title:("Metropolis") AND year:1927 AND mediatype:movies'
        )

    def test_collapses_whitespace(self):
        assert build_query("  foo   bar  ", "any", False) == '(title:("foo bar") OR foo bar)'

    def test_empty_input(self):
        assert build_query("", "movies", False) == ""

    def test_identifier_like_query_gets_identifier_attempt_first(self):
        attempts = build_query_attempts("prelinger-123", "movies", False)

        assert attempts[0] == ("identifier", "identifier:prelinger-123")
        assert any(label == "fields" for label, _query in attempts)

    def test_fielded_query_searches_ia_metadata_fields(self):
        assert build_fielded_query("Chaplin", "movies") == (
            '(title:("Chaplin") OR creator:("Chaplin") OR subject:("Chaplin") '
            'OR description:("Chaplin") OR Chaplin) AND mediatype:movies'
        )

    def test_collection_search_query_targets_collection_items(self):
        assert build_collection_search_query("jazz") == (
            '(title:("jazz") OR subject:("jazz") OR description:("jazz") OR jazz) AND mediatype:collection'
        )

    def test_within_collection_query_uses_collection_identifier(self):
        assert (
            build_within_collection_query("train", "prelinger", "movies", False)
            == '((title:("train") OR train) AND mediatype:movies) AND collection:prelinger'
        )

    def test_field_query_uses_selected_field(self):
        assert build_field_query("creator", "Charlie Chaplin", "movies") == 'creator:("Charlie Chaplin") AND mediatype:movies'


class TestQueryHelpers:
    def test_looks_like_advanced_query(self):
        assert looks_like_advanced_query("creator:(Chaplin)")
        assert looks_like_advanced_query("year:1930")
        assert looks_like_advanced_query("foo AND bar")
        assert not looks_like_advanced_query("foo bar")

    def test_looks_like_identifier(self):
        assert looks_like_identifier("prelinger-123")
        assert not looks_like_identifier("prelinger 123")

    def test_normalize_collection_identifier_uses_first_identifier(self):
        assert normalize_collection_identifier("prelinger, feature_films") == "prelinger"

    def test_split_title_year(self):
        assert split_title_year("Metropolis 1927") == ("Metropolis", "1927")
        assert split_title_year("The General (1926)") == ("The General", "1926")
        assert split_title_year("1927 Metropolis") == ("Metropolis", "1927")
        assert split_title_year("No Year") == ("No Year", "")


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
        ok, _ = is_openly_licensed({})
        assert not ok


class TestLicenseStatusFromFields:
    def test_open(self):
        status, _ = license_status_from_fields("https://creativecommons.org/licenses/by/4.0/", "")
        assert status == "open"

    def test_blocked(self):
        status, _ = license_status_from_fields("", "All rights reserved")
        assert status == "blocked"

    def test_unknown(self):
        status, _ = license_status_from_fields("", "")
        assert status == "unknown"

    def test_unclear(self):
        status, _ = license_status_from_fields("", "Some custom rights note")
        assert status == "unclear"
