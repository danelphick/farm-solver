import pytest

from pets import Pet, Pets, unlocked_tiers

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
