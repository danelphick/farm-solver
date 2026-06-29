from farm_solver import (
    GameState,
    ItemNeed,
    Mastery,
    TowerRequirements,
    solve_tower,
)


def make_state(**progress: int) -> GameState:
    masteries = {
        name: Mastery(item=name, progress=prog) for name, prog in progress.items()
    }
    return GameState(masteries=masteries)


REQS = TowerRequirements(
    levels={
        201: {"Flying Machine": 50000, "Gainite": 10000},
        205: {"Flying Machine": 100000},
        210: {"Board": 1_000_000},
    }
)


def needs_by_item(solution) -> dict[str, ItemNeed]:
    return {need.item: need for need in solution.needs}


def test_reports_shortfall_against_current_mastery():
    state = make_state(**{"Flying Machine": 21953, "Gainite": 10000})
    solution = solve_tower(state, REQS, target_level=205)
    needs = needs_by_item(solution)
    assert needs["Flying Machine"].have == 21953
    assert needs["Flying Machine"].target == 100000
    assert needs["Flying Machine"].extra == 78047


def test_completed_items_are_not_reported():
    # Gainite requirement (10000) is fully met, so it must be omitted entirely.
    state = make_state(**{"Flying Machine": 21953, "Gainite": 10000})
    solution = solve_tower(state, REQS, target_level=205)
    assert "Gainite" not in needs_by_item(solution)


def test_item_with_no_mastery_needs_full_requirement():
    state = make_state()
    solution = solve_tower(state, REQS, target_level=201)
    assert needs_by_item(solution)["Gainite"].extra == 10000


def test_target_level_limits_requirements():
    state = make_state()
    solution = solve_tower(state, REQS, target_level=201)
    needs = needs_by_item(solution)
    # Board only appears at level 210, so it is excluded for a level-201 goal.
    assert "Board" not in needs
    assert needs["Flying Machine"].target == 50000


def test_default_target_is_highest_level():
    state = make_state()
    solution = solve_tower(state, REQS)  # no target_level -> max (210)
    assert "Board" in needs_by_item(solution)


def test_all_requirements_met_yields_no_needs():
    state = make_state(
        **{"Flying Machine": 100000, "Gainite": 10000, "Board": 1_000_000}
    )
    solution = solve_tower(state, REQS)
    assert solution.needs == []


def test_needs_sorted_by_item():
    state = make_state()
    solution = solve_tower(state, REQS)
    items = [need.item for need in solution.needs]
    assert items == sorted(items)
