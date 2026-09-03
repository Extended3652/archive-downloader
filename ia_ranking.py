"""Deterministic local relevance ranking for Internet Archive search results.

Internet Archive's ``advancedsearch.php`` relevance ordering scores documents
on a loose full-text match, so a query such as ``Toad Road`` surfaces dozens of
weakly related items (Mario Kart clips, "Rainbow Road", ...) above the actual
"Toad Road" movie. This module re-orders a candidate pool locally using a small
transparent scoring function that strongly favours title matches over
metadata-only matches.

The scoring is intentionally simple and deterministic (no external
dependencies): normalize both the query and the candidate title, then bucket the
title relationship into a handful of tiers with large score gaps so that a
strong title match can never be displaced by a weak metadata match or by a
high-download-but-unrelated item.
"""
import re
from typing import Iterable, List, Tuple

# Score tiers. Gaps are large so lower signals (year, metadata, downloads)
# can nudge ordering within a tier but never promote a weaker title tier above
# a stronger one.
TIER_EXACT = 1000.0            # normalized title == normalized query
TIER_EXACT_QUALIFIED = 940.0   # title == query + trailing qualifier (year, etc.)
TIER_PREFIX = 860.0            # title starts with the full query
TIER_CONTAINS_PHRASE = 780.0  # title contains the full query as a phrase
TIER_ALL_WORDS = 560.0        # title contains every query word (any order)
TIER_FUZZY = 300.0            # title contains most query words
TIER_METADATA_ONLY = 0.0      # query only present in non-title metadata

# Secondary signals (kept small relative to tier gaps).
YEAR_MATCH_BONUS = 120.0
YEAR_MISMATCH_PENALTY = 40.0
METADATA_ALL_WORDS_BONUS = 90.0
METADATA_SOME_WORDS_BONUS = 45.0
MEDIATYPE_MATCH_BONUS = 15.0

_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_TRAILING_QUALIFIER_RE = re.compile(r"[\s(]*\b(1[89]\d{2}|20\d{2})\b[\s)]*$")


def normalize(text: str) -> str:
    """Normalize a title/query for comparison.

    Lower-cases, folds filename-style separators (dot, underscore, hyphen,
    slash, backslash) to spaces, drops
    apostrophes so "don't" == "dont", turns remaining punctuation into spaces,
    and collapses repeated whitespace.
    """
    s = (text or "").lower()
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"[._\-/\\]+", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def extract_year(text: str) -> str:
    """Return the first 4-digit year found in ``text`` (1800-2099), or ""."""
    m = _YEAR_RE.search(text or "")
    return m.group(1) if m else ""


def _strip_trailing_qualifier(normalized_title: str) -> str:
    """Drop a trailing year qualifier, e.g. "toad road 2012" -> "toad road"."""
    return _TRAILING_QUALIFIER_RE.sub("", normalized_title).strip()


def _title_tier(nq: str, nq_tokens: List[str], ntitle: str) -> float:
    """Score the relationship between a normalized query and a title."""
    if not nq or not ntitle:
        return TIER_METADATA_ONLY

    if ntitle == nq:
        return TIER_EXACT

    if _strip_trailing_qualifier(ntitle) == nq:
        return TIER_EXACT_QUALIFIED

    # Word-boundaried checks so "road" does not match "roadster".
    padded = f" {ntitle} "
    phrase = f" {nq} "
    if padded.startswith(f" {nq} ") or padded.startswith(f" {nq}"):
        # Prefix match (query is the leading run of words in the title).
        if re.match(re.escape(nq) + r"\b", ntitle):
            return TIER_PREFIX
    if phrase in padded:
        return TIER_CONTAINS_PHRASE

    title_tokens = set(padded.split())
    present = [t for t in nq_tokens if t in title_tokens]
    if not present:
        return TIER_METADATA_ONLY
    if len(present) == len(nq_tokens):
        return TIER_ALL_WORDS
    # Only treat as a (weak) fuzzy title match when most words are present.
    if len(present) * 2 >= len(nq_tokens) + 1 and len(nq_tokens) > 1:
        return TIER_FUZZY * (len(present) / len(nq_tokens))
    return TIER_METADATA_ONLY


def _metadata_bonus(nq_tokens: List[str], fields: Iterable[str]) -> float:
    """Small bonus when query words appear only in non-title metadata."""
    if not nq_tokens:
        return 0.0
    blob = normalize(" ".join(f for f in fields if f))
    if not blob:
        return 0.0
    tokens = set(blob.split())
    present = sum(1 for t in nq_tokens if t in tokens)
    if present == len(nq_tokens):
        return METADATA_ALL_WORDS_BONUS
    if present:
        return METADATA_SOME_WORDS_BONUS
    return 0.0


def score_result(result, query_text: str, media_filter: str = "any") -> float:
    """Return a deterministic relevance score for one result and a query.

    Higher is better. Callers rank a candidate pool with :func:`rerank`.
    """
    raw_query = query_text or ""
    qyear = extract_year(raw_query)
    nq = normalize(_TRAILING_QUALIFIER_RE.sub("", raw_query).strip() if qyear else raw_query)
    nq_tokens = nq.split()

    title = getattr(result, "title", "") or ""
    ntitle = normalize(title)
    score = _title_tier(nq, nq_tokens, ntitle)

    # Metadata match only meaningfully helps when the title itself is weak.
    if score <= TIER_FUZZY:
        score += _metadata_bonus(
            nq_tokens,
            (
                getattr(result, "creator", "") or "",
                getattr(result, "description", "") or "",
                getattr(result, "collection", "") or "",
            ),
        )

    if qyear:
        result_year = extract_year(getattr(result, "year", "") or "") or extract_year(
            getattr(result, "date", "") or ""
        )
        if result_year == qyear:
            score += YEAR_MATCH_BONUS
        elif result_year:
            score -= YEAR_MISMATCH_PENALTY

    if media_filter and media_filter != "any":
        if (getattr(result, "mediatype", "") or "").lower() == media_filter.lower():
            score += MEDIATYPE_MATCH_BONUS

    return score


def rerank(
    results: List,
    query_text: str,
    media_filter: str = "any",
) -> List:
    """Return ``results`` re-ordered by descending local relevance score.

    Ties fall back to download count (a mild popularity tiebreaker that can
    never cross a score tier) and finally the original IA order, which keeps the
    sort stable and deterministic.
    """
    if not results or not (query_text or "").strip():
        return list(results)

    scored: List[Tuple[float, int, int, object]] = []
    for idx, r in enumerate(results):
        try:
            downloads = int(getattr(r, "downloads", 0) or 0)
        except (TypeError, ValueError):
            downloads = 0
        scored.append((score_result(r, query_text, media_filter), downloads, idx, r))

    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [t[3] for t in scored]
