from datetime import timedelta

import pytest

from passive import (
    HOURLY,
    TEN_MINUTES,
    PassiveProducer,
    PassiveProduction,
    PassiveUse,
    format_duration,
    parse_duration,
    time_to_produce_all,
    total_time,
)

SAWMILL = PassiveProducer(
    name="sawmill",
    period=HOURLY,
    outputs={"board": 60000, "wood": 46000, "oak": 6000},
)


def test_rate_per_minute_from_hourly_output():
    production = PassiveProduction(producers=[SAWMILL])
    # 60000 boards an hour is 1000 a minute.
    assert production.rate_per_minute("board") == 1000


def test_rate_is_case_insensitive():
    production = PassiveProduction(producers=[SAWMILL])
    assert production.rate_per_minute("Board") == 1000


def test_produces_reflects_known_items():
    production = PassiveProduction(producers=[SAWMILL])
    assert production.produces("oak")
    assert not production.produces("steel")


def test_inventory_size_caps_high_production_rate():
    production = PassiveProduction(producers=[SAWMILL], inventory_size=49440)
    # Board makes 60000/hr but only 49440 fit the storehouse; the excess is
    # voided, so the usable rate is 49440 an hour.
    assert production.rate_per_minute("board") == 49440 / 60


def test_inventory_size_does_not_affect_low_production_rate():
    production = PassiveProduction(producers=[SAWMILL], inventory_size=49440)
    # Oak makes 6000/hr, well under the storehouse, so it is unaffected.
    assert production.rate_per_minute("oak") == 100


def test_time_to_produce_quantity_rounds_up_to_whole_cycles():
    production = PassiveProduction(producers=[SAWMILL])
    # 100000 boards need a second hourly cycle (60000 then 120000), so the wait
    # rounds up from 100 minutes to a whole 2 hours.
    assert production.time_to_produce("board", 100000) == timedelta(hours=2)


def test_time_to_produce_exact_multiple_is_not_rounded_up():
    production = PassiveProduction(producers=[SAWMILL])
    assert production.time_to_produce("board", 120000) == timedelta(hours=2)


def test_time_to_produce_unknown_item_is_none():
    production = PassiveProduction(producers=[SAWMILL])
    assert production.time_to_produce("steel", 100) is None


def test_rates_add_across_producers():
    other = PassiveProducer(
        name="ten_min_mill", period=TEN_MINUTES, outputs={"board": 1000}
    )
    production = PassiveProduction(producers=[SAWMILL, other])
    # 1000 a minute from the sawmill plus 1000 per 10 minutes = 100 a minute.
    assert production.rate_per_minute("board") == 1100


def test_time_to_produce_all_keeps_only_produced_materials():
    production = PassiveProduction(producers=[SAWMILL])
    times = time_to_produce_all(
        production, {"Board": 60000, "Oak": 6000, "Steel": 500}
    )
    assert times == {
        "Board": timedelta(hours=1),
        "Oak": timedelta(hours=1),
    }


@pytest.mark.parametrize(
    "text, expected",
    [
        ("5d", timedelta(days=5)),
        ("3h", timedelta(hours=3)),
        ("90m", timedelta(minutes=90)),
        ("1d6h30m", timedelta(days=1, hours=6, minutes=30)),
        ("  2H ", timedelta(hours=2)),
        ("2y", timedelta(days=730)),
        ("1y30d", timedelta(days=395)),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "5x", "abc", "1d6"])
def test_parse_duration_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize(
    "duration, expected",
    [
        (timedelta(minutes=100), "1h 40m"),
        (timedelta(hours=1), "1h"),
        (timedelta(days=1, hours=2, minutes=3), "1d 2h 3m"),
        (timedelta(0), "0m"),
        # A year or more switches to years and days, dropping hours/minutes.
        (timedelta(days=365), "1y"),
        (timedelta(days=365, hours=5), "1y"),
        (timedelta(days=400), "1y 35d"),
        (timedelta(days=25447), "69y 262d"),
    ],
)
def test_format_duration(duration, expected):
    assert format_duration(duration) == expected


def test_total_time_is_the_longest_concurrent_wait():
    uses = [
        PassiveUse("Board", 100000, timedelta(minutes=100)),
        PassiveUse("Wood", 46000, timedelta(hours=1)),
    ]
    # They run concurrently, so the total is the longest, not the sum.
    assert total_time(uses) == timedelta(minutes=100)


def test_total_time_of_nothing_is_zero():
    assert total_time([]) == timedelta()
