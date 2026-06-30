"""Reading and manipulating the Farm Solver config file.

The config is a TOML file describing the player's buildings and settings (see
``config.toml``). For now only the passive producers are read out of it, and of
those only the hourly ``[sawmill]`` section.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from passive import HOURLY, PassiveProducer, PassiveProduction

# Bundled config, used when the user does not supply their own.
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load the config file, or the bundled one if ``path`` is None."""
    source = path if path is not None else DEFAULT_CONFIG_PATH
    return tomllib.loads(source.read_text())


def passive_production(config: dict[str, Any]) -> PassiveProduction:
    """Build the passive producers described by ``config``.

    For now only the hourly ``[sawmill]`` section is read; its keys are item
    names and its values the quantity produced each hour.
    """
    producers: list[PassiveProducer] = []

    sawmill = config.get("sawmill")
    if sawmill:
        outputs = {item.lower(): int(qty) for item, qty in sawmill.items()}
        producers.append(
            PassiveProducer(name="sawmill", period=HOURLY, outputs=outputs)
        )

    return PassiveProduction(producers=producers)
