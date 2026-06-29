from farm_solver import (
    GameState,
    Inventory,
    InventoryItem,
    ItemNeed,
    solve_acquire,
)


def make_state(**held: int) -> GameState:
    inventory = Inventory(
        items={
            name.lower(): InventoryItem(name=name, quantity=qty)
            for name, qty in held.items()
        }
    )
    return GameState(inventory=inventory)


def needs_by_item(solution) -> dict[str, ItemNeed]:
    return {need.item: need for need in solution.needs}


def test_extra_needed_counts_existing_inventory():
    state = make_state(Gainite=5)
    solution = solve_acquire(state, {"Gainite": 20})
    need = needs_by_item(solution)["Gainite"]
    assert need.have == 5
    assert need.target == 20
    assert need.extra == 15


def test_item_not_in_inventory_needs_full_target():
    state = make_state()
    solution = solve_acquire(state, {"Flying Machine": 2000})
    assert needs_by_item(solution)["Flying Machine"].extra == 2000


def test_already_satisfied_needs_zero():
    state = make_state(Gainite=50)
    solution = solve_acquire(state, {"Gainite": 20})
    assert needs_by_item(solution)["Gainite"].extra == 0


def test_exactly_met_needs_zero():
    state = make_state(Gainite=20)
    solution = solve_acquire(state, {"Gainite": 20})
    assert needs_by_item(solution)["Gainite"].extra == 0


def test_multiple_goals():
    state = make_state(Gainite=5, Iron=100)
    solution = solve_acquire(state, {"Gainite": 20, "Iron": 80, "Wood": 10})
    needs = needs_by_item(solution)
    assert needs["Gainite"].extra == 15
    assert needs["Iron"].extra == 0
    assert needs["Wood"].extra == 10


def test_a_need_per_goal():
    state = make_state(Gainite=5)
    solution = solve_acquire(state, {"Gainite": 20, "Wood": 10})
    assert len(solution.needs) == 2
