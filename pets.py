"""Pet model for Farm Solver.

Pets fetch items over time. A pet of level ``N`` retrieves ``N`` items an hour,
so ``24 * N`` items a day, spread evenly across the items in the tiers it has
unlocked. There are three tiers of four items each, unlocked by level:

* levels 1-2 collect from tier 1 only (4 items),
* levels 3-5 add tier 2 (8 items),
* levels 6+ add tier 3 (12 items).

So a level-3 pet gathers ``72`` items a day across ``8`` items -- an average of
``9`` of each -- and a level-10 pet gathers ``240`` across ``12``, an average of
``20`` of each.

Most pets produce all year round. Seasonal ones only produce during a short
window of the calendar (two months). One special pet produces twice as many of
a single item once it reaches level 6.

This module only models the structures; populating them from data is done
elsewhere (parsing).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

# The bundled tier data, used unless another path is given.
DEFAULT_PETS_CSV = Path(__file__).with_name("pets.csv")

# A pet retrieves this many items per hour for each level it has.
ITEMS_PER_HOUR_PER_LEVEL = 1
HOURS_PER_DAY = 24
# Each tier holds this many items, and there are this many tiers.
ITEMS_PER_TIER = 4
TIER_COUNT = 3

# The levels at which the second and third tiers unlock.
TIER_TWO_LEVEL = 3
TIER_THREE_LEVEL = 6

# The special pet doubles its chosen item once it reaches this level.
DOUBLE_ITEM_LEVEL = 6

# The crunchy omelette increases every pet's retrieval by 50%.
CRUNCHY_OMELETTE_MULTIPLIER = 1.5

# Fixed game facts about individual pets, keyed by lower-cased name.
#
# Seasonal pets only drop items during two calendar months:
SEASONAL_MONTHS: dict[str, frozenset[int]] = {
    "polar bear": frozenset({1, 2}),  # January-February
    "hummingbird": frozenset({3, 4}),  # March-April
    "shark": frozenset({6, 7}),  # June-July
    "wolf": frozenset({9, 10}),  # September-October
}
# The special pet doubles one item at level 6:
DOUBLE_ITEMS: dict[str, str] = {"owl": "Honey"}


def unlocked_tiers(level: int) -> int:
    """How many of the three tiers a pet of this level collects from."""
    if level < 1:
        return 0
    if level < TIER_TWO_LEVEL:
        return 1
    if level < TIER_THREE_LEVEL:
        return 2
    return TIER_COUNT


@dataclass
class Pet:
    """A pet that gathers items from the tiers it has unlocked.

    ``tiers`` lists up to three tiers of item names (four each). ``months``, if
    set, limits a seasonal pet to those calendar months (1-12); ``None`` means
    it produces all year round. ``double_item``, if set, names the item the
    special pet produces at twice the normal rate once it reaches level 6.
    ``boost`` scales retrieval (e.g. 1.5 with the crunchy omelette).
    """

    name: str
    level: int
    tiers: list[list[str]] = field(default_factory=list)
    months: frozenset[int] | None = None
    double_item: str | None = None
    boost: float = 1.0

    @property
    def seasonal(self) -> bool:
        return self.months is not None

    def active_in(self, month: int) -> bool:
        """Whether the pet produces in the given calendar month (1-12)."""
        return self.months is None or month in self.months

    def active_items(self) -> list[str]:
        """Item names across every tier this pet's level has unlocked."""
        items: list[str] = []
        for tier in self.tiers[: unlocked_tiers(self.level)]:
            items.extend(tier)
        return items

    def daily_output(self) -> dict[str, float]:
        """Average items gathered per day while the pet is active.

        The ``24 * level`` items a day (scaled by ``boost``) are split evenly
        across the active items (keyed by lower-cased name); the special pet's
        chosen item then gets twice that share once the pet is level 6 or above.
        """
        items = self.active_items()
        if not items:
            return {}
        per_hour = self.level * ITEMS_PER_HOUR_PER_LEVEL * self.boost
        per_item = per_hour * HOURS_PER_DAY / len(items)
        output: dict[str, float] = {}
        for item in items:
            key = item.lower()
            output[key] = output.get(key, 0.0) + per_item
        if self.double_item and self.level >= DOUBLE_ITEM_LEVEL:
            key = self.double_item.lower()
            if key in output:
                output[key] += per_item
        return output


@dataclass
class Pets:
    """The known pets, keyed by lower-cased name for case-insensitive lookup."""

    pets: dict[str, Pet] = field(default_factory=dict)

    def pet(self, name: str) -> Pet | None:
        return self.pets.get(name.lower())

    def daily_output(self, month: int | None = None) -> dict[str, float]:
        """Combined average daily output across the pets active that month.

        With no ``month``, every pet counts; with one, seasonal pets out of
        season contribute nothing.
        """
        totals: dict[str, float] = {}
        for pet in self.pets.values():
            if month is not None and not pet.active_in(month):
                continue
            for item, quantity in pet.daily_output().items():
                totals[item] = totals.get(item, 0.0) + quantity
        return totals


def read_pet_tiers(path: Path) -> dict[str, list[list[str]]]:
    """Each pet's tiers of item names, read from a ``Pet,Tier,Item`` CSV.

    The result maps a pet's display name to its list of tiers (tier 1 first),
    each tier a list of item names in file order. This is the fixed game data;
    a pet's level, season, and special item are supplied separately.
    """
    rows_by_pet: dict[str, dict[int, list[str]]] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            pet = row["Pet"].strip()
            tier = int(row["Tier"])
            item = row["Item"].strip()
            rows_by_pet.setdefault(pet, {}).setdefault(tier, []).append(item)
    return {
        pet: [tiers[t] for t in sorted(tiers)] for pet, tiers in rows_by_pet.items()
    }


def load_pets(
    path: Path, levels: dict[str, int] | None = None, boost: float = 1.0
) -> Pets:
    """Build the pets from the tier CSV, per-pet ``levels``, and fixed facts.

    ``levels`` maps a pet's name to its current level (case-insensitive); a pet
    absent from it defaults to level 0 and so produces nothing. ``boost`` scales
    every pet's retrieval (e.g. 1.5 with the crunchy omelette). The seasonal
    windows and the special pet's doubled item come from :data:`SEASONAL_MONTHS`
    and :data:`DOUBLE_ITEMS`.
    """
    by_name = {name.lower(): level for name, level in (levels or {}).items()}
    pets: dict[str, Pet] = {}
    for name, tiers in read_pet_tiers(path).items():
        key = name.lower()
        pets[key] = Pet(
            name=name,
            level=by_name.get(key, 0),
            tiers=tiers,
            months=SEASONAL_MONTHS.get(key),
            double_item=DOUBLE_ITEMS.get(key),
            boost=boost,
        )
    return Pets(pets=pets)


def pets_from_config(config_section: dict, path: Path | None = None) -> Pets:
    """Build the pets from a ``[pets]`` config section.

    Integer entries are per-pet levels; the ``use_crunchy_omelette`` flag turns
    on the 1.5x retrieval boost. Any other keys are ignored. Tier items come
    from ``path`` (the bundled :data:`DEFAULT_PETS_CSV` by default).
    """
    boost = (
        CRUNCHY_OMELETTE_MULTIPLIER
        if config_section.get("use_crunchy_omelette")
        else 1.0
    )
    # Flags like use_crunchy_omelette are bools (an int subclass), so exclude
    # them; only genuine integer levels remain.
    levels = {
        name: level
        for name, level in config_section.items()
        if isinstance(level, int) and not isinstance(level, bool)
    }
    return load_pets(path or DEFAULT_PETS_CSV, levels, boost)
