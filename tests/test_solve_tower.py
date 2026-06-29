from farm_solver import (
    GameState,
    ItemNeed,
    Mastery,
    TowerRequirements,
    solve_tower,
)


def make_state(**progress: int) -> GameState:
    masteries = {
        name.lower(): Mastery(item=name, progress=prog)
        for name, prog in progress.items()
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


def test_item_needed_at_two_levels_is_listed_for_each():
    # Flying Machine is required at 201 (50k) and 205 (100k); with progress
    # below both, each level is reported separately in tower order.
    state = make_state(**{"Flying Machine": 21953})
    solution = solve_tower(state, REQS, target_level=205)
    flying = [need for need in solution.needs if need.item == "Flying Machine"]
    assert [(n.level, n.target, n.extra) for n in flying] == [
        (201, 50000, 28047),
        (205, 100000, 78047),
    ]


def test_met_level_requirement_dropped_independently():
    # 60k progress satisfies the 201 requirement (50k) but not the 205 one
    # (100k), so only the still-unmet level is listed.
    state = make_state(**{"Flying Machine": 60000})
    solution = solve_tower(state, REQS, target_level=205)
    flying = [need for need in solution.needs if need.item == "Flying Machine"]
    assert [(n.level, n.extra) for n in flying] == [(205, 40000)]


def test_needs_sorted_by_level():
    state = make_state()
    solution = solve_tower(state, REQS)
    # Each (level, item) requirement appears, ordered by level then item.
    assert [(need.level, need.item) for need in solution.needs] == [
        (201, "Flying Machine"),
        (201, "Gainite"),
        (205, "Flying Machine"),
        (210, "Board"),
    ]


def test_mastery_matched_case_insensitively():
    # tower.csv title-cases connector words ("Essence Of Slime") while the
    # mastery screen keeps them lowercase ("Essence of Slime"); a met
    # requirement must still be recognised despite the casing difference.
    reqs = TowerRequirements(levels={298: {"Essence Of Slime": 1_000_000}})
    state = make_state(**{"Essence of Slime": 1_165_352})
    solution = solve_tower(state, reqs)
    assert solution.needs == []
