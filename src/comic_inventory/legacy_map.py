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
