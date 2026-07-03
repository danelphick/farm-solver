"""Openable items (the Locksmith) for Farm Solver.

Some items can be opened at the Locksmith, adding their contents to the
inventory (and raising mastery in the process). There are two kinds of
openable:

* **fixed** -- opening one always yields a set number of each of its items;
* **random** -- opening one yields a single item drawn uniformly at random from
  a pool, in a uniform whole-number count between that item's own ``low`` and
  ``high`` inclusive.

Chests also cost a key to open, one per chest, by tier: Small Chests need a
Small Key, Medium Chests a Square Key, Large Chests a Treasure Key, and Mystical
Chests a Skeleton Key. Bags open freely.

For solving we only care about the *average* haul per open. A fixed openable
averages exactly its listed counts. In a random one each of the ``N`` pool
entries is equally likely to be the one picked, so entry ``i`` averages
``(low_i + high_i) / 2 / N`` per open. An openable may carry both kinds at once,
in which case the averages simply add.

This module only models the structures and their average yield; populating them
from data is done via the bundled CSV.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

# The bundled openable data, used unless another path is given.
DEFAULT_OPENABLES_CSV = Path(__file__).with_name("openables.csv")

# CSV ``Type`` column values.
FIXED = "fixed"
RANDOM = "random"

# Chests cost one key to open, by tier (matched against the openable's name).
# Bags and other openables need no key.
CHEST_KEYS: dict[str, str] = {
    "Small Chest": "Small Key",
    "Medium Chest": "Square Key",
    "Large Chest": "Treasure Key",
    "Mystical Chest": "Skeleton Key",
}


def key_for(name: str) -> str | None:
    """The key an openable named ``name`` costs to open, or ``None`` for none."""
    for prefix, key in CHEST_KEYS.items():
        if name.startswith(prefix):
            return key
    return None


@dataclass
class PoolItem:
    """One candidate of a random openable: an item and its own count range.

    When the openable is opened this item might be the one picked; if so it
    yields a uniform whole-number count between ``low`` and ``high`` inclusive.
    """

    item: str
    low: int
    high: int

    @property
    def average(self) -> float:
        """Expected count of this item on the opens where it is the one picked."""
        return (self.low + self.high) / 2


@dataclass
class Openable:
    """An item that can be opened at the Locksmith for its contents.

    ``fixed`` maps a lower-cased item name to the guaranteed count yielded each
    open. ``pool`` is the list of candidates, one of which is drawn at random
    per open; an empty pool means the openable has no random component. ``key``,
    if set, names the item consumed to open one (chests need a key; bags do not).
    """

    name: str
    key: str | None = None
    fixed: dict[str, float] = field(default_factory=dict)
    pool: list[PoolItem] = field(default_factory=list)

    def average_output(self) -> dict[str, float]:
        """Average items gained per open, keyed by lower-cased item name.

        Fixed contents count in full; each random candidate contributes its own
        expected count divided by the pool size, since it is picked one open in
        ``N``.
        """
        output: dict[str, float] = {}
        for item, count in self.fixed.items():
            key = item.lower()
            output[key] = output.get(key, 0.0) + count
        if self.pool:
            size = len(self.pool)
            for entry in self.pool:
                key = entry.item.lower()
                output[key] = output.get(key, 0.0) + entry.average / size
        return output


@dataclass
class Locksmith:
    """The known openables, keyed by lower-cased name for case-insensitive lookup.

    ``names`` maps the lower-cased form of every openable, key and contained item
    back to its original-cased display name, so a report can label them nicely
    (the models elsewhere work in lower case).
    """

    openables: dict[str, Openable] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def openable(self, name: str) -> Openable | None:
        return self.openables.get(name.lower())


def read_openables(path: Path) -> Locksmith:
    """Read openables from an ``Openable,Type,Item,Min,Max`` CSV.

    Each row belongs to a named openable. ``fixed`` rows give an item and the
    number ``Min`` of it produced each open (``Max`` is ignored). ``random``
    rows add a candidate to that openable's pool, ``Min``/``Max`` being that
    candidate's own count range.
    """
    openables: dict[str, Openable] = {}
    names: dict[str, str] = {}
    for row in _rows(path):
        name = row["Openable"].strip()
        kind = row["Type"].strip().lower()
        item = row["Item"].strip()
        openable = openables.setdefault(
            name.lower(), Openable(name=name, key=key_for(name))
        )
        names.setdefault(name.lower(), name)
        names.setdefault(item.lower(), item)
        if openable.key is not None:
            names.setdefault(openable.key.lower(), openable.key)
        if kind == FIXED:
            key = item.lower()
            openable.fixed[key] = openable.fixed.get(key, 0.0) + int(row["Min"])
        elif kind == RANDOM:
            openable.pool.append(
                PoolItem(item=item, low=int(row["Min"]), high=int(row["Max"]))
            )
        else:
            raise ValueError(f"unknown openable type {kind!r} for {name!r}")
    return Locksmith(openables=openables, names=names)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def load_openables(path: Path | None = None) -> Locksmith:
    """Load the openables from ``path`` (the bundled CSV by default)."""
    return read_openables(path or DEFAULT_OPENABLES_CSV)
