from datetime import timedelta

from crops import Crop, Farm
from farming import is_farmed, plan_farming


def make_farm(rows=13):
    return Farm(
        crops={
            "corn": Crop("Corn", timedelta(hours=1), {"corn": 4, "runestone 11": 0.01}),
        },
        rows=rows,
    )


def test_is_farmed_recognises_the_flowers():
    assert is_farmed("Blue Flowers")
    assert is_farmed("red flowers")


def test_is_farmed_excludes_non_farm_items():
    assert not is_farmed("Wood")
    assert not is_farmed("Peach")


def test_plan_farming_routes_a_crop_with_its_time_and_source():
    uses, rest = plan_farming({"Corn": 100, "Wood": 5}, make_farm())
    assert rest == {"Wood": 5}
    (corn,) = uses
    assert (corn.item, corn.quantity, corn.source) == ("Corn", 100, "Corn")
    assert corn.time is not None


def test_plan_farming_routes_a_rare_drop_via_its_crop():
    uses, _ = plan_farming({"Runestone 11": 3}, make_farm())
    (use,) = uses
    assert use.source == "Corn"  # grown by planting Corn
    assert use.time is not None


def test_plan_farming_keeps_flowers_without_a_time():
    uses, _ = plan_farming({"Red Flowers": 2}, make_farm())
    (use,) = uses
    assert use.item == "Red Flowers"
    assert use.source is None and use.time is None


def test_plan_farming_leaves_unfarmable_materials():
    uses, rest = plan_farming({"Wood": 5, "Board": 7}, make_farm())
    assert uses == []
    assert rest == {"Wood": 5, "Board": 7}


def test_plan_farming_without_rows_leaves_crop_only_drops_but_keeps_flowers():
    # With no rows nothing grows, so a crop-only drop (not a fixed farmed item)
    # falls through to the remainder; the fixed farmed flowers stay recognised.
    uses, rest = plan_farming({"Runestone 11": 3, "Red Flowers": 2}, make_farm(rows=0))
    assert rest == {"Runestone 11": 3}
    (use,) = uses
    assert use.item == "Red Flowers" and use.time is None
