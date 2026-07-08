"""Command-line entry point for Farm Solver.

See README.md for usage. In short::

    python main.py [-t <tower-requirements>] [-l level] \
        [-i item:amount] [-m item:amount] \
        [--mastery-file <masteries>] [--inventory-file <inventory>]

The mastery and inventory files default to the ``[solver]`` config section.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path

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
    """Parse ``--target``: ``tower``, or a level number (``150`` or ``T150``).

    ``tower`` (matched case-insensitively) climbs to the highest reachable
    level, same as omitting ``--target``. Anything else is a level number, an
    optional leading ``T``/``t`` stripped first (so ``T150`` and ``150`` are
    the same), which must parse as a positive integer.
    """
    text = value.strip()
    if text.lower() == "tower":
        return "tower"
    level_text = text[1:] if text[:1].lower() == "t" else text
    try:
        level = int(level_text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--target must be 'tower', a level number, or 'T<level>', not {value!r}"
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


# Nominal player-to-player gold cost of one Pumpkin Juice, used when the config
# does not set mastery.pumpkin_juice_price.
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
    an integer cap, or ``false``/absent (off). The command line wins. The price comes
    from ``mastery.pumpkin_juice_price`` (default :data:`DEFAULT_PUMPKIN_JUICE_PRICE`).
    """
    mastery_cfg = cfg.get("mastery", {})
    price = float(mastery_cfg.get("pumpkin_juice_price", DEFAULT_PUMPKIN_JUICE_PRICE))
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
        metavar="tower|N|TN",
        help=(
            "the goal to solve for: 'tower' (the default) climbs to the highest "
            "reachable level (or -l's level); a level number ('150' or 'T150') "
            "climbs to just that level, overriding -l. With -m/-i also given, "
            "those become extra goals added to the tower's: a lower -m mastery "
            "target than the tower already demands for that item is absorbed "
            "(mastery is cumulative), and -i keeps at least that many of the "
            "item in inventory at the end, on top of whatever mastery or "
            "crafting already demands of it. Without --target, -m/-i solve for "
            "that goal alone, ignoring the tower (the previous behaviour)"
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
    parser.add_argument(
        "--average-seasons",
        action="store_true",
        help=(
            "scale seasonal pets to their yearly average instead of modelling "
            "the calendar (the default; overrides pets.model_calendar in the "
            "config, forcing the single-solve fast path)"
        ),
    )
    parser.add_argument(
        "--crafting-bonus",
        type=float,
        default=crafting.DEFAULT_CRAFTING_BONUS,
        metavar="NUM",
        help=(
            "average items produced per craft, reducing the materials needed "
            f"(default: {crafting.DEFAULT_CRAFTING_BONUS})"
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
            "mastery.pumpkin_juice_price gold each"
        ),
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
    parser.add_argument(
        "--plans",
        type=int,
        default=5,
        metavar="N",
        help="how many plans to spread across the frontier (default: 5)",
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
    # farm_solver.solve_goals/solve_target); without --target they replace the
    # tower goal, with --target they are added on top of it instead.
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
            "--target it is added on top (at least this many held at the end, "
            "on top of whatever mastery or crafting already needs of it). "
            "AMOUNT accepts M/GM/MM (10K/100K/1M) and k/m suffixes, "
            "case-insensitively"
        ),
    )

    return parser


# Seasonal pet availability depends on the plan's makespan, which depends back on
# it; iterate the solve at most this many times to settle that loop.
_MAX_SEASON_ITERS = 5


def _makespans_close(a: timedelta, b: timedelta) -> bool:
    """Whether two makespans agree closely enough to stop the seasonal iteration."""
    return abs((a - b).total_seconds()) <= max(3600.0, 0.001 * b.total_seconds())


def _should_model_calendar(pets_config: dict, average_seasons: bool) -> bool:
    """Whether to model the seasonal calendar rather than a yearly average.

    The calendar model costs an extra solve (an iterative fixed point on the
    makespan), so it is off by default and opted into with ``pets.model_calendar``
    in the config. ``--average-seasons`` forces it off, and there is nothing to
    model unless the config actually has a seasonal pet.
    """
    if average_seasons or not pets_config.get("model_calendar"):
        return False
    return any(name.lower() in pets.SEASONAL_MONTHS for name in pets_config)


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


def resolve_files(
    args: argparse.Namespace, cfg: dict
) -> tuple[Path | None, Path | None]:
    """The mastery and inventory files, from the CLI or the [solver] config.

    A command-line ``--mastery-file`` / ``--inventory-file`` overrides the
    config; otherwise the value comes from the ``[solver]`` section. A file
    missing from both raises a :class:`ValueError`. ``--empty-mastery`` /
    ``--empty-inventory`` skip resolution entirely (``None``), so neither the
    flag nor the config need be set.
    """
    solver = cfg.get("solver", {})

    def pick(cli: Path | None, key: str, flag: str, empty: bool) -> Path | None:
        if empty:
            return None
        if cli is not None:
            return cli
        value = solver.get(key)
        if value is not None:
            return Path(value)
        raise ValueError(
            f"no {key.replace('_', ' ')}: pass {flag} or set solver.{key} "
            "in the config"
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


def within_limits(
    plan: optimizer.SourcingPlan,
    time_limit: timedelta | None = None,
    max_ap: int | None = None,
    max_ln: int | None = None,
    max_gold: int | None = None,
) -> bool:
    """Whether a plan respects every given time, consumable and gold ceiling."""
    return (
        (time_limit is None or plan.time <= time_limit)
        and (max_ap is None or round(plan.arnold_palmers) <= max_ap)
        and (max_ln is None or round(plan.large_nets) <= max_ln)
        and (max_gold is None or round(plan.gold) <= max_gold)
    )


def limit_shortfall(
    plans: list[optimizer.SourcingPlan],
    time_limit: timedelta | None,
    max_ap: int | None,
    max_ln: int | None,
    max_gold: int | None = None,
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
    if max_gold is not None:
        least = round(min(p.gold for p in plans))
        if least > max_gold:
            parts.append(f"within {max_gold:,} gold (least is {least:,})")
    fits = " or ".join(parts) if parts else "every limit at once"
    return f"No plan fits {fits}; showing the full frontier."


def cheapest_plan(plans: list[optimizer.SourcingPlan]) -> int:
    """Index of the cheapest plan: least net gold, ties broken by least time.

    A plan's net gold is ``plan.gold - plan.surplus_gold`` (see
    :attr:`optimizer.SourcingPlan.cost`) -- the gold it spends buying consumables
    less the sale value of the surplus it banks. The cheapest plan is the one lowest
    on that axis: it buys least and banks most, the fully self-sourced, passive-
    leaning end of the frontier, which credited for its banked surplus can sit below
    zero gold.
    """
    return min(
        range(len(plans)),
        key=lambda i: (
            plans[i].gold - plans[i].surplus_gold,
            plans[i].time.total_seconds(),
        ),
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


def load_config() -> dict:
    """Read the config file, or an empty config if there is none."""
    try:
        return config.load_config()
    except FileNotFoundError:
        return {}


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    apply_overrides(cfg, args.override)
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
        production = config.passive_production(cfg, today=start)
        obtainable = obtainable_items(production, farm, fish, explore)
        report.print_unobtainable(unobtainable_materials(workshop, obtainable))
        return 0

    # The mastery and inventory files come from the command line, falling back
    # to the [solver] config section.
    mastery_file, inventory_file = resolve_files(args, cfg)

    # Parse the time limit first so a bad duration fails before any output.
    time_limit = (
        passive.parse_duration(args.passive_limit)
        if args.passive_limit is not None
        else None
    )

    state = load_state(mastery_file, inventory_file)
    # Loaded unconditionally (not just for the tower goal below): reaching
    # T250 auto-detects the Scythe of Dewstar (see resolve_scythe_of_dewstar),
    # regardless of which goal this run solves.
    requirements = farm_solver.load_tower_requirements(args.tower)
    farm = crops.farm_from_config(
        cfg.get("farming", {}),
        have_scythe_of_dewstar=resolve_scythe_of_dewstar(cfg, state, requirements),
        have_dewstars_journal=resolve_dewstars_journal(cfg, state.inventory),
    )
    explore = exploring.exploring_from_config(
        cfg.get("exploring", {}),
        have_fungus_processing=resolve_fungus_processing(cfg, state.inventory),
    )

    # Without --target, -m/-i solve for that goal alone, ignoring the tower (the
    # previous behaviour). With --target given, they instead layer on top of the
    # tower goal (see farm_solver.solve_target and the --target help text). -m
    # and -i can be combined either way.
    mastery_goals = (
        parsing.parse_item_amounts(args.mastery) if args.mastery is not None else None
    )
    item_goals = (
        parsing.parse_item_amounts(args.items) if args.items is not None else None
    )
    if args.target is None and (mastery_goals is not None or item_goals is not None):
        solution = farm_solver.solve_goals(state, mastery_goals, item_goals)
    else:
        target_level = args.target if isinstance(args.target, int) else args.level
        solution = farm_solver.solve_target(
            state, requirements, target_level, mastery_goals, item_goals
        )

    report.print_solution(solution)

    # Every recipe is a craft flow; the optimiser sources the top-level demand,
    # choosing craft-vs-gather for itself. Openables are free multi-output flows
    # (open a chest or bag for its contents), so the solver can gather a chest and
    # open it when that beats gathering the contents directly. (The channel Offers
    # are rebuilt below, since the passive ones depend on the plan span.)
    recipes = crafting.recipe_flows(workshop)
    smith = locksmith.load_openables()
    open_flows = sources.open_flows(smith)
    demand = crafting.top_level_demand(solution.needs, args.mastery_bonus)
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
    prices = config.trading_prices(cfg, crafting_bonus=args.crafting_bonus)

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

    # A seasonal pet is only in season for part of the year, so how much it
    # contributes depends on how long the plan runs -- which is itself an output
    # of the solve. When modelling the calendar (opt-in, see _should_model_calendar)
    # iterate to a fixed point: solve, take the expanded plan's makespan as the plan
    # span, rebuild the seasonal pet rates for it, and solve again until the makespan
    # settles. Otherwise a single steady-state pass (horizon None) is enough.
    model_calendar = _should_model_calendar(cfg.get("pets", {}), args.average_seasons)
    horizon: timedelta | None = None
    production = config.passive_production(cfg, today=start)
    # Assigned in the loop below (which always runs at least once); declared here
    # so the data flow past the loop is explicit rather than leaking out of it.
    shown: list[optimizer.SourcingPlan] = []
    offers: list[sources.Offer] = []
    plans: list[optimizer.SourcingPlan] = []
    within: list[optimizer.SourcingPlan] = []
    for _ in range(_MAX_SEASON_ITERS):
        production = config.passive_production(
            cfg, today=start, start=start, horizon=horizon
        )
        offers = optimizer.all_offers(production, fish, explore, farm)
        plans = optimizer.frontier(
            offers, demand, recipes, args.crafting_bonus, args.plans, open_flows,
            inventory, cookie_sets, report._pet_dominated_items(production),
            prices=prices, pj_gain=pj_gain, pj_price=pj_price, pj_limit=pj_limit,
        )
        if not plans:
            return 0  # nothing left to source
        # The limits drop plans over a time or consumable ceiling, unless that
        # would leave none (then the whole frontier is shown, with a warning below).
        within = [
            p for p in plans
            if within_limits(p, time_limit, args.max_ap, args.max_ln, args.max_gold)
        ]
        shown = within or plans
        new_horizon = shown[
            selected_plan(shown, args.plan, args.cheapest_plan)
        ].time
        if not model_calendar or (
            horizon is not None and _makespans_close(new_horizon, horizon)
        ):
            break
        horizon = new_horizon

    cheapest = cheapest_plan(shown)
    selected = selected_plan(shown, args.plan, args.cheapest_plan)

    if model_calendar:
        print(
            f"\nStarting {start:%d %b %Y}; a seasonal pet counts only for the "
            "part of the plan it is in season."
        )
    if not within:
        print(
            f"\n{limit_shortfall(plans, time_limit, args.max_ap, args.max_ln, args.max_gold)}"
        )
    # Display names for the items a reason or the plan report may mention.
    display = report.build_display(demand, workshop)
    # Full requirement yardstick: the passive residuals of the cheap endpoint (the
    # fastest fully self-sourced plan), for judging whether a resource is at its
    # complete gathering point (see print_critical_path, and the frontier reasons).
    # Recomputed via self_sourced_endpoint rather than read off the frontier: it is a
    # stable, moderate-strategy reference, where the slow passive-leaning extension
    # understates some items (cider stamina from Apples cuts the Orange requirement)
    # and the fast plans overstate net materials (Antler) -- and thinning may drop it.
    reference = optimizer.self_sourced_endpoint(
        offers, demand, recipes, args.crafting_bonus, openables=open_flows,
        inventory=inventory, cookie_sets=cookie_sets, prices=prices,
    )
    max_passive_residuals = optimizer.passive_residuals(
        reference, demand, recipes, args.crafting_bonus, open_flows, inventory
    )
    reasons = report._frontier_reasons(
        shown, demand, recipes, args.crafting_bonus, open_flows, inventory,
        max_passive_residuals, production, display,
    )
    # Split each plan's gold into what must still be bought vs. what the plan makes
    # or holds passively (Large Nets from straw/wood/stone/antlers, cookie sets from
    # held cookies, etc.). Cookie sets come from held cookies, so credit the plan's
    # allowance as a holding (there is no "cookie set" inventory line).
    golds = None
    if prices is not None:
        golds = [optimizer.plan_gold_breakdown(p, prices) for p in shown]
    report.print_frontier(shown, selected, cheapest, reasons, prices, golds)
    chosen = shown[selected]
    solution.arnold_palmers = round(chosen.arnold_palmers)
    solution.large_nets = round(chosen.large_nets)
    # Items a consumable channel can supply: growing one of these is optional fill,
    # not a farming bottleneck (see print_critical_path).
    sourceable_elsewhere = {
        key for offer in offers
        if offer.track in (sources.FISHING_TRACK, sources.EXPLORING_TRACK)
        for key in offer.yields
    }
    for flow in open_flows:
        sourceable_elsewhere |= set(flow.yields)
    # When to drink each Pumpkin Juice: the mastery levels (near the goal, in the
    # item's final tier) that land its intended boost -- shown in the plan's report.
    pj_schedule = pumpkin_juice_schedule(chosen, solution.needs, pj_gain)
    report.print_plan(
        chosen, demand, farm, workshop, args.crafting_bonus, production,
        open_flows, smith.names, inventory, sourceable_elsewhere,
        max_passive_residuals, prices, pj_schedule,
    )

    if args.breakdown:
        report.print_crafts(chosen, workshop, args.crafting_bonus, args.mastery_bonus)
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
