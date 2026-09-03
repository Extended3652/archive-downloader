"""Tests for deterministic local relevance ranking of IA search results."""
import ia_ranking
from ia_common import SearchResult


def make(identifier, title, **kw):
    return SearchResult(identifier=identifier, title=title, **kw)


def order(query, results, media_filter="any"):
    ranked = ia_ranking.rerank(results, query, media_filter)
    return [r.identifier for r in ranked]


def test_exact_title_beats_metadata_only_match():
    exact = make("exact", "Toad Road")
    meta = make(
        "meta",
        "A Completely Different Film",
        description="Filmed on a dusty toad road in summer",
        downloads=9999,
    )
    assert order("Toad Road", [meta, exact]) == ["exact", "meta"]


def test_title_with_year_qualifier_ranks_very_high():
    qualified = make("q2012", "Toad Road (2012)")
    unrelated = make("kart", "Mario Kart Rainbow Road", downloads=50000)
    ranked = order("Toad Road", [unrelated, qualified])
    assert ranked[0] == "q2012"
    # The qualified title should score in the top tiers, close to an exact match.
    assert ia_ranking.score_result(qualified, "Toad Road") >= ia_ranking.TIER_EXACT_QUALIFIED


def test_capitalization_does_not_matter():
    a = make("a", "TOAD ROAD")
    b = make("b", "toad road")
    assert ia_ranking.score_result(a, "Toad Road") == ia_ranking.score_result(b, "toad road")
    assert ia_ranking.score_result(a, "toad road") >= ia_ranking.TIER_EXACT


def test_punctuation_and_separator_differences_do_not_matter():
    dotted = make("dot", "Toad.Road")
    hyphen = make("hy", "Toad-Road")
    apos = make("ap", "Rock'n'Road")
    assert ia_ranking.score_result(dotted, "toad road") >= ia_ranking.TIER_EXACT
    assert ia_ranking.score_result(hyphen, "toad road") >= ia_ranking.TIER_EXACT
    assert ia_ranking.score_result(apos, "rocknroad") >= ia_ranking.TIER_EXACT


def test_query_year_favors_matching_year():
    right = make("right", "Toad Road", year="2012")
    wrong = make("wrong", "Toad Road", year="1999")
    ranked = order("Toad Road 2012", [wrong, right])
    assert ranked == ["right", "wrong"]


def test_query_year_matches_against_date_field():
    right = make("right", "Toad Road", date="2012-05-01")
    wrong = make("wrong", "Toad Road", date="2001-01-01")
    assert order("Toad Road (2012)", [wrong, right]) == ["right", "wrong"]


def test_all_title_words_beat_description_only_match():
    all_words = make("words", "The Toad and the Road")
    desc_only = make(
        "desc",
        "Unrelated Feature",
        description="a story involving a toad on a country road",
        downloads=8000,
    )
    assert order("toad road", [desc_only, all_words]) == ["words", "desc"]


def test_unrelated_high_download_item_does_not_outrank_exact_title():
    exact = make("exact", "Toad Road", downloads=1)
    popular = make("popular", "Super Mario 64 100%", downloads=500000, description="road toad")
    assert order("Toad Road", [popular, exact])[0] == "exact"


def test_prefix_match_beats_scattered_word_match():
    prefix = make("prefix", "Toad Road: The Director's Cut")
    scattered = make("scatter", "The Road Less Traveled by a Toad")
    ranked = order("Toad Road", [scattered, prefix])
    assert ranked[0] == "prefix"


def test_rerank_is_stable_for_equal_scores():
    a = make("a", "Toad Road")
    b = make("b", "Toad Road")
    # Equal score and equal downloads -> original order preserved.
    assert order("Toad Road", [a, b]) == ["a", "b"]
    assert order("Toad Road", [b, a]) == ["b", "a"]


def test_empty_query_returns_original_order():
    a = make("a", "One")
    b = make("b", "Two")
    assert order("", [a, b]) == ["a", "b"]


def test_normalize_and_extract_year():
    assert ia_ranking.normalize("  Toad__Road!!  ") == "toad road"
    assert ia_ranking.normalize("Don't Look-Now") == "dont look now"
    assert ia_ranking.extract_year("Toad Road (2012)") == "2012"
    assert ia_ranking.extract_year("no year here") == ""
