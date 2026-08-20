"""Pure mappings from legacy public.comics values to inventory types.

SQL in etl.py must stay in sync with these functions.
"""

from __future__ import annotations

import re

GRADE_MAP = {
    "M": "mint",
    "NM": "near_mint",
    "VF": "very_fine",
    "FN": "fine",
    "F": "fair",
    "VG": "very_good",
    "G": "good",
    "P": "poor",
}

_MONTH_RE = re.compile(r"^(0?[1-9]|1[0-2])$")
_PADDED_DIGITS_RE = re.compile(r"^0+[0-9]+$")
_SUFFIX_RE = re.compile(r",\s*(Jr\.?|Sr\.?|III|II|IV)\b", re.IGNORECASE)
_TITLE_TOKEN = r"[A-Z][A-Za-z0-9'.-]*"
_COVER_BY_RE = re.compile(
    rf"cover(?:\s+[A-C])?\s+by\s+([^;]+)",
    re.IGNORECASE,
)
_NAME_THEN_COVER_RE = re.compile(
    rf"\b((?:{_TITLE_TOKEN})(?:\s+{_TITLE_TOKEN}){{0,2}})\s+[Cc]over\b"
)
_ART_COVER_ONLY_RE = re.compile(
    rf"\b((?:{_TITLE_TOKEN})(?:\s+{_TITLE_TOKEN}){{0,2}})\s+art\b[^(]*\(\s*cover only\s*\)"
)
COVER_NOISE = frozenset(
    {
        "foil",
        "hologram",
        "holographic",
        "acetate",
        "alternate",
        "value",
        "yellow",
        "red",
        "glow",
        "dark",
        "back",
        "front",
        "outer",
        "spine",
        "slight",
        "crease",
        "tear",
        "price",
        "story",
        "lost",
        "frozen",
        "kilowat",
        "sabertooth",
        "cover",
    }
)


def normalize_issue_number(raw: str | None) -> str:
    if raw is None:
        return "-"
    value = raw.strip()
    if not value:
        return "-"
    value = re.sub(r"^#+", "", value)
    if not value:
        return "-"
    if _PADDED_DIGITS_RE.fullmatch(value):
        return value.lstrip("0") or "0"
    return value


def normalize_volume(raw: str | None) -> str:
    """Legacy comics.series is the run (1/2/3). Missing / '-' become ''."""
    if raw is None:
        return ""
    value = raw.strip()
    if value in ("", "-"):
        return ""
    return value


def map_condition_grade(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().upper()
    if not value:
        return None
    return GRADE_MAP.get(value)


def parse_publish_month(raw: str | None) -> int | None:
    if raw is None:
        return None
    value = raw.strip()
    if not _MONTH_RE.fullmatch(value):
        return None
    return int(value)


def normalize_creator_name(raw: str | None) -> str:
    if raw is None:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def split_creator_names(raw: str | None) -> list[str]:
    if raw is None:
        return []
    value = raw.strip()
    if value in ("", "-"):
        return []
    value = _SUFFIX_RE.sub(lambda match: " " + match.group(1), value)
    value = value.replace("&", ",").replace("/", ",")
    names: list[str] = []
    for part in value.split(","):
        name = normalize_creator_name(part)
        if not name or name == "-":
            continue
        names.append(name)
    return names


def inker_names(art: str | None, inks: str | None) -> list[str]:
    """If inks is empty, the penciller also inked."""
    inked = split_creator_names(inks)
    if inked:
        return inked
    return split_creator_names(art)


def _cover_name_from_tokens(blob: str) -> str | None:
    tokens = blob.split()
    if not tokens:
        return None
    if tokens[0].lower() in COVER_NOISE:
        return None
    if len(tokens) == 3:
        tokens = tokens[:2]
    return normalize_creator_name(" ".join(tokens)) or None


def extract_cover_names(comments: str | None) -> list[str]:
    """Parse cover-artist credits out of comments. Not a general name splitter."""
    if comments is None:
        return []
    text = comments.strip()
    if not text:
        return []

    found: list[str] = []

    match = _COVER_BY_RE.search(text)
    if match:
        name = normalize_creator_name(match.group(1))
        if name:
            found.append(name)

    for match in _NAME_THEN_COVER_RE.finditer(text):
        name = _cover_name_from_tokens(match.group(1))
        if name:
            found.append(name)

    match = _ART_COVER_ONLY_RE.search(text)
    if match:
        name = _cover_name_from_tokens(match.group(1))
        if name:
            found.append(name)

    unique: list[str] = []
    seen: set[str] = set()
    for name in found:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique
