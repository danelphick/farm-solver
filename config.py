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

from passive import HOURLY, PassiveProducer, PassiveProduction

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

    For now only the hourly ``[sawmill]`` section is read; its keys are item
    names and its values the quantity produced each hour. The storehouse size
    (see :func:`storehouse_size`) is attached so production rates respect how
    much can be held at once.
    """
    producers: list[PassiveProducer] = []

    sawmill = config.get("sawmill")
    if sawmill:
        outputs = {item.lower(): int(qty) for item, qty in sawmill.items()}
        producers.append(
            PassiveProducer(name="sawmill", period=HOURLY, outputs=outputs)
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
