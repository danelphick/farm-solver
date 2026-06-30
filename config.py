"""Reading and manipulating the Farm Solver config file.

The config is a TOML file describing the player's buildings and settings (see
``config.toml``). For now the passive producers (the hourly ``[sawmill]``) and
the ``[storehouse]`` size are read out of it.
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime
from pathlib import Path
from typing import Any

from passive import (
    DAILY,
    HOURLY,
    TEN_MINUTES,
    PassiveProducer,
    PassiveProduction,
)

# Bundled config, used when the user does not supply their own.
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load the config file, or the bundled one if ``path`` is None."""
    source = path if path is not None else DEFAULT_CONFIG_PATH
    return tomllib.loads(source.read_text())


def passive_production(
    config: dict[str, Any], today: date | None = None
) -> PassiveProduction:
    """Build the passive producers described by ``config``.

    Each building has its own cadence and outputs:

    * ``[sawmill]`` -- hourly; its keys are item names and values the hourly
      quantity.
    * ``[chicken_coop]`` -- ``produces`` eggs and feathers each day, plus (when
      ``completed_starmap``) gold feathers at five percent of the feathers.
    * ``[hay_field]`` -- straw every ten minutes.
    * ``[quarry]`` -- stone every ten minutes and coal every hour.
    * ``[steelworks]`` -- steel every hour, plus steel wire at a third of that
      (rounded down).

    The storehouse size (see :func:`storehouse_size`) is attached so production
    rates respect how much can be held at once.
    """
    producers: list[PassiveProducer] = []

    sawmill = config.get("sawmill")
    if sawmill:
        outputs = {item.lower(): int(qty) for item, qty in sawmill.items()}
        producers.append(
            PassiveProducer(name="sawmill", period=HOURLY, outputs=outputs)
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

    return PassiveProduction(
        producers=producers, inventory_size=storehouse_size(config, today)
    )


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
