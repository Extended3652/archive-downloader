"""Pure helpers for search query building, naming, and license heuristics."""
import os
import re
from typing import Any, Dict, List, Optional, Tuple


IA_FIELD_NAMES = (
    "title",
    "creator",
    "subject",
    "description",
    "identifier",
    "collection",
    "mediatype",
    "date",
    "publicdate",
    "licenseurl",
    "rights",
)


def sanitize_folder(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[\/\\:\*\?\"<>\|]+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Unknown"


def detect_sxxeyy(text: str) -> Optional[Tuple[int, int]]:
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def auto_clean_movie_folder_name(item_title: str, filename: str) -> str:
    raw = (item_title or "").strip() or (filename or "").strip()
    raw = os.path.basename(raw)
    raw = os.path.splitext(raw)[0]
    raw = re.sub(r"[\[\](){}]", " ", raw)
    raw = re.sub(r"[._]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()

    year = ""
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", raw)
    if year_match:
        year = year_match.group(1)
        title_part = raw[: year_match.start()]
    else:
        title_part = raw

    scene_rx = re.compile(
        r"\b(?:"
        r"2160p|1080p|720p|480p|"
        r"bluray|brrip|bdrip|webrip|web-dl|hdrip|dvdrip|"
        r"x264|x265|h264|h265|hevc|av1|"
        r"aac2?\.0|aac|dts(?:-?hd)?|ddp?5?\.1|ac3|"
        r"proper|repack|extended|remastered|unrated|"
        r"yify|yts|rarbg"
        r")\b",
        re.IGNORECASE,
    )
    title_part = scene_rx.sub(" ", title_part)
    title_part = re.sub(r"\s+", " ", title_part).strip(" -._")

    cleaned_title = sanitize_folder(title_part or raw)
    if year:
        return sanitize_folder(f"{cleaned_title} ({year})")
    return cleaned_title


def looks_like_advanced_query(text: str) -> bool:
    s = text or ""
    up = f" {s.upper()} "
    if re.search(r"\b[a-zA-Z][\w-]*\s*:", s):
        return True
    if any(op in up for op in (" AND ", " OR ", " NOT ")):
        return True
    return False


def split_title_year(text: str) -> Tuple[str, str]:
    s = re.sub(r"\s+", " ", (text or "").strip())
    if not s:
        return "", ""

    year = ""
    title = s

    paren_match = re.search(r"\s*\((19\d{2}|20\d{2})\)\s*$", s)
    if paren_match:
        year = paren_match.group(1)
        title = s[: paren_match.start()].strip()
    else:
        end_match = re.search(r"\s+(19\d{2}|20\d{2})\s*$", s)
        start_match = re.search(r"^(19\d{2}|20\d{2})\s+", s)
        if end_match:
            year = end_match.group(1)
            title = s[: end_match.start()].strip()
        elif start_match:
            year = start_match.group(1)
            title = s[start_match.end() :].strip()

    return title or s, year


def quote_title(value: str) -> str:
    escaped = (value or "").replace('"', r"\"")
    return f'title:("{escaped}")'


def quote_field(field: str, value: str) -> str:
    field = (field or "").strip().lower()
    if field not in IA_FIELD_NAMES:
        raise ValueError(f"Unsupported IA search field: {field}")
    escaped = re.sub(r"\s+", " ", (value or "").strip()).replace('"', r"\"")
    return f'{field}:("{escaped}")'


def add_media_filter(query: str, media_filter: str) -> str:
    if media_filter and media_filter != "any":
        return f"{query} AND mediatype:{media_filter}"
    return query


def looks_like_identifier(text: str) -> bool:
    s = (text or "").strip()
    if not s or " " in s:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}", s))


def build_query(user_text: str, media_filter: str, title_only: bool) -> str:
    s = re.sub(r"\s+", " ", (user_text or "").strip())
    if not s:
        return ""

    if looks_like_advanced_query(s):
        return s

    title, year = split_title_year(s)
    base = quote_title(title)
    if not title_only:
        base = f"({base} OR {title})"
    if year:
        base = f"{base} AND year:{year}"
    return add_media_filter(base, media_filter)


def build_fielded_query(user_text: str, media_filter: str) -> str:
    s = re.sub(r"\s+", " ", (user_text or "").strip())
    if not s:
        return ""
    if looks_like_advanced_query(s):
        return s
    title, year = split_title_year(s)
    clauses = [
        quote_field("title", title),
        quote_field("creator", title),
        quote_field("subject", title),
        quote_field("description", title),
        title,
    ]
    base = "(" + " OR ".join(clauses) + ")"
    if year:
        base = f"{base} AND year:{year}"
    return add_media_filter(base, media_filter)


def build_collection_search_query(user_text: str) -> str:
    s = re.sub(r"\s+", " ", (user_text or "").strip())
    if not s:
        return "mediatype:collection"
    if looks_like_advanced_query(s):
        return f"({s}) AND mediatype:collection"
    return f"({quote_field('title', s)} OR subject:(\"{s}\") OR description:(\"{s}\") OR {s}) AND mediatype:collection"


def normalize_collection_identifier(value: str) -> str:
    s = (value or "").strip()
    if "," in s:
        s = s.split(",", 1)[0].strip()
    return s


def build_within_collection_query(user_text: str, collection: str, media_filter: str, title_only: bool) -> str:
    coll = normalize_collection_identifier(collection)
    base = build_query(user_text, media_filter, title_only) if user_text.strip() else add_media_filter("*:*", media_filter)
    if not coll:
        return base
    return f"({base}) AND collection:{coll}"


def build_field_query(field: str, value: str, media_filter: str = "any") -> str:
    s = re.sub(r"\s+", " ", (value or "").strip())
    if not s:
        return ""
    return add_media_filter(quote_field(field, s), media_filter)


def build_query_attempts(user_text: str, media_filter: str, title_only: bool) -> List[Tuple[str, str]]:
    s = re.sub(r"\s+", " ", (user_text or "").strip())
    if not s:
        return []
    first = build_query(s, media_filter, title_only)
    if looks_like_advanced_query(s) or title_only:
        return [("advanced" if looks_like_advanced_query(s) else "title", first)]

    attempts: List[Tuple[str, str]] = []
    if looks_like_identifier(s):
        attempts.append(("identifier", f"identifier:{s}"))
    attempts.append(("title", first))
    attempts.append(("fields", build_fielded_query(s, media_filter)))
    attempts.append(("plain", add_media_filter(s, media_filter)))

    deduped: List[Tuple[str, str]] = []
    seen = set()
    for label, query in attempts:
        if query and query not in seen:
            deduped.append((label, query))
            seen.add(query)
    return deduped


def is_openly_licensed(meta: Dict[str, Any]) -> Tuple[bool, str]:
    m = meta.get("metadata", {}) or {}
    licenseurl = str(m.get("licenseurl", "") or "").lower()
    rights = str(m.get("rights", "") or "").lower()
    possible = [licenseurl, rights]

    allow_markers = [
        "creativecommons.org",
        "cc-by",
        "cc0",
        "public domain",
        "publicdomain",
        "no known copyright",
    ]
    deny_markers = [
        "all rights reserved",
        "no redistribution",
        "permission required",
    ]
    # Phrases that look open at first glance but are actually negated.
    negation_markers = [
        "not in the public domain",
        "not public domain",
        "not creative commons",
        "no public domain",
    ]

    joined = " | ".join([p for p in possible if p])

    for n in negation_markers:
        if n in joined:
            return False, f"Negated open-license phrase: {n}"

    for d in deny_markers:
        if d in joined:
            return False, f"Blocked by rights metadata: {d}"

    for a in allow_markers:
        if a in joined:
            return True, "Open license detected"

    return False, "No clear open license in metadata (licenseurl/rights)."


def license_status_from_fields(licenseurl: str = "", rights: str = "") -> Tuple[str, str]:
    """Return a compact status label and reason for search-result rights fields."""
    if not (licenseurl or rights):
        return "unknown", "No license fields in search result."
    ok, why = is_openly_licensed({"metadata": {"licenseurl": licenseurl, "rights": rights}})
    if ok:
        return "open", why
    lowered = why.lower()
    if "blocked" in lowered or "negated" in lowered:
        return "blocked", why
    return "unclear", why
