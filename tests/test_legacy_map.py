from comic_inventory.legacy_map import (
    extract_cover_names,
    inker_names,
    map_condition_grade,
    normalize_creator_name,
    normalize_issue_number,
    normalize_volume,
    parse_publish_month,
    split_creator_names,
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


def test_split_creator_names_jr_is_one_person() -> None:
    assert split_creator_names("John Romita, Jr.") == ["John Romita Jr."]
    assert split_creator_names("John Romita Jr., Bob Wiacek") == [
        "John Romita Jr.",
        "Bob Wiacek",
    ]


def test_split_creator_names_separators() -> None:
    assert split_creator_names("Mark Waid, Brian Augustyn") == [
        "Mark Waid",
        "Brian Augustyn",
    ]
    assert split_creator_names("Giacoia & Esposito") == ["Giacoia", "Esposito"]
    assert split_creator_names("Patterson/Milgrom") == ["Patterson", "Milgrom"]


def test_split_creator_names_empty() -> None:
    assert split_creator_names(None) == []
    assert split_creator_names("") == []
    assert split_creator_names("   ") == []
    assert split_creator_names("-") == []


def test_normalize_creator_name_collapses_whitespace() -> None:
    assert normalize_creator_name("  Mark   Waid ") == "Mark Waid"


def test_inker_names_fallback() -> None:
    assert inker_names("John Byrne", "-") == ["John Byrne"]
    assert inker_names("John Byrne", None) == ["John Byrne"]
    assert inker_names("John Byrne", "Terry Austin") == ["Terry Austin"]
    assert inker_names(None, "-") == []


COVER_TRUE_POSITIVES = {
    "Cover by Steranko": ["Steranko"],
    "Cover C by Richards": ["Richards"],
    "Steranko cover": ["Steranko"],
    "Sienkiewicz cover": ["Sienkiewicz"],
    "1st app. Drakula and Frank Drake; Neal Adams cover": ["Neal Adams"],
    "Last 15 cent issue; Neal Adams Cover": ["Neal Adams"],
    "Invisible Man; Steranko Cover; Purchased at Heroes Con in Charlotte, NC, 1999": [
        "Steranko"
    ],
    "1st Neal Adams art on Batman (cover only)": ["Neal Adams"],
    "Rick Mays Scarab Cover": ["Rick Mays"],
}

COVER_TRUE_NEGATIVES = [
    "Foil cover",
    "Foil Cover",
    "Glow in the dark cover",
    "Acetate Cover",
    "Alternate Cover",
    "Hologram cover",
    "Holographic cover",
    "Red foil cover",
    "Value cover or less",
    "Value Cover or less",
    "cover crease",
    "Slight Cover tear - Bottom front.",
    "Spine tear - Back Cover",
    "Got this one at San Diego '99; This is an English comic, it has 6p as the cover price!",
    "From the lost collection; Frozen Super-Sons Cover; 'Have you seen our missing sons?'...'NOPE!'",
    "Kilowat cover",
    "Sabertooth cover-story",
    "Yellow cover; \"Shaman\" begins, ends #5; outer cover has 4 different color variations, all worth the same",
]


def test_extract_cover_names_true_positives() -> None:
    for comments, expected in COVER_TRUE_POSITIVES.items():
        assert extract_cover_names(comments) == expected, comments


def test_extract_cover_names_true_negatives() -> None:
    for comments in COVER_TRUE_NEGATIVES:
        assert extract_cover_names(comments) == [], comments
