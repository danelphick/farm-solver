import pytest

from parsing import parse_amount, parse_item_amounts


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1000000", 1_000_000),
        ("5k", 5_000),
        ("1.5m", 1_500_000),
        ("M", 10_000),
        ("GM", 100_000),
        ("MM", 1_000_000),
        # case-insensitive
        ("m", 10_000),
        ("gm", 100_000),
        ("mm", 1_000_000),
        ("5K", 5_000),
        ("2M", 2_000_000),
        (" 3k ", 3_000),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("bad", ["k", "abc", "", "1.2.3"])
def test_parse_amount_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_amount(bad)


def test_parse_item_amounts_applies_shorthands():
    assert parse_item_amounts("Butter:MM, Corn:5k") == {
        "Butter": 1_000_000,
        "Corn": 5_000,
    }


def test_parse_item_amounts_reports_bad_amount():
    with pytest.raises(ValueError, match="invalid amount in 'Corn:k'"):
        parse_item_amounts("Corn:k")
