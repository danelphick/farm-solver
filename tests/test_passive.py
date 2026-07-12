from datetime import timedelta

import pytest

from passive import (
    DAILY,
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
        ("3s", timedelta(seconds=3)),
        ("1m30s", timedelta(minutes=1, seconds=30)),
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


def test_quantized_wait_rounds_up_to_whole_deposits():
    production = PassiveProduction(
        producers=[PassiveProducer("raptor pen", DAILY, {"antlers": 100})]
    )
    # 250 antlers at 100 a day: the third deposit lands at day 3, so the wait is
    # a whole 3 days, not the continuous 2.5-day estimate.
    assert production.quantized_wait("antlers", 250) == timedelta(days=3)


def test_quantized_wait_exact_multiple_is_not_rounded_up():
    production = PassiveProduction(
        producers=[PassiveProducer("raptor pen", DAILY, {"antlers": 100})]
    )
    assert production.quantized_wait("antlers", 200) == timedelta(days=2)
    # Float noise a hair past an exact multiple must not cost a whole extra day.
    assert production.quantized_wait("antlers", 200.0000001) == timedelta(days=2)


def test_quantized_wait_of_nothing_is_zero():
    production = PassiveProduction(
        producers=[PassiveProducer("raptor pen", DAILY, {"antlers": 100})]
    )
    assert production.quantized_wait("antlers", 0) == timedelta(0)


def test_quantized_wait_unknown_item_is_none():
    production = PassiveProduction(
        producers=[PassiveProducer("raptor pen", DAILY, {"antlers": 100})]
    )
    assert production.quantized_wait("steel", 100) is None


def test_quantized_wait_deposits_are_storehouse_clamped():
    production = PassiveProduction(
        producers=[PassiveProducer("raptor pen", DAILY, {"antlers": 2_617_810})],
        inventory_size=49_440,
    )
    # Each daily deposit fills the storehouse and voids the rest, so one
    # storehouse-worth takes a day and one item more takes a second day.
    assert production.quantized_wait("antlers", 49_440) == timedelta(days=1)
    assert production.quantized_wait("antlers", 49_441) == timedelta(days=2)


def test_quantized_wait_staggers_same_period_producers():
    # The pen and its snare share a daily period but not a phase: both clamp to
    # a full storehouse, landing 12 hours apart -- so one storehouse-worth takes
    # 12 hours, not a day, and the schedule steps every half day thereafter.
    production = PassiveProduction(
        producers=[
            PassiveProducer("raptor pen", DAILY, {"antlers": 2_617_810}),
            PassiveProducer("raptor pen snare", DAILY, {"antlers": 261_781}),
        ],
        inventory_size=49_440,
    )
    assert production.quantized_wait("antlers", 49_440) == timedelta(hours=12)
    assert production.quantized_wait("antlers", 60_000) == timedelta(hours=24)
    assert production.quantized_wait("antlers", 98_880) == timedelta(hours=24)
    assert production.quantized_wait("antlers", 98_881) == timedelta(hours=36)


def test_quantized_wait_assumes_worst_case_deposit_order():
    # Unequal same-period deposits arrive in an unknown order within the cycle,
    # so each stagger slot is assumed to hold the smallest deposits first: 0.2
    # at 12h, then 2 at 24h, cycling.
    production = PassiveProduction(
        producers=[
            PassiveProducer("raptor pen", DAILY, {"antlers": 2}),
            PassiveProducer("raptor pen snare", DAILY, {"antlers": 0.2}),
        ]
    )
    assert production.quantized_wait("antlers", 0.2) == timedelta(hours=12)
    assert production.quantized_wait("antlers", 0.3) == timedelta(hours=24)
    assert production.quantized_wait("antlers", 2.3) == timedelta(hours=36)


def test_quantized_wait_mixed_periods_run_independent_schedules():
    production = PassiveProduction(
        producers=[
            PassiveProducer("hourly mill", HOURLY, {"board": 60}),
            PassiveProducer("daily mill", DAILY, {"board": 240}),
        ]
    )
    # The hourly schedule alone covers a small ask at its first deposit.
    assert production.quantized_wait("board", 60) == timedelta(hours=1)
    # 1440 needs all 24 hourly deposits; the daily lump lands at the same
    # moment, so the crossing is exactly 24h -- not the continuous estimate
    # (under 21h at the combined 1680/day rate).
    assert production.quantized_wait("board", 1440) == timedelta(hours=24)


def test_quantized_wait_smeared_producers_ramp_continuously():
    smeared = lambda name: name.startswith("pet ")  # noqa: E731
    production = PassiveProduction(
        producers=[
            PassiveProducer("stable", DAILY, {"horn": 100}),
            PassiveProducer("pet Dog", DAILY, {"horn": 50}),
        ]
    )
    # The pet's averaged 50/day ramps continuously but only reaches 50 by the
    # time the stable's first real deposit of 100 lands -- the deposit is what
    # crosses 100, at a whole day.
    assert production.quantized_wait("horn", 100, smeared) == timedelta(days=1)


def test_quantized_wait_purely_smeared_item_stays_continuous():
    smeared = lambda name: name.startswith("pet ")  # noqa: E731
    production = PassiveProduction(
        producers=[PassiveProducer("pet Dog", DAILY, {"horn": 50})]
    )
    # A pet-only item has no real deposits to quantize to; the averaged rate is
    # the whole story, so the continuous estimate stands.
    assert production.quantized_wait("horn", 100, smeared) == timedelta(days=2)


def test_time_to_produce_staggers_same_period_producers():
    # time_to_produce treats every producer as a real depositor (no smeared
    # predicate); the multi-producer same-period case used to fall back to the
    # combined continuous rate and could report a fraction of a period.
    production = PassiveProduction(
        producers=[
            PassiveProducer("raptor pen", DAILY, {"antlers": 2}),
            PassiveProducer("raptor pen snare", DAILY, {"antlers": 0.2}),
        ]
    )
    assert production.time_to_produce("antlers", 5) == timedelta(days=3)


def test_total_time_is_the_longest_concurrent_wait():
    uses = [
        PassiveUse("Board", 100000, timedelta(minutes=100)),
        PassiveUse("Wood", 46000, timedelta(hours=1)),
    ]
    # They run concurrently, so the total is the longest, not the sum.
    assert total_time(uses) == timedelta(minutes=100)


def test_total_time_of_nothing_is_zero():
    assert total_time([]) == timedelta()
