from farm_solver import GameState, Mastery, TowerRequirements, solve_target


def make_state(**progress: int) -> GameState:
    masteries = {
        name.lower(): Mastery(item=name, progress=prog)
        for name, prog in progress.items()
    }
    return GameState(masteries=masteries)


REQS = TowerRequirements(
    levels={
        201: {"Flying Machine": 50000},
        210: {"Grand Piano": 500_000},
    }
)


def needs_by_item(solution):
    result = {}
    for need in solution.needs:
        result.setdefault(need.item, []).append(need)
    return result


def test_with_no_extra_goals_matches_solve_tower():
    state = make_state()
    solution = solve_target(state, REQS)
    needs = needs_by_item(solution)
    assert set(needs) == {"Flying Machine", "Grand Piano"}


def test_extra_mastery_goal_is_added_alongside_tower_needs():
    state = make_state()
    solution = solve_target(
        state, REQS, mastery_goals={"Grand Piano": 1_000_000}
    )
    needs = needs_by_item(solution)["Grand Piano"]
    # Two separate ItemNeed entries -- the tower's own and the extra -m goal;
    # crafting.top_level_demand is what reduces them to one figure.
    assert [n.target for n in needs] == [500_000, 1_000_000]


def test_extra_item_goal_is_added_alongside_tower_needs():
    state = make_state()
    solution = solve_target(state, REQS, item_goals={"Steel Wire": 50})
    needs = needs_by_item(solution)
    assert needs["Steel Wire"][0].target == 50
    assert needs["Steel Wire"][0].is_mastery is False


def test_target_level_limits_the_tower_needs():
    state = make_state()
    solution = solve_target(state, REQS, target_level=201)
    assert "Grand Piano" not in needs_by_item(solution)


def test_no_extra_goals_given_omits_them_entirely():
    state = make_state()
    solution = solve_target(state, REQS, mastery_goals=None, item_goals=None)
    assert all(need.level is not None for need in solution.needs)
