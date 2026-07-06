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


def cheapest_plan(
    plans: list[optimizer.SourcingPlan],
    prices: dict[str, float] | None = None,
) -> int:
    """Index of the cheapest plan (ties: least time).

    With trading ``prices`` (per-unit gold, see :func:`config.trading_prices`)
    "cheapest" is the least real gold spent -- the principled ranking. Without
    them it falls back to the fewest scarce resources counted equally (cookie
    sets and Grape Juices alongside Arnold Palmers, Large Nets and Apple Ciders),
    so the thriftiest plan is the one leaning hardest on free farming.
    """
    if prices is not None:
        return min(
            range(len(plans)),
            key=lambda i: (
                optimizer.plan_gold(plans[i], prices),
                plans[i].time.total_seconds(),
            ),
        )
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
    prices: dict[str, float] | None = None,
) -> int:
    """Index of the plan to expand, from ``--plan`` / ``--cheapest-plan``.

    Explicit ``--plan N`` wins (1-based, validated); ``--cheapest-plan`` picks
    the cheapest plan (by gold when ``prices`` are given); otherwise the default
    is the fastest, which is plan 0 since the frontier is sorted fastest first.
    """
    if plan is not None:
        if not 1 <= plan <= len(plans):
            raise ValueError(f"--plan must be between 1 and {len(plans)}")
        return plan - 1
    if cheapest:
        return cheapest_plan(plans, prices)
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
    workshop = crafting.load_workshop()
    farm = crops.farm_from_config(cfg.get("farming", {}))
    fish = fishing.fishing_from_config(cfg.get("fishing", {}))
    explore = exploring.exploring_from_config(cfg.get("exploring", {}))
    start = args.start_date or date.today()

    # A standalone report over the whole workshop; no solving needed. Use the
    # steady-state producers (no plan span) so every pet's items are counted.
    if args.unobtainable:
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

    if args.mastery is not None:
        goals = parsing.parse_item_amounts(args.mastery)
        solution = farm_solver.solve_mastery(state, goals)
    elif args.items is not None:
        goals = parsing.parse_item_amounts(args.items)
        solution = farm_solver.solve_acquire(state, goals)
    else:
        requirements = farm_solver.load_tower_requirements(args.tower)
        solution = farm_solver.solve_tower(state, requirements, args.level)

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
    # it (inventory plan mode); the drink-Orange-Juice flow tops it up over time.
    if args.plan_mode == sources.INVENTORY and args.current_stamina:
        inventory["stamina"] = inventory.get("stamina", 0.0) + args.current_stamina
    cookie_sets = cookie_sets_available(inventory, args.cookie_sets)
    # Per-unit gold prices for the tradable consumables (the [trading] config).
    # When present, the frontier trades makespan against real gold and "cheapest"
    # means least gold; absent, the solver stays on its gold-agnostic behaviour.
    prices = config.trading_prices(cfg, crafting_bonus=args.crafting_bonus)

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
        offers = optimizer.all_offers(production, fish, explore, farm, args.plan_mode)
        plans = optimizer.frontier(
            offers, demand, recipes, args.crafting_bonus, args.plans, open_flows,
            inventory, cookie_sets, report._pet_dominated_items(production),
            prices=prices,
        )
        if not plans:
            return 0  # nothing left to source
        # The limits drop plans over a time or consumable ceiling, unless that
        # would leave none (then the whole frontier is shown, with a warning below).
        within = [
            p for p in plans if within_limits(p, time_limit, args.max_ap, args.max_ln)
        ]
        shown = within or plans
        new_horizon = shown[
            selected_plan(shown, args.plan, args.cheapest_plan, prices)
        ].time
        if not model_calendar or (
            horizon is not None and _makespans_close(new_horizon, horizon)
        ):
            break
        horizon = new_horizon

    cheapest = cheapest_plan(shown, prices)
    selected = selected_plan(shown, args.plan, args.cheapest_plan, prices)

    if model_calendar:
        print(
            f"\nStarting {start:%d %b %Y}; a seasonal pet counts only for the "
            "part of the plan it is in season."
        )
    if not within:
        print(f"\n{limit_shortfall(plans, time_limit, args.max_ap, args.max_ln)}")
    # Display names for the items a reason or the plan report may mention.
    display = report.build_display(demand, workshop)
    # The cheapest plan crafts the most, so its passive residuals are each item's
    # full requirement -- the yardstick for whether a resource is at its complete
    # gathering point (see print_critical_path, and the frontier reasons).
    max_passive_residuals = optimizer.passive_residuals(
        plans[-1], demand, recipes, args.crafting_bonus, open_flows, inventory
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
        holdings = {**inventory, "cookie set": cookie_sets}
        golds = [
            optimizer.plan_gold_breakdown(p, prices, holdings) for p in shown
        ]
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
    report.print_plan(
        chosen, demand, farm, workshop, args.crafting_bonus, production,
        open_flows, smith.names, inventory, sourceable_elsewhere,
        max_passive_residuals,
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
