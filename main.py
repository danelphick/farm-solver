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
import sys
from pathlib import Path

import config
import crafting
import farm_solver
import farming
import parsing
import passive
import pets


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
        "-r",
        "--recursive",
        action="store_true",
        help=(
            "expand craftable materials into their own materials, down to the "
            "base ones"
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
        "--passive-limit",
        metavar="DURATION",
        help=(
            "how long to rely on passive production (e.g. '5d', '3h', '1d6h'); "
            "anything it cannot supply in that time is crafted instead. With no "
            "limit, passively-produced items are taken entirely from production."
        ),
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
PETS_CSV = Path(__file__).with_name("pets.csv")


def _csv_items(path: Path) -> set[str]:
    """Lower-cased item names from the ``Item`` column of a drop-rate CSV."""
    if not path.exists():
        return set()
    with path.open(newline="") as f:
        return {row["Item"].strip().lower() for row in csv.DictReader(f)}


def pet_items(cfg: dict) -> set[str]:
    """Lower-cased items the configured pets can fetch at their current level.

    Pet levels come from the ``[pets]`` config section; a pet's unlocked tiers
    depend on that level. Seasonal pets still count -- their items are
    obtainable, just only for part of the year. The crunchy omelette only
    scales quantities, so it does not change which items are obtainable.
    """
    pets_cfg = cfg.get("pets", {})
    if not pets_cfg or not PETS_CSV.exists():
        return set()
    boost = (
        pets.CRUNCHY_OMELETTE_MULTIPLIER
        if pets_cfg.get("use_crunchy_omelette")
        else 1.0
    )
    # Only integer entries are pet levels; flags like use_crunchy_omelette
    # (a bool, which is an int subclass) are excluded.
    levels = {
        name: level
        for name, level in pets_cfg.items()
        if isinstance(level, int) and not isinstance(level, bool)
    }
    return set(pets.load_pets(PETS_CSV, levels, boost).daily_output())


def obtainable_items(production: passive.PassiveProduction, cfg: dict) -> set[str]:
    """Lower-cased items obtainable without crafting.

    Covers passive production, fishing, exploring, farming, pets, and the
    materials the game supplies automatically.
    """
    items = set(crafting.IGNORED_MATERIALS) | set(farming.FARMING_ITEMS)
    for producer in production.producers:
        items |= set(producer.outputs)
    items |= _csv_items(FISHING_CSV)
    items |= _csv_items(EXPLORING_CSV)
    items |= pet_items(cfg)
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


def print_materials(materials: dict[str, int]) -> None:
    if not materials:
        return
    print("\nTotal Materials required:")
    for material in sorted(materials):
        print(f"  {materials[material]:,}x {material}")


def print_breakdown(steps: list[crafting.CraftStep]) -> None:
    if not steps:
        return
    print("\nRequired Crafts:")
    for step in steps:
        print(
            f"\n{step.crafts:,}x {step.item} "
            f"(Produces {step.produced:,}, Adds {step.mastery_added:,} Mastery)"
        )
        for material, quantity in step.materials:
            print(f"  {quantity:,}x {material}")


def print_farming(materials: dict[str, int]) -> None:
    if not materials:
        return
    print("\nFarming required:")
    for material in sorted(materials):
        print(f"  {materials[material]:,}x {material}")


def print_unobtainable(materials: list[str]) -> None:
    print("The following materials are required for some crafts but are unobtainable:")
    if not materials:
        print("  (none)")
        return
    for material in materials:
        print(f"  {material}")


def print_passive(uses: list[passive.PassiveUse]) -> None:
    if not uses:
        return
    print("\nPassive Production required:")
    for use in uses:
        print(
            f"  {use.quantity:,}x {use.item} " f"in {passive.format_duration(use.time)}"
        )
    total = passive.total_time(uses)
    print(f"  Total time: {passive.format_duration(total)} (concurrent)")


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

    # A standalone report over the whole workshop; no solving needed.
    if args.unobtainable:
        obtainable = obtainable_items(production, cfg)
        print_unobtainable(unobtainable_materials(workshop, obtainable))
        return 0

    # The mastery and inventory files come from the command line, falling back
    # to the [solver] config section.
    mastery_file, inventory_file = resolve_files(args, cfg)

    # Parse the passive limit first so a bad duration fails before any output.
    passive_limit = (
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

    if production.producers:
        plan = crafting.craft_plan(
            solution.needs,
            workshop,
            production,
            args.crafting_bonus,
            args.mastery_bonus,
            passive_limit,
        )
        materials = plan.materials if args.recursive else plan.shallow_materials
        materials, farmed = farming.split_farmed(materials)
        print_materials(materials)
        print_farming(farmed)
        print_passive(plan.passive)
        if args.breakdown:
            print_breakdown(plan.steps)
    else:
        materials = crafting.required_materials(
            solution.needs,
            workshop,
            args.crafting_bonus,
            args.mastery_bonus,
            args.recursive,
        )
        materials, farmed = farming.split_farmed(materials)
        print_materials(materials)
        print_farming(farmed)
        if args.breakdown:
            steps = crafting.craft_breakdown(
                solution.needs, workshop, args.crafting_bonus, args.mastery_bonus
            )
            print_breakdown(steps)
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
