import textwrap

import pytest

from farm_solver import Mastery, parse_masteries

# A representative paste covering every tier, comma grouping, the optional
# trailing tier-header line, and a fully mastered (∞) item.
SAMPLE = textwrap.dedent(
    """\
    Mastery In-ProgressCopy Mastery Data
    Stop Tracking All
    Tier V (MM) chevron_down

    Steel Plate
    976,311 / 1,000,000 Progress
    97.6311% Track

    Pumpkin
    914,704 / 1,000,000 Progress
    91.4704% Track
    Tier IV (GM) chevron_down

    Frost Shield
    96,653 / 100,000 Progress
    96.653% Stop

    Lucky Potato
    835 / 1,000 Progress
    83.5% Track

    11th Leaf Centerpiece
    94 / 100 Progress
    94% Track
    No Tier chevron_down

    Lobster
    2 / 10 Progress
    20% Track
    Mega Mastered

    Board
    832,681,556 / ∞ Progress
    Complete!
    """
)


def test_parses_all_items():
    masteries = parse_masteries(SAMPLE)
    assert set(masteries) == {
        "Steel Plate",
        "Pumpkin",
        "Frost Shield",
        "Lucky Potato",
        "11th Leaf Centerpiece",
        "Lobster",
        "Board",
    }


def test_skips_header():
    masteries = parse_masteries(SAMPLE)
    assert "Mastery In-ProgressCopy Mastery Data" not in masteries
    assert "Stop Tracking All" not in masteries


def test_strips_comma_grouping():
    masteries = parse_masteries(SAMPLE)
    assert masteries["Steel Plate"] == Mastery(
        item="Steel Plate", progress=976_311, required=1_000_000
    )


def test_trailing_tier_header_line_is_ignored():
    # "Pumpkin" has a 4th line ("Tier IV (GM) chevron_down") that must not leak
    # into the parsed item or create a spurious entry.
    masteries = parse_masteries(SAMPLE)
    assert masteries["Pumpkin"] == Mastery(
        item="Pumpkin", progress=914_704, required=1_000_000
    )
    assert "Tier IV (GM) chevron_down" not in masteries


def test_infinity_denominator_marks_item_mastered():
    board = parse_masteries(SAMPLE)["Board"]
    assert board.progress == 832_681_556
    assert board.required is None
    assert board.mastered is True


@pytest.mark.parametrize(
    "item, expected_tier",
    [
        ("Lobster", 0),  # / 10  -> No Tier
        ("11th Leaf Centerpiece", 1),  # / 100 -> Tier I
        ("Lucky Potato", 2),  # / 1,000 -> Tier II
        ("Frost Shield", 4),  # / 100,000 -> Tier IV
        ("Steel Plate", 5),  # / 1,000,000 -> Tier V
        ("Board", 6),  # ∞ -> fully mastered
    ],
)
def test_tier_derived_from_threshold(item, expected_tier):
    assert parse_masteries(SAMPLE)[item].tier == expected_tier


def test_not_mastered_items():
    assert parse_masteries(SAMPLE)["Steel Plate"].mastered is False


def test_empty_input_yields_no_masteries():
    assert parse_masteries("") == {}


def test_input_without_header():
    text = "Hops\n940,162 / 1,000,000 Progress\n94.0162% Track\n"
    masteries = parse_masteries(text)
    assert masteries["Hops"] == Mastery(
        item="Hops", progress=940_162, required=1_000_000
    )


def test_malformed_progress_line_raises():
    text = "Mystery Item\nnot a progress line\n50% Track\n"
    with pytest.raises(ValueError, match="could not parse progress line"):
        parse_masteries(text)
