"""Command-line entry point for Farm Solver.

See README.md for usage. In short::

    python main.py [-t <tower-requirements>] [-l level] \
        [-i item:amount] [-m item:amount] \
        [--mastery-file <masteries>] [--inventory-file <inventory>]

The mastery and inventory files default to the ``[solver]`` config section.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import timedelta
from pathlib import Path

import config
import crafting
import crops
import exploring
import farm_solver
import farming
import fishing
import optimizer
import parsing
import passive
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
            "plans slower than this are dropped from the frontier. With no limit "
            "the whole frontier is shown, however long the slowest plan takes."
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

    # Which frontier plan to expand in full: the cheapest (fewest Arnold Palmers
    # and Large Nets, the default) or a specific one by its number in the table.
    plan = parser.add_mutually_exclusive_group()
    plan.add_argument(
        "--cheapest-plan",
        action="store_true",
        help=(
            "expand the plan spending the fewest Arnold Palmers and Large Nets "
            "(the default)"
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


def cheapest_plan(plans: list[optimizer.SourcingPlan]) -> int:
    """Index of the plan spending the fewest consumables (ties: least time)."""
    return min(
        range(len(plans)),
        key=lambda i: (
            plans[i].arnold_palmers + plans[i].large_nets,
            plans[i].time.total_seconds(),
        ),
    )


def print_frontier(
    plans: list[optimizer.SourcingPlan], selected: int, cheapest: int
) -> None:
    """The frontier table: each plan's time and consumables, fastest first."""
    print("\nSourcing plans (time / Arnold Palmers / Large Nets):")
    for i, plan in enumerate(plans):
        marker = ">" if i == selected else " "
        tags = []
        if i == 0:
            tags.append("fastest")
        if i == cheapest:
            tags.append("cheapest")
        note = f"  [{', '.join(tags)}]" if tags else ""
        print(
            f" {marker} {i + 1}. {passive.format_duration(plan.time)}"
            f" / {round(plan.arnold_palmers):,} AP"
            f" / {round(plan.large_nets):,} LN{note}"
        )


def print_plan(
    plan: optimizer.SourcingPlan,
    demand: dict[str, float],
    farm: crops.Farm,
    workshop: crafting.Workshop,
    crafting_bonus: float,
) -> None:
    """Expand one plan into its per-channel actions and leftover materials."""
    # Display names for everything that can appear (demand plus every recipe item
    # and material), keyed lower-cased.
    display = {item.lower(): item for item in demand}
    for key, recipe in workshop.recipes.items():
        display.setdefault(key, recipe.name)
        for material in recipe.materials:
            display.setdefault(material.lower(), material)

    # What the plan actually needs of each item: the direct demand plus whatever
    # the chosen crafts consume as materials.
    need: dict[str, float] = {item.lower(): float(qty) for item, qty in demand.items()}
    for item, count in plan.crafts.items():
        recipe = workshop.get(item)
        if recipe is None:
            continue
        for material, quantity in recipe.materials.items():
            key = material.lower()
            if key not in crafting.IGNORED_MATERIALS:
                need[key] = need.get(key, 0.0) + quantity * count
    # What crafting itself produces (crafting_bonus per craft).
    crafted = {item: crafting_bonus * count for item, count in plan.crafts.items()}
    interesting = set(need) | set(crafted)

    def wanted(offer: sources.Offer) -> str:
        return ", ".join(
            sorted(display.get(key, key) for key in offer.yields if key in interesting)
        )

    def draws_on(track: str) -> list[sources.Draw]:
        return [d for d in plan.draws if d.offer.track == track]

    fishing_draws = draws_on(sources.FISHING_TRACK)
    if fishing_draws:
        print("\nFishing:")
        for draw in fishing_draws:
            zone = draw.offer.name.removeprefix("fish ")
            print(
                f"  {math.ceil(draw.units):,}x Large Net at {zone}"
                f" ({wanted(draw.offer)})"
            )

    exploring_draws = draws_on(sources.EXPLORING_TRACK)
    if exploring_draws:
        print("\nExploring:")
        for draw in exploring_draws:
            location = draw.offer.name.removeprefix("explore ")
            print(
                f"  {math.ceil(draw.units):,}x Arnold Palmer at {location}"
                f" ({wanted(draw.offer)})"
            )

    farming_draws = draws_on(sources.FARMING_TRACK)
    if farming_draws:
        print("\nFarming:")
        for draw in farming_draws:
            crop = draw.offer.name.removeprefix("grow ")
            line = (
                f"  {math.ceil(draw.units):,} plantings of {crop}"
                f" in {passive.format_duration(draw.time)}"
            )
            juices = farm.grape_juices_used(draw.time)
            if round(juices) >= 1:
                line += f" using {round(juices):,} Grape Juice"
            print(f"{line} ({wanted(draw.offer)})")

    passive_draws = draws_on(sources.PASSIVE_TRACK)
    if passive_draws:
        offer = passive_draws[0].offer
        # Passive only covers what the other channels and crafting did not, so
        # subtract their output first -- otherwise an item fully fished, explored
        # or crafted would still show its full passive wait. Each item's own wait
        # is its residual over its per-minute rate; the longest is the whole
        # track's time, so listing them longest-first shows what drives it.
        elsewhere: dict[str, float] = dict(crafted)
        for other in plan.draws:
            if other.offer.track == sources.PASSIVE_TRACK:
                continue
            for item, quantity in other.produced.items():
                elsewhere[item] = elsewhere.get(item, 0.0) + quantity
        rows = []
        for key, rate in offer.yields.items():
            if rate <= 0:
                continue
            residual = need.get(key, 0.0) - elsewhere.get(key, 0.0)
            if residual > 1e-6:
                wait = timedelta(minutes=residual / rate)
                rows.append((wait, display.get(key, key), round(residual)))
        if rows:
            print("\nPassive production (concurrent):")
            for wait, name, quantity in sorted(rows, reverse=True):
                print(f"  {quantity:,}x {name} in {passive.format_duration(wait)}")

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


def load_config() -> dict:
    """Read the config file, or an empty config if there is none."""
    try:
        return config.load_config()
    except FileNotFoundError:
        return {}


def run(args: argparse.Namespace) -> int:
    cfg = load_config()
    workshop = crafting.load_workshop()
    production = config.passive_production(cfg)
    farm = crops.farm_from_config(cfg.get("farming", {}))
    fish = fishing.fishing_from_config(cfg.get("fishing", {}))
    explore = exploring.exploring_from_config(cfg.get("exploring", {}))

    # A standalone report over the whole workshop; no solving needed.
    if args.unobtainable:
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

    # Every channel is an Offer and every recipe a craft flow; the optimiser
    # sources the top-level demand, choosing craft-vs-gather for itself.
    offers = optimizer.all_offers(production, fish, explore, farm)
    recipes = crafting.recipe_flows(workshop)
    demand = crafting.top_level_demand(solution.needs, args.mastery_bonus)

    if args.plans < 1:
        raise ValueError("--plans must be at least 1")
    plans = optimizer.frontier(offers, demand, recipes, args.crafting_bonus, args.plans)
    if not plans:
        return 0  # nothing left to source

    # The limits drop plans that exceed a time or consumable ceiling, unless that
    # would leave none (then the whole frontier is shown).
    shown = [
        p for p in plans if within_limits(p, time_limit, args.max_ap, args.max_ln)
    ] or plans

    cheapest = cheapest_plan(shown)
    if args.plan is not None:
        if not 1 <= args.plan <= len(shown):
            raise ValueError(f"--plan must be between 1 and {len(shown)}")
        selected = args.plan - 1
    else:
        selected = cheapest

    print_frontier(shown, selected, cheapest)
    chosen = shown[selected]
    solution.arnold_palmers = round(chosen.arnold_palmers)
    solution.large_nets = round(chosen.large_nets)
    print_plan(chosen, demand, farm, workshop, args.crafting_bonus)

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
