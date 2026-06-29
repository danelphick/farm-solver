from farm_solver import GameState, ItemNeed, Mastery, solve_mastery


def make_state(**progress: int) -> GameState:
    masteries = {
        name: Mastery(item=name, progress=prog) for name, prog in progress.items()
    }
    return GameState(masteries=masteries)


def needs_by_item(solution) -> dict[str, ItemNeed]:
    return {need.item: need for need in solution.needs}


def test_extra_needed_counts_current_progress():
    state = make_state(**{"Flying Machine": 40000})
    solution = solve_mastery(state, {"Flying Machine": 100000})
    need = needs_by_item(solution)["Flying Machine"]
    assert need.have == 40000
    assert need.target == 100000
    assert need.extra == 60000


def test_item_with_no_mastery_needs_full_target():
    state = make_state()
    solution = solve_mastery(state, {"Gainite": 100000})
    assert needs_by_item(solution)["Gainite"].extra == 100000


def test_already_mastered_needs_zero():
    state = make_state(Board=832_681_556)
    solution = solve_mastery(state, {"Board": 100000})
    assert needs_by_item(solution)["Board"].extra == 0


def test_exactly_met_needs_zero():
    state = make_state(Gainite=100000)
    solution = solve_mastery(state, {"Gainite": 100000})
    assert needs_by_item(solution)["Gainite"].extra == 0


def test_multiple_goals():
    state = make_state(**{"Flying Machine": 40000, "Gainite": 100000})
    solution = solve_mastery(
        state, {"Flying Machine": 100000, "Gainite": 100000, "Wood": 10000}
    )
    needs = needs_by_item(solution)
    assert needs["Flying Machine"].extra == 60000
    assert needs["Gainite"].extra == 0
    assert needs["Wood"].extra == 10000


def test_a_need_per_goal():
    state = make_state(Gainite=5)
    solution = solve_mastery(state, {"Gainite": 100000, "Wood": 10000})
    assert len(solution.needs) == 2
