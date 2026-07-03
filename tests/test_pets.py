from datetime import date, timedelta
from pathlib import Path

import pytest

from pets import (
    Pet,
    Pets,
    load_pets,
    read_pet_tiers,
    seasonal_fraction,
    unlocked_tiers,
)

PETS_CSV = Path(__file__).resolve().parent.parent / "pets.csv"

# Three tiers of four items, enough to exercise every level.
TIERS = [
    ["Acorn", "Berry", "Clay", "Dew"],
    ["Ember", "Fern", "Gem", "Husk"],
    ["Ice", "Jade", "Kelp", "Lily"],
]


def make_pet(level, **kwargs):
    return Pet(name="Rex", level=level, tiers=TIERS, **kwargs)


@pytest.mark.parametrize(
    "level, tiers",
    [(0, 0), (1, 1), (2, 1), (3, 2), (5, 2), (6, 3), (10, 3)],
)
def test_unlocked_tiers(level, tiers):
    assert unlocked_tiers(level) == tiers


# The per-item daily averages quoted in the game, level by level.
@pytest.mark.parametrize(
    "level, per_item",
    [
        (1, 6),
        (2, 12),
        (3, 9),
        (4, 12),
        (5, 15),
        (6, 12),
        (7, 14),
        (8, 16),
        (9, 18),
        (10, 20),
    ],
)
def test_daily_output_matches_the_table(level, per_item):
    output = make_pet(level).daily_output()
    assert set(output.values()) == {per_item}


def test_low_level_only_collects_tier_one():
    output = make_pet(2).daily_output()
    assert set(output) == {"acorn", "berry", "clay", "dew"}


def test_mid_level_collects_two_tiers():
    output = make_pet(4).daily_output()
    assert set(output) == {
        "acorn", "berry", "clay", "dew",
        "ember", "fern", "gem", "husk",
    }


def test_high_level_collects_all_three_tiers():
    output = make_pet(6).daily_output()
    assert len(output) == 12


def test_level_zero_produces_nothing():
    assert make_pet(0).daily_output() == {}


def test_item_names_are_lowercased():
    assert "acorn" in make_pet(1).daily_output()


def test_special_pet_doubles_one_item_at_level_six():
    pet = make_pet(6, double_item="Ice")
    output = pet.daily_output()
    # Every other item is the usual 12 a day; ice is doubled to 24.
    assert output["ice"] == 24
    assert output["acorn"] == 12


def test_special_pet_does_not_double_below_level_six():
    pet = make_pet(5, double_item="Ember")
    output = pet.daily_output()
    # Ice's tier is not even unlocked at level 5, but Ember is; it is not
    # doubled because the pet is below level 6.
    assert output["ember"] == 15


def test_seasonal_pet_is_active_only_in_its_months():
    pet = make_pet(6, months=frozenset({6, 7}))
    assert pet.seasonal
    assert pet.active_in(6)
    assert not pet.active_in(1)


def test_year_round_pet_is_active_every_month():
    pet = make_pet(6)
    assert not pet.seasonal
    assert pet.active_in(1)


JUN_JUL = frozenset({6, 7})


def test_seasonal_fraction_year_round_pet_is_always_one():
    assert seasonal_fraction(None) == 1.0
    assert seasonal_fraction(None, date(2026, 1, 1), timedelta(days=10)) == 1.0


def test_seasonal_fraction_steady_state_is_days_over_year():
    # With no plan span given, June-July is its 61-of-365 yearly share.
    assert seasonal_fraction(JUN_JUL) == pytest.approx(61 / 365)


def test_seasonal_fraction_fully_in_season_is_one():
    # A month wholly inside the window overlaps completely.
    assert seasonal_fraction(JUN_JUL, date(2026, 6, 1), timedelta(days=30)) == 1.0


def test_seasonal_fraction_out_of_season_is_zero():
    # A January plan never reaches the June-July window.
    assert seasonal_fraction(JUN_JUL, date(2026, 1, 1), timedelta(days=31)) == 0.0


def test_seasonal_fraction_partial_overlap():
    # May 16 for 31 days is May 16-Jun 15; only the 15 June days are in season.
    frac = seasonal_fraction(JUN_JUL, date(2026, 5, 16), timedelta(days=31))
    assert frac == pytest.approx(15 / 31)


def test_seasonal_fraction_multi_year_plan_settles_to_steady_state():
    # Over many whole years the start phase washes out to the yearly share.
    frac = seasonal_fraction(JUN_JUL, date(2026, 8, 1), timedelta(days=10 * 365))
    assert frac == pytest.approx(61 / 365)


def test_pets_lookup_is_case_insensitive():
    pets = Pets(pets={"rex": make_pet(1)})
    assert pets.pet("REX") is not None
    assert pets.pet("Nope") is None


def test_pets_daily_output_combines_active_pets():
    pets = Pets(
        pets={
            "rex": Pet(name="Rex", level=1, tiers=[["Acorn"]]),
            "fido": Pet(name="Fido", level=1, tiers=[["Acorn"]]),
        }
    )
    # Both level-1 single-item pets gather 24 acorns a day; combined, 48.
    assert pets.daily_output() == {"acorn": 48}


def test_read_pet_tiers_groups_by_tier(tmp_path):
    path = tmp_path / "pets.csv"
    path.write_text(
        "Pet,Tier,Item\n"
        "Cat,1,Drum\n"
        "Cat,2,Bluegill\n"
        "Cat,1,Carp\n"
        "Cat,3,Pearl\n"
    )
    tiers = read_pet_tiers(path)
    # Tiers come back in order, items in file order within each tier.
    assert tiers == {"Cat": [["Drum", "Carp"], ["Bluegill"], ["Pearl"]]}


def test_read_real_pets_csv_has_cat_with_three_full_tiers():
    tiers = read_pet_tiers(PETS_CSV)
    assert tiers["Cat"] == [
        ["Drum", "Crappie", "Feathers", "Carp"],
        ["Blue Catfish", "Blue Shell", "Bluegill", "Blue Feathers"],
        ["Pearl", "Gold Feather", "Globber", "Shinefish"],
    ]


def test_boost_scales_every_item():
    # A 1.5x boost lifts a level-1 pet's four tier-1 items from 6 to 9 a day.
    output = make_pet(1, boost=1.5).daily_output()
    assert set(output.values()) == {9}


def test_boost_scales_the_doubled_item_too():
    pet = make_pet(6, double_item="Ice", boost=1.5)
    output = pet.daily_output()
    # Ordinary items go from 12 to 18; the doubled item from 24 to 36.
    assert output["acorn"] == 18
    assert output["ice"] == 36


def test_load_pets_passes_the_boost_through():
    pets = load_pets(PETS_CSV, {"Cat": 1}, boost=1.5)
    assert pets.pet("Cat").boost == 1.5
    # Level-1 Cat's tier-1 items rise from 6 to 9 a day.
    assert pets.pet("Cat").daily_output()["drum"] == 9


def test_load_pets_applies_levels_and_fixed_facts():
    pets = load_pets(PETS_CSV, {"Owl": 6, "Wolf": 3})
    # Levels come from the mapping; unlisted pets default to 0.
    assert pets.pet("Owl").level == 6
    assert pets.pet("Cat").level == 0
    # The Owl is the special pet: 2x honey once at level 6.
    assert pets.pet("Owl").double_item == "Honey"
    assert pets.pet("Owl").daily_output()["honey"] == 24
    # The Wolf is seasonal (September-October).
    assert pets.pet("Wolf").months == frozenset({9, 10})


def test_load_pets_marks_the_four_seasonal_pets():
    pets = load_pets(PETS_CSV)
    seasonal = {name for name, pet in pets.pets.items() if pet.seasonal}
    assert seasonal == {"wolf", "polar bear", "hummingbird", "shark"}


def test_load_pets_daily_output_respects_season():
    pets = load_pets(PETS_CSV, {"Shark": 6})
    # The Shark only produces in June-July.
    assert pets.daily_output(month=1) == {}
    assert pets.daily_output(month=6)  # non-empty in season


def test_pets_daily_output_skips_out_of_season_pets():
    pets = Pets(
        pets={
            "rex": Pet(name="Rex", level=1, tiers=[["Acorn"]]),
            "snow": Pet(
                name="Snow", level=1, tiers=[["Berry"]], months=frozenset({12, 1})
            ),
        }
    )
    # In July only Rex is active.
    assert pets.daily_output(month=7) == {"acorn": 24}
    # In December both are.
    assert pets.daily_output(month=12) == {"acorn": 24, "berry": 24}
