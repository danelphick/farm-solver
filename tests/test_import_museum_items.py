import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import import_museum_items as museum


def _item(name: str) -> str:
    return (
        '<a href="x"><img src="x.png" alt="x" class="itemimgsm"></a>'
        f'<span style="display:none">{name.lower()}</span> {name} '
    )


PAGE = (
    '<a href="#">Museum Completion</a></span>'
    '<b><span style="font-size:20px;"><span style="color:red;">Items</span></span></b>'
    " Count = 2<br>" + _item("Wood") + _item("**Duck Egg**") + "<br>"
    '<b><span style="font-size:20px;"><span style="color:red;">Event</span></span></b>'
    " Count = 3<br>" + _item("King Apple") + _item("Fall Leaves") + _item("Pi&ntilde;ata")
)


def test_parse_groups_items_by_section_and_strips_unobtainable_marker():
    assert museum.parse_museum(PAGE) == {
        "Items": ["Wood", "Duck Egg"],
        "Event": ["King Apple", "Fall Leaves", "Piñata"],
    }


def test_merge_flags_event_items_with_known_windows_else_true():
    merged = museum.merge(museum.parse_museum(PAGE), {})
    assert merged["wood"] == ("Wood", "")
    assert merged["king apple"] == ("King Apple", "September Apple Bobbing Event")
    # The Wolf is a September-October seasonal pet.
    assert merged["fall leaves"] == ("Fall Leaves", "September-October")
    assert merged["piñata"] == ("Piñata", "true")


def test_merge_keeps_hand_filled_event_text():
    existing = {"piñata": ("Piñata", "May, Cinco de Mayo")}
    merged = museum.merge(museum.parse_museum(PAGE), existing)
    assert merged["piñata"] == ("Piñata", "May, Cinco de Mayo")


def test_items_csv_round_trips(tmp_path):
    path = tmp_path / "items.csv"
    items = {"wood": ("Wood", ""), "king apple": ("King Apple", "September")}
    museum.write_items_csv(items, path)
    assert museum.read_items_csv(path) == items
