"""Generate the ``[trading.ap.prices]`` block from a static price snapshot.

FarmRPG resources trade between players for Arnold Palmers (a finer-grained
currency than gold, which only settles in units of 5). This emits the config
table of those AP trade prices -- the low end of each quoted range, in "apk"
(Arnold Palmers per 1000, matching ``[trading] grapes``) -- for every tradeable
item the SOLVER knows: masterable (``mastery_items.txt``), a workshop recipe
material/product, or a CSV drop/seed. That excludes cosmetics/prizes the solver
never reasons about (cards, chests, eggs, gems), and the five consumables already
priced in gold under ``[trading]``.

**No network access.** The prices come from ``scripts/trade_prices_snapshot.txt``
-- a plain ``Name|price`` snapshot captured once, by hand, from the community
price list. This script only parses that committed file; it deliberately contains
nothing that fetches the live site or defeats its bot challenge.

Prices on the site are quoted either per 1000 (``8-10 AP/k``) or per single item
(``5-12 AP``, meals and one-offs). Both are normalised to apk here (a per-each
price is multiplied by 1000) so the config carries one unit and
``config.arnold_palmer_prices`` divides by 1000 uniformly.

Run with: ``python scripts/generate_trade_prices.py`` -- it prints the block to
stdout; paste it under ``[trading.ap]`` in ``config.toml`` and
``web_default_config.toml`` (a one-time/occasional step, then edit directly).
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import crafting  # noqa: E402

SNAPSHOT = Path(__file__).with_name("trade_prices_snapshot.txt")

# Already priced in gold under [trading]; left gold-only (no duplicate AP channel).
CONSUMABLES = {
    "arnold palmer", "large net", "orange juice", "apple cider",
    "hickory omelette", "cookie set", "grape juice",
}

_MEAL_SUFFIX = re.compile(r"\s*\(meal\)\s*$")
_FIRST_NUMBER = re.compile(r"-?\d*\.?\d+")


def solver_items() -> set[str]:
    """Every lower-cased item name the solver reasons about."""
    names: set[str] = set()
    for line in (REPO_ROOT / "mastery_items.txt").read_text().splitlines():
        if line.strip():
            names.add(line.strip().lower())
    workshop = crafting.load_workshop()
    for recipe in workshop.recipes.values():
        names.add(recipe.name.lower())
        names |= {m.lower() for m in recipe.materials}
    for csv_name in (
        "crops.csv", "seed_drops.csv", "fishing.csv",
        "exploring.csv", "openables.csv",
    ):
        with (REPO_ROOT / csv_name).open(newline="") as fh:
            for row in csv.reader(fh):
                for cell in row:
                    cell = cell.strip().lower()
                    if cell and not cell.replace(".", "").replace("-", "").isdigit():
                        names.add(cell)
    return names


def parse_snapshot() -> list[tuple[str, float, str]]:
    """``[(item_name, apk_low_end, original_price_string)]`` from the snapshot.

    Skips rows with no AP price (``x``). A per-each price (no ``/k``) is scaled to
    apk (x1000); a per-1000 price (``/k``) is taken as-is.
    """
    rows: list[tuple[str, float, str]] = []
    for line in SNAPSHOT.read_text().splitlines():
        if "|" not in line:
            continue
        name, price = (part.strip() for part in line.split("|", 1))
        name = _MEAL_SUFFIX.sub("", name)
        match = _FIRST_NUMBER.search(price)
        if match is None:  # "x" / non-numeric -> not AP-tradeable
            continue
        low = float(match.group())
        apk = low if "/k" in price else low * 1000  # per-each -> per-1000
        rows.append((name, apk, price))
    return rows


def _fmt(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def main() -> None:
    universe = solver_items()
    entries: list[tuple[str, float, str]] = []
    for name, apk, price in parse_snapshot():
        key = name.lower()
        if key in universe and key not in CONSUMABLES:
            entries.append((key, apk, price))
    entries.sort()

    print("[trading.ap.prices]  # AP trade prices in apk (Arnold Palmers per 1000)")
    for key, apk, price in entries:
        note = price if "/k" in price else f"{price} each"
        print(f'"{key}" = {_fmt(apk):<7} # {note}')
    print(f"\n# {len(entries)} tradeable items", file=sys.stderr)


if __name__ == "__main__":
    main()
