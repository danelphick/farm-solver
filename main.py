"""Command-line entry point for Farm Solver.

See README.md for usage. In short::

    python main.py [-t <tower-requirements>] [-l level] \
        [-i item:amount] [-m item:amount] \
        [--mastery-file <masteries>] [--inventory-file <inventory>]

The mastery and inventory files default to the ``[solver]`` config section.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import math
import sys
from collections.abc import Iterable
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
import sources


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
        "--plan-mode",
        choices=(sources.UNLIMITED, sources.INVENTORY),
        default=sources.UNLIMITED,
        help=(
            "how consumables are sourced. 'unlimited' (the default) spends "
            "abstract Arnold Palmers / Large Nets / Grape Juice freely; "
            "'inventory' grounds them in real crafted/held items (from the "
            "passive economy) and adds the Apple Cider exploring channel fed by "
            "stamina from drinking Orange Juice"
        ),
    )
    parser.add_argument(
        "--current-stamina",
        type=int,
        default=0,
        metavar="N",
        help=(
            "stamina already banked, spendable on Apple Cider explores "
            "(inventory plan mode only; default: 0)"
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
    # cheapest (fewest Arnold Palmers and Large Nets), or a specific one by its
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
        help=(
            "expand the plan spending the fewest Arnold Palmers and Large Nets"
        ),
    )
    plan.add_argument(
        "--plan",
        type=int,
        metavar="N",
        help="expand plan number N from the frontier table instead",
    )

    # The goal is mutually exclusive: climb the tower (default), master items,
    # or acquire items.
    goal = parser.add_mutually_exclusive_group()
    goal.add_argument(
        "-m",
        "--mastery",
        metavar="ITEM:AMOUNT[,...]",
        help="solve for mastering the given item(s) instead of the tower",
    )
    goal.add_argument(
        "-i",
        "--items",
        metavar="ITEM:AMOUNT[,...]",
        help="solve for acquiring the given item(s) instead of the tower",
    )

    return parser


FISHING_CSV = Path(__file__).with_name("fishing.csv")
EXPLORING_CSV = Path(__file__).with_name("exploring.csv")

# Seasonal pet availability depends on the plan's makespan, which depends back on
# it; iterate the solve at most this many times to settle that loop.
_MAX_SEASON_ITERS = 5

# How close to the makespan the farming track may fall and still count as the
# critical path (the rest being passive fill); see :func:`print_critical_path`.
_FARMING_COMAX_MARGIN = 0.01

# How close to its full (cheapest-plan) requirement a passive item's residual must
# be to count as fully gathered -- the resource whose completion gates the plan
# (see :func:`_defining_passive_resource`).
_FULL_REQUIREMENT_MARGIN = 0.02


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


def _csv_items(path: Path) -> set[str]:
    """Lower-cased item names from the ``Item`` column of a drop-rate CSV."""
    if not path.exists():
        return set()
    with path.open(newline="") as f:
        return {row["Item"].strip().lower() for row in csv.DictReader(f)}


def obtainable_items(
    production: passive.PassiveProduction, farm: crops.Farm | None = None
) -> set[str]:
    """Lower-cased items obtainable without crafting.

    Covers passive production (which includes pets), fishing, exploring, farming
    (the fixed farmed items plus everything the crop model can grow, including
    rare seed drops), and the materials the game supplies automatically.
    """
    items = set(crafting.IGNORED_MATERIALS) | set(farming.FARMING_ITEMS)
    for producer in production.producers:
        items |= set(producer.outputs)
    if farm is not None:
        items |= farm.growable_items()
    items |= _csv_items(FISHING_CSV)
    items |= _csv_items(EXPLORING_CSV)
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
) -> tuple[Path, Path]:
    """The mastery and inventory files, from the CLI or the [solver] config.

    A command-line ``--mastery-file`` / ``--inventory-file`` overrides the
    config; otherwise the value comes from the ``[solver]`` section. A file
    missing from both raises a :class:`ValueError`.
    """
    solver = cfg.get("solver", {})

    def pick(cli: Path | None, key: str, flag: str) -> Path:
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
        pick(args.mastery_file, "mastery_file", "--mastery-file"),
        pick(args.inventory_file, "inventory_file", "--inventory-file"),
    )


def load_state(masteries_path: Path, inventory_path: Path) -> farm_solver.GameState:
    masteries = parsing.parse_masteries(masteries_path.read_text())
    inventory = parsing.parse_inventory(inventory_path.read_text())
    return farm_solver.GameState(masteries=masteries, inventory=inventory)


def print_solution(solution: farm_solver.Solution) -> None:
    for need in solution.needs:
        prefix = f"T{need.level}: " if need.level is not None else ""
        if need.extra > 0:
            print(
                f"{prefix}{need.item}: need {need.extra:,} more "
                f"(have {need.have:,} of {need.target:,})"
            )
        else:
            print(
                f"{prefix}{need.item}: already have "
                f"{need.have:,} of {need.target:,}"
            )

    for note in solution.notes:
        print(note)


def print_unobtainable(materials: list[str]) -> None:
    print("The following materials are required for some crafts but are unobtainable:")
    if not materials:
        print("  (none)")
        return
    for material in materials:
        print(f"  {material}")


def within_limits(
    plan: optimizer.SourcingPlan,
    time_limit: timedelta | None = None,
    max_ap: int | None = None,
    max_ln: int | None = None,
) -> bool:
    """Whether a plan respects every given time and consumable ceiling."""
    return (
        (time_limit is None or plan.time <= time_limit)
        and (max_ap is None or round(plan.arnold_palmers) <= max_ap)
        and (max_ln is None or round(plan.large_nets) <= max_ln)
    )


def limit_shortfall(
    plans: list[optimizer.SourcingPlan],
    time_limit: timedelta | None,
    max_ap: int | None,
    max_ln: int | None,
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
    fits = " or ".join(parts) if parts else "every limit at once"
    return f"No plan fits {fits}; showing the full frontier."


def _cookie_sets_used(plan: optimizer.SourcingPlan) -> int:
    """Whole cookie sets a plan spends — partial sets aren't usable, so round up.

    One cookie set covers one real day, boosting whichever crops you harvest that
    day alongside the day's Grape Juice allocation. Crops grow one after another,
    so the sets a plan needs is the whole number of days its cookie-boosted
    farming spans (``ceil`` of the total), not the sum of each crop's rounded-up
    days — that would double-count a day two crops share.
    """
    return math.ceil(plan.cookie_sets)


def cheapest_plan(plans: list[optimizer.SourcingPlan]) -> int:
    """Index of the plan spending the fewest scarce resources (ties: least time).

    Cookie sets and Grape Juices count alongside the Arnold Palmers and Large
    Nets, so the thriftiest plan is the one that leans hardest on free farming.
    """
    return min(
        range(len(plans)),
        key=lambda i: (
            plans[i].arnold_palmers
            + plans[i].large_nets
            + plans[i].cookie_sets
            + plans[i].grape_juices
            + plans[i].apple_ciders,
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
    the fewest-consumable plan; otherwise the default is the fastest, which is
    plan 0 since the frontier is sorted fastest first.
    """
    if plan is not None:
        if not 1 <= plan <= len(plans):
            raise ValueError(f"--plan must be between 1 and {len(plans)}")
        return plan - 1
    if cheapest:
        return cheapest_plan(plans)
    return 0


def print_frontier(
    plans: list[optimizer.SourcingPlan],
    selected: int,
    cheapest: int,
    reasons: list[str | None] | None = None,
) -> None:
    """The frontier table: each plan's time and consumables, fastest first.

    Cookie sets and Grape Juices are shown as extra columns only when some plan
    actually spends them, so a plan that uses neither keeps the plain time/AP/LN
    table. ``reasons`` annotates why each plan is on the frontier -- a resource
    plan says "all Steel Wire from passive" (its gathering plateau); endpoints and
    farming intermediates carry only their fastest/cheapest tag.
    """
    show_cookies = any(_cookie_sets_used(p) >= 1 for p in plans)
    show_juices = any(round(p.grape_juices) >= 1 for p in plans)
    show_ciders = any(round(p.apple_ciders) >= 1 for p in plans)

    columns = ["time", "Arnold Palmers", "Large Nets"]
    if show_cookies:
        columns.append("cookie sets")
    if show_juices:
        columns.append("Grape Juice")
    if show_ciders:
        columns.append("Apple Cider")
    print(f"\nSourcing plans ({' / '.join(columns)}):")

    for i, plan in enumerate(plans):
        marker = ">" if i == selected else " "
        tags = []
        if i == 0:
            tags.append("fastest")
        if i == cheapest:
            tags.append("cheapest")
        note_parts = []
        if tags:
            note_parts.append(f"[{', '.join(tags)}]")
        reason = reasons[i] if reasons is not None else None
        if reason:
            note_parts.append(f"— {reason}")
        note = "  " + " ".join(note_parts) if note_parts else ""
        parts = [
            passive.format_duration(plan.time),
            f"{round(plan.arnold_palmers):,} AP",
            f"{round(plan.large_nets):,} LN",
        ]
        if show_cookies:
            parts.append(f"{_cookie_sets_used(plan):,} sets")
        if show_juices:
            parts.append(f"{round(plan.grape_juices):,} GJ")
        if show_ciders:
            parts.append(f"{round(plan.apple_ciders):,} cider")
        print(f" {marker} {i + 1}. {' / '.join(parts)}{note}")


def _frontier_reasons(
    plans: list[optimizer.SourcingPlan],
    demand: dict[str, float],
    recipes: dict[str, dict[str, float]],
    crafting_bonus: float,
    open_flows: list[sources.OpenFlow],
    inventory: dict[str, float],
    max_passive_residuals: dict[str, float],
    production: passive.PassiveProduction | None,
    display: dict[str, str],
) -> list[str | None]:
    """Why each plan is on the frontier: the resource whose full-requirement
    gathering plateau it sits at ("all Steel Wire from passive"), else ``None``
    (an endpoint or a farming-accelerated intermediate). Mirrors the chosen plan's
    critical path (see :func:`_defining_passive_resource`)."""
    reasons: list[str | None] = []
    for plan in plans:
        residuals = optimizer.passive_residuals(
            plan, demand, recipes, crafting_bonus, open_flows, inventory
        )
        tolerance = max(timedelta(seconds=1), plan.time * _FARMING_COMAX_MARGIN)
        defining = _defining_passive_resource(
            plan, residuals, display, plan.time, tolerance,
            max_passive_residuals, production,
        )
        reasons.append(f"all {defining[0]} from passive" if defining else None)
    return reasons


def _pet_passive_rate(
    production: passive.PassiveProduction | None, item: str
) -> float:
    """Per-minute rate at which pets (not buildings) passively make ``item``."""
    if production is None:
        return 0.0
    key = item.lower()
    total = 0.0
    for producer in production.producers:
        if not producer.name.startswith(config.PET_PRODUCER_PREFIX):
            continue
        quantity = producer.outputs.get(key)
        if quantity:
            per_cycle = quantity
            if production.inventory_size is not None:
                per_cycle = min(per_cycle, production.inventory_size)
            total += per_cycle / (producer.period.total_seconds() / 60)
    return total


def _pet_dominated_items(production: passive.PassiveProduction) -> set[str]:
    """Passive items a pet mostly makes -- a slow trickle, not a real plateau.

    Their completion times are the multi-decade pet tail, not the gathering
    breakpoints worth building a plan around (see :func:`optimizer.frontier`).
    """
    items = {key for producer in production.producers for key in producer.outputs}
    return {
        key for key in items
        if _pet_passive_rate(production, key) > production.rate_per_minute(key) / 2
    }


def _slowest_passive_item(
    plan: optimizer.SourcingPlan,
    need: dict[str, float],
    elsewhere: dict[str, float],
    display: dict[str, str],
    production: passive.PassiveProduction | None = None,
) -> tuple[str, int, timedelta, str] | None:
    """The passive item that caps the plan's time: ``(name, quantity, wait, key)``.

    Each item's wait is its residual (what other channels and crafting did not
    cover) over its per-minute rate; the longest of these is the passive track's
    time, so it is what caps a passive-bound plan.

    A pet slowly trickling out a small top-up is skipped, though: pets are a last
    resort, so unless a pet is the *primary* source of an item it should not be
    named as the critical path over a building doing the real work (a pet finishing
    a 2,302 residual of Horn just after a sawmill makes 13M Oak is co-maximal noise,
    not the bottleneck). Only if every passive item is such a pet top-up do we fall
    back to the slowest of them, so a genuinely pet-bound plan still reports one.
    """
    passive_draws = [d for d in plan.draws if d.offer.track == sources.PASSIVE_TRACK]
    if not passive_draws:
        return None
    offer = passive_draws[0].offer
    best: tuple[str, int, timedelta, str] | None = None  # slowest non-pet-top-up
    fallback: tuple[str, int, timedelta, str] | None = None  # slowest of any, backup
    for key, rate in offer.yields.items():
        if rate <= 0:
            continue
        total_need = need.get(key, 0.0)
        residual = total_need - elsewhere.get(key, 0.0)
        if residual <= 1e-6:
            continue
        wait = timedelta(minutes=residual / rate)
        row = (display.get(key, key), round(residual), wait, key)
        if fallback is None or wait > fallback[2]:
            fallback = row
        # A pet top-up: pets make most of this item's passive supply yet supply
        # less than half of the item overall (another channel is its main source).
        pet_rate = _pet_passive_rate(production, key)
        pet_made = residual * (pet_rate / rate)
        pet_topup = pet_rate > rate / 2 and pet_made < total_need / 2
        if not pet_topup and (best is None or wait > best[2]):
            best = row
    return best or fallback


def _num_width(counts: Iterable[int]) -> int:
    """Width of the widest comma-formatted count, for right-aligning a column."""
    return max((len(f"{n:,}") for n in counts), default=0)


def _farming_drives(
    plan: optimizer.SourcingPlan,
    need: dict[str, float],
    elsewhere: dict[str, float],
    display: dict[str, str],
    production: passive.PassiveProduction | None = None,
    sourceable_elsewhere: set[str] | None = None,
) -> bool:
    """Whether the farming track, not passive production, sets the makespan.

    Farming that reaches the makespan is usually the driver, but if its longest
    crop is fill that a consumable channel could supply and a passive item is
    co-maximal, that passive item drives instead (see :func:`print_critical_path`).
    Both the critical-path line and the passive section's ordering use this so they
    agree on which track is the bottleneck.
    """
    track_time: dict[str, timedelta] = {}
    for draw in plan.draws:
        track_time[draw.offer.track] = (
            track_time.get(draw.offer.track, timedelta(0)) + draw.time
        )
    makespan = max(track_time.values(), default=timedelta(0))
    if makespan <= timedelta(0):
        return False
    farming_time = track_time.get(sources.FARMING_TRACK, timedelta(0))
    tolerance = max(timedelta(seconds=1), makespan * _FARMING_COMAX_MARGIN)
    if farming_time < makespan - tolerance:
        return False
    item = _slowest_passive_item(plan, need, elsewhere, display, production)
    passive_comaximal = item is not None and item[2] >= makespan - tolerance
    return not (passive_comaximal and _binding_crop_is_fill(plan, sourceable_elsewhere))


def _harvest_alls(count: int) -> str:
    """``count`` "Harvest All"s (the full-field harvest action), pluralised."""
    return f"{count:,} Harvest All{'' if count == 1 else 's'}"


def _farming_action(draw: sources.Draw) -> str:
    """The action and crop a farming draw stands for, cookie days included."""
    if draw.offer.cookies:
        # A partial cookie set still consumes a whole one, so round days (and the
        # sets they stand for) up — never down.
        days = math.ceil(draw.units)
        crop = draw.offer.name.removeprefix("grow ").removesuffix(" with cookies")
        noun = "day" if days == 1 else "days"
        return f"{days:,} cookie-boosted {noun} of {crop}"
    crop = draw.offer.name.removeprefix("grow ").removesuffix(" without grape juice")
    return f"{_harvest_alls(math.ceil(draw.units))} of {crop}"


def _farming_bottleneck(plan: optimizer.SourcingPlan, total: timedelta) -> str:
    """Describe the farming sequence that runs the makespan (grown one at a time)."""
    farming_draws = [d for d in plan.draws if d.offer.track == sources.FARMING_TRACK]
    biggest = max(farming_draws, key=lambda d: d.time)
    duration = passive.format_duration(total)
    if len(farming_draws) == 1:
        return (
            f"{_farming_action(biggest)} ({duration}) — crops grow one at a time"
        )
    return (
        f"{len(farming_draws)} crops grown back-to-back ({duration}), "
        f"longest {_farming_action(biggest)}"
    )


def _binding_crop_is_fill(
    plan: optimizer.SourcingPlan, sourceable_elsewhere: set[str] | None
) -> bool:
    """Whether the longest-grown crop could instead be sourced with consumables.

    A crop that fishing, exploring or opening can supply is grown only because
    doing so is free and fits inside a wait another track already forces -- so the
    farming that reaches the makespan is fill, not the real driver (see
    :func:`print_critical_path`). A crop with no such source (Pine, say) is
    genuinely farming-bound.
    """
    if not sourceable_elsewhere:
        return False
    farming = [d for d in plan.draws if d.offer.track == sources.FARMING_TRACK]
    if not farming:
        return False
    biggest = max(farming, key=lambda d: d.time)
    crop = (
        biggest.offer.name.removeprefix("grow ")
        .removesuffix(" with cookies")
        .removesuffix(" without grape juice")
        .lower()
    )
    return crop in sourceable_elsewhere


def _defining_passive_resource(
    plan: optimizer.SourcingPlan,
    residuals: dict[str, float],
    display: dict[str, str],
    makespan: timedelta,
    tolerance: timedelta,
    max_passive_residuals: dict[str, float] | None,
    production: passive.PassiveProduction | None,
) -> tuple[str, int, timedelta, str] | None:
    """The passive item at its full requirement whose completion sets the makespan.

    A material like Steel Wire is needed only up to its products' full demand;
    gather that much passively and no more is required, so the time it takes is a
    hard gate -- and a plan clamped there is *its* plan. It qualifies when its
    ``residuals`` entry has reached its full (cheapest-plan) requirement and it
    completes at the makespan; a resource still being sped up sits below its full
    requirement, and one that finishes early is not co-maximal. A pet-made
    material is skipped -- its trickle is a last resort, and the pet-primary case
    is handled by :func:`_slowest_passive_item`. Shared by the critical-path line
    and the frontier's per-plan reason (see :func:`print_frontier`).
    """
    if not max_passive_residuals:
        return None
    passive_draw = next(
        (d for d in plan.draws if d.offer.track == sources.PASSIVE_TRACK), None
    )
    if passive_draw is None:
        return None
    best: tuple[str, int, timedelta, str] | None = None
    for key, rate in passive_draw.offer.yields.items():
        if rate <= 0:
            continue
        residual = residuals.get(key, 0.0)
        full = max_passive_residuals.get(key, 0.0)
        if residual <= 1e-6 or full <= 0:
            continue
        if residual < full * (1 - _FULL_REQUIREMENT_MARGIN):
            continue  # still being sped up with consumables, not the gate
        wait = timedelta(minutes=residual / rate)
        if wait < makespan - tolerance:
            continue  # finishes early, so it is not what caps the plan
        if _pet_passive_rate(production, key) > rate / 2:
            continue  # a pet trickle, not a real gathering plateau
        if best is None or wait > best[2]:
            best = (display.get(key, key), round(residual), wait, key)
    return best


def _passive_residuals_of(
    plan: optimizer.SourcingPlan, need: dict[str, float], elsewhere: dict[str, float]
) -> dict[str, float]:
    """Each passive item's residual (net requirement) in ``plan``, from need/elsewhere."""
    passive_draw = next(
        (d for d in plan.draws if d.offer.track == sources.PASSIVE_TRACK), None
    )
    if passive_draw is None:
        return {}
    return {
        key: need.get(key, 0.0) - elsewhere.get(key, 0.0)
        for key in passive_draw.offer.yields
    }


def print_critical_path(
    plan: optimizer.SourcingPlan,
    need: dict[str, float],
    elsewhere: dict[str, float],
    display: dict[str, str],
    production: passive.PassiveProduction | None = None,
    sourceable_elsewhere: set[str] | None = None,
    max_passive_residuals: dict[str, float] | None = None,
) -> None:
    """Highlight the track that truly caps how fast the plan can run.

    Tracks run concurrently, so the total time is the slowest of them. Fishing and
    exploring are instant, so that leaves farming and passive production. They are
    not symmetric, though: passive runs for free alongside everything, so it
    opportunistically fills whatever wait another track already forces -- an item
    that could be fished or explored instead is made passively just to save a
    consumable, expanding the passive track to exactly the makespan. So when
    farming is (co-)maximal it is usually the real driver and the passive track
    beside it is fill.

    The reverse happens too, though: on a plan clamped to a passive item's
    completion, farming grows free crops to fill the passive-forced wait. So if the
    longest-grown crop could itself be sourced with consumables (it is fill, see
    :func:`_binding_crop_is_fill`) and a passive item is co-maximal, that passive
    item is the driver instead. Naming the driver shows what to attack (source it
    with consumables, or pick a faster plan) to finish sooner.
    """
    track_time: dict[str, timedelta] = {}
    for draw in plan.draws:
        track_time[draw.offer.track] = (
            track_time.get(draw.offer.track, timedelta(0)) + draw.time
        )
    makespan = max(track_time.values(), default=timedelta(0))
    if makespan <= timedelta(0):
        return  # everything is instant; there is no wait to explain

    tolerance = max(timedelta(seconds=1), makespan * _FARMING_COMAX_MARGIN)
    defining = _defining_passive_resource(
        plan, _passive_residuals_of(plan, need, elsewhere), display, makespan,
        tolerance, max_passive_residuals, production,
    )
    if defining is not None:
        # A resource gathered to its full requirement exactly here: its completion
        # is the gate, and this is its plan (more time gathers no more of it).
        name, quantity, wait, _ = defining
        line = (
            f"{quantity:,}x {name} from passive production "
            f"({passive.format_duration(wait)}) — its full requirement, "
            "after which no more is needed"
        )
    elif _farming_drives(
        plan, need, elsewhere, display, production, sourceable_elsewhere
    ):
        line = _farming_bottleneck(plan, makespan)
    elif (item := _slowest_passive_item(
        plan, need, elsewhere, display, production
    )) is not None:
        # Passive sets the pace: it is the sole longest track, or farming beside it
        # is merely filling the wait this item forces with free crops.
        name, quantity, wait, _ = item
        line = (
            f"{quantity:,}x {name} from passive production "
            f"({passive.format_duration(wait)}) — the slowest item to build up"
        )
    else:
        return
    print(f"\nCritical path (sets the {passive.format_duration(makespan)} total):")
    print(f"  {line}")


def _apportion(quantity: int, weights: list[float]) -> list[int]:
    """Split ``quantity`` into integer parts proportional to ``weights``.

    Uses the largest-remainder method, so the parts always sum to exactly
    ``quantity`` with no rounding drift across the sources.
    """
    total = sum(weights)
    if total <= 0:
        return [0] * len(weights)
    raw = [quantity * w / total for w in weights]
    parts = [math.floor(r) for r in raw]
    for i in sorted(
        range(len(raw)), key=lambda i: (raw[i] - parts[i], -i), reverse=True
    )[: quantity - sum(parts)]:
        parts[i] += 1
    return parts


def _season_note(producer_name: str) -> str:
    """A bracketed active-months window for a seasonal pet, else empty.

    Seasonal pets only fetch during a short calendar window, so their output is
    modelled as a long-run yearly average (see :func:`config._pet_producers`) --
    which is why such a pet contributes less than a year-round one. Flagging the
    window in the report explains that reduced share (and that shifting the plan
    into or out of that window would change it in-game).
    """
    key = producer_name.removeprefix(config.PET_PRODUCER_PREFIX).lower()
    months = pets.SEASONAL_MONTHS.get(key)
    if not months:
        return ""
    lo, hi = min(months), max(months)
    span = (
        calendar.month_abbr[lo]
        if lo == hi
        else f"{calendar.month_abbr[lo]}–{calendar.month_abbr[hi]}"
    )
    return f" [seasonal: {span}]"


def _passive_sources(
    production: passive.PassiveProduction, item: str, quantity: int
) -> str:
    """Name the passive producer(s) that make ``quantity`` of ``item``.

    A single producer is named plainly (``(from Windmill)``); when several make
    the item, the quantity is broken down between them by their per-minute rate
    share (which is exactly each one's share of the shared wait), largest first.
    A seasonal pet contributes less because its output is a yearly average, and
    the breakdown flags its active window (see :func:`_season_note`). Returns an
    empty string when nothing known produces the item.
    """
    key = item.lower()
    contributors: list[tuple[float, str]] = []  # (rate, name)
    for producer in production.producers:
        output = producer.outputs.get(key)
        if not output:
            continue
        per_cycle = output
        if production.inventory_size is not None:
            per_cycle = min(per_cycle, production.inventory_size)
        rate = per_cycle / (producer.period.total_seconds() / 60)
        if rate > 0:
            contributors.append((rate, producer.name))
    if not contributors:
        return ""
    if len(contributors) == 1:
        name = contributors[0][1]
        return f" (from {name}{_season_note(name)})"
    counts = _apportion(quantity, [rate for rate, _ in contributors])
    labelled = sorted(
        ((count, rate, name) for count, (rate, name) in zip(counts, contributors)),
        key=lambda t: (-t[0], -t[1], t[2]),  # biggest share, then rate, then name
    )
    parts = [f"{count:,} from {name}{_season_note(name)}" for count, _, name in labelled]
    return " (" + ", ".join(parts) + ")"


def _print_cookie_farming(
    draw: sources.Draw,
    juices: float,
    interesting: set[str],
    display: dict[str, str],
    width: int = 0,
) -> None:
    """Render one cookie-boosted farming draw: a crop grown under cookie sets.

    A cookie set covers a whole real day, boosting the day's Grape Juice
    allocation :data:`crops.COOKIE_SET_MULTIPLIER`-fold. Sets are a farm-wide
    daily resource shared across crops, so the whole-set count lives in the
    Farming heading; each crop line just shows its span, juice and yield.
    """
    crop = draw.offer.name.removeprefix("grow ").removesuffix(" with cookies")
    line = f"  Cookie-boosted {crop} in {passive.format_duration(draw.time)}"
    if round(juices) >= 1:
        line += (
            f" using {round(juices):,} Grape Juice"
            f" (yield {crops.COOKIE_SET_MULTIPLIER}x)"
        )
    print(f"{line}:")
    for key in sorted(k for k in draw.offer.yields if k in interesting):
        print(f"    {round(draw.produced[key]):>{width},}x {display.get(key, key)}")


def print_plan(
    plan: optimizer.SourcingPlan,
    demand: dict[str, float],
    farm: crops.Farm,
    workshop: crafting.Workshop,
    crafting_bonus: float,
    production: passive.PassiveProduction | None = None,
    open_flows: list[sources.OpenFlow] | None = None,
    openable_names: dict[str, str] | None = None,
    inventory: dict[str, float] | None = None,
    sourceable_elsewhere: set[str] | None = None,
    max_passive_residuals: dict[str, float] | None = None,
) -> None:
    """Expand one plan into its per-channel actions and leftover materials."""
    open_flows = open_flows or []
    inventory = inventory or {}
    flows_by_name = {flow.name.lower(): flow for flow in open_flows}
    demanded = {item.lower() for item in demand}

    # Display names for everything that can appear (demand plus every recipe item
    # and material, plus openable names, keys and contents), keyed lower-cased.
    display = {item.lower(): item for item in demand}
    for key, recipe in workshop.recipes.items():
        display.setdefault(key, recipe.name)
        for material in recipe.materials:
            display.setdefault(material.lower(), material)
    for key, name in (openable_names or {}).items():
        display.setdefault(key, name)

    # What the plan actually needs of each item: the direct demand, whatever the
    # chosen crafts consume as materials, and whatever opening chests consumes
    # (the chest tokens and their keys).
    need: dict[str, float] = {item.lower(): float(qty) for item, qty in demand.items()}
    for item, count in plan.crafts.items():
        recipe = workshop.get(item)
        if recipe is None:
            continue
        for material, quantity in recipe.materials.items():
            key = material.lower()
            if key not in crafting.IGNORED_MATERIALS:
                need[key] = need.get(key, 0.0) + quantity * count
    # What crafting produces (crafting_bonus per craft) and what opening yields;
    # both are free, instant supply that spares the passive/farming channels.
    crafted = {item: crafting_bonus * count for item, count in plan.crafts.items()}
    opened: dict[str, float] = {}
    for name, count in plan.opens.items():
        flow = flows_by_name.get(name.lower())
        if flow is None:
            continue
        for item, quantity in flow.consumes.items():
            need[item] = need.get(item, 0.0) + quantity * count
        for item, quantity in flow.yields.items():
            opened[item] = opened.get(item, 0.0) + quantity * count
    # Only genuinely-needed items (demand, craft materials, and the chests/keys
    # opens consume) are worth showing; an openable's other contents are byproducts.
    interesting = set(need) | set(crafted)

    def wanted(offer: sources.Offer) -> str:
        return ", ".join(
            sorted(display.get(key, key) for key in offer.yields if key in interesting)
        )

    def sourced(draws: list[sources.Draw]) -> list[sources.Draw]:
        # Drop residue draws: CBC can leave a sliver of a net, palmer or planting
        # on a channel nothing needs, which rounds to zero produced in ~zero time
        # or consumable -- not a real action, so keep it out of the report.
        return [
            d
            for d in draws
            if any(round(d.produced[k]) >= 1 for k in d.offer.yields if k in interesting)
        ]

    def draws_on(track: str) -> list[sources.Draw]:
        return [d for d in plan.draws if d.offer.track == track]

    # What every non-passive channel, crafting and opening already supplies. The
    # passive track and the critical-path summary both work in residuals -- an item
    # fully sourced elsewhere adds no passive wait -- so compute it once, up front.
    elsewhere: dict[str, float] = dict(crafted)
    for item, quantity in opened.items():
        elsewhere[item] = elsewhere.get(item, 0.0) + quantity
    # Stock on hand covers intermediates the plan does not produce (as in the LP,
    # a directly-demanded item gets no credit -- its need is already net of stock).
    for item, quantity in inventory.items():
        if item not in demanded:
            elsewhere[item] = elsewhere.get(item, 0.0) + quantity
    for draw in plan.draws:
        if draw.offer.track == sources.PASSIVE_TRACK:
            continue
        for item, quantity in draw.produced.items():
            elsewhere[item] = elsewhere.get(item, 0.0) + quantity

    print_critical_path(
        plan, need, elsewhere, display, production, sourceable_elsewhere,
        max_passive_residuals,
    )

    fishing_draws = sourced(draws_on(sources.FISHING_TRACK))
    if fishing_draws:
        print("\nFishing:")
        ordered = sorted(fishing_draws, key=lambda d: d.ln, reverse=True)
        width = _num_width(math.ceil(d.units) for d in ordered)
        for draw in ordered:
            zone = draw.offer.name.removeprefix("fish ")
            print(
                f"  {math.ceil(draw.units):>{width},}x Large Net at {zone}"
                f" ({wanted(draw.offer)})"
            )

    exploring_draws = sourced(draws_on(sources.EXPLORING_TRACK))
    # Two ways to explore: Arnold Palmers (ap cost / consumes a palmer) and, in
    # the inventory plan mode, Apple Cider (consumes stamina and part of a cider).
    palmer_draws = [
        d for d in exploring_draws if not d.offer.consumes.get("apple cider")
    ]
    cider_draws = [d for d in exploring_draws if d.offer.consumes.get("apple cider")]
    if palmer_draws:
        print("\nExploring:")
        ordered = sorted(palmer_draws, key=lambda d: d.ap, reverse=True)
        width = _num_width(math.ceil(d.units) for d in ordered)
        for draw in ordered:
            location = draw.offer.name.removeprefix("explore ")
            print(
                f"  {math.ceil(draw.units):>{width},}x Arnold Palmer at {location}"
                f" ({wanted(draw.offer)})"
            )
    if cider_draws:
        print("\nExploring (Apple Cider):")
        # A cider draw's units are explores; ciders used and stamina spent are its
        # per-explore consumes scaled up.
        def ciders(d: sources.Draw) -> float:
            return d.units * d.offer.consumes.get("apple cider", 0.0)

        ordered = sorted(cider_draws, key=ciders, reverse=True)
        width = _num_width(math.ceil(ciders(d)) for d in ordered)
        for draw in ordered:
            zone = draw.offer.name.removeprefix("cider ")
            stamina = draw.units * draw.offer.consumes.get("stamina", 0.0)
            print(
                f"  {math.ceil(ciders(draw)):>{width},}x Apple Cider at {zone}"
                f" ({round(stamina):,} stamina, {wanted(draw.offer)})"
            )

    # Drinking Orange Juice for stamina (inventory plan mode). One instant flow;
    # report the Orange Juice drunk and the stamina it banks for cider explores.
    drinking_draws = draws_on(sources.DRINKING_TRACK)
    total_oj = sum(
        d.units * d.offer.consumes.get("orange juice", 0.0) for d in drinking_draws
    )
    if round(total_oj) >= 1:
        total_stamina = sum(d.produced.get("stamina", 0.0) for d in drinking_draws)
        print("\nDrinking (stamina):")
        print(
            f"  Drink {round(total_oj):,}x Orange Juice"
            f" -> {round(total_stamina):,} stamina"
        )

    # Opening chests and bags at the Locksmith. Skip residue opens that yield less
    # than one of anything wanted (CBC can leave a sliver, as with draws).
    opens = [
        (name, count)
        for name, count in plan.opens.items()
        if (flow := flows_by_name.get(name.lower())) is not None
        and any(
            round(flow.yields[item] * count) >= 1
            for item in flow.yields
            if item in need
        )
    ]
    if opens:
        print("\nOpening (Locksmith):")
        ordered = sorted(opens, key=lambda nc: nc[1], reverse=True)
        width = _num_width(math.ceil(count) for _, count in ordered)
        for name, count in ordered:
            flow = flows_by_name[name.lower()]
            keys = [
                f"{round(quantity * count):,} {display.get(item, item)}"
                for item, quantity in flow.consumes.items()
                if item != name.lower() and round(quantity * count) >= 1
            ]
            cost = f" using {', '.join(keys)}" if keys else ""
            gained = ", ".join(
                f"{round(flow.yields[item] * count):,}x {display.get(item, item)}"
                for item in sorted(flow.yields)
                if item in need and round(flow.yields[item] * count) >= 1
            )
            detail = f" -> {gained}" if gained else ""
            print(
                f"  {math.ceil(count):>{width},}x"
                f" {display.get(name.lower(), name)}{cost}{detail}"
            )

    farming_draws = sourced(draws_on(sources.FARMING_TRACK))
    if farming_draws:
        # Crops grow one at a time, so the whole track's time is their sum.
        total_farming = sum((d.time for d in farming_draws), timedelta(0))
        heading = f"\nFarming ({passive.format_duration(total_farming)}"
        # One cookie set covers one real day, shared across whatever crops are
        # harvested that day, so the count is the whole days the cookie-boosted
        # farming spans (ceil of the total) — not each crop's days rounded up.
        total_sets = math.ceil(sum(d.cookies for d in farming_draws if d.cookies))
        if total_sets >= 1:
            heading += f", {total_sets:,} cookie sets"
        print(heading + "):")
        # Right-align the yield counts across the whole section (every crop's
        # produced items), so the "x" lines up regardless of which crop.
        width = _num_width(
            round(d.produced[key])
            for d in farming_draws
            for key in d.offer.yields
            if key in interesting
        )
        for draw in sorted(farming_draws, key=lambda d: d.time, reverse=True):
            juices = draw.gj  # LP-tracked; zero for the without-grape-juice offer
            if draw.offer.cookies:
                _print_cookie_farming(draw, juices, interesting, display, width)
                continue
            crop = draw.offer.name.removeprefix("grow ").removesuffix(
                " without grape juice"
            )
            line = (
                f"  {_harvest_alls(math.ceil(draw.units))} of {crop}"
                f" in {passive.format_duration(draw.time)}"
            )
            if round(juices) >= 1:
                line += f" using {round(juices):,} Grape Juice"
            print(f"{line}:")
            # Grape-juice and natural harvests yield the same bundle, so the
            # grape-juice share of the plantings splits every produced item the
            # same way.
            gj_share = juices / draw.units if draw.units else 0.0
            for key in sorted(k for k in draw.offer.yields if k in interesting):
                total = draw.produced[key]
                name = display.get(key, key)
                if round(juices) >= 1:
                    from_gj = total * gj_share
                    print(
                        f"    {round(total):>{width},}x {name}"
                        f" ({round(from_gj):,} from Grape Juice,"
                        f" {round(total - from_gj):,} from normal farming)"
                    )
                else:
                    print(f"    {round(total):>{width},}x {name}")

    passive_draws = draws_on(sources.PASSIVE_TRACK)
    if passive_draws:
        offer = passive_draws[0].offer
        # Passive only covers what the other channels and crafting did not (see
        # `elsewhere` above), so an item fully fished, explored or crafted adds no
        # passive wait. Each item's own wait is its residual over its per-minute
        # rate; the longest is the whole track's time, so listing them longest-first
        # shows what drives it.
        rows = []
        for key, rate in offer.yields.items():
            if rate <= 0:
                continue
            residual = need.get(key, 0.0) - elsewhere.get(key, 0.0)
            if residual > 1e-6:
                wait = timedelta(minutes=residual / rate)
                rows.append((wait, display.get(key, key), round(residual), key))
        if rows:
            print("\nPassive production (concurrent):")
            # List the item on the critical path first (only when passive is the
            # plan's bottleneck track), then the rest by quantity, then by how long
            # they take. The critical item is whatever print_critical_path names --
            # not simply the slowest, since a pet top-up is skipped over a building.
            critical_key = None
            if not _farming_drives(
                plan, need, elsewhere, display, production, sourceable_elsewhere
            ):
                critical = _slowest_passive_item(
                    plan, need, elsewhere, display, production
                )
                if critical is not None:
                    critical_key = critical[3]
            width = _num_width(quantity for _, _, quantity, _ in rows)
            ordered = sorted(
                rows, key=lambda r: (r[3] != critical_key, -r[2], -r[0])
            )
            for wait, name, quantity, key in ordered:
                source = (
                    _passive_sources(production, key, quantity)
                    if production is not None
                    else ""
                )
                print(
                    f"  {quantity:>{width},}x {name}"
                    f" in {passive.format_duration(wait)}{source}"
                )

    if plan.remainder:
        print("\nMaterials with no known source (supply yourself):")
        for material in sorted(plan.remainder):
            print(f"  {plan.remainder[material]:,}x {material}")


def print_crafts(
    plan: optimizer.SourcingPlan,
    workshop: crafting.Workshop,
    crafting_bonus: float,
    mastery_bonus: float,
) -> None:
    """List the crafts the plan performs, parents before the items they consume."""
    if not plan.crafts:
        return
    consumed = {
        material.lower()
        for item in plan.crafts
        if (recipe := workshop.get(item)) is not None
        for material in recipe.materials
    }
    order: list[str] = []
    seen: set[str] = set()

    def visit(item: str) -> None:
        if item in seen or item not in plan.crafts:
            return
        seen.add(item)
        order.append(item)
        recipe = workshop.get(item)
        if recipe is not None:
            for material in recipe.materials:
                visit(material.lower())

    for item in sorted(k for k in plan.crafts if k not in consumed):
        visit(item)
    for item in sorted(plan.crafts):  # any left in a cycle
        visit(item)

    print("\nRequired Crafts:")
    for key in order:
        count = plan.crafts[key]
        recipe = workshop.get(key)
        assert recipe is not None  # only craftable items are in plan.crafts
        produced = count * crafting_bonus
        print(
            f"\n{round(count):,}x {recipe.name} "
            f"(Produces {round(produced):,}, Adds {round(produced * mastery_bonus):,} Mastery)"
        )
        for material, quantity in recipe.materials.items():
            if material.lower() not in crafting.IGNORED_MATERIALS:
                print(f"  {round(count * quantity):,}x {material}")


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
    workshop = crafting.load_workshop()
    farm = crops.farm_from_config(cfg.get("farming", {}))
    fish = fishing.fishing_from_config(cfg.get("fishing", {}))
    explore = exploring.exploring_from_config(cfg.get("exploring", {}))
    start = args.start_date or date.today()

    # A standalone report over the whole workshop; no solving needed. Use the
    # steady-state producers (no plan span) so every pet's items are counted.
    if args.unobtainable:
        production = config.passive_production(cfg, today=start)
        obtainable = obtainable_items(production, farm)
        print_unobtainable(unobtainable_materials(workshop, obtainable))
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

    if args.mastery is not None:
        goals = parsing.parse_item_amounts(args.mastery)
        solution = farm_solver.solve_mastery(state, goals)
    elif args.items is not None:
        goals = parsing.parse_item_amounts(args.items)
        solution = farm_solver.solve_acquire(state, goals)
    else:
        requirements = farm_solver.load_tower_requirements(args.tower)
        solution = farm_solver.solve_tower(state, requirements, args.level)

    print_solution(solution)

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
    # it (inventory plan mode); the drink-Orange-Juice flow tops it up over time.
    if args.plan_mode == sources.INVENTORY and args.current_stamina:
        inventory["stamina"] = inventory.get("stamina", 0.0) + args.current_stamina
    cookie_sets = cookie_sets_available(inventory, args.cookie_sets)

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
    shown: list[optimizer.SourcingPlan] = []
    for _ in range(_MAX_SEASON_ITERS):
        production = config.passive_production(
            cfg, today=start, start=start, horizon=horizon
        )
        offers = optimizer.all_offers(production, fish, explore, farm, args.plan_mode)
        plans = optimizer.frontier(
            offers, demand, recipes, args.crafting_bonus, args.plans, open_flows,
            inventory, cookie_sets, _pet_dominated_items(production),
        )
        if not plans:
            return 0  # nothing left to source
        # The limits drop plans over a time or consumable ceiling, unless that
        # would leave none (then the whole frontier is shown, with a warning below).
        within = [
            p for p in plans if within_limits(p, time_limit, args.max_ap, args.max_ln)
        ]
        shown = within or plans
        new_horizon = shown[selected_plan(shown, args.plan, args.cheapest_plan)].time
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
        print(f"\n{limit_shortfall(plans, time_limit, args.max_ap, args.max_ln)}")
    # Display names for the items a reason or the plan report may mention.
    display = {item.lower(): item for item in demand}
    for key, recipe in workshop.recipes.items():
        display.setdefault(key, recipe.name)
        for material in recipe.materials:
            display.setdefault(material.lower(), material)
    # The cheapest plan crafts the most, so its passive residuals are each item's
    # full requirement -- the yardstick for whether a resource is at its complete
    # gathering point (see print_critical_path, and the frontier reasons).
    max_passive_residuals = optimizer.passive_residuals(
        plans[-1], demand, recipes, args.crafting_bonus, open_flows, inventory
    )
    reasons = _frontier_reasons(
        shown, demand, recipes, args.crafting_bonus, open_flows, inventory,
        max_passive_residuals, production, display,
    )
    print_frontier(shown, selected, cheapest, reasons)
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
    print_plan(
        chosen, demand, farm, workshop, args.crafting_bonus, production,
        open_flows, smith.names, inventory, sourceable_elsewhere,
        max_passive_residuals,
    )

    if args.breakdown:
        print_crafts(chosen, workshop, args.crafting_bonus, args.mastery_bonus)
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
