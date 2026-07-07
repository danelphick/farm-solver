"""Reading and manipulating the Farm Solver config file.

The config is a TOML file describing the player's buildings and settings (see
``config.toml``). For now the passive producers (the hourly ``[sawmill]``) and
the ``[storehouse]`` size are read out of it.
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import crafting
import pets
from passive import (
    DAILY,
    HOURLY,
    MINUTE,
    TEN_MINUTES,
    PassiveProducer,
    PassiveProduction,
)

# Pet producers are named with this prefix so they can be told apart from the
# faster buildings and used only as a last-resort source (pets are slow).
PET_PRODUCER_PREFIX = "pet "

# The Hickory Omelette meal adds this fraction of the sawmill's hourly output as
# an extra deposit every ten minutes (see the sawmill handling in
# :func:`passive_production`).
HICKORY_OMELETTE_FRACTION = 0.2

# The Farmhouse tops actual stamina up each day by this multiple of the natural
# stamina cap, capped at :data:`FARMHOUSE_GAIN_CAP` (raised to
# :data:`FARMHOUSE_GAIN_CAP_APPLE_PIE` with the Apple Pie Enthusiast perk).
FARMHOUSE_DAILY_MULTIPLIER = 8
FARMHOUSE_GAIN_CAP = 500_000
FARMHOUSE_GAIN_CAP_APPLE_PIE = 1_000_000

# Bundled config, used when the user does not supply their own.
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load the config file, or the bundled one if ``path`` is None."""
    source = path if path is not None else DEFAULT_CONFIG_PATH
    return tomllib.loads(source.read_text())


def passive_production(
    config: dict[str, Any],
    today: date | None = None,
    start: date | None = None,
    horizon: timedelta | None = None,
) -> PassiveProduction:
    """Build the passive producers described by ``config``.

    Each building has its own cadence and outputs:

    * ``[sawmill]`` -- hourly; its numeric keys are item names and values the
      hourly quantity (oak only with ``have_sturdy_saw``). It also makes pine
      board a minute at a time, doubled with ``have_pine_boost``. The Hickory
      Omelette (``use_hickory_omelette``) adds 20% of the hourly output every
      ten minutes: the top-of-the-hour deposit merges with the main run into one
      1.2x deposit (still storehouse-capped), and the other five deposits an
      hour each land 20% separately (each capped on its own).
    * ``[chicken_coop]`` -- ``produces`` eggs and feathers each day, plus (when
      ``completed_starmap``) gold feathers at five percent of the feathers.
    * ``[worm_habitat]`` -- worms, gummy worms and mealworms every hour.
    * ``[trout_bait_farm]`` -- grubs and minnows every hour, trout every day.
    * ``[orchard]`` -- apples, oranges and lemons every day, each tree count
      scaled by the multiplicative ``bonus`` and floored; with
      ``have_tree_shaker`` a separate daily deposit adds a further 10%
      (floored) of each fruit twelve hours later, like the raptor snare.
    * ``[raptor_pen]`` -- antlers every day, plus (when ``have_antler_snare``)
      a separate daily deposit of a further 10% (floored) twelve hours later,
      so that extra is not lost to the storehouse cap of the main run.
    * ``[cow_pasture]`` -- milk every day.
    * ``[vineyard]`` -- grapes every day.
    * ``[flour_mill]`` -- one flour a minute, two with ``have_flour_power``.
    * ``[feed_mill]`` -- one feed a minute, two with ``have_feed_boost``.
    * ``[sugar_cane_mill]`` -- one sugar cane a minute, doubling with each of
      ``have_sugar_boost_i`` then ``have_sugar_boost_ii`` (1 -> 2 -> 4). Boost
      II without boost I is an error.
    * ``[hay_field]`` -- straw every ten minutes.
    * ``[quarry]`` -- stone every ten minutes and coal every hour.
    * ``[steelworks]`` -- steel every hour, plus steel wire at a third of that
      (rounded down).
    * ``[farmhouse]`` -- a daily stamina deposit for the Apple Cider channel
      (inventory plan mode); see :func:`_farmhouse_producer`.
    * ``[pets]`` -- each listed pet fetches its average daily haul of items (see
      :func:`_pet_producers` and :mod:`pets`), as a daily producer.

    The storehouse size (see :func:`storehouse_size`) is attached so production
    rates respect how much can be held at once.
    """
    producers: list[PassiveProducer] = []

    sawmill = config.get("sawmill")
    if sawmill:
        # The hourly outputs are the numeric entries; flags like
        # have_pine_boost are handled separately below.
        outputs = {
            item.lower(): int(qty)
            for item, qty in sawmill.items()
            if not isinstance(qty, bool)
        }
        # Oak is only produced with the sturdy saw.
        if not sawmill.get("have_sturdy_saw"):
            outputs.pop("oak", None)
        if sawmill.get("use_hickory_omelette"):
            # The Hickory Omelette deposits 20% of the hourly output every ten
            # minutes. The deposit at the top of the hour coincides with the
            # main run, so that combined deposit is 1.2x the base output (still
            # subject to the storehouse cap, like any deposit). The other five
            # deposits an hour each land 20% on their own, each capped
            # independently, like the orchard shaker / raptor snare -- so they
            # are a separate producer whose per-cycle output is one such deposit.
            # A fifth-of-an-hour period makes each producer_rates cycle exactly
            # one off-hour deposit, giving 5 x min(0.2*out, cap) an hour.
            producers.append(
                PassiveProducer(
                    name="sawmill",
                    period=HOURLY,
                    outputs={
                        item: qty * (1 + HICKORY_OMELETTE_FRACTION)
                        for item, qty in outputs.items()
                    },
                )
            )
            producers.append(
                PassiveProducer(
                    name="sawmill_omelette",
                    period=HOURLY / 5,
                    outputs={
                        item: qty * HICKORY_OMELETTE_FRACTION
                        for item, qty in outputs.items()
                    },
                )
            )
        else:
            producers.append(
                PassiveProducer(name="sawmill", period=HOURLY, outputs=outputs)
            )
        # Pine board comes off the sawmill a minute at a time, like the flour
        # and feed mills, doubled with the pine boost.
        rate = 2 if sawmill.get("have_pine_boost") else 1
        producers.append(
            PassiveProducer(
                name="sawmill", period=MINUTE, outputs={"pine board": rate}
            )
        )

    chicken_coop = config.get("chicken_coop")
    if chicken_coop:
        produces = int(chicken_coop["produces"])
        outputs = {"eggs": produces, "feathers": produces}
        if chicken_coop.get("completed_starmap"):
            # Gold feathers come in at five percent of the feathers produced.
            outputs["gold feather"] = produces // 20
        producers.append(
            PassiveProducer(name="chicken_coop", period=DAILY, outputs=outputs)
        )

    worm_habitat = config.get("worm_habitat")
    if worm_habitat:
        # Config keys use underscores ("gummy_worms"); the items are spaced.
        outputs = {
            item.lower().replace("_", " "): int(qty)
            for item, qty in worm_habitat.items()
        }
        producers.append(
            PassiveProducer(
                name="worm_habitat", period=HOURLY, outputs=outputs
            )
        )

    trout_bait_farm = config.get("trout_bait_farm")
    if trout_bait_farm:
        hourly = {
            item.lower(): int(qty)
            for item, qty in trout_bait_farm.items()
            if item.lower() in ("grubs", "minnows")
        }
        if hourly:
            producers.append(
                PassiveProducer(
                    name="trout_bait_farm", period=HOURLY, outputs=hourly
                )
            )
        if "trout" in trout_bait_farm:
            producers.append(
                PassiveProducer(
                    name="trout_bait_farm",
                    period=DAILY,
                    outputs={"trout": int(trout_bait_farm["trout"])},
                )
            )

    flour_mill = config.get("flour_mill")
    if flour_mill is not None:
        # One flour a minute, doubled with flour power.
        rate = 2 if flour_mill.get("have_flour_power") else 1
        producers.append(
            PassiveProducer(
                name="flour_mill", period=MINUTE, outputs={"flour": rate}
            )
        )

    feed_mill = config.get("feed_mill")
    if feed_mill is not None:
        # One feed a minute, doubled with the feed boost.
        rate = 2 if feed_mill.get("have_feed_boost") else 1
        producers.append(
            PassiveProducer(
                name="feed_mill", period=MINUTE, outputs={"feed": rate}
            )
        )

    sugar_cane_mill = config.get("sugar_cane_mill")
    if sugar_cane_mill is not None:
        boost_i = bool(sugar_cane_mill.get("have_sugar_boost_i"))
        boost_ii = bool(sugar_cane_mill.get("have_sugar_boost_ii"))
        if boost_ii and not boost_i:
            raise ValueError(
                "sugar_cane_mill: have_sugar_boost_ii requires have_sugar_boost_i"
            )
        # One sugar cane a minute, doubling with each boost: 1 -> 2 -> 4.
        rate = 4 if boost_ii else 2 if boost_i else 1
        producers.append(
            PassiveProducer(
                name="sugar_cane_mill",
                period=MINUTE,
                outputs={"sugar cane": rate},
            )
        )

    orchard = config.get("orchard")
    if orchard:
        # Each tree count yields its fruit daily, scaled by the bonus and
        # floored. The trees map to their singular fruit name.
        bonus = float(orchard.get("bonus", 1.0))
        fruit = {"apple_trees": "apple", "orange_trees": "orange", "lemon_trees": "lemon"}
        outputs = {
            name: int(orchard[trees] * bonus)
            for trees, name in fruit.items()
            if trees in orchard
        }
        producers.append(
            PassiveProducer(name="orchard", period=DAILY, outputs=outputs)
        )
        if orchard.get("have_tree_shaker"):
            # Like the raptor snare, the shaker adds 10% (floored) of each
            # fruit at noon, twelve hours after the main run. A separate daily
            # producer gives it its own storehouse-cap window.
            producers.append(
                PassiveProducer(
                    name="orchard_shaker",
                    period=DAILY,
                    outputs={name: qty // 10 for name, qty in outputs.items()},
                )
            )

    raptor_pen = config.get("raptor_pen")
    if raptor_pen:
        antlers = int(raptor_pen["antlers"])
        producers.append(
            PassiveProducer(
                name="raptor_pen", period=DAILY, outputs={"antler": antlers}
            )
        )
        if raptor_pen.get("have_antler_snare"):
            # The snare adds 10% (floored) at noon, twelve hours after the
            # midnight run. It is a separate daily producer so it gets its own
            # storehouse-cap window rather than being voided alongside the
            # (much larger) main deposit.
            producers.append(
                PassiveProducer(
                    name="raptor_pen_snare",
                    period=DAILY,
                    outputs={"antler": antlers // 10},
                )
            )

    cow_pasture = config.get("cow_pasture")
    if cow_pasture:
        producers.append(
            PassiveProducer(
                name="cow_pasture",
                period=DAILY,
                outputs={"milk": int(cow_pasture["milk"])},
            )
        )

    vineyard = config.get("vineyard")
    if vineyard:
        producers.append(
            PassiveProducer(
                name="vineyard",
                period=DAILY,
                outputs={"grapes": int(vineyard["grapes"])},
            )
        )

    hay_field = config.get("hay_field")
    if hay_field:
        producers.append(
            PassiveProducer(
                name="hay_field",
                period=TEN_MINUTES,
                outputs={"straw": int(hay_field["hay"])},
            )
        )

    quarry = config.get("quarry")
    if quarry:
        if "stone" in quarry:
            producers.append(
                PassiveProducer(
                    name="quarry",
                    period=TEN_MINUTES,
                    outputs={"stone": int(quarry["stone"])},
                )
            )
        if "coal" in quarry:
            producers.append(
                PassiveProducer(
                    name="quarry",
                    period=HOURLY,
                    outputs={"coal": int(quarry["coal"])},
                )
            )

    steelworks = config.get("steelworks")
    if steelworks:
        steel = int(steelworks["steel"])
        producers.append(
            PassiveProducer(
                name="steelworks",
                period=HOURLY,
                outputs={"steel": steel, "steel wire": steel // 3},
            )
        )

    farmhouse = config.get("farmhouse")
    if farmhouse:
        producers.append(_farmhouse_producer(farmhouse, today))

    pets_config = config.get("pets")
    if pets_config:
        producers.extend(_pet_producers(pets_config, start, horizon))

    return PassiveProduction(
        producers=producers, inventory_size=storehouse_size(config, today)
    )


def _farmhouse_producer(
    farmhouse: dict[str, Any], today: date | None = None
) -> PassiveProducer:
    """A daily stamina producer for the ``[farmhouse]`` config section.

    The Farmhouse raises the natural stamina cap by one each day (two with the
    Mattress Pad perk), so the cap recorded on ``registered_on`` has grown by that
    much per day since -- computed at ``today`` like the storehouse
    (see :func:`storehouse_size`). Each day actual stamina then rises by
    :data:`FARMHOUSE_DAILY_MULTIPLIER` times that cap, capped at
    :data:`FARMHOUSE_GAIN_CAP` (:data:`FARMHOUSE_GAIN_CAP_APPLE_PIE` with the
    Apple Pie Enthusiast perk). Stamina is exempt from the storehouse cap (see
    :data:`passive.UNCAPPED_ITEMS`).
    """
    cap = int(farmhouse["stamina_cap"])
    daily_increase = 2 if farmhouse.get("have_mattress_pad") else 1
    registered_on = farmhouse.get("registered_on")
    if registered_on is not None:
        days = max(0, ((today or date.today()) - _as_date(registered_on)).days)
        cap += daily_increase * days
    gain_cap = (
        FARMHOUSE_GAIN_CAP_APPLE_PIE
        if farmhouse.get("have_apple_pie_enthusiast")
        else FARMHOUSE_GAIN_CAP
    )
    daily_gain = min(FARMHOUSE_DAILY_MULTIPLIER * cap, gain_cap)
    return PassiveProducer(
        name="farmhouse", period=DAILY, outputs={"stamina": float(daily_gain)}
    )


def _pet_producers(
    pets_config: dict[str, Any],
    start: date | None = None,
    horizon: timedelta | None = None,
) -> list[PassiveProducer]:
    """Daily producers for the pets described by a ``[pets]`` config section.

    Each pet fetches items over time; its average daily haul (see
    :func:`pets.pets_from_config`) becomes a daily producer. A seasonal pet only
    fetches during its calendar window, so its haul is scaled by the fraction of
    the plan (running ``horizon`` from ``start``) that overlaps that window (see
    :func:`pets.seasonal_fraction`) -- with no plan span given, the steady-state
    yearly share. A pet out of season for the whole span, or with no level,
    produces nothing and is skipped.
    """
    producers: list[PassiveProducer] = []
    for pet in pets.pets_from_config(pets_config).pets.values():
        outputs = pet.daily_output()
        if not outputs:
            continue
        if pet.months is not None:
            factor = pets.seasonal_fraction(pet.months, start, horizon)
            if factor <= 0:
                continue  # out of season for the whole plan; produces nothing
            outputs = {item: qty * factor for item, qty in outputs.items()}
        producers.append(
            PassiveProducer(
                name=f"{PET_PRODUCER_PREFIX}{pet.name}", period=DAILY, outputs=outputs
            )
        )
    return producers


def _as_date(value: Any) -> date:
    """Coerce a TOML date or ISO date string to a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def storehouse_size(
    config: dict[str, Any], today: date | None = None
) -> int | None:
    """The storehouse capacity today, or None if the config has no storehouse.

    The storehouse held ``inventory`` items on ``updated_on`` and gains
    ``growth`` more each day since, so its size today is
    ``inventory + growth * days_since_updated``. Dates in the future leave the
    size at its recorded value.
    """
    storehouse = config.get("storehouse")
    if not storehouse:
        return None

    inventory = int(storehouse["inventory"])
    growth = int(storehouse.get("growth", 0))
    updated_on = storehouse.get("updated_on")
    if updated_on is None:
        return inventory

    if today is None:
        today = date.today()
    days = max(0, (today - _as_date(updated_on)).days)
    return inventory + growth * days


# Gold prices in the ``[trading]`` section are quoted per 1000 units ("gk"), so
# the per-unit gold is the config value divided by this. Cookie sets are the
# exception -- priced individually, in plain gold each.
GK_UNITS = 1000

# Grape Juice is not traded directly; its price is derived from grapes, which
# *are* traded (for Arnold Palmers, not gold -- hence the ``grapes`` price is in
# "apk", Arnold Palmers per 1000). Its workshop recipe presses 200 grapes (plus a
# Glass Bottle, whose cost is ignored) per craft, and a craft yields
# ``crafting_bonus`` juices, so one Grape Juice costs this many grapes to produce.
GRAPES_PER_JUICE = 200


def trading_prices(
    config: dict[str, Any],
    crafting_bonus: float = crafting.DEFAULT_CRAFTING_BONUS,
) -> dict[str, float] | None:
    """Per-unit gold prices for the tradable consumables, or None if unpriced.

    Reads the ``[trading]`` section: ``orange_juice``, ``apple_cider``,
    ``arnold_palmer`` and ``large_net`` are gold per 1000 units (divided by
    :data:`GK_UNITS`), and ``cookie_set`` is gold each. Grape Juice is not traded
    directly; its price is its **production cost** in grapes: pressing
    :data:`GRAPES_PER_JUICE` grapes yields ``crafting_bonus`` juices, so a juice is
    ``GRAPES_PER_JUICE / crafting_bonus`` grapes, each worth ``grapes/1000`` Arnold
    Palmers (``grapes`` is in "apk" -- AP per 1000, since grapes trade for AP, not
    gold), each worth the Arnold Palmer's own per-unit gold. Absent ``grapes`` it
    stays free (``0``). Keys are the lower-cased item names the solver uses, so a
    plan's gold is a direct dot product with its consumable tally. Returns ``None``
    when the section is absent, leaving the solver on its unweighted (gold-agnostic)
    behaviour.
    """
    section = config.get("trading")
    if not section:
        return None

    def per_unit(key: str) -> float:
        return float(section.get(key, 0.0)) / GK_UNITS

    arnold_palmer = per_unit("arnold_palmer")
    grape = float(section.get("grapes", 0.0)) / GK_UNITS * arnold_palmer  # gold/grape
    return {
        "arnold palmer": arnold_palmer,
        "large net": per_unit("large_net"),
        "apple cider": per_unit("apple_cider"),
        "orange juice": per_unit("orange_juice"),
        # Production cost: grapes per juice (recipe / crafting bonus) at grape gold.
        "grape juice": GRAPES_PER_JUICE / crafting_bonus * grape,
        "cookie set": float(section.get("cookie_set", 0.0)),  # priced per unit
    }
