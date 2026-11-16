"""Listing text for a CGC slab. Photos and the grade come from the operator."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from comic_inventory.legacy_map import normalize_issue_number

TITLE_LIMIT = 80
ASPECT_LIMIT = 65
_YEAR_SUFFIX_RE = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
_SKIP_VARIANT = frozenset({"", "standard", "none", "n/a", "-", "regular"})

# eBay category 259104 Grade values. 10.0 is "10 Gem Mint", not "10.0".
EBAY_GRADE_LABELS = {
    Decimal("10.0"): "10 Gem Mint",
    Decimal("9.9"): "9.9 Mint",
    Decimal("9.8"): "9.8 Near Mint/Mint",
    Decimal("9.6"): "9.6 Near Mint+",
    Decimal("9.4"): "9.4 Near Mint",
    Decimal("9.2"): "9.2 Near Mint-",
    Decimal("9.0"): "9.0 Very Fine/Near Mint",
    Decimal("8.5"): "8.5 Very Fine+",
    Decimal("8.0"): "8.0 Very Fine",
    Decimal("7.5"): "7.5 Very Fine-",
    Decimal("7.0"): "7.0 Fine/Very Fine",
    Decimal("6.5"): "6.5 Fine+",
    Decimal("6.0"): "6.0 Fine",
    Decimal("5.5"): "5.5 Fine-",
    Decimal("5.0"): "5.0 Very Good/Fine",
    Decimal("4.5"): "4.5 Very Good+",
    Decimal("4.0"): "4.0 Very Good",
    Decimal("3.5"): "3.5 Very Good-",
    Decimal("3.0"): "3.0 Good/Very Good",
    Decimal("2.5"): "2.5 Good+",
    Decimal("2.0"): "2.0 Good",
    Decimal("1.8"): "1.8 Good-",
    Decimal("1.5"): "1.5 Fair/Good",
    Decimal("1.0"): "1.0 Fair",
    Decimal("0.5"): "0.5 Poor",
}

CGC_MARKER = "CGC lookup:"


class CommandError(Exception):
    """A problem the operator can fix. Printed without a traceback."""


@dataclass(frozen=True)
class CgcCert:
    cert_number: str
    display_grade: str
    grade_numeric: Decimal | None
    grade_category: str
    title: str
    issue: str
    publisher: str
    year: int | None
    variant: str
    page_quality: str
    grader_notes: tuple[str, ...]
    signatures: tuple[str, ...]
    label_text: str
    front_url: str
    rear_url: str
    signed: bool
    restored: bool

    @property
    def grade_text(self) -> str:
        if self.grade_numeric is None:
            return self.display_grade or "unknown"
        return f"{self.grade_numeric:.1f}"

    @property
    def issue_numbers(self) -> list[str]:
        raw = (self.issue or "").strip()
        normalized = normalize_issue_number(self.issue)
        numbers = []
        for value in (raw, normalized):
            if value and value not in numbers:
                numbers.append(value)
        return numbers or ["-"]


def parse_grade(display: str | None) -> Decimal | None:
    """Pull a CGC scale number out of a display grade. Unmapped text returns None."""
    if not display:
        return None
    for match in re.finditer(r"\d+(?:\.\d+)?", display):
        number = Decimal(match.group(0)).quantize(Decimal("0.1"))
        if number in EBAY_GRADE_LABELS:
            return number
    return None


def ebay_grade_label(grade: Decimal | None) -> str | None:
    if grade is None:
        return None
    return EBAY_GRADE_LABELS.get(grade.quantize(Decimal("0.1")))


def flag_signed(category: str | None, signatures: list[str] | tuple[str, ...]) -> bool:
    if any(str(name).strip() for name in signatures):
        return True
    return "signature" in (category or "").casefold()


def flag_restored(category: str | None) -> bool:
    return "restor" in (category or "").casefold()


def series_match_names(title: str | None) -> list[str]:
    """Names to compare with series.name: strip a trailing (year), and try with/without The."""
    text = " ".join((title or "").split())
    if not text:
        return []
    names = [text]
    no_year = _YEAR_SUFFIX_RE.sub("", text).strip()
    if no_year and no_year.casefold() != text.casefold():
        names.append(no_year)
    expanded: list[str] = []
    for name in names:
        expanded.append(name)
        if name.casefold().startswith("the "):
            rest = name[4:].strip()
            if rest:
                expanded.append(rest)
        else:
            expanded.append(f"The {name}")
    seen: set[str] = set()
    out: list[str] = []
    for name in expanded:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _clip(value: str, limit: int = ASPECT_LIMIT) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _usable_variant(variant: str | None) -> str:
    text = " ".join((variant or "").split())
    if text.casefold() in _SKIP_VARIANT:
        return ""
    return text


def build_title(
    *,
    series: str,
    issue: str,
    grade: str,
    restored: bool,
    variant: str | None,
    publisher: str | None,
    year: int | None,
) -> str:
    """80-character eBay title. Optional pieces drop from the right until it fits."""
    series_text = " ".join((series or "Comic").split()) or "Comic"
    suffix = f" #{issue} CGC {grade}"
    if len(series_text) + len(suffix) > TITLE_LIMIT:
        room = TITLE_LIMIT - len(suffix)
        series_text = series_text[: max(room, 0)].rstrip()
    core = f"{series_text}{suffix}"[:TITLE_LIMIT]

    extras: list[str] = []
    if restored:
        extras.append("Restored")
    variant_text = _usable_variant(variant)
    if variant_text:
        extras.append(variant_text)
    extras.append("Comic Book")
    publisher_text = " ".join((publisher or "").split())
    if publisher_text:
        extras.append(publisher_text)
    if year:
        extras.append(str(year))

    pending = list(extras)
    while pending:
        title = " ".join([core, *pending])
        if len(title) <= TITLE_LIMIT:
            return title
        pending.pop()
    return core


def build_aspects(
    *,
    cert_number: str,
    series: str,
    issue: str,
    publisher: str | None,
    year: int | None,
    grade: Decimal | None,
    signed: bool,
) -> dict[str, list[str]]:
    aspects: dict[str, list[str]] = {
        "Professional Grader": ["Certified Guaranty Company (CGC)"],
        "Certification Number": [cert_number],
        "Format": ["Single Issue"],
        "Signed": ["Yes" if signed else "No"],
        "Type": ["Comic Book"],
    }
    label = ebay_grade_label(grade)
    if label:
        aspects["Grade"] = [label]
    if series:
        aspects["Series Title"] = [_clip(series)]
    if issue:
        aspects["Issue Number"] = [_clip(issue)]
    if publisher:
        aspects["Publisher"] = [_clip(publisher)]
    if year:
        aspects["Publication Year"] = [str(year)]
    return aspects


def build_description(cert: CgcCert, *, series: str, issue: str, publisher: str) -> str:
    lines = [
        f"{series} #{issue} CGC {cert.grade_text}.",
        f"Publisher: {publisher}." if publisher else "",
        f"Certification number {cert.cert_number}.",
        f"Verify at https://www.cgccomics.com/certlookup/{cert.cert_number}/",
    ]
    variant = _usable_variant(cert.variant)
    if variant:
        lines.append(f"Variant: {variant}.")
    if cert.page_quality:
        lines.append(f"Page quality: {cert.page_quality}.")
    if cert.signatures:
        lines.append("Signatures: " + ", ".join(cert.signatures) + ".")
    if cert.grader_notes:
        lines.append("Grader notes: " + "; ".join(cert.grader_notes) + ".")
    if cert.label_text:
        lines.append(cert.label_text)
    return "\n".join(line for line in lines if line)


def cgc_comment_block(cert: CgcCert) -> str:
    lines = [f"{CGC_MARKER} {cert.cert_number} grade {cert.grade_text}"]
    if cert.page_quality:
        lines.append(f"Page quality: {cert.page_quality}")
    if cert.grader_notes:
        lines.append("Grader notes: " + "; ".join(cert.grader_notes))
    return "\n".join(lines)


def merge_grading_comments(existing: str | None, block: str) -> str:
    text = (existing or "").strip()
    if not text:
        return block
    if CGC_MARKER in text:
        head = text.split(CGC_MARKER)[0].strip()
        if head:
            return head + "\n" + block
        return block
    return text + "\n" + block


def select_candidate(candidates: list[dict], legacy_id: int) -> dict:
    matches = [row for row in candidates if row.get("legacy_id") == legacy_id]
    if len(matches) != 1:
        raise CommandError(
            f"legacy_id {legacy_id} is not one of the {len(candidates)} matching copies"
        )
    return matches[0]


def parse_price(raw: str) -> Decimal:
    try:
        price = Decimal(raw)
    except Exception as exc:
        raise CommandError(f"price is not a number: {raw}") from exc
    if not price.is_finite() or price <= 0:
        raise CommandError("price must be greater than 0")
    if price.as_tuple().exponent < -2:
        raise CommandError("price has more than 2 decimal places")
    return price.quantize(Decimal("0.01"))


def clean_cert(raw: str) -> str:
    text = (raw or "").strip()
    if not re.fullmatch(r"\d{6,14}", text):
        raise CommandError(f"cert must be 6 to 14 digits, got {raw!r}")
    return text


def _strings(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def parse_cert(payload: dict, cert_number: str) -> CgcCert:
    if not isinstance(payload, dict):
        raise CommandError("CGC lookup did not return a cert record")
    grade = payload.get("grade") or {}
    info = payload.get("additionalInfo") or {}
    book = payload.get("collectible") or {}
    images = payload.get("images") or {}
    display = str(grade.get("displayGrade") or "").strip()
    category = str(grade.get("gradeCategory") or "").strip()
    signatures = _strings(info.get("signatures"))
    year = book.get("year")
    year_int = int(year) if isinstance(year, int) and year > 0 else None
    returned = str(payload.get("certNumber") or cert_number).strip()
    return CgcCert(
        cert_number=returned or cert_number,
        display_grade=display,
        grade_numeric=parse_grade(display),
        grade_category=category,
        title=str(book.get("title") or "").strip(),
        issue=str(book.get("issue") or "").strip(),
        publisher=str(book.get("publisher") or "").strip(),
        year=year_int,
        variant=str(book.get("variant") or "").strip(),
        page_quality=str(info.get("pageQuality") or "").strip(),
        grader_notes=_strings(info.get("graderNotes")),
        signatures=signatures,
        label_text=str(info.get("labelText") or "").strip(),
        front_url=str(images.get("frontUrl") or "").strip(),
        rear_url=str(images.get("rearUrl") or "").strip(),
        signed=flag_signed(category, signatures),
        restored=flag_restored(category),
    )
