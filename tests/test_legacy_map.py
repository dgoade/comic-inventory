from comic_inventory.legacy_map import (
    map_condition_grade,
    normalize_issue_number,
    normalize_volume,
    parse_publish_month,
)


def test_normalize_issue_number_strips_hash_and_leading_zeros() -> None:
    assert normalize_issue_number("#1") == "1"
    assert normalize_issue_number("01") == "1"
    assert normalize_issue_number("1") == "1"
    assert normalize_issue_number("001") == "1"


def test_normalize_issue_number_preserves_odd_values() -> None:
    for raw in ("-1", "0.5", "1/2", "PR13", "X", "-"):
        assert normalize_issue_number(raw) == raw


def test_normalize_issue_number_empty() -> None:
    assert normalize_issue_number(None) == "-"
    assert normalize_issue_number("") == "-"
    assert normalize_issue_number("   ") == "-"
    assert normalize_issue_number("#") == "-"


def test_map_condition_grade() -> None:
    assert map_condition_grade("F") == "fair"
    assert map_condition_grade("FN") == "fine"
    assert map_condition_grade("M") == "mint"
    assert map_condition_grade("nm") == "near_mint"
    assert map_condition_grade("nope") is None
    assert map_condition_grade(None) is None
    assert map_condition_grade("") is None


def test_parse_publish_month() -> None:
    assert parse_publish_month("09") == 9
    assert parse_publish_month("9") == 9
    assert parse_publish_month("12") == 12
    assert parse_publish_month("13") is None
    assert parse_publish_month("foo") is None
    assert parse_publish_month(None) is None


def test_normalize_volume() -> None:
    assert normalize_volume("-") == ""
    assert normalize_volume(None) == ""
    assert normalize_volume("  ") == ""
    assert normalize_volume("1") == "1"
    assert normalize_volume("2") == "2"
