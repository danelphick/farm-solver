from pathlib import Path

import pytest

from locksmith import (
    Locksmith,
    Openable,
    PoolItem,
    key_for,
    load_openables,
    read_openables,
)

OPENABLES_CSV = Path(__file__).resolve().parent.parent / "openables.csv"


def test_fixed_openable_averages_its_listed_counts():
    box = Openable(name="Gift Box", fixed={"wood": 10, "board": 5})
    assert box.average_output() == {"wood": 10, "board": 5}


def test_random_openable_spreads_the_expected_count_over_the_pool():
    # A uniform 1-3 (average 2) of one of four items: each averages 2/4 = 0.5.
    bag = Openable(
        name="Bag",
        pool=[
            PoolItem("Silver", 1, 3),
            PoolItem("Gold", 1, 3),
            PoolItem("Iron", 1, 3),
            PoolItem("Tin", 1, 3),
        ],
    )
    output = bag.average_output()
    assert output == {"silver": 0.5, "gold": 0.5, "iron": 0.5, "tin": 0.5}


def test_random_candidates_keep_their_own_ranges():
    # Two equally-likely picks with different ranges: Copper averages 3 on its
    # opens, Gold 10; halved for the 50/50 pick, that is 1.5 and 5.
    bag = Openable(name="Bag", pool=[PoolItem("Copper", 1, 5), PoolItem("Gold", 5, 15)])
    assert bag.average_output() == {"copper": 1.5, "gold": 5}


def test_random_pool_lowercases_and_sums_repeated_items():
    # A duplicated candidate is twice as likely, so it gets twice the share.
    bag = Openable(name="Bag", pool=[PoolItem("Gold", 2, 2), PoolItem("gold", 2, 2)])
    assert bag.average_output() == {"gold": 2}


def test_openable_combines_fixed_and_random_contents():
    box = Openable(
        name="Box",
        fixed={"wood": 3},
        pool=[PoolItem("Wood", 2, 4), PoolItem("Stone", 2, 4)],
    )
    # Fixed 3 wood plus an average 3/2 = 1.5 shared over wood and stone.
    assert box.average_output() == {"wood": 4.5, "stone": 1.5}


def test_empty_openable_produces_nothing():
    assert Openable(name="Empty").average_output() == {}


def test_read_fixed_openable(tmp_path):
    path = tmp_path / "openables.csv"
    path.write_text(
        "Openable,Type,Item,Min,Max\n"
        "Chest,fixed,Ruby,10,\n"
        "Chest,fixed,Jade,10,\n"
    )
    smith = read_openables(path)
    chest = smith.openable("chest")
    assert chest.average_output() == {"ruby": 10, "jade": 10}


def test_read_random_openable(tmp_path):
    path = tmp_path / "openables.csv"
    path.write_text(
        "Openable,Type,Item,Min,Max\n"
        "Sack,random,Copper,1,5\n"
        "Sack,random,Silver,10,20\n"
    )
    sack = read_openables(path).openable("Sack")
    # Copper averages 3 on its opens, Silver 15; halved for the 50/50 pick.
    assert sack.average_output() == {"copper": 1.5, "silver": 7.5}


def test_read_rejects_unknown_type(tmp_path):
    path = tmp_path / "openables.csv"
    path.write_text("Openable,Type,Item,Min,Max\nMystery,weird,Thing,1,\n")
    with pytest.raises(ValueError):
        read_openables(path)


def test_lookup_is_case_insensitive():
    smith = Locksmith(openables={"chest": Openable(name="Chest")})
    assert smith.openable("CHEST") is not None
    assert smith.openable("nope") is None


@pytest.mark.parametrize(
    "name, key",
    [
        ("Small Chest 01", "Small Key"),
        ("Medium Chest 03", "Square Key"),
        ("Large Chest 02", "Treasure Key"),
        ("Mystical Chest 01", "Skeleton Key"),
        ("Grab Bag 01", None),
        ("Bait Bag 01", None),
    ],
)
def test_key_for_matches_chest_tier(name, key):
    assert key_for(name) == key


def test_read_records_the_chest_key(tmp_path):
    path = tmp_path / "openables.csv"
    path.write_text("Openable,Type,Item,Min,Max\nSmall Chest 09,fixed,Ruby,10,\n")
    assert read_openables(path).openable("Small Chest 09").key == "Small Key"


def test_load_real_openables_csv_has_small_chest_01():
    smith = load_openables(OPENABLES_CSV)
    chest = smith.openable("Small Chest 01")
    assert chest.average_output() == {
        "amethyst": 10,
        "aquamarine": 10,
        "emerald": 10,
        "jade": 10,
        "lemon quartz": 10,
        "ruby": 10,
    }
