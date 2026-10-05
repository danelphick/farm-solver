"""Import the game's full item list from a saved Museum Completion wiki page.

The in-game library page "Museum Completion" lists every item in the game,
grouped into sections (Crops, Fish, Items, Seeds, Bait, Meals, Cards, Event).
Save it from the browser and run::

    python scripts/import_museum_items.py ~/Downloads/items.html

This writes the repo-root ``items.csv`` (``item,event``), the hand-maintained
catalogue of every item. ``event`` is blank for an ordinary item; anything
else marks an event item, one usually only obtainable at certain times of
year. It is free text, never parsed: ``March-April`` for a seasonal pet's
months, ``September Apple Bobbing Event`` for a named event, or just ``true``
when the dates are not known yet.

Re-running is a merge, not an overwrite: an item already in ``items.csv``
keeps its ``event`` text (so hand-filled dates survive), and new items are
added -- an Event-section item seeded from :data:`KNOWN_EVENTS`, else
``true``. Items no longer on the page are kept, since the page lags new
content as often as it drops anything. ``**Name**`` (the page's "currently
unobtainable" marker) is stripped to the bare name.

``scripts/generate_items.py`` folds these names into ``data/items.txt``.
"""

from __future__ import annotations

import csv
import html
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pets  # noqa: E402

ITEMS_CSV = REPO_ROOT / "items.csv"

# The event text for Event-section items whose window is known, keyed by
# lower-cased name. Seasonal pets' Event-section items are added from
# pets.SEASONAL_MONTHS by _seasonal_pet_events, so they are not repeated here.
KNOWN_EVENTS: dict[str, str] = {
    "king apple": "September Apple Bobbing Event",
}

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)

# A section heading: <span style="color:red;">Event</span></span></b> Count = 287
_SECTION = re.compile(r'color:red;">([^<]+)</span></span></b>\s*Count\s*=\s*\d+')
# One item: the icon's link, its hidden search text, then the visible name up
# to the next tag.
_ITEM = re.compile(
    r'class="itemimgsm"></a><span style="display:none">[^<]*</span>\s*([^<]+)'
)


def parse_museum(page: str) -> dict[str, list[str]]:
    """``{section: [item names in page order]}`` from the saved page."""
    start = page.find("Museum Completion</a></span>")
    if start == -1:
        raise ValueError("not a saved Museum Completion page")
    body = page[start:]
    headings = list(_SECTION.finditer(body))
    sections: dict[str, list[str]] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        names = [
            html.unescape(match.group(1)).strip().strip("*").strip()
            for match in _ITEM.finditer(body, heading.end(), end)
        ]
        sections[heading.group(1).strip()] = [name for name in names if name]
    return sections


def _seasonal_pet_events() -> dict[str, str]:
    """``{lower item: "March-April"}`` for every seasonal pet's items."""
    tiers = pets.read_pet_tiers(REPO_ROOT / "pets.csv")
    events: dict[str, str] = {}
    for pet, months in pets.SEASONAL_MONTHS.items():
        first, last = min(months), max(months)
        window = f"{_MONTHS[first - 1]}-{_MONTHS[last - 1]}"
        for pet_name, pet_tiers in tiers.items():
            if pet_name.lower() != pet:
                continue
            for items in pet_tiers:
                for item in items:
                    events[item.lower()] = window
    return events


def read_items_csv(path: Path = ITEMS_CSV) -> dict[str, tuple[str, str]]:
    """``{lower item: (name, event)}`` from ``items.csv``; ``{}`` if missing."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    rows = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return {
        row["item"].strip().lower(): (row["item"].strip(), (row.get("event") or "").strip())
        for row in csv.DictReader(rows)
        if (row.get("item") or "").strip()
    }


def merge(
    sections: dict[str, list[str]], existing: dict[str, tuple[str, str]]
) -> dict[str, tuple[str, str]]:
    """``existing`` plus every page item not already in it."""
    seasonal = _seasonal_pet_events()
    merged = dict(existing)
    for section, names in sections.items():
        is_event = section.lower() == "event"
        for name in names:
            key = name.lower()
            if key in merged:
                continue
            event = ""
            if is_event:
                event = KNOWN_EVENTS.get(key) or seasonal.get(key) or "true"
            merged[key] = (name, event)
    return merged


def write_items_csv(items: dict[str, tuple[str, str]], path: Path = ITEMS_CSV) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["item", "event"])
        for name, event in sorted(items.values(), key=lambda row: row[0].lower()):
            writer.writerow([name, event])


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <saved Museum Completion page>", file=sys.stderr)
        return 2
    sections = parse_museum(Path(argv[1]).read_text())
    for section, names in sections.items():
        print(f"{section}: {len(names)}")
    write_items_csv(merge(sections, read_items_csv()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
