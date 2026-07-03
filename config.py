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
      board a minute at a time, doubled with ``have_pine_boost``.
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

    pets_config = config.get("pets")
    if pets_config:
        producers.extend(_pet_producers(pets_config, start, horizon))

    return PassiveProduction(
        producers=producers, inventory_size=storehouse_size(config, today)
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
