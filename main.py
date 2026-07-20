"""Command-line entry point for Farm Solver.

See README.md for usage. In short::

    python main.py [-t <tower-requirements>] [-l level] \
        [-i item:amount] [-m item:amount] \
        [--mastery-file <masteries>] [--inventory-file <inventory>] \
        [--override-inventory item:amount]

The mastery and inventory files default to the ``[solver]`` config section.
"""

from __future__ import annotations

import argparse
import copy
import math
import os
import sys
import tomllib
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import ascension
import config
import crafting
import crops
import exploring
import farm_solver
import farming
import fishing
import locksmith
import optimizer
import parsing
import passive
import pets
import report
import sources


def parse_target(value: str) -> str | int:
    """Parse ``--target``: ``tower``, ``none``, or a level number (``150``/``T150``).

    ``tower`` (matched case-insensitively) climbs to the highest reachable
    level, and additionally always keeps the tower as a goal even when -m/-i
    are given. ``none`` drops the tower entirely, solving only the -m/-i goals
    -- an *explicit* form of the behaviour that omitting ``--target`` gives when
    -m/-i are set (see :func:`build_inputs`). Anything else is a level number,
    an optional leading ``T``/``t`` stripped first (so ``T150`` and ``150`` are
    the same), which must parse as a positive integer.
    """
    text = value.strip()
    if text.lower() == "tower":
        return "tower"
    if text.lower() == "none":
        return "none"
    level_text = text[1:] if text[:1].lower() == "t" else text
    try:
        level = int(level_text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--target must be 'tower', 'none', a level number, or 'T<level>', "
            f"not {value!r}"
        ) from None
    if level < 1:
        raise argparse.ArgumentTypeError("--target level must be positive")
    return level


def parse_gold_budget(value: str) -> int | None:
    """Parse ``--max-gold``: ``inventory`` -> 0, ``unlimited`` -> no cap (None).

    The two old plan modes survive as named budgets -- ``inventory`` is a zero
    budget (self-source everything) and ``unlimited`` no cap -- alongside any
    non-negative integer in between.
    """
    text = value.strip().lower()
    if text == "unlimited":
        return None
    if text == "inventory":
        return 0
    try:
        budget = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--max-gold must be a non-negative integer, 'inventory', or "
            f"'unlimited', not {value!r}"
        ) from None
    if budget < 0:
        raise argparse.ArgumentTypeError("--max-gold must be non-negative")
    return budget


def parse_format(value: str) -> str:
    """Parse ``--format``: ``text``/``txt`` or ``markdown``/``md``."""
    aliases = {"text": "text", "txt": "text", "markdown": "markdown", "md": "markdown"}
    text = value.strip().lower()
    try:
        return aliases[text]
    except KeyError:
        raise argparse.ArgumentTypeError(
            f"--format must be 'text'/'txt' or 'markdown'/'md', not {value!r}"
        ) from None


# Axis names --spread/[frontier] spread accept, plus the "pj" shorthand for the
# flagship "spend Pumpkin Juice" use case -- both map onto optimizer.SpreadSpec.axis.
_SPREAD_AXIS_ALIASES = {
    "time": "time",
    "gold": "gold",
    "pumpkin-juice": "pumpkin-juice",
    "pj": "pumpkin-juice",
    "passive": "passive",
}


def _parse_spread(text: str) -> optimizer.SpreadSpec:
    """Parse ``--spread AXIS[=LO:HI]`` into an :class:`optimizer.SpreadSpec`.

    ``AXIS`` is one of ``time``/``gold``/``pumpkin-juice`` (alias ``pj``)/
    ``passive``, case-insensitively; an optional ``=LO:HI`` (both floats, both
    required together) narrows the sampled range, otherwise ``lo``/``hi`` are
    ``None`` and :func:`optimizer._spread` picks that axis's natural default
    (ignored entirely for ``passive``, which enumerates breakpoints instead of
    sampling a range -- see the module docstring on ``optimizer.SpreadSpec``).
    """
    raw = text.strip()
    axis_part, sep, range_part = raw.partition("=")
    axis = _SPREAD_AXIS_ALIASES.get(axis_part.strip().lower())
    if axis is None:
        raise argparse.ArgumentTypeError(
            "--spread axis must be one of time, gold, pumpkin-juice (pj), "
            f"passive, not {axis_part.strip()!r}"
        )
    if not sep:
        return optimizer.SpreadSpec(axis)
    lo_text, colon, hi_text = range_part.partition(":")
    if not colon:
        raise argparse.ArgumentTypeError(
            f"--spread range must be 'LO:HI', not {range_part!r}"
        )
    try:
        lo = float(lo_text)
        hi = float(hi_text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--spread range LO:HI must both be numbers, not {range_part!r}"
        ) from None
    return optimizer.SpreadSpec(axis, lo, hi)


# Nominal gold cost of one Pumpkin Juice, used when the config does not set
# trading.pumpkin_juice. Nominal because juice cannot be bought from another player:
# it is what a juice is reckoned to be worth, so a plan that drinks one can be ranked
# against a plan that spends anything else.
DEFAULT_PUMPKIN_JUICE_PRICE = 230.0


def _pumpkin_juice_limit(value: str) -> int:
    """Parse ``--use-pumpkin-juice N``: a non-negative cap on juices spent."""
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--use-pumpkin-juice must be a non-negative integer, not {value!r}"
        ) from None
    if limit < 0:
        raise argparse.ArgumentTypeError("--use-pumpkin-juice must be non-negative")
    return limit


def resolve_pumpkin_juice(
    cli: int | None, cfg: dict
) -> tuple[bool, int | None, float]:
    """Whether Pumpkin Juice is allowed, its optional cap, and its gold price.

    ``cli`` is ``args.use_pumpkin_juice``: ``None`` when the flag is absent (falling
    back to ``mastery.use_pumpkin_juice``), ``-1`` for the bare flag (enabled, no
    limit), or ``N >= 0`` for a cap. The config value is ``true`` (enabled, no limit),
    an integer cap, or ``false``/absent (off). The command line wins.

    The price comes from ``trading.pumpkin_juice`` -- it sits with the other prices
    even though juice is never *bought* from a player, since it is still a cost the
    plans are ranked on. Gold each (not "gk"/per-1000, like ``cookie_set``), defaulting
    to :data:`DEFAULT_PUMPKIN_JUICE_PRICE`. Unlike the rest of ``[trading]``, presence
    does **not** decide availability -- ``use_pumpkin_juice`` alone does that -- so an
    absent key means "priced at the default", not "not traded". A present ``0`` means
    free, as it does for every other price here.
    """
    mastery_cfg = cfg.get("mastery", {})
    # Refuse the old key rather than ignore it: a config that still sets a price here
    # would otherwise fall back to the default, silently repricing every plan.
    if "pumpkin_juice_price" in mastery_cfg:
        raise ValueError(
            "mastery.pumpkin_juice_price has moved to trading.pumpkin_juice "
            "(gold each), alongside the other prices"
        )
    price = float(
        cfg.get("trading", {}).get("pumpkin_juice", DEFAULT_PUMPKIN_JUICE_PRICE)
    )
    if cli is not None:
        return True, (None if cli < 0 else cli), price
    setting = mastery_cfg.get("use_pumpkin_juice")
    if not setting:  # None, False, or 0 -- off
        return False, None, price
    if setting is True:
        return True, None, price
    limit = int(setting)
    return True, (None if limit < 0 else limit), price


# The artifact whose presence changes Gary's Crushroom's drop table -- see
# exploring.FUNGUS_PROCESSING_FLAG.
FUNGUS_PROCESSING_ARTIFACT = "Fungus Processing and You"


def resolve_fungus_processing(
    cfg: dict, inventory: farm_solver.Inventory | None = None
) -> bool:
    """Whether Gary's Crushroom's drop table is boosted by Fungus Processing and You.

    ``exploring.have_fungus_processing`` in the config overrides the check;
    otherwise the artifact's presence in inventory decides it. With no
    inventory to check (the ``--unobtainable`` report, which reads no
    inventory file) and no override, it defaults to off.
    """
    override = cfg.get("exploring", {}).get("have_fungus_processing")
    if override is not None:
        return bool(override)
    if inventory is None:
        return False
    return inventory.amount(FUNGUS_PROCESSING_ARTIFACT) > 0


# The artifact governing the Cookie interaction with Dewstar bonus crops -- see
# crops.Farm.dewstar_cookie_bonus.
DEWSTARS_JOURNAL_ARTIFACT = "Dewstar's Journal"

# The tower level whose completion grants the Scythe of Dewstar -- see
# resolve_scythe_of_dewstar.
DEWSTAR_TOWER_LEVEL = 250


def resolve_scythe_of_dewstar(
    cfg: dict,
    state: farm_solver.GameState | None = None,
    requirements: farm_solver.TowerRequirements | None = None,
) -> bool:
    """Whether farming gets the Scythe of Dewstar's bonus-crop yield.

    The Scythe is a reward for reaching tower level :data:`DEWSTAR_TOWER_LEVEL`,
    not a held item, so it has no inventory presence to auto-detect the
    way Fungus Processing and You or Dewstar's Journal do. Instead, with a
    ``state`` and ``requirements`` to check, ``farm_solver.has_tower_level``
    stands in for holding it. ``farming.have_scythe_of_dewstar`` in the config
    overrides the check either way; with nothing to check (the ``--unobtainable``
    report, which solves no tower goal) and no override, it defaults to off.
    """
    override = cfg.get("farming", {}).get("have_scythe_of_dewstar")
    if override is not None:
        return bool(override)
    if state is None or requirements is None:
        return False
    return farm_solver.has_tower_level(state, requirements, DEWSTAR_TOWER_LEVEL)


def resolve_dewstars_journal(
    cfg: dict, inventory: farm_solver.Inventory | None = None
) -> bool:
    """Whether Dewstar's Journal extends the Cookie bonus to Dewstar bonus crops.

    ``farming.have_dewstars_journal`` in the config overrides the check;
    otherwise the journal's presence in inventory decides it. With no inventory to
    check and no override, it defaults to off.
    """
    override = cfg.get("farming", {}).get("have_dewstars_journal")
    if override is not None:
        return bool(override)
    if inventory is None:
        return False
    return inventory.amount(DEWSTARS_JOURNAL_ARTIFACT) > 0


# The tower level whose completion grants the Mechanical Heart -- the artifact
# handing over a monthly bulk of Apple Cider and Arnold Palmer (config.MONTHLY_GRANTS).
MONTHLY_GRANT_TOWER_LEVEL = 300


def resolve_monthly_grants(
    cfg: dict,
    state: farm_solver.GameState | None = None,
    requirements: farm_solver.TowerRequirements | None = None,
) -> bool:
    """Whether the Mechanical Heart's monthly grants (see config.MONTHLY_GRANTS) apply.

    Like the Scythe of Dewstar, the Mechanical Heart is a tower reward, not a
    held item, so it has no inventory presence to auto-detect: with a ``state``
    and ``requirements`` to check, ``farm_solver.has_tower_level`` at
    :data:`MONTHLY_GRANT_TOWER_LEVEL` (tower level 300) stands in for holding it.
    ``exploring.have_mechanical_heart`` in the config overrides the check either
    way; with nothing to check (the ``--unobtainable`` report) and no override,
    it defaults to off.
    """
    override = cfg.get("exploring", {}).get("have_mechanical_heart")
    if override is not None:
        return bool(override)
    if state is None or requirements is None:
        return False
    return farm_solver.has_tower_level(state, requirements, MONTHLY_GRANT_TOWER_LEVEL)


def _crafting_bonus_for_cfg(explicit: float | None, cfg: dict) -> float:
    """The crafting bonus for ``cfg``: ``explicit`` wins when given, else it is
    resolved from ``[workshop]``/``[tower]`` (see :func:`resolve_crafting_bonus`).

    Factored out as a pure function so a caller solving several configs at
    once -- ascension mode's per-stage cfgs, each with its own ``[tower]
    level`` once a reward is crossed -- can resolve each stage's own bonus
    without mutating ``args`` (which stays at the single value the rest of a
    non-ascension run uses).
    """
    if explicit is not None:
        return explicit
    return config.crafting_bonus_for(cfg, config.current_tower_level(cfg))


def resolve_crafting_bonus(args: argparse.Namespace, cfg: dict) -> None:
    """Resolve ``args.crafting_bonus`` from ``[workshop]``/``[tower]`` when unset.

    ``--crafting-bonus`` defaults to ``None`` (see :func:`build_parser`); an
    explicit flag value always wins and is left untouched. Otherwise the
    bonus comes from the player's current tower level and Resource Saver
    setting (:func:`config.crafting_bonus_for`/:func:`config.current_tower_level`)
    -- for a config with no ``[workshop]``/``[tower]`` section this resolves to
    the same 1.45 every solve used before tower-level gating existed. Mutates
    ``args`` in place, so every downstream reader of ``args.crafting_bonus``
    (``build_inputs``, ``trading_prices``, the reporters) sees a plain float
    either way; call this before any of them run.
    """
    args.crafting_bonus = _crafting_bonus_for_cfg(args.crafting_bonus, cfg)


def price_stamina_enabled(args: argparse.Namespace, cfg: dict) -> bool:
    """Whether to charge free stamina its Orange-Juice-equivalent opportunity cost.

    Stamina an Apple Cider explore burns that arrived for free -- the banked
    ``--current-stamina`` and the Farmhouse's passive deposit -- is otherwise a
    free resource, so a plan that drains it looks cheaper than it is. With this on
    (the default), it is valued at what replacing it would cost in Orange Juice
    (``orange_juice_price / 100``) and folded into a plan's gold (see
    :func:`optimizer.plan_gold`, :attr:`optimizer.SourcingPlan.cost`). ``--price-
    stamina``/``--no-price-stamina`` override ``[trading] price_stamina`` (default
    true). Off in ascension mode -- a staged climb prices stamina by stage and does
    not thread the opportunity cost through :class:`StagedPlan`.
    """
    override = getattr(args, "price_stamina", None)
    if override is not None:
        return override
    return bool(cfg.get("trading", {}).get("price_stamina", True))


def resolve_frontier_spread(
    args: argparse.Namespace, cfg: dict
) -> optimizer.SpreadSpec | None:
    """Resolve ``--spread``/``[frontier] spread`` into an :class:`optimizer.SpreadSpec`.

    ``--spread`` (already parsed to a ``SpreadSpec`` by :func:`_parse_spread`) wins
    when given. Otherwise ``[frontier] spread`` (the same ``AXIS[=LO:HI]`` grammar,
    as a plain string -- e.g. ``"pumpkin-juice"`` or ``"gold=0:5000"``) is parsed the
    same way; a range-less config string can still get one from ``[frontier]
    spread_range`` (a two-element ``[lo, hi]``), so a shared range needn't be
    repeated inline. ``None`` (both absent) means the default time-thinned frontier,
    unchanged.
    """
    if args.spread is not None:
        return args.spread
    frontier_cfg = cfg.get("frontier", {})
    text = frontier_cfg.get("spread")
    if not text:
        return None
    spec = _parse_spread(text)
    if spec.lo is None and spec.hi is None:
        span = frontier_cfg.get("spread_range")
        if span is not None:
            lo, hi = span
            spec = optimizer.SpreadSpec(spec.axis, float(lo), float(hi))
    return spec


def resolve_frontier_graph(args: argparse.Namespace, cfg: dict) -> str | None:
    """Resolve ``--graph``/``[frontier] graph``: the impact-graph metric, or ``None``
    (off, the default) when neither is set. ``--graph`` wins when given (bare ->
    ``"auto"``); otherwise ``[frontier] graph`` (``"auto"``/``"gold"``/``"time"``).
    """
    if args.graph is not None:
        return args.graph
    return cfg.get("frontier", {}).get("graph")


def pumpkin_juice_gains(
    needs: list[farm_solver.ItemNeed],
    state: farm_solver.GameState,
    mastery_bonus: float,
) -> dict[str, float]:
    """Units of demand one Pumpkin Juice covers, per mastery-goal item.

    A juice instantly grants 10% of an item's current mastery tier. In the
    optimiser's items-to-find units (a found item is worth ``mastery_bonus``
    mastery), that is ``0.1 * threshold / mastery_bonus`` -- the threshold being the
    item's mastery denominator (its current tier), or the goal target for an item
    not yet tracked. Only mastery goals (tower, ``-m``) with a shortfall qualify;
    an ``-i`` acquire goal earns no mastery, so a juice does nothing for it.
    """
    gains: dict[str, float] = {}
    for need in needs:
        if not need.is_mastery or need.extra <= 0:
            continue
        mastery = state.mastery(need.item)
        threshold = mastery.required if mastery and mastery.required else need.target
        if threshold > 0:
            gains[need.item.lower()] = 0.1 * threshold / mastery_bonus
    return gains


def pumpkin_juice_schedule(
    plan: optimizer.SourcingPlan,
    needs: list[farm_solver.ItemNeed],
    pj_gain: dict[str, float],
) -> dict[str, list[int]]:
    """The mastery levels at which to drink each of ``plan``'s Pumpkin Juices.

    A juice only lands its intended boost drunk late, in the goal's final tier. It
    covers ``pj_gain`` of the item's shortfall -- the same demand units the LP credits
    (``0.1 * threshold / mastery_bonus``, see :func:`pumpkin_juice_gains`) -- so the
    drink points step down from the target by that: the last lands the item on target,
    the one before a step below, and so on. They all sit above the current progress --
    the LP caps juice at the shortfall, so ``count * gain`` never exceeds ``target -
    have`` -- and are clamped there anyway. Using the LP's own ``pj_gain`` keeps the
    schedule from drifting from the crediting. Returns ``{lower-cased item: [level,
    ...]}`` ascending (the order you reach them), for the items the plan drinks on.
    """
    targets = {
        need.item.lower(): (need.have, need.target)
        for need in needs
        if need.is_mastery
    }
    schedule: dict[str, list[int]] = {}
    for item, count in plan.pumpkin_juices.items():
        whole = round(count)
        if whole < 1 or item not in targets or item not in pj_gain:
            continue
        have, target = targets[item]
        gain = pj_gain[item]
        schedule[item] = [
            max(have, round(target - step * gain)) for step in range(whole, 0, -1)
        ]
    return schedule


def _parse_override_value(text: str) -> object:
    """Interpret an override's ``value`` the way TOML would.

    ``true``/``false`` become booleans, numbers become ints/floats, and quoted
    strings drop their quotes; anything TOML cannot parse (e.g. a bare path) is
    kept as the literal string, so ``solver.mastery_file=foo.txt`` works.
    """
    try:
        return tomllib.loads(f"_ = {text}")["_"]
    except tomllib.TOMLDecodeError:
        return text


def apply_overrides(cfg: dict, overrides: list[str] | None) -> None:
    """Apply ``--override section.flag=value`` settings to ``cfg`` in place.

    The dotted key walks (creating) nested tables; the value is parsed as TOML
    (falling back to a literal string). Malformed overrides raise ``ValueError``.
    """
    for override in overrides or []:
        key, sep, value = override.partition("=")
        if not sep:
            raise ValueError(f"--override needs section.flag=value, not {override!r}")
        path = [part.strip() for part in key.split(".")]
        if len(path) < 2 or not all(path):
            raise ValueError(
                f"--override key must be section.flag (dotted), not {key.strip()!r}"
            )
        table = cfg
        for part in path[:-1]:
            table = table.setdefault(part, {})
            if not isinstance(table, dict):
                raise ValueError(
                    f"--override cannot descend into non-table {part!r} in {key.strip()!r}"
                )
        table[path[-1]] = _parse_override_value(value.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="farm-solver",
        description=(
            "Find the cheapest way to spend resources to climb the tower in " "FarmRPG."
        ),
    )

    parser.add_argument(
        "--mastery-file",
        type=Path,
        metavar="FILE",
        help=(
            "text file of masteries copied from FarmRPG "
            "(overrides solver.mastery_file in the config)"
        ),
    )
    parser.add_argument(
        "--inventory-file",
        type=Path,
        metavar="FILE",
        help=(
            "text file of your inventory copied from FarmRPG "
            "(overrides solver.inventory_file in the config)"
        ),
    )
    parser.add_argument(
        "--empty-mastery",
        action="store_true",
        help=(
            "ignore the mastery file (and solver.mastery_file config) and start "
            "from no mastery progress"
        ),
    )
    parser.add_argument(
        "--empty-inventory",
        action="store_true",
        help=(
            "ignore the inventory file (and solver.inventory_file config) and "
            "start from an empty inventory"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help=(
            "config file to load instead of the bundled config.toml (e.g. "
            "web_default_config.toml, the web app's defaults). --override still "
            "applies on top"
        ),
    )
    parser.add_argument(
        "--override-inventory",
        metavar="ITEM:AMOUNT[,...]",
        help=(
            "force the held amount of the given item(s) to AMOUNT, applied "
            "after the inventory file (or --empty-inventory) is loaded, so it "
            "overrides whatever that gave those items (e.g. "
            "'Medium Chest:0,Arnold Palmer:100'). AMOUNT accepts M/GM/MM "
            "(10K/100K/1M) and k/m suffixes, case-insensitively"
        ),
    )

    parser.add_argument(
        "--override",
        action="append",
        metavar="SECTION.FLAG=VALUE",
        help=(
            "override a config value for this run without editing config.toml "
            "(e.g. --override sawmill.use_hickory_omelette=false). The value is "
            "parsed as TOML (true/false, numbers, quoted strings), falling back "
            "to a literal string. Repeatable"
        ),
    )

    parser.add_argument(
        "-t",
        "--tower",
        type=Path,
        dest="tower",
        metavar="FILE",
        help="tower-requirements file to use instead of the built-in ones",
    )
    parser.add_argument(
        "-l",
        "--level",
        type=int,
        help="target tower level (default: highest reachable level)",
    )
    parser.add_argument(
        "--target",
        type=parse_target,
        default=None,
        metavar="tower|none|N|TN",
        help=(
            "the goal to solve for: 'tower' (the default) climbs to the highest "
            "reachable level (or -l's level); a level number ('150' or 'T150') "
            "climbs to just that level, overriding -l. With -m/-i also given, "
            "those become extra goals added to the tower's: a found item counts "
            "toward mastery and sits in inventory both, so a goal for an item "
            "the tower (or another -m/-i goal) already targets is compared the "
            "same way -- whichever needs the most items produced wins, clearing "
            "the smaller as a side effect (a lower -m target than the tower's "
            "own is absorbed; an -i floor only takes over once it calls for "
            "more items than a mastery goal already produces). An -i goal on a "
            "pure crafting material (not itself mastered) is instead a floor on "
            "top of what the tower's crafting already consumes of it, so the "
            "solver crafts extra to leave that many spare. 'none' drops the "
            "tower and solves only the -m/-i goals; omitting --target does the "
            "same implicitly when -m/-i are set (the previous behaviour)"
        ),
    )
    parser.add_argument(
        "--ascend",
        type=int,
        default=None,
        metavar="LEVEL",
        help=(
            "ascension mode: climb the tower from the current level to LEVEL by "
            "choosing masteries and waiting on chores (rather than solving a fixed "
            "goal), producing a frontier of (time, net gold) plans. Conflicts with "
            "--target/-m/-i/--use-pumpkin-juice. LEVEL must be above the current "
            "level and at most 200 (101-200 additionally schedules the "
            "megamasteries those levels require)"
        ),
    )
    parser.add_argument(
        "--sweep",
        type=int,
        default=None,
        metavar="N",
        help=(
            "how many gold-weight passes the --ascend frontier sweep solves "
            "(default 16: the fast/cheap endpoints plus 14 log-spaced interior "
            "weights; minimum 2). Every pass is a full staged solve, so the "
            "sweep dominates --ascend's wall time and fewer passes are "
            "proportionally faster -- at the cost of a sparser frontier "
            "between the endpoints. 2 still finds the fastest and the "
            "cheapest plans exactly"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help=(
            "how many worker processes to fan the --ascend gold-weight sweep "
            "across (the passes are independent). Default: one per CPU (or the "
            "FARM_SOLVER_ASCEND_JOBS env var), capped at the pass count. 1 forces "
            "the in-process sequential path. Ignored outside --ascend"
        ),
    )
    parser.add_argument(
        "--starting-ak",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Ascension Knowledge already banked, for --ascend (default: [tower] "
            "ascension_knowledge, else 0)"
        ),
    )
    parser.add_argument(
        "--tower-level",
        type=int,
        default=None,
        metavar="N",
        help=(
            "override the current tower level ([tower] level in the config); "
            "with --ascend and neither this nor the config set, defaults to 0 "
            "(a fresh account) rather than [tower] level's usual \"assume every "
            "reward owned\" meaning"
        ),
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help=(
            "date the plan starts, used to count how much of it a seasonal pet "
            "is in season for (default: today)"
        ),
    )
    calendar_mode = parser.add_mutually_exclusive_group()
    calendar_mode.add_argument(
        "--model-calendar",
        action="store_true",
        help=(
            "model the seasonal calendar in full -- date-aware seasonal pets and "
            "Mechanical Heart grants, given the plan's start date -- instead of "
            "the yearly average (the default; overrides solver.calendar in "
            "the config); costs an extra solve (a makespan fixed point)"
        ),
    )
    calendar_mode.add_argument(
        "--average-calendar",
        action="store_true",
        help=(
            "scale seasonal pets and Mechanical Heart grants to their yearly "
            "average instead of modelling the calendar, overriding "
            "solver.calendar in the config, forcing the single-solve fast path"
        ),
    )
    calendar_mode.add_argument(
        "--ignore-calendar",
        action="store_true",
        help=(
            "leave the seasonal pets and Mechanical Heart grants out of the "
            "plan entirely (no seasonal/monthly output at all), overriding "
            "solver.calendar in the config"
        ),
    )
    parser.add_argument(
        "--solver",
        metavar="NAME",
        default=None,
        help=(
            "LP solver backend: 'highs' (in-process, the default when highspy "
            "is installed), 'cbc' (PuLP's bundled fallback), or any installed "
            "PuLP solver name; overrides solver.backend in the config"
        ),
    )
    parser.add_argument(
        "--crafting-bonus",
        type=float,
        default=None,
        metavar="NUM",
        help=(
            "average items produced per craft, reducing the materials needed "
            "(default: from [workshop] resource_saver (else 1.45))"
        ),
    )
    parser.add_argument(
        "--mastery-bonus",
        type=float,
        default=crafting.DEFAULT_MASTERY_BONUS,
        metavar="NUM",
        help=(
            "mastery gained per crafted item, reducing the crafts needed for "
            f"mastery goals (default: {crafting.DEFAULT_MASTERY_BONUS})"
        ),
    )
    parser.add_argument(
        "-b",
        "--breakdown",
        action="store_true",
        help="list every craft that needs to happen, with the materials it uses",
    )
    parser.add_argument(
        "--unobtainable",
        action="store_true",
        help=(
            "list workshop materials with no known source (not produced "
            "passively, fished, explored, farmed, fetched by pets, "
            "auto-supplied, or craftable)"
        ),
    )
    parser.add_argument(
        "--max-gold",
        type=parse_gold_budget,
        default=None,
        metavar="N",
        help=(
            "cap the gold a plan may spend buying consumables, trading it against "
            "time. The frontier always spans self-sourced (spend nothing, slowest) "
            "to buy-freely (fastest); this drops plans over the cap. 'inventory' "
            "means 0 (self-source everything), 'unlimited' (the default) means no "
            "cap -- the whole frontier"
        ),
    )
    parser.add_argument(
        "--use-pumpkin-juice",
        nargs="?",
        const=-1,
        default=None,
        type=_pumpkin_juice_limit,
        metavar="N",
        help=(
            "let the solver buy Pumpkin Juice -- a bought item that instantly boosts "
            "one mastery goal by 10%% of its tier -- to finish sooner or cheaper "
            "(overrides mastery.use_pumpkin_juice). Bare, any number may be used; "
            "'=N' caps the total spent (the cap need not be reached). Priced at "
            "trading.pumpkin_juice gold each"
        ),
    )
    trading = parser.add_mutually_exclusive_group()
    trading.add_argument(
        "--use-trading",
        dest="use_trading",
        action="store_true",
        default=None,
        help=(
            "let the solver trade resources for Arnold Palmers (overrides "
            "[trading.ap] enabled). A trade is an instant buy priced in AP, "
            "converted to gold; it earns no mastery"
        ),
    )
    trading.add_argument(
        "--no-trading",
        dest="use_trading",
        action="store_false",
        help="forbid Arnold-Palmer trading even if [trading.ap] enabled is set",
    )
    parser.add_argument(
        "--current-stamina",
        type=int,
        default=0,
        metavar="N",
        help=(
            "stamina already banked, spendable on Apple Cider explores "
            "(default: 0)"
        ),
    )
    stamina_pricing = parser.add_mutually_exclusive_group()
    stamina_pricing.add_argument(
        "--price-stamina",
        dest="price_stamina",
        action="store_true",
        default=None,
        help=(
            "charge free (banked + passive) stamina its Orange-Juice-equivalent "
            "opportunity cost, so a plan that drains the stamina bank is not free "
            "(overrides [trading] price_stamina; the default is on)"
        ),
    )
    stamina_pricing.add_argument(
        "--no-price-stamina",
        dest="price_stamina",
        action="store_false",
        help="treat banked/passive stamina as free (do not price it)",
    )
    parser.add_argument(
        "--plans",
        type=int,
        default=5,
        metavar="N",
        help="how many plans to spread across the frontier (default: 5)",
    )
    parser.add_argument(
        "--spread",
        type=_parse_spread,
        default=None,
        metavar="AXIS[=LO:HI]",
        help=(
            "spread the --plans shown frontier plans linearly along AXIS instead "
            "of the default time-thin -- one of time, gold, pumpkin-juice (alias "
            "pj), or passive (overrides [frontier] spread). An optional '=LO:HI' "
            "narrows the sampled range (ignored for passive, which enumerates its "
            "own breakpoints); the default range stays within the fast regime "
            "already explored (each axis's own fast/cheap endpoints, or the "
            "feasible max for pumpkin-juice), so an explicit lo:hi is only needed "
            "to go beyond that. E.g. 'pj=0:10' to see 0..10 Pumpkin Juice spent, "
            "'gold=0:5000', or bare 'time'"
        ),
    )
    parser.add_argument(
        "--graph",
        nargs="?",
        const="auto",
        default=None,
        choices=["auto", "gold", "time"],
        help=(
            "render an impact graph (the chosen metric against the spread axis) "
            "alongside the frontier table -- ASCII in the terminal, an SVG on the "
            "web (overrides [frontier] graph). Bare, 'auto' picks the natural "
            "counterpart of the spread axis (gold, or time when spreading by "
            "gold); 'gold'/'time' pick one explicitly. Off by default"
        ),
    )
    parser.add_argument(
        "--passive-limit",
        metavar="DURATION",
        help=(
            "cap the total time of a sourcing plan (e.g. '5d', '3h', '1d6h'); "
            "plans slower than this are dropped from the frontier. A cap below the "
            "fastest plan cannot be met (the farming critical path sets the floor), "
            "so the whole frontier is shown with a note. With no limit the whole "
            "frontier is shown, however long the slowest plan takes."
        ),
    )
    parser.add_argument(
        "--max-ap",
        type=int,
        metavar="N",
        help="drop frontier plans that spend more than N Arnold Palmers",
    )
    parser.add_argument(
        "--max-ln",
        type=int,
        metavar="N",
        help="drop frontier plans that spend more than N Large Nets",
    )
    parser.add_argument(
        "--max-ac",
        type=int,
        metavar="N",
        help="drop frontier plans that spend more than N Apple Ciders",
    )
    parser.add_argument(
        "--cookie-sets",
        type=int,
        metavar="N",
        help=(
            "number of full cookie sets available to speed up farming, "
            "overriding the count taken from inventory (the fewest of Happy, "
            "Spooky and Lovely Cookies). A set boosts one day of a slow crop's "
            "Grape Juice grows ninefold"
        ),
    )

    # Which frontier plan to expand in full: the fastest (the default), the
    # cheapest (lowest net gold -- buys least, banks most), or a specific one by its
    # number in the table.
    plan = parser.add_mutually_exclusive_group()
    plan.add_argument(
        "--fastest-plan",
        action="store_true",
        help="expand the fastest plan (the default)",
    )
    plan.add_argument(
        "--cheapest-plan",
        action="store_true",
        help="expand the plan with the lowest net gold (buys least, banks most)",
    )
    plan.add_argument(
        "--plan",
        type=int,
        metavar="N",
        help="expand plan number N from the frontier table instead",
    )

    # -m and -i can be combined (each just contributes its own need list, see
    # farm_solver.solve_goals/solve_target); without --target (or with
    # --target none) they replace the tower goal, with --target tower/N they are
    # added on top of it instead.
    parser.add_argument(
        "-m",
        "--mastery",
        metavar="ITEM:AMOUNT[,...]",
        help=(
            "solve for mastering the given item(s), combined with -i if that is "
            "also given; without --target this replaces the tower goal, with "
            "--target it is added on top. AMOUNT accepts M/GM/MM (10K/100K/1M) "
            "and k/m suffixes, case-insensitively"
        ),
    )
    parser.add_argument(
        "-i",
        "--items",
        metavar="ITEM:AMOUNT[,...]",
        help=(
            "solve for acquiring the given item(s), combined with -m if that is "
            "also given; without --target this replaces the tower goal, with "
            "--target it is an extra goal (at least this many held at the end); "
            "see --target's help for how it interacts with a mastery goal on "
            "the same item. AMOUNT accepts M/GM/MM (10K/100K/1M) and k/m "
            "suffixes, case-insensitively"
        ),
    )
    parser.add_argument(
        "--format",
        type=parse_format,
        default="text",
        metavar="text|markdown",
        help=(
            "report layout: 'text' (default) or 'markdown' ('txt'/'md' also "
            "accepted), with tables for the frontier and each plan section. "
            "Markdown renders through the terminal (via rich) when stdout is a "
            "terminal, or prints the raw Markdown source when redirected (e.g. "
            "to a .md file)"
        ),
    )
    parser.add_argument(
        "--pager",
        action="store_true",
        help=(
            "page the rendered report through the system pager ($PAGER, else "
            "less), preserving the rendered tables and styling -- unlike piping "
            "this process's own output to a pager, which would just show the "
            "raw Markdown source. Requires --format markdown and a terminal"
        ),
    )

    return parser


# Seasonal pet availability depends on the plan's makespan, which depends back on
# it; iterate the solve at most this many times to settle that loop.
_MAX_SEASON_ITERS = 5


def _makespans_close(a: timedelta, b: timedelta) -> bool:
    """Whether two makespans agree closely enough to stop the seasonal iteration."""
    return abs((a - b).total_seconds()) <= max(3600.0, 0.001 * b.total_seconds())


CALENDAR_MODES = ("full", "average", "ignore")


def resolve_calendar_mode(args: argparse.Namespace, cfg: dict) -> str:
    """How date-dependent sources (seasonal pets, monthly grants) are treated.

    ``full`` models the calendar exactly from the plan's start date, ``average``
    smears each source to its steady-state yearly/monthly share, and ``ignore``
    leaves them out of the plan entirely. The three CLI flags beat the
    ``solver.calendar`` config key; absent both, ``full`` (the historical
    default behaviour, subject to :func:`_should_model_calendar`'s
    is-anything-actually-dated check).
    """
    if args.model_calendar:
        return "full"
    if args.average_calendar:
        return "average"
    if args.ignore_calendar:
        return "ignore"
    solver_cfg = cfg.get("solver", {})
    if "model_calendar" in solver_cfg:
        raise ValueError(
            "solver.model_calendar is gone; set solver.calendar to "
            "'full', 'average' or 'ignore' instead"
        )
    mode = solver_cfg.get("calendar", "full")
    if mode not in CALENDAR_MODES:
        raise ValueError(
            f"solver.calendar must be one of {', '.join(CALENDAR_MODES)}, "
            f"not {mode!r}"
        )
    return mode


def strip_dated_sources(cfg: dict) -> dict:
    """A copy of ``cfg`` with every seasonal pet dropped (calendar ``ignore``).

    Year-round pets and every building stay; the Mechanical Heart's monthly
    grants are gated separately (``build_inputs`` forces ``monthly_grants``
    off in this mode) since they come from the tower level, not the config.
    """
    kept = {
        name: level
        for name, level in cfg.get("pets", {}).items()
        if name.lower() not in pets.SEASONAL_MONTHS
    }
    return {**cfg, "pets": kept}


def _should_model_calendar(
    pets_config: dict,
    calendar_mode: str,
    monthly_grants: bool = False,
) -> bool:
    """Whether to model the seasonal calendar rather than a yearly average.

    The calendar model costs an extra solve (an iterative fixed point on the
    makespan), so only the ``full`` mode (see :func:`resolve_calendar_mode`)
    ever pays it -- and even then only when something in the plan is actually
    date-dependent: a seasonal pet, or the Mechanical Heart's monthly grants
    (``monthly_grants``). With neither, the yearly average is exact and the
    extra solve is skipped. ``average`` and ``ignore`` never model it
    (``ignore`` has no dated sources left to model).
    """
    if calendar_mode != "full":
        return False
    has_seasonal_pet = any(name.lower() in pets.SEASONAL_MONTHS for name in pets_config)
    return has_seasonal_pet or monthly_grants


def obtainable_items(
    production: passive.PassiveProduction,
    farm: crops.Farm | None = None,
    fish: fishing.Fishing | None = None,
    explore: exploring.Exploring | None = None,
) -> set[str]:
    """Lower-cased items obtainable without crafting.

    Covers passive production (which includes pets), farming (the fixed farmed
    items plus everything the crop model can grow, including rare seed drops),
    the fishing/exploring drops (from the built models, when given), and the
    materials the game supplies automatically.
    """
    items = set(crafting.IGNORED_MATERIALS) | set(farming.FARMING_ITEMS)
    for producer in production.producers:
        items |= set(producer.outputs)
    if farm is not None:
        items |= farm.growable_items()
    if fish is not None:
        for zone in fish.zones.values():
            items |= set(zone.drop_rates)
    if explore is not None:
        for location in explore.locations.values():
            items |= set(location.drop_rates)
    return items


def unobtainable_materials(
    workshop: crafting.Workshop, obtainable: set[str]
) -> list[str]:
    """Workshop materials that are neither obtainable nor craftable.

    These have no known source: they are not produced passively, fished,
    explored, farmed, fetched by pets, auto-supplied, nor made from a recipe of
    their own.
    """
    craftable = set(workshop.recipes)
    found: dict[str, str] = {}  # lower-cased key -> display name
    for recipe in workshop.recipes.values():
        for material in recipe.materials:
            key = material.lower()
            if key not in obtainable and key not in craftable:
                found[key] = material
    return sorted(found.values())


def _latest_dated_file(directory: Path, prefix: str) -> Path | None:
    """The most recent ``{prefix}*`` file in ``directory``, or ``None``.

    Filenames are expected to look like ``mastery-2026-07-08.txt``: the
    ISO-formatted date suffix sorts correctly as a plain string, so the last
    name in sorted order is the most recent file.
    """
    candidates = sorted(p for p in directory.glob(f"{prefix}*") if p.is_file())
    return candidates[-1] if candidates else None


def resolve_files(
    args: argparse.Namespace, cfg: dict
) -> tuple[Path | None, Path | None]:
    """The mastery and inventory files, from the CLI or the [solver] config.

    A command-line ``--mastery-file`` / ``--inventory-file`` overrides the
    config; otherwise the value comes from the ``[solver]`` section. If
    neither gives a file and ``solver.data_directory`` is set, the most
    recent ``mastery-*``/``inventory-*`` file in that directory is used
    (sorted by filename, so a ``YYYY-MM-DD`` suffix picks the latest date). A
    file missing from all three raises a :class:`ValueError`.
    ``--empty-mastery`` / ``--empty-inventory`` skip resolution entirely
    (``None``), so neither the flag nor the config need be set.
    """
    solver = cfg.get("solver", {})
    data_directory = solver.get("data_directory")

    def pick(cli: Path | None, key: str, flag: str, empty: bool) -> Path | None:
        if empty:
            return None
        if cli is not None:
            return cli
        value = solver.get(key)
        if value is not None:
            return Path(value)
        prefix = key.removesuffix("_file")
        if data_directory is not None:
            found = _latest_dated_file(Path(data_directory), prefix)
            if found is not None:
                return found
        raise ValueError(
            f"no {key.replace('_', ' ')}: pass {flag}, set solver.{key}, or "
            f"set solver.data_directory to a directory containing a {prefix}* "
            "file, in the config"
        )

    return (
        pick(args.mastery_file, "mastery_file", "--mastery-file", args.empty_mastery),
        pick(
            args.inventory_file,
            "inventory_file",
            "--inventory-file",
            args.empty_inventory,
        ),
    )


def load_state(
    masteries_path: Path | None, inventory_path: Path | None
) -> farm_solver.GameState:
    masteries = (
        parsing.parse_masteries(masteries_path.read_text()) if masteries_path else {}
    )
    inventory = (
        parsing.parse_inventory(inventory_path.read_text())
        if inventory_path
        else farm_solver.Inventory()
    )
    return farm_solver.GameState(masteries=masteries, inventory=inventory)


def apply_inventory_overrides(
    inventory: farm_solver.Inventory, overrides: dict[str, int]
) -> None:
    """Force each item in ``overrides`` to the given held amount, in place.

    Meant to run right after the inventory file (or ``--empty-inventory``) is
    loaded, so these values replace whatever amount the file gave those items
    -- the file's amount for an overridden item is ignored entirely.
    """
    for item, amount in overrides.items():
        existing = inventory.get(item)
        category = existing.category if existing is not None else None
        inventory.items[item.lower()] = farm_solver.InventoryItem(
            name=item, quantity=amount, category=category
        )


def within_limits(
    plan: optimizer.SourcingPlan,
    time_limit: timedelta | None = None,
    max_ap: int | None = None,
    max_ln: int | None = None,
    max_gold: int | None = None,
    max_ac: int | None = None,
) -> bool:
    """Whether a plan respects every given time, consumable and gold ceiling."""
    return (
        (time_limit is None or plan.time <= time_limit)
        and (max_ap is None or round(plan.arnold_palmers) <= max_ap)
        and (max_ln is None or round(plan.large_nets) <= max_ln)
        and (max_ac is None or round(plan.apple_ciders) <= max_ac)
        and (max_gold is None or round(plan.gold) <= max_gold)
    )


def limit_shortfall(
    plans: list[optimizer.SourcingPlan],
    time_limit: timedelta | None,
    max_ap: int | None,
    max_ln: int | None,
    max_gold: int | None = None,
    max_ac: int | None = None,
) -> str:
    """Explain why no plan met the limits, naming the best each axis reaches.

    The frontier's fastest plan is the makespan floor (the farming critical path,
    which consumables cannot shorten), so a limit under it is simply infeasible --
    worth saying rather than silently showing the whole frontier as if unlimited.
    Called only when the limits leave no plan; each axis a plan can hit alone is
    dropped, so an empty list means no single plan meets them all at once.
    """
    parts = []
    if time_limit is not None:
        fastest = min(p.time for p in plans)
        if fastest > time_limit:
            parts.append(
                f"under {passive.format_duration(time_limit)}"
                f" (fastest is {passive.format_duration(fastest)})"
            )
    if max_ap is not None:
        fewest = round(min(p.arnold_palmers for p in plans))
        if fewest > max_ap:
            parts.append(f"within {max_ap:,} AP (fewest is {fewest:,})")
    if max_ln is not None:
        fewest = round(min(p.large_nets for p in plans))
        if fewest > max_ln:
            parts.append(f"within {max_ln:,} LN (fewest is {fewest:,})")
    if max_ac is not None:
        fewest = round(min(p.apple_ciders for p in plans))
        if fewest > max_ac:
            parts.append(f"within {max_ac:,} AC (fewest is {fewest:,})")
    if max_gold is not None:
        least = round(min(p.gold for p in plans))
        if least > max_gold:
            parts.append(f"within {max_gold:,} gold (least is {least:,})")
    fits = " or ".join(parts) if parts else "every limit at once"
    return f"No plan fits {fits}; showing the full frontier."


def cheapest_plan(plans: list[optimizer.SourcingPlan]) -> int:
    """Index of the cheapest plan: least net gold, ties broken by least time.

    A plan's net gold is ``plan.cost[1]`` (see :attr:`optimizer.SourcingPlan.cost`)
    -- the gold it spends buying consumables, less the sale value of the surplus it
    banks, plus the opportunity cost of the free stamina it burns. The cheapest plan
    is the one lowest on that axis: it buys least and banks most, the fully
    self-sourced, passive-leaning end of the frontier, which credited for its banked
    surplus can sit below zero gold.
    """
    return min(
        range(len(plans)),
        key=lambda i: (plans[i].cost[1], plans[i].time.total_seconds()),
    )


def selected_plan(
    plans: list[optimizer.SourcingPlan],
    plan: int | None,
    cheapest: bool,
) -> int:
    """Index of the plan to expand, from ``--plan`` / ``--cheapest-plan``.

    Explicit ``--plan N`` wins (1-based, validated); ``--cheapest-plan`` picks
    the least-gold plan; otherwise the default is the fastest, which is plan 0
    since the frontier is sorted fastest first.
    """
    if plan is not None:
        if not 1 <= plan <= len(plans):
            raise ValueError(f"--plan must be between 1 and {len(plans)}")
        return plan - 1
    if cheapest:
        return cheapest_plan(plans)
    return 0


# A full cookie set is one of each type active at once; the number of sets is
# limited by whichever type is scarcest.
COOKIE_ITEMS = ("happy cookies", "spooky cookies", "lovely cookies")


def cookie_sets_available(
    inventory: dict[str, float], override: int | None
) -> float:
    """How many full cookie sets a plan may spend on farming.

    ``override`` (the ``--cookie-sets`` flag) wins when given, in either
    direction; otherwise it is the fewest of the three cookie types held, since a
    set needs one of each. With none held and no override, it is zero.
    """
    if override is not None:
        return max(0, override)
    return min((inventory.get(item, 0.0) for item in COOKIE_ITEMS), default=0.0)


def load_config(path: Path | None = None) -> dict:
    """Read the config file (``path``, else the bundled default), or ``{}`` if none."""
    try:
        return config.load_config(path)
    except FileNotFoundError:
        return {}


@dataclass
class SolveInputs:
    """Everything one (re-)solve needs, built once from args/config/state."""

    time_limit: timedelta | None
    state: farm_solver.GameState
    requirements: farm_solver.TowerRequirements
    farm: crops.Farm
    explore: exploring.Exploring
    fish: fishing.Fishing
    workshop: crafting.Workshop
    monthly_grants: bool
    solution: farm_solver.Solution
    recipes: dict
    smith: locksmith.Locksmith
    open_flows: list[sources.OpenFlow]
    demand: dict[str, float]
    inventory: dict[str, float]
    cookie_sets: float | None
    prices: dict[str, float] | None
    # Hickory Omelette sawmill-boost offers (empty unless [sawmill] hickory_omelette
    # is set); static across the seasonal fixed point, so built once here.
    boost_offers: list[sources.Offer]
    pj_gain: dict[str, float]
    pj_price: float
    pj_limit: int | None
    model_calendar: bool
    start: date
    # The tower level the goal targets (an explicit --target/-l, else the
    # tower's top level), or None when a pure -m/-i goal has no tower part.
    target_level: int | None = None
    # Items demanded only as a plain -i count (never -m mastery); a trade may
    # satisfy these, so the optimiser's buy-floor is relaxed for them (see
    # crafting.non_mastery_goal_items and optimizer.minimize's floor). Defaulted so
    # hand-built test SolveInputs need not supply it.
    non_mastery_goals: frozenset[str] = frozenset()
    # Per-unit Arnold Palmer trade prices (see _ChannelSetup.ap_prices). Defaulted
    # likewise; empty means no AP-trading.
    ap_prices: dict[str, float] = field(default_factory=dict)


@dataclass
class _ChannelSetup:
    """Channel/price/inventory/recipe/openable setup independent of any goal.

    Everything :func:`build_inputs` builds before it ever looks at ``-m``/``-i``/
    the tower goal -- factored out so :func:`solve_ascension_frontier` (which
    has no goal at all, just an empty ``demand``) can build the same channels
    per stage without duplicating this. ``model_calendar`` mirrors
    :class:`SolveInputs`' field; ascension mode ignores it (see that function's
    steady-state note) but it costs nothing extra to compute here.
    """

    requirements: farm_solver.TowerRequirements
    farm: crops.Farm
    explore: exploring.Exploring
    fish: fishing.Fishing
    workshop: crafting.Workshop
    monthly_grants: bool
    recipes: dict
    smith: locksmith.Locksmith
    open_flows: list[sources.OpenFlow]
    inventory: dict[str, float]
    cookie_sets: float | None
    prices: dict[str, float] | None
    boost_offers: list[sources.Offer]
    model_calendar: bool
    # Per-unit Arnold Palmer trade prices (config.arnold_palmer_prices), keyed by
    # lower-cased item name; empty when trading is disabled. Their gold-equivalent
    # is already folded into ``prices``; this copy is for the report's Trade section
    # and for deriving the ``ap_tradeable`` buyable set passed to the optimiser.
    # Defaulted so hand-built test _ChannelSetups need not supply it.
    ap_prices: dict[str, float] = field(default_factory=dict)


def _build_channel_setup(
    args: argparse.Namespace,
    cfg: dict,
    state: farm_solver.GameState,
    start: date,
    crafting_bonus: float,
) -> _ChannelSetup:
    """Build the goal-independent half of :func:`build_inputs`.

    ``crafting_bonus`` is a plain, already-resolved float rather than read off
    ``args`` -- a caller solving several configs at once (ascension mode's
    per-stage cfgs, whose crafting bonus changes once the level-90 reward is
    crossed) passes its own per-stage value; :func:`build_inputs` passes
    ``args.crafting_bonus`` (resolved once, for the whole run, by
    :func:`resolve_crafting_bonus` before any of this runs).
    """
    workshop = crafting.load_workshop()
    fish = fishing.fishing_from_config(cfg.get("fishing", {}))

    # Loaded unconditionally (not just for a tower goal): reaching T250 auto-
    # detects the Scythe of Dewstar (see resolve_scythe_of_dewstar) regardless
    # of which goal (if any) this run solves.
    requirements = farm_solver.load_tower_requirements(args.tower)
    # [farming] grape_juices_per_day is the undoubled base value; the level-150
    # tower reward doubles it (see config.effective_grape_juices). tower_level
    # (config.current_tower_level) is None -- assume every level-gated reward
    # owned -- unless a [tower] level says otherwise.
    tower_level = config.current_tower_level(cfg)
    farm = crops.farm_from_config(
        {
            **cfg.get("farming", {}),
            "grape_juices_per_day": config.effective_grape_juices(cfg, tower_level),
        },
        have_scythe_of_dewstar=resolve_scythe_of_dewstar(cfg, state, requirements),
        have_dewstars_journal=resolve_dewstars_journal(cfg, state.inventory),
    )
    explore = exploring.exploring_from_config(
        cfg.get("exploring", {}),
        have_fungus_processing=resolve_fungus_processing(cfg, state.inventory),
    )
    # The T300 tower artifact grants a monthly bulk of Apple Cider / Arnold Palmer
    # (config.MONTHLY_GRANTS), auto-detected from the tower level like the Scythe;
    # added to passive production below (scaled to the plan span, like a pet).
    # The grants come from the tower level rather than the config, so the
    # calendar 'ignore' mode gates them here (seasonal pets are instead
    # stripped from cfg by the caller, see strip_dated_sources).
    calendar_mode = resolve_calendar_mode(args, cfg)
    monthly_grants = (
        resolve_monthly_grants(cfg, state, requirements) and calendar_mode != "ignore"
    )
    model_calendar = _should_model_calendar(cfg.get("pets", {}), calendar_mode, monthly_grants)

    # Every recipe is a craft flow; the optimiser sources the top-level demand,
    # choosing craft-vs-gather for itself. Openables are free multi-output flows
    # (open a chest or bag for its contents), so the solver can gather a chest and
    # open it when that beats gathering the contents directly. (The channel Offers
    # are rebuilt by the caller, since the passive ones depend on the plan span.)
    recipes = crafting.recipe_flows(workshop)
    smith = locksmith.load_openables()
    open_flows = sources.open_flows(smith)
    # Items already in stock are free supply for the intermediates a plan would
    # otherwise gather, craft or open (the optimiser skips crediting the directly
    # demanded items, whose shortfall is already net of what is held).
    inventory = {
        key: float(entry.quantity) for key, entry in state.inventory.items.items()
    }
    # Stamina already banked seeds the inventory so Apple Cider explores can spend
    # it; the drink-Orange-Juice flow tops it up over time.
    if args.current_stamina:
        inventory["stamina"] = inventory.get("stamina", 0.0) + args.current_stamina
    cookie_sets = cookie_sets_available(inventory, args.cookie_sets)
    # Per-unit gold prices for the buyable consumables (the [trading] config): what
    # makes each buyable, so the frontier can trade makespan against real gold.
    # Absent, nothing is buyable and every plan is fully self-sourced.
    prices = config.trading_prices(cfg, crafting_bonus=crafting_bonus)
    # Value free (banked + passive) stamina at its Orange-Juice equivalent so a
    # plan that drains it is not treated as free (see price_stamina_enabled,
    # optimizer._build_base). A valuation-only key, like grape juice -- never
    # buyable. Off in ascension mode (a staged climb does not aggregate it).
    if (
        prices
        and "orange juice" in prices
        and not getattr(args, "ascend", None)
        and price_stamina_enabled(args, cfg)
    ):
        prices["stamina"] = prices["orange juice"] / sources.ORANGE_JUICE_STAMINA
    # Arnold-Palmer trade prices (per-unit AP) for the resources not already
    # gold-buyable: an extra instant-buy channel whose gold-equivalent is already
    # folded into ``prices`` by trading_prices. Empty when disabled. Kept for the
    # report's Trade section and to derive the optimiser's ``ap_tradeable`` set.
    ap_prices = config.arnold_palmer_prices(
        cfg, enabled_override=getattr(args, "use_trading", None)
    )

    # Hickory Omelette sawmill boost: a grounded, omelette-consuming channel (see
    # sources.sawmill_boost_offers) built from the sawmill's base hourly outputs, the
    # [sawmill] hickory_omelette setting and the storehouse size (which caps each of
    # the boost's ten-minute deposits, as it caps the base run). Static across the
    # seasonal fixed point.
    boost_offers = sources.sawmill_boost_offers(
        config.sawmill_outputs(cfg),
        config.hickory_omelette_mode(cfg),
        config.storehouse_size(cfg, today=start),
    )

    return _ChannelSetup(
        requirements=requirements,
        farm=farm,
        explore=explore,
        fish=fish,
        workshop=workshop,
        monthly_grants=monthly_grants,
        recipes=recipes,
        smith=smith,
        open_flows=open_flows,
        inventory=inventory,
        cookie_sets=cookie_sets,
        prices=prices,
        ap_prices=ap_prices,
        boost_offers=boost_offers,
        model_calendar=model_calendar,
    )


def build_inputs(
    args: argparse.Namespace, cfg: dict, state: farm_solver.GameState
) -> SolveInputs:
    """Build a :class:`SolveInputs` from parsed args/config and a loaded state.

    ``state`` is expected already loaded, with ``--override-inventory``
    applied (the CLI does file loading + the override in :func:`run`; a web
    caller instead builds a state directly from pasted request text).
    """
    start = args.start_date or date.today()

    # Parse the time limit first so a bad duration fails before any output.
    time_limit = (
        passive.parse_duration(args.passive_limit)
        if args.passive_limit is not None
        else None
    )

    setup = _build_channel_setup(args, cfg, state, start, args.crafting_bonus)
    requirements = setup.requirements

    # Goal selection (see farm_solver.solve_goals/solve_target and the --target
    # help text). --target none drops the tower explicitly; omitting --target
    # does the same implicitly *when -m/-i are set* (the previous behaviour).
    # Otherwise the tower is a goal, with any -m/-i layered on top. -m and -i can
    # be combined either way.
    mastery_goals = (
        parsing.parse_item_amounts(args.mastery) if args.mastery is not None else None
    )
    item_goals = (
        parsing.parse_item_amounts(args.items) if args.items is not None else None
    )
    goals_only = args.target == "none" or (
        args.target is None and (mastery_goals is not None or item_goals is not None)
    )
    if goals_only:
        solution = farm_solver.solve_goals(state, mastery_goals, item_goals)
        target_level = None  # no tower component to this goal
    else:
        target_level = args.target if isinstance(args.target, int) else args.level
        solution = farm_solver.solve_target(
            state, requirements, target_level, mastery_goals, item_goals
        )
        if target_level is None:
            # What "the tower" resolves to (solve_tower's own default), so a
            # caller can display the level actually targeted.
            target_level = requirements.max_level

    demand = crafting.top_level_demand(solution.needs, args.mastery_bonus)
    non_mastery_goals = crafting.non_mastery_goal_items(solution.needs)

    # Pumpkin Juice: an optional bought mastery boost. Each juice covers part of a
    # mastery goal's shortfall directly, trading gold for a faster (or cheaper) plan;
    # the optimiser decides how many (up to an optional cap) each frontier plan uses.
    pj_enabled, pj_limit, pj_price = resolve_pumpkin_juice(args.use_pumpkin_juice, cfg)
    pj_gain = (
        pumpkin_juice_gains(solution.needs, state, args.mastery_bonus)
        if pj_enabled
        else {}
    )

    if args.plans < 1:
        raise ValueError("--plans must be at least 1")

    return SolveInputs(
        time_limit=time_limit,
        state=state,
        requirements=requirements,
        farm=setup.farm,
        explore=setup.explore,
        fish=setup.fish,
        workshop=setup.workshop,
        monthly_grants=setup.monthly_grants,
        solution=solution,
        recipes=setup.recipes,
        smith=setup.smith,
        open_flows=setup.open_flows,
        demand=demand,
        non_mastery_goals=non_mastery_goals,
        inventory=setup.inventory,
        cookie_sets=setup.cookie_sets,
        prices=setup.prices,
        ap_prices=setup.ap_prices,
        boost_offers=setup.boost_offers,
        pj_gain=pj_gain,
        pj_price=pj_price,
        pj_limit=pj_limit,
        model_calendar=setup.model_calendar,
        start=start,
        target_level=target_level,
    )


@dataclass
class FrontierResult:
    """The frontier sweep's output, before any refilter (limits/selection)."""

    plans: list[optimizer.SourcingPlan]
    within: list[optimizer.SourcingPlan]
    shown: list[optimizer.SourcingPlan]
    offers: list[sources.Offer]
    # None only in stripped-down callers (hand-built test FrontierResults); the
    # solve pipeline always fills it in.
    production: passive.PassiveProduction | None
    horizon: timedelta | None


def solve_frontier(
    inputs: SolveInputs,
    args: argparse.Namespace,
    cfg: dict,
    horizon_seed: timedelta | None = None,
) -> FrontierResult:
    """Run the seasonal fixed-point loop and the frontier sweep.

    ``cfg`` is needed (not part of :class:`SolveInputs`) purely to rebuild
    ``config.passive_production`` each iteration -- the config itself never
    changes mid-solve, unlike everything else in ``inputs``. ``horizon_seed``
    initialises the fixed point's ``horizon`` instead of ``None`` -- used by
    the web API's horizon-guard re-solve, seeded from the already-selected
    plan's makespan; the CLI never passes it.
    """
    time_limit = inputs.time_limit
    fish = inputs.fish
    explore = inputs.explore
    farm = inputs.farm
    monthly_grants = inputs.monthly_grants
    recipes = inputs.recipes
    open_flows = inputs.open_flows
    demand = inputs.demand
    inventory = inputs.inventory
    cookie_sets = inputs.cookie_sets
    prices = inputs.prices
    ap_tradeable = frozenset(inputs.ap_prices)
    non_mastery_goals = inputs.non_mastery_goals
    pj_gain = inputs.pj_gain
    pj_price = inputs.pj_price
    pj_limit = inputs.pj_limit
    model_calendar = inputs.model_calendar
    start = inputs.start
    spread = resolve_frontier_spread(args, cfg)

    horizon: timedelta | None = horizon_seed
    production = config.passive_production(
        cfg, today=start, monthly_grants=monthly_grants
    )
    # Assigned in the loop below (which always runs at least once); declared here
    # so the data flow past the loop is explicit rather than leaking out of it.
    shown: list[optimizer.SourcingPlan] = []
    offers: list[sources.Offer] = []
    plans: list[optimizer.SourcingPlan] = []
    within: list[optimizer.SourcingPlan] = []
    # With no --plan/--cheapest-plan and no limit, the plan the fixed point below
    # settles on is always shown[0] -- the frontier's fastest plan, one minimize
    # call (see optimizer.fastest_endpoint) rather than the whole frontier
    # (~250 solves). Seed the horizon on that cheap iteration instead, so the
    # loop below need only run the real frontier once, at the already-converged
    # horizon (its own convergence check then passes on the first pass). With a
    # limit or an explicit plan selection, the selected plan can differ from the
    # fastest, so the seed would converge on the wrong horizon -- skip it and
    # fall through to the full frontier loop as before.
    fast_path = (
        model_calendar and args.plan is None and not args.cheapest_plan
        and time_limit is None and args.max_ap is None and args.max_ln is None
        and args.max_ac is None and args.max_gold is None
    )
    if fast_path:
        for _ in range(_MAX_SEASON_ITERS):
            production = config.passive_production(
                cfg, today=start, start=start, horizon=horizon,
                monthly_grants=monthly_grants,
            )
            quantize = config.passive_quantizer(production)
            offers = optimizer.all_offers(
                production, fish, explore, farm, inputs.boost_offers
            )
            fast = optimizer.fastest_endpoint(
                offers, demand, recipes, args.crafting_bonus, prices=prices,
                openables=open_flows, inventory=inventory, cookie_sets=cookie_sets,
                pj_gain=pj_gain, pj_price=pj_price, pj_limit=pj_limit,
                quantize=quantize, ap_tradeable=ap_tradeable,
                non_mastery_goals=non_mastery_goals,
            )
            if not fast.draws and not fast.crafts:  # nothing to source
                break
            if horizon is not None and _makespans_close(fast.time, horizon):
                break
            horizon = fast.time

    for _ in range(_MAX_SEASON_ITERS):
        production = config.passive_production(
            cfg, today=start, start=start, horizon=horizon,
            monthly_grants=monthly_grants,
        )
        quantize = config.passive_quantizer(production)
        offers = optimizer.all_offers(
            production, fish, explore, farm, inputs.boost_offers
        )
        plans = optimizer.frontier(
            offers, demand, recipes, args.crafting_bonus, args.plans, open_flows,
            inventory, cookie_sets, report._pet_dominated_items(production),
            prices=prices, pj_gain=pj_gain, pj_price=pj_price, pj_limit=pj_limit,
            quantize=quantize, ap_tradeable=ap_tradeable,
            non_mastery_goals=non_mastery_goals, spread=spread,
        )
        if not plans:
            return FrontierResult(
                plans=[], within=[], shown=[], offers=offers,
                production=production, horizon=horizon,
            )
        # The limits drop plans over a time or consumable ceiling, unless that
        # would leave none (then the whole frontier is shown, with a warning below).
        within = [
            p for p in plans
            if within_limits(
                p, time_limit, args.max_ap, args.max_ln, args.max_gold, args.max_ac
            )
        ]
        shown = within or plans
        # Now quantized (see optimizer.frontier's quantize kwarg above), so this
        # can in principle flip between two adjacent deposit boundaries as the
        # seasonal rates driving `production` shift with the horizon -- no
        # different in kind from the unquantized value changing, and still
        # bounded by the same _MAX_SEASON_ITERS cap below.
        new_horizon = shown[
            selected_plan(shown, args.plan, args.cheapest_plan)
        ].time
        if not model_calendar or (
            horizon is not None and _makespans_close(new_horizon, horizon)
        ):
            break
        horizon = new_horizon

    return FrontierResult(
        plans=plans, within=within, shown=shown, offers=offers,
        production=production, horizon=horizon,
    )


@dataclass
class PlanContextData:
    """Report-only context derived from a solved frontier."""

    cheapest: int
    display: dict[str, str]
    max_passive_residuals: dict[str, float]
    reasons: list[str | None]
    golds: list[tuple[float, float]] | None
    sourceable_elsewhere: set[str]


def derive_report_inputs(
    inputs: SolveInputs, fr: FrontierResult, args: argparse.Namespace
) -> PlanContextData:
    cheapest = cheapest_plan(fr.shown)
    # Display names for the items a reason or the plan report may mention.
    display = report.build_display(inputs.demand, inputs.workshop)
    # Full requirement yardstick: the passive residuals of the cheap endpoint (the
    # fastest fully self-sourced plan), for judging whether a resource is at its
    # complete gathering point (see print_critical_path, and the frontier reasons).
    # Recomputed via self_sourced_endpoint rather than read off the frontier: it is a
    # stable, moderate-strategy reference, where the slow passive-leaning extension
    # understates some items (cider stamina from Apples cuts the Orange requirement)
    # and the fast plans overstate net materials (Antler) -- and thinning may drop it.
    # Its own `.time` is unused here (only its residuals are), but quantizing it
    # too keeps the invariant that every plan carrying a printable time is quantized.
    # `production` is None only in stripped-down callers (see the webapi tests'
    # hand-built FrontierResults); the report side is equally tolerant of that.
    quantize = (
        config.passive_quantizer(fr.production) if fr.production is not None else None
    )
    reference = optimizer.self_sourced_endpoint(
        fr.offers, inputs.demand, inputs.recipes, args.crafting_bonus,
        openables=inputs.open_flows, inventory=inputs.inventory,
        cookie_sets=inputs.cookie_sets, prices=inputs.prices,
        quantize=quantize,
    )
    max_passive_residuals = optimizer.passive_residuals(
        reference, inputs.demand, inputs.recipes, args.crafting_bonus,
        inputs.open_flows, inputs.inventory,
    )
    reasons = report._frontier_reasons(
        fr.shown, inputs.demand, inputs.recipes, args.crafting_bonus,
        inputs.open_flows, inputs.inventory, max_passive_residuals,
        fr.production, display,
    )
    # Split each plan's gold into what must still be bought vs. what the plan makes
    # or holds passively (Large Nets from straw/wood/stone/antlers, cookie sets from
    # held cookies, etc.). Cookie sets come from held cookies, so credit the plan's
    # allowance as a holding (there is no "cookie set" inventory line).
    golds = None
    if inputs.prices is not None:
        golds = [optimizer.plan_gold_breakdown(p, inputs.prices) for p in fr.shown]
    # Items a consumable channel can supply: growing one of these is optional fill,
    # not a farming bottleneck (see print_critical_path).
    sourceable_elsewhere = {
        key for offer in fr.offers
        if offer.track in (sources.FISHING_TRACK, sources.EXPLORING_TRACK)
        for key in offer.yields
    }
    for flow in inputs.open_flows:
        sourceable_elsewhere |= set(flow.yields)
    return PlanContextData(
        cheapest=cheapest,
        display=display,
        max_passive_residuals=max_passive_residuals,
        reasons=reasons,
        golds=golds,
        sourceable_elsewhere=sourceable_elsewhere,
    )


# --- Ascension mode (--ascend): climb the tower via masteries/chores rather ---
# --- than solving a fixed goal. See ascension.py for the AK/chore/mega maths ---
# --- and optimizer.py's "Ascension extension" for the per-stage LP.          ---


@dataclass
class StageResult:
    """One stage's solved plan, from :func:`solve_ascension_frontier`.

    ``plan.ascension`` (an :class:`ascension.AscensionOutcome`) carries the
    masters/grandmasters/AK breakdown; ``plan`` itself carries the draws/crafts/
    gold like any other :class:`optimizer.SourcingPlan`. The fields below are
    this stage's own copy of everything :func:`report.print_plan`/
    :func:`report.markdown_plan` need to render ``plan``'s full per-channel
    breakdown (milestone 5) -- one snapshot per stage, since each stage's
    config/production/inventory differs (see :func:`solve_ascension_frontier`'s
    "static setup" note and :func:`_run_ascend_pass`'s evolution).
    """

    from_level: int
    to_level: int
    plan: optimizer.SourcingPlan
    farm: crops.Farm
    workshop: crafting.Workshop
    crafting_bonus: float
    # None only when config.passive_production ever legitimately returns one
    # with no producers; report.py treats that the same as "no production".
    production: passive.PassiveProduction | None
    open_flows: list[sources.OpenFlow]
    openable_names: dict[str, str]
    # This stage's STARTING inventory (before its own solve) -- what the base
    # LP was built against, and what report.print_plan's "From inventory"
    # section should read (not the evolved, post-solve inventory the next
    # stage starts from).
    inventory: dict[str, float]
    prices: dict[str, float] | None


@dataclass
class StagedPlan:
    """One whole climb (every stage back to back) at one point on the sweep."""

    stages: list[StageResult]
    time: timedelta  # sum of stage plan times
    gold: float  # sum of stage plan.gold
    surplus_gold: float  # sum of stage surplus credits
    # The AK carry left over after the final stage (see _run_ascend_pass's
    # per-stage carry threading) -- the balancing term in the report's AK
    # ledger: starting_ak + steps_ak + chores_ak == 100*(target-current) +
    # carry_out (see report._ascension_ledger).
    carry_out: float = 0.0

    @property
    def cost(self) -> tuple[float, float]:
        """The ``(time, net gold)`` pair the frontier prunes/sorts on -- mirrors
        :attr:`optimizer.SourcingPlan.cost`."""
        return (self.time.total_seconds(), round(self.gold - self.surplus_gold, 2))


@dataclass
class AscensionFrontierResult:
    plans: list[StagedPlan]  # non-dominated, fastest first
    stage_bounds: list[tuple[int, int]]
    unmasterable: list[str]  # union across stages/passes (from the LP bases)
    # Mastery-paste items missing from the masterable catalogue (display
    # names) -- an empty catalogue file disables the check. See
    # load_mastery_catalogue.
    unexpected_masteries: list[str] = field(default_factory=list)


# The full masterable-item catalogue (one display-cased name per line). The
# mastery paste only lists items the player has *progress* on, so the
# ascension candidate universe comes from here (ascension.candidates'
# catalogue param): a sparse -- or --empty-mastery -- paste must still offer
# every masterable item at progress 0, exactly as the game does. A solver
# data file like tower.csv, generated by scripts/generate_items.py (which
# also writes the web Mastery picker's static copy from the same set --
# regenerate with the script, never edit by hand).
MASTERY_CATALOGUE_PATH = Path(__file__).with_name("mastery_items.txt")


def load_mastery_catalogue(path: Path | None = None) -> dict[str, str]:
    """``{lower-cased key: display name}`` of every masterable item.

    Reads :data:`MASTERY_CATALOGUE_PATH` (or ``path``); a missing file returns
    ``{}``, which callers treat as "no catalogue" -- candidates then come from
    the mastery paste alone (pre-catalogue behaviour) and the unexpected-item
    check is skipped.
    """
    source = path if path is not None else MASTERY_CATALOGUE_PATH
    try:
        lines = source.read_text().splitlines()
    except FileNotFoundError:
        return {}
    return {name.lower(): name for name in (line.strip() for line in lines) if name}


def resolve_ascend_params(args: argparse.Namespace, cfg: dict) -> tuple[int, int, int]:
    """``(current, target, starting_ak)`` for ``--ascend`` mode.

    ``current`` is ``--tower-level``, else ``[tower] level``, else ``0`` --
    ascension mode's own default: elsewhere an absent level means "assume
    every reward owned" (see :func:`config.current_tower_level`), but here it
    means a fresh account with none owned yet, since the climb is *from*
    that level. ``target`` is ``--ascend``'s value, unvalidated (see
    :func:`_validate_ascend`). ``starting_ak`` is ``--starting-ak``, else
    ``[tower] ascension_knowledge``, else 0.
    """
    current = (
        args.tower_level if args.tower_level is not None
        else config.current_tower_level(cfg)
    )
    if current is None:
        current = 0
    starting_ak = (
        args.starting_ak if args.starting_ak is not None
        else config.starting_ascension_knowledge(cfg)
    )
    return current, args.ascend, starting_ak


def _validate_ascend(
    args: argparse.Namespace, cfg: dict, state: farm_solver.GameState
) -> tuple[int, int, int]:
    """Validate ``--ascend``'s mode and return its resolved ``(current, target,
    starting_ak)`` (see :func:`resolve_ascend_params`).

    ``--ascend`` picks a different mode entirely -- climb by AK rather than
    solve a fixed goal -- so it conflicts with every goal-only flag
    (``--target``/``-m``/``-i``/``--use-pumpkin-juice``). The level range is
    checked against :data:`ascension.MAX_ASCEND_LEVEL`.

    Levels 101-200 additionally need megamasteries (milestone 6, see
    :func:`ascension.megas_required_by`). This validates only a SIMPLE bound
    here -- candidates with *any* mega step at all, against the net
    requirement (``megas_required_by(target) - ascension.mega_count(state)``)
    -- since real obtainability needs the LP (channels/crafts/opens), not
    known yet at validate-time. A shortfall this bound misses (every
    mega-step candidate turns out unreachable) surfaces later from the
    aggregate mega-selection solve in :func:`solve_ascension_frontier` (see
    ``_select_megas``), as the same "not enough megamasterable items" message.
    """
    if args.target is not None:
        raise ValueError("--ascend conflicts with --target")
    if args.mastery is not None:
        raise ValueError("--ascend conflicts with -m/--mastery")
    if args.items is not None:
        raise ValueError("--ascend conflicts with -i/--items")
    if args.use_pumpkin_juice is not None:
        raise ValueError("--ascend conflicts with --use-pumpkin-juice")
    if args.sweep is not None and args.sweep < 2:
        raise ValueError("--sweep needs at least 2 passes (the fast and cheap endpoints)")
    current, target, starting_ak = resolve_ascend_params(args, cfg)
    if not (current < target <= ascension.MAX_ASCEND_LEVEL):
        raise ValueError(
            f"--ascend requires the current level ({current}) < target "
            f"({target}) <= {ascension.MAX_ASCEND_LEVEL}"
        )
    megas_needed = ascension.megas_required_by(target) - ascension.mega_count(state)
    if megas_needed > 0:
        mega_candidates = sum(
            1
            for c in ascension.candidates(state, load_mastery_catalogue().values())
            if c.delta_mega is not None
        )
        if mega_candidates < megas_needed:
            raise ValueError(
                f"--ascend: not enough megamasterable items: need {megas_needed} "
                f"more (only {mega_candidates} candidate(s) have a mega step)"
            )
    if starting_ak < 0:
        raise ValueError("--starting-ak must be non-negative")
    return current, target, starting_ak


# The two frontier endpoints (buy freely / buy nothing) plus this many
# log-spaced weights between them -- mirrors optimizer._time_caps' geometric
# spacing, but swept over gold_weight rather than a makespan cap. No
# favour_passive slow-regime extension in v1: the chore-AK tradeoff already
# stretches the slow end of this sweep on its own (a pricier gold weight
# masters fewer candidates and waits longer on chores instead of buying), so a
# separate buy-nothing-and-bank-surplus regime would mostly duplicate that
# shape rather than reveal a genuinely different plan.
_ASCEND_SWEEP_INTERIOR = 14


def _ascend_gold_weights(n_interior: int = _ASCEND_SWEEP_INTERIOR) -> list[float]:
    """The gold weights the ascension sweep solves at, fast endpoint first.

    ``n_interior`` (``--sweep`` minus the two endpoints) trades frontier
    density between the endpoints for wall time -- every weight costs a full
    staged solve, the dominant cost of the whole mode.
    """
    lo = math.log(optimizer._FAST_PRICE)
    hi = math.log(optimizer._FREE_PRICE)
    interior = [
        math.exp(lo + (hi - lo) * (i + 1) / (n_interior + 1))
        for i in range(n_interior)
    ]
    return [optimizer._FAST_PRICE, *interior, optimizer._FREE_PRICE]


def _ascend_stage_cfgs(current: int, target: int, cfg: dict) -> list[dict]:
    """One config per :func:`ascension.stage_bounds` stage.

    Stage 0 is a deep copy of ``cfg`` with ``[tower] level`` set to ``current``
    directly (so a player's own ``[tower] level`` -- which may be stale, or
    absent -- doesn't leak in; ``--tower-level``/the config's own resolution
    already folded into ``current``, see :func:`resolve_ascend_params`). Stage
    ``k >= 1`` applies whatever reward completing stage ``k - 1`` granted
    (``ascension.apply_rewards(stage_cfgs[k - 1], bounds[k - 1][0],
    bounds[k][0])`` -- the two bounds are the same level, a stage never
    straddles a reward, see :func:`ascension.stage_bounds`).
    """
    bounds = ascension.stage_bounds(current, target)
    stage_cfgs: list[dict] = []
    base_cfg = copy.deepcopy(cfg)
    base_cfg.setdefault("tower", {})["level"] = current
    stage_cfgs.append(base_cfg)
    for k in range(1, len(bounds)):
        prev_start, prev_end = bounds[k - 1]
        stage_cfgs.append(ascension.apply_rewards(stage_cfgs[k - 1], prev_start, prev_end))
    return stage_cfgs


def _run_ascend_pass(
    stage_crafting_bonus: list[float],
    stage_setups: list[_ChannelSetup],
    stage_offers: list[list[sources.Offer]],
    stage_quantize: list,
    stage_production: list[passive.PassiveProduction | None],
    bounds: list[tuple[int, int]],
    state: farm_solver.GameState,
    args: argparse.Namespace,
    starting_ak: int,
    gold_weight: float,
    mega_schedule: dict[str, int],
    catalogue: Iterable[str] = (),
    base_cache: dict | None = None,
) -> tuple[StagedPlan, set[str]]:
    """Solve every stage once, in order, at one ``gold_weight``.

    ``catalogue`` is the full masterable-item universe (see
    :func:`load_mastery_catalogue`), handed to :func:`ascension.candidates`
    each stage so progress-0 items the paste never mentions stay candidates.

    ``base_cache`` (shared across every pass of one frontier sweep) reuses a
    stage's LP base whenever its *actual solve inputs* recur: the key is
    everything ``_build_base`` sees that can vary between passes -- the stage
    index (standing in for the pass-invariant offers/recipes/prices setup),
    the :class:`ascension.AscensionSpec` contents, the starting inventory and
    the cookie sets -- so a hit is exact reuse, not an approximation. Stage
    0's inputs are identical on every pass (nothing has evolved yet), and
    later stages recur whenever adjacent gold weights chose the same plan
    prefix -- which is most of them, since a 16-point sweep typically yields
    only a handful of distinct plans. Rebuilding bases per pass used to be
    roughly half the mode's wall time. Reuse across sequential solves is what
    ``_solve_configured``'s ephemeral mechanism exists for; a miss just
    builds fresh.

    Evolves three things stage to stage: the AK carry (steps + chores earned,
    less what the level cost, floored at 0), the masteries
    (:func:`ascension.advance`), and the inventory. Inventory evolution uses
    :func:`optimizer.plan_leftovers` with an *empty* demand: with nothing
    directly demanded, every held item lands in ``plan_balance``'s
    ``elsewhere`` (see ``plan_balance``'s "demand is already net of stock"
    exception -- moot here, nothing is ever directly demanded), so
    ``plan_leftovers``' "supply - used" is exactly each item's ending stock
    (held + produced - consumed by crafts/opens/offer inputs), the next
    stage's starting inventory -- not an approximation, a genuine per-item
    balance.

    ``mega_schedule`` (milestone 6, ``{item: due stage index}``, from
    :func:`solve_ascension_frontier`'s ONE global mega-selection solve --
    fixed across every pass, see that function's docstring) drives each
    stage's ``mega_supply_floors``, recomputed every stage from *this pass's*
    evolved ``masteries`` (:func:`ascension.mega_stage_floors`) so an
    earlier stage's overshoot shrinks a later stage's floor. ``megas_required``
    is a belt-and-braces safety net alongside the floors (see
    ``ascension.AscensionSpec``'s field and optimizer.py's "Ascension
    extension"): recomputed each stage straight from the pass's current
    :func:`ascension.mega_count` rather than threaded like the AK carry,
    since (unlike AK) it needs no carry bookkeeping of its own -- mega_count
    already reflects every item that crossed the threshold so far this pass,
    scheduled or incidental alike.
    """
    masteries = state.masteries
    inventory = dict(stage_setups[0].inventory)
    carry = float(starting_ak)
    unmasterable: set[str] = set()
    stage_results: list[StageResult] = []
    total_time = timedelta(0)
    total_gold = 0.0
    total_surplus = 0.0

    for k, (a, b) in enumerate(bounds):
        setup = stage_setups[k]
        crafting_bonus_k = stage_crafting_bonus[k]
        offers = stage_offers[k]
        level_ak = ascension.AK_PER_LEVEL * (b - a)
        # cookie_sets depends on held cookies, which evolve pass to pass (see
        # the inventory note above), so it is recomputed here rather than
        # taken from the (pass-invariant) stage setup.
        cookie_sets = cookie_sets_available(inventory, args.cookie_sets)
        gs = farm_solver.GameState(masteries=masteries)
        spec = ascension.AscensionSpec(
            candidates=ascension.candidates(gs, catalogue),
            ak_required=max(0.0, level_ak - carry),
            chore_rate_per_day=ascension.chore_rate_per_day(),
            megas_required=max(0, ascension.megas_required_by(b) - ascension.mega_count(gs)),
            mastery_bonus=args.mastery_bonus,
            mega_supply_floors=ascension.mega_stage_floors(masteries, mega_schedule, k),
        )
        cache_key = (
            k,
            tuple(spec.candidates),
            spec.ak_required,
            spec.megas_required,
            tuple(sorted(spec.mega_supply_floors.items())),
            tuple(sorted(inventory.items())),
            cookie_sets,
        )
        base = base_cache.get(cache_key) if base_cache is not None else None
        if base is None:
            base = optimizer._build_base(
                offers, {}, recipes=setup.recipes, crafting_bonus=crafting_bonus_k,
                openables=setup.open_flows, inventory=inventory, cookie_sets=cookie_sets,
                prices=setup.prices, ascension=spec,
            )
            if base_cache is not None:
                base_cache[cache_key] = base
        plan = optimizer._solve_configured(base, gold_weight=gold_weight)
        optimizer._quantize_plan_time(
            plan, stage_quantize[k], {}, setup.recipes, crafting_bonus_k,
            setup.open_flows, inventory,
        )
        plan.surplus_gold = optimizer.plan_surplus_gold(
            plan, {}, setup.recipes, crafting_bonus_k, setup.open_flows, inventory,
            setup.prices or {},
        )
        unmasterable.update(base.unmasterable)

        outcome = plan.ascension
        assert outcome is not None  # an AscensionSpec was always given above
        # This stage's starting inventory -- what the base LP above was built
        # against, and what the report's per-stage "From inventory" section
        # should read (captured before it's overwritten below by the evolved,
        # post-solve inventory the *next* stage starts from).
        stage_inventory = inventory
        next_inventory = optimizer.plan_leftovers(
            plan, {}, setup.recipes, crafting_bonus_k, setup.open_flows, inventory,
        )
        carry = max(0.0, carry + outcome.ak_from_steps + outcome.ak_from_chores - level_ak)
        masteries = ascension.advance(masteries, outcome)
        inventory = next_inventory

        stage_results.append(StageResult(
            from_level=a, to_level=b, plan=plan, farm=setup.farm, workshop=setup.workshop,
            crafting_bonus=crafting_bonus_k, production=stage_production[k],
            open_flows=setup.open_flows,
            openable_names=setup.smith.names if setup.smith is not None else {},
            inventory=stage_inventory, prices=setup.prices,
        ))
        total_time += plan.time
        total_gold += plan.gold
        total_surplus += plan.surplus_gold

    staged = StagedPlan(
        stages=stage_results, time=total_time, gold=total_gold, surplus_gold=total_surplus,
        carry_out=carry,
    )
    return staged, unmasterable


def _select_megas(
    state: farm_solver.GameState,
    current: int,
    target: int,
    starting_ak: int,
    args: argparse.Namespace,
    last_crafting_bonus: float,
    last_setup: _ChannelSetup,
    last_offers: list[sources.Offer],
    catalogue: Iterable[str] = (),
) -> tuple[list[str], set[str]]:
    """Choose WHICH items go megamastery for the WHOLE climb, in one aggregate
    solve (milestone 6) -- see :func:`solve_ascension_frontier`'s "global mega
    selection" note for why this runs once, not per stage/pass.

    Builds an :class:`ascension.AscensionSpec` covering the *entire* remaining
    climb at once -- the full remaining AK (``ascension.ak_cost(current,
    target) - starting_ak``, floored at 0) and the full net mega requirement
    (``ascension.megas_required_by(target) - ascension.mega_count(state)``) --
    against the pre-climb ``state``'s candidates, and solves it once at the
    fast endpoint (``optimizer._FAST_PRICE``, buy freely: which items are
    *cheapest to mass-produce* is what should drive this pick, not a gold
    budget). The chosen mega set is ``plan.ascension.megas``.

    **Approximations** (documented, not revisited per stage):

    - **Solved against the LAST stage's channels/offers**, standing in for
      the whole climb: a megamastery is ~900k+ mastery of production, which
      realistically spans most of the climb, so by the time most of it is
      produced most rewards are already owned -- the final stage's channels
      are the closest single stand-in for "the climb as a whole" available
      without solving every stage's combination up front (which is exactly
      the per-stage sweep this selection is meant to avoid repeating).
    - **One gold weight, not swept**: a ~900k-mastery-point supply decision
      is dominated by which items are cheapest to mass-produce, not by the
      buy/self-source tradeoff that moves with ``gold_weight`` -- it barely
      shifts across the sweep. Solving it once keeps the chosen mega set
      IDENTICAL at every point on the frontier, so "the frontier" stays one
      climb priced differently, not a different climb at each point.

    Returns ``(chosen, unmasterable)`` -- ``chosen`` trimmed/validated to
    exactly the net requirement (see the trimming note below) and
    ``unmasterable`` the candidates this solve's own base dropped as
    unreachable (``optimizer._Base.unmasterable``), folded into the
    frontier's result the same way each stage's own solve already is.
    """
    megas_required = max(0, ascension.megas_required_by(target) - ascension.mega_count(state))
    if megas_required == 0:
        return [], set()
    ak_required = max(0.0, ascension.ak_cost(current, target) - starting_ak)
    cookie_sets = cookie_sets_available(last_setup.inventory, args.cookie_sets)
    spec = ascension.AscensionSpec(
        candidates=ascension.candidates(state, catalogue),
        ak_required=ak_required,
        chore_rate_per_day=ascension.chore_rate_per_day(),
        megas_required=megas_required,
        mastery_bonus=args.mastery_bonus,
    )
    base = optimizer._build_base(
        last_offers, {}, recipes=last_setup.recipes, crafting_bonus=last_crafting_bonus,
        openables=last_setup.open_flows, inventory=last_setup.inventory,
        cookie_sets=cookie_sets, prices=last_setup.prices, ascension=spec,
    )
    try:
        plan = optimizer._solve_configured(base, gold_weight=optimizer._FAST_PRICE)
    except ValueError as exc:
        raise ValueError(
            f"--ascend: not enough megamasterable items: need {megas_required} more"
        ) from exc
    assert plan.ascension is not None
    chosen = sorted(plan.ascension.megas)
    # The LP's mega-count constraint is `>=`, so a free byproduct could in
    # principle over-select; schedule_megas needs an exact match (see its
    # docstring), so trim to the cheapest `megas_required` by remaining
    # supply. Under-selection would mean the constraint itself failed to
    # bind -- that can't happen (an infeasible solve above already raises).
    if len(chosen) > megas_required:
        chosen = sorted(
            chosen, key=lambda item: (ascension.mega_remaining(state.masteries, item), item)
        )[:megas_required]
    return chosen, set(base.unmasterable)


# --- Parallel gold-weight sweep (ascension mode) ---------------------------
#
# solve_ascension_frontier's gold-weight passes are independent: each
# _run_ascend_pass takes the pass-invariant per-stage setup plus one gold
# weight and returns its own StagedPlan. On a heavy fresh-account climb the
# sweep is the wall-time knob (e.g. 0->200 --empty-mastery: 16 passes x 10
# stages of ~10k trial re-solves), so the passes fan out across processes.
# HiGHS/PuLP hold non-thread-safe solver state, so this MUST be process-based,
# never threads.
#
# The invariant setup (offers/setups/production/...) is shipped to each worker
# ONCE via the pool initializer and stashed in a module global, so a 16-task
# sweep pickles it ~once per worker rather than once per task. Each worker keeps
# its own base_cache (the cross-pass LP-construction reuse _build_base wants)
# for whatever chunk of weights it handles; cross-worker reuse is lost, but that
# only ever saved base *construction*, never the dominant solving -- and a
# base_cache hit is exact reuse (via _solve_configured's ephemeral mechanism),
# so a pass's result is identical whether its base was freshly built, reused
# within a worker, or reused across the sequential sweep. That identity is what
# makes the parallel and sequential frontiers byte-for-byte the same.
_ASCEND_WORKER_PAYLOAD: dict | None = None
_ASCEND_WORKER_BASE_CACHE: dict = {}


def _ascend_worker_init(payload: dict) -> None:
    """Pool initializer: stash the pass-invariant setup + a fresh base_cache."""
    global _ASCEND_WORKER_PAYLOAD, _ASCEND_WORKER_BASE_CACHE
    _ASCEND_WORKER_PAYLOAD = payload
    _ASCEND_WORKER_BASE_CACHE = {}


def _strip_ascend_origins(staged: StagedPlan) -> None:
    """Drop each stage plan's ``_origin`` before it crosses a process boundary.

    ``optimizer._solve_configured`` stamps every plan with ``_origin = (base,
    kwargs)`` so ``optimizer.frontier`` can re-solve it exact -- but ascension
    mode never uses that path (Pumpkin Juice is rejected in --ascend, so there
    is no integer finish), and the ``_Base`` it points at holds a live HiGHS
    solver model that cannot pickle (``cannot pickle 'Highs' object``).
    ``_origin`` is ``compare=False, repr=False`` and read nowhere in ascension,
    so clearing it is invisible to the report and to the frontier pruning.
    """
    for stage in staged.stages:
        if getattr(stage.plan, "_origin", None) is not None:
            stage.plan._origin = None


def _ascend_worker_run(gold_weight: float) -> tuple[StagedPlan, set[str]]:
    """Solve one gold-weight pass in a worker, using the stashed setup."""
    payload = _ASCEND_WORKER_PAYLOAD
    assert payload is not None, "worker used before _ascend_worker_init"
    staged, unmasterable = _run_ascend_pass(
        payload["stage_crafting_bonus"], payload["stage_setups"],
        payload["stage_offers"], payload["stage_quantize"],
        payload["stage_production"], payload["bounds"], payload["state"],
        payload["args"], payload["starting_ak"], gold_weight,
        payload["mega_schedule"], payload["catalogue"],
        base_cache=_ASCEND_WORKER_BASE_CACHE,
    )
    _strip_ascend_origins(staged)
    return staged, unmasterable


def _ascend_worker_count(n_passes: int, args: argparse.Namespace) -> int:
    """How many worker processes to fan the gold-weight sweep across.

    ``--jobs`` wins if given (``--jobs 1`` forces the in-process sequential
    path); else the ``FARM_SOLVER_ASCEND_JOBS`` env var (the webapp sets this so
    its own worker pool controls nesting); else one per CPU. Always at least 1
    and never more than ``n_passes`` (a pass per worker at most).
    """
    requested = getattr(args, "jobs", None)
    if requested is None:
        env = os.environ.get("FARM_SOLVER_ASCEND_JOBS")
        requested = int(env) if env else (os.cpu_count() or 1)
    return max(1, min(requested, n_passes))


def solve_ascension_frontier(
    args: argparse.Namespace, cfg: dict, state: farm_solver.GameState
) -> AscensionFrontierResult:
    """Climb the tower from a current level to ``--ascend``'s target.

    Splits the climb into stages at every reward threshold crossed
    (:func:`ascension.stage_bounds`; see :func:`_ascend_stage_cfgs`), then
    sweeps a range of gold weights (buy freely through buy nothing, mirroring
    :func:`optimizer.frontier`'s fast regime -- see :func:`_ascend_gold_weights`)
    driving one LP per stage per weight through the milestone-3 ascension
    extension (:data:`ascension.AscensionSpec`/``optimizer._build_base``'s
    ``ascension`` param), which chooses which candidates to master/
    grandmaster to clear each stage's AK gap.

    **v1 approximations**, all documented further at their own site:

    - **Steady-state calendar** (see the ``config.passive_production`` call
      below): every stage's passive production -- seasonal pets, the
      Mechanical Heart's monthly grants -- is the yearly/monthly average,
      never the calendar-exact model. A stage's real span is only known after
      solving it, and there is no natural fixed point to iterate here the way
      :func:`solve_frontier` does for a single goal (each of many stages would
      need its own fixed point, nested inside the gold-weight sweep) -- a
      later milestone's job if it matters in practice.
    - **No favour_passive slow regime** -- see :data:`_ASCEND_SWEEP_INTERIOR`'s
      comment.
    - **Perk auto-detection uses the pre-climb state** -- see
      :func:`_build_channel_setup`'s callers here: Scythe of Dewstar/Dewstar's
      Journal/Fungus Processing/the Mechanical Heart are detected once per
      stage from ``state`` as passed in, not re-evaluated against each pass's
      evolved masteries. They gate on tower levels (250/300) far past the
      101-200 range this milestone even allows, so this cannot yet matter in
      practice.
    - **Inventory evolution** -- see :func:`_run_ascend_pass`'s docstring.
    - **Global mega selection** (milestone 6, levels 101-200) -- see
      :func:`_select_megas`'s docstring: which items go mega is chosen ONCE,
      in one aggregate solve over the whole climb, before the sweep below --
      not re-decided per stage or per gold-weight pass.
    """
    current, target, starting_ak = resolve_ascend_params(args, cfg)
    bounds = ascension.stage_bounds(current, target)
    stage_cfgs = _ascend_stage_cfgs(current, target, cfg)
    start = args.start_date or date.today()

    # The candidate universe: the full masterable catalogue (progress-0 items
    # included), not just what the paste lists (see load_mastery_catalogue).
    # Paste items missing from the catalogue are reported back as unexpected
    # (a stale catalogue, or a paste from newer game content) rather than
    # silently treated as masterable no one else can see.
    catalogue = load_mastery_catalogue()
    unexpected = sorted(
        mastery.item
        for key, mastery in state.masteries.items()
        if catalogue and key not in catalogue
    )

    # Per-stage static setup (channels, recipes, prices, offers, the quantize
    # callable): built once per stage and shared across every gold-weight pass
    # below, since none of it depends on the sweep or on how a pass's earlier
    # stages evolved (see this function's docstring's "static setup" note and
    # _run_ascend_pass's inventory/masteries evolution, which is the part that
    # does vary pass to pass).
    stage_crafting_bonus: list[float] = []
    stage_setups: list[_ChannelSetup] = []
    stage_offers: list[list[sources.Offer]] = []
    stage_quantize: list = []
    # One passive.PassiveProduction per stage, kept alongside stage_offers
    # (which is built from it) so the report can name passive producers in a
    # stage's own "Passive production" section (report.print_plan/markdown_plan
    # take a `production` argument for exactly that).
    stage_production: list[passive.PassiveProduction | None] = []
    for cfg_k in stage_cfgs:
        crafting_bonus_k = _crafting_bonus_for_cfg(args.crafting_bonus, cfg_k)
        setup = _build_channel_setup(args, cfg_k, state, start, crafting_bonus_k)
        # Steady state: no start/horizon span (see the docstring's calendar
        # note) -- mirrors solve_frontier's non-seasonal (average/ignore) path.
        production = config.passive_production(
            cfg_k, today=start, start=start, horizon=None,
            monthly_grants=setup.monthly_grants,
        )
        offers = optimizer.all_offers(
            production, setup.fish, setup.explore, setup.farm, setup.boost_offers
        )
        stage_crafting_bonus.append(crafting_bonus_k)
        stage_setups.append(setup)
        stage_offers.append(offers)
        stage_quantize.append(config.passive_quantizer(production))
        stage_production.append(production)

    # Global mega selection + schedule (milestone 6): computed ONCE here, before
    # the gold-weight sweep, and passed unchanged into every _run_ascend_pass --
    # see _select_megas' docstring for why one solve, against the last stage's
    # channels, stands in for the whole climb. schedule_megas then assigns each
    # chosen item a due stage (cheapest-remaining-supply first), which
    # _run_ascend_pass turns into per-stage mega_supply_floors.
    chosen_megas, mega_unmasterable = _select_megas(
        state, current, target, starting_ak, args,
        stage_crafting_bonus[-1], stage_setups[-1], stage_offers[-1],
        catalogue.values(),
    )
    mega_schedule: dict[str, int] = {}
    if chosen_megas:
        per_item_remaining = {
            item: ascension.mega_remaining(state.masteries, item) for item in chosen_megas
        }
        mega_schedule = ascension.schedule_megas(
            chosen_megas, bounds, ascension.mega_count(state), per_item_remaining
        )

    unmasterable: set[str] = set(mega_unmasterable)
    staged_plans: list[StagedPlan] = []
    n_interior = (
        args.sweep - 2 if args.sweep is not None else _ASCEND_SWEEP_INTERIOR
    )
    gold_weights = _ascend_gold_weights(n_interior)
    n_workers = _ascend_worker_count(len(gold_weights), args)
    if n_workers <= 1:
        # In-process sequential path: one shared base_cache across every pass
        # (the reuse _run_ascend_pass's docstring describes).
        base_cache: dict = {}
        for gold_weight in gold_weights:
            staged, pass_unmasterable = _run_ascend_pass(
                stage_crafting_bonus, stage_setups, stage_offers,
                stage_quantize, stage_production, bounds, state, args,
                starting_ak, gold_weight, mega_schedule, catalogue.values(),
                base_cache=base_cache,
            )
            unmasterable.update(pass_unmasterable)
            staged_plans.append(staged)
    else:
        # Fan the independent passes across processes. The invariant setup goes
        # to each worker once (initializer); each task ships only a gold weight.
        # pool.map preserves input order, so staged_plans stays in gold-weight
        # order exactly as the sequential path produces it -- and each pass's
        # result is base_cache-independent (exact ephemeral reuse), so the two
        # paths' frontiers are byte-for-byte identical.
        payload = {
            "stage_crafting_bonus": stage_crafting_bonus,
            "stage_setups": stage_setups,
            "stage_offers": stage_offers,
            "stage_quantize": stage_quantize,
            "stage_production": stage_production,
            "bounds": bounds,
            "state": state,
            "args": args,
            "starting_ak": starting_ak,
            "mega_schedule": mega_schedule,
            "catalogue": list(catalogue.values()),
        }
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_ascend_worker_init,
            initargs=(payload,),
        ) as pool:
            for staged, pass_unmasterable in pool.map(
                _ascend_worker_run, gold_weights
            ):
                unmasterable.update(pass_unmasterable)
                staged_plans.append(staged)

    # Bound the slow end at the chore-only time: every AK earned from chores
    # alone, with not one item mastered, takes ak_cost/rate days -- no plan
    # needs to run longer. Without this the slowest passes survive pruning on
    # their banked-surplus credit alone (net gold falls without bound as the
    # span grows -- passive production accumulates forever), exactly the
    # unbounded tail optimizer.frontier bounds with t_bound in the normal
    # mode; 52-year "plans" were observed before this cut. Generous on
    # purpose: starting AK is ignored (it only shortens the true need) and
    # the fastest plan always survives.
    # The margin keeps the chore-only endpoint itself alive: its makespan is
    # exactly ak/rate days, which deposit quantisation or float rounding can
    # push a hair past the raw bound.
    chore_only = timedelta(
        days=ascension.ak_cost(current, target) / ascension.chore_rate_per_day()
    ) * 1.05 + timedelta(days=1)
    fastest = min(plan.time for plan in staged_plans)
    staged_plans = [
        plan for plan in staged_plans if plan.time <= max(chore_only, fastest)
    ]

    # optimizer._non_dominated/_thin both work purely off .time/.gold/
    # .surplus_gold/.cost, which StagedPlan carries under the same names as
    # SourcingPlan -- reused as-is rather than duplicating the pruning logic.
    shown = optimizer._thin(optimizer._non_dominated(staged_plans), args.plans)

    # The LP works on lower-cased keys; report the original display names --
    # the paste's where the item has an entry, else the catalogue's (a
    # progress-0 candidate only the catalogue knows).
    display_names = sorted(
        m.item if (m := state.mastery(item)) is not None
        else catalogue.get(item, item)
        for item in unmasterable
    )
    return AscensionFrontierResult(
        plans=shown, stage_bounds=bounds, unmasterable=display_names,
        unexpected_masteries=unexpected,
    )


def run(args: argparse.Namespace) -> int:
    if args.pager and args.format != "markdown":
        raise ValueError("--pager requires --format markdown")
    # All output goes through this: text prints immediately (today's behaviour),
    # markdown buffers into a MarkdownDoc and renders once in reporter.finish().
    reporter = report.build_reporter(args.format, pager=args.pager)
    cfg = load_config(args.config)
    apply_overrides(cfg, args.override)
    # In the calendar 'ignore' mode seasonal pets contribute nothing: drop them
    # from the config before anything reads [pets] (build_inputs separately
    # gates the tower-derived monthly grants off in this mode).
    if resolve_calendar_mode(args, cfg) == "ignore":
        cfg = strip_dated_sources(cfg)

    if args.ascend is not None:
        # Ascension mode: a different mode entirely (climb by AK, not solve a
        # fixed goal), validated and dispatched before any of the goal-solving
        # setup below runs. Its own crafting bonus is resolved per stage (see
        # solve_ascension_frontier), not globally like resolve_crafting_bonus
        # does for the rest of run() -- args.crafting_bonus is left untouched.
        backend = args.solver or cfg.get("solver", {}).get("backend")
        if backend:
            optimizer.set_solver_backend(backend)
        mastery_file, inventory_file = resolve_files(args, cfg)
        state = load_state(mastery_file, inventory_file)
        if args.override_inventory is not None:
            apply_inventory_overrides(
                state.inventory, parsing.parse_item_amounts(args.override_inventory)
            )
        # Validated against `state` (megas_required_by/mega_count/candidates
        # need it, see _validate_ascend's docstring) -- so this must come
        # after the state load above, unlike every other mode's validation.
        current, target, starting_ak = _validate_ascend(args, cfg, state)
        result = solve_ascension_frontier(args, cfg, state)
        reporter.ascension_goal(
            current, target, starting_ak, result.unmasterable,
            result.unexpected_masteries,
        )
        if not result.plans:
            reporter.note("No plan found.")
            reporter.finish()
            return 0
        selected = selected_plan(result.plans, args.plan, args.cheapest_plan)
        cheapest = cheapest_plan(result.plans)
        reporter.ascension_frontier(result, selected, cheapest)
        reporter.ascension_plan(
            result, selected, current, target, starting_ak, args.start_date or date.today()
        )
        reporter.finish()
        return 0

    # An explicit --crafting-bonus wins; otherwise resolve it from [workshop]/
    # [tower] before anything downstream (trading_prices, build_inputs, the
    # reporters) reads args.crafting_bonus.
    resolve_crafting_bonus(args, cfg)
    # Select the LP backend before anything solves; the flag beats the config.
    backend = args.solver or cfg.get("solver", {}).get("backend")
    if backend:
        optimizer.set_solver_backend(backend)
    workshop = crafting.load_workshop()
    fish = fishing.fishing_from_config(cfg.get("fishing", {}))
    start = args.start_date or date.today()

    # A standalone report over the whole workshop; no solving needed. Use the
    # steady-state producers (no plan span) so every pet's items are counted.
    # No inventory file is read for this report, so the Fungus Processing and
    # Scythe of Dewstar / Dewstar's Journal checks fall back to config overrides
    # only.
    if args.unobtainable:
        farm = crops.farm_from_config(
            cfg.get("farming", {}),
            have_scythe_of_dewstar=resolve_scythe_of_dewstar(cfg),
            have_dewstars_journal=resolve_dewstars_journal(cfg),
        )
        explore = exploring.exploring_from_config(
            cfg.get("exploring", {}),
            have_fungus_processing=resolve_fungus_processing(cfg),
        )
        production = config.passive_production(
            cfg, today=start, monthly_grants=resolve_monthly_grants(cfg)
        )
        obtainable = obtainable_items(production, farm, fish, explore)
        reporter.unobtainable(unobtainable_materials(workshop, obtainable))
        reporter.finish()
        return 0

    # The mastery and inventory files come from the command line, falling back
    # to the [solver] config section.
    mastery_file, inventory_file = resolve_files(args, cfg)

    state = load_state(mastery_file, inventory_file)
    if args.override_inventory is not None:
        apply_inventory_overrides(
            state.inventory, parsing.parse_item_amounts(args.override_inventory)
        )

    inputs = build_inputs(args, cfg, state)

    fr = solve_frontier(inputs, args, cfg)
    # plan.remainder is identical across every plan the frontier produced (it
    # comes from demand vs. obtainability, not any plan's own choices), so any
    # one of them names the goal items nothing can source -- report it here,
    # up in the Goal section, rather than waiting for the selected plan below.
    reporter.solution(inputs.solution, fr.plans[0].remainder if fr.plans else None)
    if not fr.plans:
        reporter.finish()
        return 0  # nothing left to source

    selected = selected_plan(fr.shown, args.plan, args.cheapest_plan)
    ctx = derive_report_inputs(inputs, fr, args)

    if inputs.model_calendar:
        dated = []
        if any(name.lower() in pets.SEASONAL_MONTHS for name in cfg.get("pets", {})):
            dated.append("seasonal pets")
        if inputs.monthly_grants:
            dated.append("the Mechanical Heart's grants")
        reporter.note(
            f"Starting {inputs.start:%d %b %Y}; {' and '.join(dated)} count only for "
            "the part of the plan they fall in."
        )
    if not fr.within:
        reporter.note(
            limit_shortfall(
                fr.plans, inputs.time_limit, args.max_ap, args.max_ln,
                args.max_gold, args.max_ac,
            )
        )
    # Recomputed (rather than threaded through FrontierResult) since both resolvers
    # are pure functions of args+cfg, same as price_stamina_enabled's usage elsewhere.
    resolved_spread = resolve_frontier_spread(args, cfg)
    reporter.frontier(
        fr.shown, selected, ctx.cheapest, ctx.reasons, inputs.prices, ctx.golds,
        spread_axis=resolved_spread.axis if resolved_spread else None,
        graph_metric=resolve_frontier_graph(args, cfg),
    )
    chosen = fr.shown[selected]
    inputs.solution.arnold_palmers = round(chosen.arnold_palmers)
    inputs.solution.large_nets = round(chosen.large_nets)
    # When to drink each Pumpkin Juice: the mastery levels (near the goal, in the
    # item's final tier) that land its intended boost -- shown in the plan's report.
    pj_schedule = pumpkin_juice_schedule(
        chosen, inputs.solution.needs, inputs.pj_gain
    )
    reporter.plan(
        chosen, inputs.demand, inputs.farm, inputs.workshop, args.crafting_bonus,
        fr.production, inputs.open_flows, inputs.smith.names, inputs.inventory,
        ctx.sourceable_elsewhere, ctx.max_passive_residuals, inputs.prices,
        pj_schedule, ap_prices=inputs.ap_prices,
    )

    if args.breakdown:
        reporter.crafts(chosen, inputs.workshop, args.crafting_bonus, args.mastery_bonus)
    reporter.finish()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except FileNotFoundError as exc:
        parser.error(f"file not found: {exc.filename}")
    except (ValueError, NotImplementedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
