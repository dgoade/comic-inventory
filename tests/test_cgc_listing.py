from decimal import Decimal

import pytest

from comic_inventory.cgc_client import (
    CommandError,
    build_aspects,
    build_title,
    cgc_comment_block,
    ebay_grade_label,
    merge_grading_comments,
    parse_cert,
    parse_grade,
    parse_price,
    select_candidate,
    series_match_names,
)
from comic_inventory.ebay import inventory_item_body, missing_names, offer_body, package_weight_and_size


def _cert(**overrides):
    payload = {
        "certNumber": "4719983018",
        "grade": {"displayGrade": "9.8", "gradeCategory": "Universal"},
        "additionalInfo": {
            "pageQuality": "WHITE Pages",
            "graderNotes": ["light spine stress"],
            "signatures": [],
        },
        "collectible": {
            "title": "Amazing Spider-Man",
            "issue": "#300",
            "publisher": "Marvel",
            "year": 1988,
            "variant": "Newsstand",
        },
        "images": {
            "frontUrl": "https://images.example/front.jpg",
            "rearUrl": "https://images.example/rear.jpg",
        },
    }
    payload.update(overrides)
    return parse_cert(payload, "4719983018")


def test_parse_grade_reads_cgc_and_ebay_wording() -> None:
    assert parse_grade("9.8") == Decimal("9.8")
    assert parse_grade("9.8 Near Mint/Mint") == Decimal("9.8")
    assert parse_grade("NM/MT 9.8") == Decimal("9.8")
    assert parse_grade("10 Gem Mint") == Decimal("10.0")
    assert parse_grade("Universal") is None
    assert parse_grade(None) is None


def test_ebay_grade_label() -> None:
    assert ebay_grade_label(Decimal("9.8")) == "9.8 Near Mint/Mint"
    assert ebay_grade_label(Decimal("10.0")) == "10 Gem Mint"
    assert ebay_grade_label(None) is None


def test_parse_cert_flags_signature_and_restored() -> None:
    signed = _cert(grade={"displayGrade": "9.8", "gradeCategory": "Signature Series"})
    assert signed.signed
    assert signed.issue_numbers == ["#300", "300"]
    restored = _cert(
        grade={"displayGrade": "9.4", "gradeCategory": "Restored"},
        additionalInfo={"signatures": ["Stan Lee"], "graderNotes": [], "pageQuality": ""},
    )
    assert restored.restored
    assert restored.signed
    assert restored.grade_numeric == Decimal("9.4")


def test_title_keeps_the_label_and_stays_within_80() -> None:
    short = build_title(
        series="Batman",
        issue="1",
        grade="9.8",
        restored=True,
        variant="",
        publisher="DC Comics",
        year=1940,
    )
    assert short == "Batman #1 CGC 9.8 Restored Comic Book DC Comics 1940"

    long = build_title(
        series="X" * 200,
        issue="123456",
        grade="9.8",
        restored=True,
        variant="Y" * 100,
        publisher="Z" * 80,
        year=2020,
    )
    assert len(long) <= 80
    assert "CGC 9.8" in long
    assert "2020" not in long


def test_title_drops_year_before_variant() -> None:
    title = build_title(
        series="Uncanny X-Men",
        issue="141",
        grade="9.8",
        restored=False,
        variant="Days of Future Past Newsstand Edition",
        publisher="Marvel Comics",
        year=1981,
    )
    assert len(title) <= 80
    assert title.startswith("Uncanny X-Men #141 CGC 9.8")
    assert "Days of Future Past" in title
    assert "1981" not in title


def test_aspects_use_ebay_grade_words() -> None:
    aspects = build_aspects(
        cert_number="4719983018",
        series="Amazing Spider-Man",
        issue="300",
        publisher="Marvel Comics",
        year=1988,
        grade=Decimal("9.8"),
        signed=False,
    )
    assert aspects["Professional Grader"] == ["Certified Guaranty Company (CGC)"]
    assert aspects["Grade"] == ["9.8 Near Mint/Mint"]
    assert aspects["Certification Number"] == ["4719983018"]
    assert aspects["Signed"] == ["No"]
    assert aspects["Format"] == ["Single Issue"]

    unmapped = build_aspects(
        cert_number="4719983018",
        series="Amazing Spider-Man",
        issue="300",
        publisher="Marvel",
        year=None,
        grade=None,
        signed=True,
    )
    assert "Grade" not in unmapped
    assert unmapped["Signed"] == ["Yes"]


def test_select_candidate_refuses_a_legacy_id_that_is_not_listed() -> None:
    rows = [{"legacy_id": 10}, {"legacy_id": 11}]
    assert select_candidate(rows, 11)["legacy_id"] == 11
    with pytest.raises(CommandError):
        select_candidate(rows, 99)
    with pytest.raises(CommandError):
        select_candidate([], 10)


def test_merge_grading_comments_replaces_the_cgc_block() -> None:
    cert = _cert()
    block = cgc_comment_block(cert)
    once = merge_grading_comments("spine tick", block)
    twice = merge_grading_comments(once, block)
    assert twice.count("CGC lookup:") == 1
    assert twice.startswith("spine tick")


def test_series_match_names_strips_year_and_the() -> None:
    names = series_match_names("The Amazing Spider-Man (1963)")
    folded = {name.casefold() for name in names}
    assert "the amazing spider-man (1963)" in folded
    assert "amazing spider-man" in folded
    assert "the amazing spider-man" in folded


def test_price() -> None:
    assert parse_price("25") == Decimal("25.00")
    assert parse_price("25.50") == Decimal("25.50")
    with pytest.raises(CommandError):
        parse_price("0")
    with pytest.raises(CommandError):
        parse_price("1.999")


def test_missing_policies_are_named() -> None:
    missing = missing_names({}, ("EBAY_FULFILLMENT_POLICY_ID", "EBAY_PAYMENT_POLICY_ID"))
    assert missing == ["EBAY_FULFILLMENT_POLICY_ID", "EBAY_PAYMENT_POLICY_ID"]
    assert missing_names({"EBAY_FULFILLMENT_POLICY_ID": "1"}, ("EBAY_FULFILLMENT_POLICY_ID",)) == []


def test_ebay_bodies_carry_images_grade_and_slab_package() -> None:
    item = inventory_item_body(
        title="Batman #1 CGC 9.8",
        description="Certification number 4719983018.",
        aspects={"Grade": ["9.8 Near Mint/Mint"]},
        image_urls=["https://cdn.example/01.jpg", "https://cdn.example/02.jpg"],
        cert_number="4719983018",
        grade_text="9.8",
        package=package_weight_and_size({}),
    )
    assert item["condition"] == "USED_VERY_GOOD"
    assert item["product"]["imageUrls"][0].endswith("01.jpg")
    assert item["packageWeightAndSize"]["weight"] == {"value": 24.0, "unit": "OUNCE"}
    offer = offer_body(
        sku="4719983018",
        description="Certification number 4719983018.",
        price="25.00",
        category_id="259104",
        env={
            "EBAY_FULFILLMENT_POLICY_ID": "f",
            "EBAY_PAYMENT_POLICY_ID": "p",
            "EBAY_RETURN_POLICY_ID": "r",
        },
    )
    assert offer["categoryId"] == "259104"
    assert offer["pricingSummary"]["price"]["value"] == "25.00"
    assert "merchantLocationKey" not in offer


def test_read_local_image_accepts_jpeg_and_refuses_other_bytes(tmp_path) -> None:
    from comic_inventory.storage import read_local_image

    jpeg = tmp_path / "front.jpg"
    jpeg.write_bytes(b"\xff\xd8" + b"x" * 40)
    data, content_type, extension = read_local_image(jpeg)
    assert data.startswith(b"\xff\xd8")
    assert content_type == "image/jpeg"
    assert extension == "jpg"

    other = tmp_path / "notes.txt"
    other.write_bytes(b"not an image, just some text content here")
    with pytest.raises(CommandError, match="jpeg, png, or webp"):
        read_local_image(other)
    with pytest.raises(CommandError, match="not found"):
        read_local_image(tmp_path / "missing.jpg")
