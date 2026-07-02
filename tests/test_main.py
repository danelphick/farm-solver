from datetime import timedelta

from farming import FarmUse
from main import print_farming, print_passive, split_pet_production
from passive import PassiveProducer, PassiveProduction, PassiveUse


def test_split_pet_production_separates_pets_from_fast_producers():
    production = PassiveProduction(
        producers=[
            PassiveProducer("sawmill", timedelta(hours=1), {"wood": 10}),
            PassiveProducer("pet Cat", timedelta(days=1), {"drum": 5}),
        ],
        inventory_size=100,
    )
    fast, pets = split_pet_production(production)
    assert [p.name for p in fast.producers] == ["sawmill"]
    assert [p.name for p in pets.producers] == ["pet Cat"]
    # The storehouse capacity carries over to both.
    assert fast.inventory_size == 100 and pets.inventory_size == 100


def test_print_farming_renders_times_and_sources(capsys):
    print_farming(
        [
            FarmUse("Corn", 500, "Corn", timedelta(hours=2)),
            FarmUse("Runestone 11", 10, "Corn", timedelta(days=1)),
            FarmUse("Red Flowers", 2),
        ]
    )
    lines = [line.strip() for line in capsys.readouterr().out.splitlines()]
    # The crop's name is dropped when it matches the item, kept for a rare drop,
    # and a flower outside the crop model shows no time.
    assert "500x Corn in 2h" in lines
    assert "10x Runestone 11 in 1d (via Corn)" in lines
    assert "2x Red Flowers" in lines


def test_print_farming_notes_grape_juices_used(capsys):
    print_farming([FarmUse("Beet", 100, "Beet", timedelta(days=2), grape_juices=28)])
    line = capsys.readouterr().out
    assert "100x Beet in 2d using 28 Grape Juice" in line


def test_print_farming_omits_grape_juices_when_none_used(capsys):
    print_farming([FarmUse("Corn", 500, "Corn", timedelta(hours=2), grape_juices=0)])
    assert "Grape Juice" not in capsys.readouterr().out


def test_passive_total_omits_concurrent_for_a_single_item(capsys):
    print_passive([PassiveUse(item="Corn Oil", quantity=720, time=timedelta(days=30))])
    out = capsys.readouterr().out
    assert "Total time: 30d" in out
    assert "(concurrent)" not in out


def test_passive_total_notes_concurrent_for_several_items(capsys):
    print_passive(
        [
            PassiveUse(item="Corn Oil", quantity=720, time=timedelta(days=30)),
            PassiveUse(item="Steel", quantity=400, time=timedelta(hours=2)),
        ]
    )
    out = capsys.readouterr().out
    # The total is the longest wait, marked concurrent since the waits overlap.
    assert "Total time: 30d (concurrent)" in out
