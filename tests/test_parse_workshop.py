import textwrap

from crafting import WorkshopRecipe
from parsing import parse_workshop

SAMPLE = textwrap.dedent(
    """\
    Your Crafting Level is 99.
    Jump to items at your Crafting Level

    Board
    Visible in Workshop
    5x Wood
    1 Silver
    Crafting XP: 25
    Crafting Lv 1

    Bucket
    Visible in Workshop
    5x Board
    1x Iron
    5 Silver
    Crafting XP: 250
    Crafting Lv 1

    Secret Thing
    Hidden in Workshop
    2x Hide
    25 Silver
    Crafting XP: 250
    Crafting Lv 1

    Nothing
    Crafting Lv 39
    """
)


def test_parses_visible_and_hidden_items():
    shop = parse_workshop(SAMPLE)
    assert set(shop.recipes) == {"board", "bucket", "secret thing"}


def test_header_is_ignored():
    shop = parse_workshop(SAMPLE)
    assert "your crafting level is 99." not in shop.recipes


def test_nothing_divider_is_ignored():
    shop = parse_workshop(SAMPLE)
    assert "nothing" not in shop.recipes


def test_keeps_only_name_and_materials():
    shop = parse_workshop(SAMPLE)
    assert shop.recipes["bucket"] == WorkshopRecipe(
        name="Bucket", materials={"Board": 5, "Iron": 1}
    )


def test_single_material():
    shop = parse_workshop(SAMPLE)
    assert shop.recipes["board"].materials == {"Wood": 5}


def test_discards_silver_xp_and_level_lines():
    shop = parse_workshop(SAMPLE)
    # Only the real materials remain; nothing from the trailing lines leaks in.
    assert shop.recipes["secret thing"].materials == {"Hide": 2}


def test_lookup_is_case_insensitive():
    shop = parse_workshop(SAMPLE)
    assert shop.get("BUCKET").name == "Bucket"


def test_empty_input():
    assert parse_workshop("").recipes == {}
