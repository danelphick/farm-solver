"""Generate the item-name catalogues served to the web UI's goal pickers.

Writes two files under the repo-root ``data/`` directory, which
``farmweb.settings.STATICFILES_DIRS`` serves at ``/static/solverapp/``:

* ``mastery_items.txt`` -- items the Mastery goal picker suggests. Sourced
  from a single snapshot, the newest ``mydata/mastery-*.txt`` file: the
  user's own mastery history already tracks nearly every masterable item, so
  one snapshot is enough, and unioning the CSVs would pull in plenty of
  non-masterable items (fish, chest drops, ...). Not web-only: the ascension
  mode's candidate universe is this same file
  (``main.load_mastery_catalogue``). A plain text file can't carry a
  "generated" comment without the pickers treating it as an item, so always
  regenerate with this script, never edit by hand.
* ``items.txt`` -- items the Item goal picker suggests. The broad union of
  every ``mydata/mastery-*.txt``/``inventory-*.txt`` snapshot plus every
  per-domain data file (crops, seed drops, fishing, exploring, pets,
  openables, tower, workshop recipes + materials), plus the full game
  catalogue in ``items.csv`` (see ``scripts/import_museum_items.py``).

This is a one-time/occasional generation step (run again after mastery or
inventory data changes, or new game content) -- the output is checked into
git like the other static assets, not regenerated on every request.

Run with: ``python scripts/generate_items.py``
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import crafting  # noqa: E402
import parsing  # noqa: E402

MYDATA_DIR = REPO_ROOT / "mydata"
DATA_DIR = REPO_ROOT / "data"


def _latest_dated_file(directory: Path, prefix: str) -> Path | None:
    """The most recent ``{prefix}*`` file in ``directory``, or ``None``.

    Mirrors ``main._latest_dated_file``: an ISO-dated suffix
    (``mastery-2026-07-10.txt``) sorts correctly as a plain string.
    """
    candidates = sorted(p for p in directory.glob(f"{prefix}*") if p.is_file())
    return candidates[-1] if candidates else None


def _csv_column(path: Path, column: str) -> set[str]:
    with path.open(newline="") as fh:
        return {
            row[column].strip()
            for row in csv.DictReader(fh)
            if row.get(column, "").strip()
        }


def _write_sorted(path: Path, names: set[str]) -> None:
    """Dedupe case-insensitively (first-seen casing wins), then sort and write."""
    by_lower: dict[str, str] = {}
    for name in names:
        by_lower.setdefault(name.lower(), name)
    path.write_text("\n".join(sorted(by_lower.values(), key=str.lower)) + "\n")


def mastery_items() -> set[str]:
    latest = _latest_dated_file(MYDATA_DIR, "mastery")
    if latest is None:
        return set()
    masteries = parsing.parse_masteries(latest.read_text())
    return {mastery.item for mastery in masteries.values()}


def broad_items() -> set[str]:
    names: set[str] = set()

    for path in MYDATA_DIR.glob("mastery-*.txt"):
        masteries = parsing.parse_masteries(path.read_text())
        names |= {mastery.item for mastery in masteries.values()}
    for path in MYDATA_DIR.glob("inventory-*.txt"):
        inventory = parsing.parse_inventory(path.read_text())
        names |= {entry.name for entry in inventory.items.values()}

    names |= _csv_column(REPO_ROOT / "crops.csv", "Crop")
    names |= _csv_column(REPO_ROOT / "seed_drops.csv", "Item")
    names |= _csv_column(REPO_ROOT / "fishing.csv", "Item")
    names |= _csv_column(REPO_ROOT / "exploring.csv", "Item")
    names |= _csv_column(REPO_ROOT / "pets.csv", "Item")
    names |= _csv_column(REPO_ROOT / "openables.csv", "Item")
    names |= _csv_column(REPO_ROOT / "tower.csv", "Mastery Required")
    names |= _csv_column(REPO_ROOT / "items.csv", "item")

    workshop = crafting.load_workshop()
    for recipe in workshop.recipes.values():
        names.add(recipe.name)
        names |= set(recipe.materials)

    return names


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write_sorted(DATA_DIR / "mastery_items.txt", mastery_items())
    _write_sorted(DATA_DIR / "items.txt", broad_items())


if __name__ == "__main__":
    main()
