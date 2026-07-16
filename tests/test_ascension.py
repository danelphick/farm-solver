from datetime import date

import pytest

from ascension import (
    AK_PER_LEVEL,
    CHORE_BONUS_DAYS,
    GRANDMASTER_THRESHOLD,
    MASTER_THRESHOLD,
    MEGA_THRESHOLD,
    ORCHARD_TREES_L60,
    ORCHARD_TREES_L110,
    REWARD_LEVELS,
    STOREHOUSE_BONUS,
    AscensionOutcome,
    advance,
    ak_cost,
    apply_rewards,
    candidates,
    chore_ak,
    chore_rate_per_day,
    mega_count,
    mega_remaining,
    mega_stage_floors,
    megas_required_by,
    schedule_megas,
    stage_bounds,
)
from farm_solver import GameState, Mastery


# --- ak_cost -----------------------------------------------------------


def test_ak_cost_normal():
    assert ak_cost(0, 1) == AK_PER_LEVEL
    assert ak_cost(50, 60) == 10 * AK_PER_LEVEL
    assert ak_cost(0, 200) == 200 * AK_PER_LEVEL


def test_ak_cost_rejects_current_at_or_above_target():
    with pytest.raises(ValueError):
        ak_cost(10, 10)
    with pytest.raises(ValueError):
        ak_cost(10, 5)


def test_ak_cost_rejects_target_above_max():
    with pytest.raises(ValueError):
        ak_cost(0, 201)


def test_ak_cost_rejects_negative_current():
    with pytest.raises(ValueError):
        ak_cost(-1, 10)


# --- megas_required_by ---------------------------------------------------


def test_megas_required_by_pins():
    assert megas_required_by(100) == 0
    assert megas_required_by(101) == 1
    assert megas_required_by(104) == 1
    assert megas_required_by(105) == 2
    assert megas_required_by(200) == 25


def test_megas_required_by_below_100_is_zero():
    assert megas_required_by(1) == 0
    assert megas_required_by(0) == 0


# --- chore_rate_per_day ----------------------------------------------------


def test_chore_rate_per_day_approx():
    assert chore_rate_per_day() == pytest.approx(2.6098, abs=1e-4)


# --- chore_ak --------------------------------------------------------------


def test_chore_ak_zero_length_window_is_zero():
    assert chore_ak(date(2026, 1, 7), 0) == 0


def test_chore_ak_fractional_pro_rata_no_bonus():
    # Jan 1 is not a bonus date, so this is purely the continuous daily rate.
    assert chore_ak(date(2026, 1, 1), 0.5) == pytest.approx(0.5)


def test_chore_ak_window_containing_one_bonus_day():
    # Feb 1..Feb 10 (10 days starting Feb 1) contains the 7th only.
    total = chore_ak(date(2026, 2, 1), 10)
    assert total == pytest.approx(10 + CHORE_BONUS_DAYS[7])


def test_chore_ak_window_containing_all_three_bonus_days():
    # Feb 1..Feb 28 (28 days starting Feb 1) contains the 7th, 14th and 28th.
    total = chore_ak(date(2026, 2, 1), 28)
    assert total == pytest.approx(28 + 7 + 14 + 28)


def test_chore_ak_straddles_month_boundary():
    # Jan 25 + 10 days reaches Feb 3 (window [Jan25, Feb4)): catches Jan 28's
    # +28, but Feb 7 is outside the window entirely.
    total = chore_ak(date(2026, 1, 25), 10)
    assert total == pytest.approx(10 + 28)


def test_chore_ak_bonus_lands_the_instant_its_date_starts():
    # A window opening exactly on a bonus date earns it in full even though
    # the window barely covers any of that day.
    total = chore_ak(date(2026, 1, 7), 0.001)
    assert total == pytest.approx(0.001 + 7)


def test_chore_ak_partial_trailing_day_can_still_earn_its_bonus():
    # The window covers all of Jan 6 and a bit of Jan 7 -- Jan 7's bonus
    # lands anyway, since it only needs the date to have started.
    total = chore_ak(date(2026, 1, 6), 1.5)
    assert total == pytest.approx(1.5 + 7)


def test_chore_ak_trailing_day_excluded_when_window_ends_exactly_at_its_start():
    # The window is [Jan6, Jan7) -- it ends exactly as Jan 7 begins, so Jan
    # 7's bonus is not earned.
    total = chore_ak(date(2026, 1, 6), 1)
    assert total == pytest.approx(1)


def test_chore_ak_rejects_negative_days():
    with pytest.raises(ValueError):
        chore_ak(date(2026, 1, 1), -1)


# --- candidates --------------------------------------------------------


def test_candidates_fresh_item_has_all_three_deltas():
    state = GameState(
        masteries={"gainite": Mastery(item="Gainite", progress=0, required=10)}
    )
    [c] = candidates(state)
    assert c.item == "gainite"
    assert c.delta_master == MASTER_THRESHOLD
    assert c.delta_grandmaster == GRANDMASTER_THRESHOLD - MASTER_THRESHOLD
    assert c.delta_mega == MEGA_THRESHOLD - GRANDMASTER_THRESHOLD


def test_candidates_mastered_item_has_no_master_delta():
    # Per the delta_mega formula (MEGA_THRESHOLD - max(p, GRANDMASTER_THRESHOLD)),
    # the mega leg's cost is fixed at 900_000 for any p <= GRANDMASTER_THRESHOLD --
    # it is the cost of the grandmaster->mega leg specifically, not the raw
    # distance from p to MEGA_THRESHOLD.
    state = GameState(
        masteries={
            "gainite": Mastery(item="Gainite", progress=40_000, required=100_000)
        }
    )
    [c] = candidates(state)
    assert c.delta_master is None
    assert c.delta_grandmaster == 60_000
    assert c.delta_mega == 900_000


def test_candidates_grandmastered_item_has_only_mega_delta():
    state = GameState(
        masteries={
            "gainite": Mastery(item="Gainite", progress=100_000, required=1_000_000)
        }
    )
    [c] = candidates(state)
    assert c.delta_master is None
    assert c.delta_grandmaster is None
    assert c.delta_mega == 900_000


def test_candidates_fully_mega_item_is_omitted():
    state = GameState(
        masteries={
            "gainite": Mastery(item="Gainite", progress=5_000_000, required=None)
        }
    )
    assert candidates(state) == []


def test_candidates_sorted_by_item_name():
    state = GameState(
        masteries={
            "zebra fruit": Mastery(item="Zebra Fruit", progress=0, required=10),
            "apple": Mastery(item="Apple", progress=0, required=10),
        }
    )
    items = [c.item for c in candidates(state)]
    assert items == ["apple", "zebra fruit"]


def test_candidates_catalogue_adds_fresh_items_at_progress_zero():
    # The paste only lists items with progress; the catalogue supplies the
    # rest as fresh candidates with the full three steps.
    state = GameState(
        masteries={"apple": Mastery(item="Apple", progress=4_000, required=10_000)}
    )
    result = candidates(state, catalogue=["Apple", "Zebra Fruit"])
    assert [c.item for c in result] == ["apple", "zebra fruit"]
    apple, zebra = result
    # The paste's progress wins for an item both know.
    assert apple.delta_master == 6_000
    assert (zebra.delta_master, zebra.delta_grandmaster, zebra.delta_mega) == (
        10_000, 90_000, 900_000,
    )


def test_candidates_catalogue_never_resurrects_a_mega_item():
    state = GameState(
        masteries={"gainite": Mastery(item="Gainite", progress=5_000_000, required=None)}
    )
    assert candidates(state, catalogue=["Gainite"]) == []


# --- mega_count ----------------------------------------------------------


def test_mega_count():
    state = GameState(
        masteries={
            "a": Mastery(item="A", progress=5_000_000, required=None),
            "b": Mastery(item="B", progress=5_000_000, required=None),
            "c": Mastery(item="C", progress=100, required=1_000),
        }
    )
    assert mega_count(state) == 2


# --- mega_remaining (milestone 6) -------------------------------------------


def test_mega_remaining_from_below_grandmaster():
    # Below GRANDMASTER_THRESHOLD, the remaining amount is measured from the
    # threshold itself (matches candidates()' delta_mega convention).
    masteries = {"x": Mastery("x", progress=0, required=10_000)}
    assert mega_remaining(masteries, "x") == pytest.approx(
        MEGA_THRESHOLD - GRANDMASTER_THRESHOLD
    )


def test_mega_remaining_from_above_grandmaster():
    masteries = {"x": Mastery("x", progress=150_000, required=1_000_000)}
    assert mega_remaining(masteries, "x") == pytest.approx(MEGA_THRESHOLD - 150_000)


def test_mega_remaining_zero_once_mega():
    masteries = {"x": Mastery("x", progress=0, required=None)}
    assert mega_remaining(masteries, "x") == 0.0


def test_mega_remaining_unknown_item_defaults_to_full_threshold():
    assert mega_remaining({}, "unseen") == float(MEGA_THRESHOLD)


def test_mega_remaining_is_case_insensitive():
    masteries = {"gold rose": Mastery("Gold Rose", progress=200_000, required=1_000_000)}
    assert mega_remaining(masteries, "Gold Rose") == pytest.approx(MEGA_THRESHOLD - 200_000)


# --- mega_stage_floors (milestone 6) ----------------------------------------


def test_mega_stage_floors_splits_evenly_across_stages_to_the_due_stage():
    schedule = {"corn": 2}  # due at stage index 2
    masteries = {"corn": Mastery("corn", progress=100_000, required=1_000_000)}
    floors = mega_stage_floors(masteries, schedule, 0)
    # 3 stages remain (0, 1, 2) from stage 0 to the due stage inclusive.
    assert floors == {"corn": pytest.approx((MEGA_THRESHOLD - 100_000) / 3)}


def test_mega_stage_floors_due_stage_is_the_full_remaining_amount():
    schedule = {"corn": 1}
    masteries = {"corn": Mastery("corn", progress=850_000, required=1_000_000)}
    floors = mega_stage_floors(masteries, schedule, 1)
    assert floors == {"corn": pytest.approx(150_000)}


def test_mega_stage_floors_shrinks_after_overproduction():
    schedule = {"corn": 2}
    floors0 = mega_stage_floors(
        {"corn": Mastery("corn", progress=100_000, required=1_000_000)}, schedule, 0
    )
    assert floors0["corn"] == pytest.approx((MEGA_THRESHOLD - 100_000) / 3)

    # Stage 0 overshoots the floor (produces well past what it alone forced) --
    # stage 1's floor recomputes off the new, higher progress and shrinks, both
    # because less remains and because fewer stages are left to spread it over.
    floors1 = mega_stage_floors(
        {"corn": Mastery("corn", progress=700_000, required=1_000_000)}, schedule, 1
    )
    assert floors1["corn"] == pytest.approx((MEGA_THRESHOLD - 700_000) / 2)
    assert floors1["corn"] < floors0["corn"]


def test_mega_stage_floors_omits_items_not_due_yet_or_already_mega():
    schedule = {"corn": 3, "wheat": 1}
    masteries = {
        "corn": Mastery("corn", progress=100_000, required=1_000_000),
        "wheat": Mastery("wheat", progress=0, required=None),  # already mega'd
    }
    floors = mega_stage_floors(masteries, schedule, 2)
    assert "wheat" not in floors  # nothing left to force
    assert "corn" in floors  # due=3 >= stage_index=2, still due


def test_mega_stage_floors_drops_items_whose_due_stage_has_passed():
    schedule = {"corn": 1}
    masteries = {"corn": Mastery("corn", progress=500_000, required=1_000_000)}
    assert mega_stage_floors(masteries, schedule, 2) == {}


# --- schedule_megas (milestone 6) -------------------------------------------


def test_schedule_megas_assigns_due_stages_by_cumulative_threshold():
    # 100 -> 112, one stage per level (12 stages); 3 megas needed overall
    # (megas_required_by(112) == 3). Cheapest item gets the earliest deadline.
    bounds = [(100 + i, 101 + i) for i in range(12)]
    chosen = ["c", "b", "a"]
    per_item_remaining = {"a": 100.0, "b": 200.0, "c": 300.0}
    due = schedule_megas(chosen, bounds, already=0, per_item_remaining=per_item_remaining)
    # cumulative 1 -> first stage where megas_required_by(b) >= 1: level 101 (k=0).
    # cumulative 2 -> level 105 (k=4). cumulative 3 -> level 109 (k=8).
    assert due == {"a": 0, "b": 4, "c": 8}


def test_schedule_megas_already_offsets_the_cumulative_threshold():
    bounds = [(100 + i, 101 + i) for i in range(12)]
    chosen = ["a", "b"]
    per_item_remaining = {"a": 1.0, "b": 2.0}
    due = schedule_megas(chosen, bounds, already=1, per_item_remaining=per_item_remaining)
    # already=1 pre-owned mega: only 2 more needed by 112 (3 - 1).
    # a (cumulative 1 new = absolute 2): first k where megas_required_by(b)-1>=1
    #   -> level 105 (megas_required_by(105)=2), k=4.
    # b (cumulative 2 new = absolute 3): level 109 (megas_required_by(109)=3), k=8.
    assert due == {"a": 4, "b": 8}


def test_schedule_megas_ties_break_by_item_name():
    bounds = [(100, 105)]  # megas_required_by(105) == 2
    due = schedule_megas(
        ["b", "a"], bounds, already=0, per_item_remaining={"a": 5.0, "b": 5.0}
    )
    # Equal remaining supply: "a" sorts first alphabetically, gets the earlier
    # (only, here) due stage tied with "b" -- both land on stage 0 regardless,
    # so this mainly pins down determinism of the ordering itself.
    assert due == {"a": 0, "b": 0}


def test_schedule_megas_raises_when_chosen_count_does_not_match_required():
    bounds = [(100, 112)]  # megas_required_by(112) == 3
    with pytest.raises(ValueError, match="schedule_megas"):
        schedule_megas(["a"], bounds, already=0, per_item_remaining={"a": 1.0})
    with pytest.raises(ValueError):
        schedule_megas(
            ["a", "b", "c", "d"], bounds, already=0,
            per_item_remaining={"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
        )


# --- advance ---------------------------------------------------------------


def test_advance_crosses_one_threshold():
    masteries = {"gainite": Mastery(item="Gainite", progress=9_000, required=10_000)}
    outcome = AscensionOutcome(
        masters=[], grandmasters=[], megas=[],
        ak_from_steps=0, ak_from_chores=0,
        mastery_supply={"gainite": 2_000},
    )
    new = advance(masteries, outcome)
    assert new["gainite"].progress == 11_000
    assert new["gainite"].required == 100_000


def test_advance_crosses_two_thresholds():
    masteries = {"gainite": Mastery(item="Gainite", progress=90, required=100)}
    outcome = AscensionOutcome(
        masters=[], grandmasters=[], megas=[],
        ak_from_steps=0, ak_from_chores=0,
        mastery_supply={"gainite": 9_920},
    )
    new = advance(masteries, outcome)
    assert new["gainite"].progress == 10_010
    assert new["gainite"].required == 100_000


def test_advance_reaching_mega_sets_required_none():
    masteries = {
        "gainite": Mastery(item="Gainite", progress=900_000, required=1_000_000)
    }
    outcome = AscensionOutcome(
        masters=[], grandmasters=[], megas=[],
        ak_from_steps=0, ak_from_chores=0,
        mastery_supply={"gainite": 200_000},
    )
    new = advance(masteries, outcome)
    assert new["gainite"].progress == 1_100_000
    assert new["gainite"].required is None


def test_advance_does_not_mutate_input():
    masteries = {"gainite": Mastery(item="Gainite", progress=9_000, required=10_000)}
    outcome = AscensionOutcome(
        masters=[], grandmasters=[], megas=[],
        ak_from_steps=0, ak_from_chores=0,
        mastery_supply={"gainite": 2_000},
    )
    new = advance(masteries, outcome)
    assert masteries["gainite"].progress == 9_000
    assert new is not masteries
    assert new["gainite"] is not masteries["gainite"]


def test_advance_creates_fresh_item():
    outcome = AscensionOutcome(
        masters=[], grandmasters=[], megas=[],
        ak_from_steps=0, ak_from_chores=0,
        mastery_supply={"Gainite": 5},
    )
    new = advance({}, outcome)
    assert new["gainite"].progress == 5
    assert new["gainite"].required == 10
    assert new["gainite"].item == "Gainite"


# --- stage_bounds ----------------------------------------------------------


def test_stage_bounds_full_climb():
    assert stage_bounds(0, 200) == [
        (0, 10), (10, 60), (60, 70), (70, 90), (90, 100),
        (100, 110), (110, 150), (150, 160), (160, 170), (170, 200),
    ]


def test_stage_bounds_partial_climb():
    assert stage_bounds(85, 95) == [(85, 90), (90, 95)]


def test_stage_bounds_single_stage_no_threshold_crossed():
    assert stage_bounds(60, 70) == [(60, 70)]


def test_stage_bounds_rejects_current_at_or_above_target():
    with pytest.raises(ValueError):
        stage_bounds(10, 10)


def test_stage_bounds_rejects_target_above_max():
    with pytest.raises(ValueError):
        stage_bounds(0, 201)


def test_stage_bounds_rejects_negative_current():
    with pytest.raises(ValueError):
        stage_bounds(-1, 10)


# --- apply_rewards -----------------------------------------------------


def test_apply_rewards_85_to_110():
    cfg = {"farming": {"speed_reduction": 90}}
    new_cfg = apply_rewards(cfg, 85, 110)
    # Resource Saver 3 (level 90) has no direct config delta.
    assert new_cfg["farming"]["speed_reduction"] == 90
    assert new_cfg["storehouse"]["inventory"] == STOREHOUSE_BONUS
    assert new_cfg["orchard"]["apple_trees"] == ORCHARD_TREES_L110
    assert new_cfg["orchard"]["orange_trees"] == ORCHARD_TREES_L110
    assert new_cfg["orchard"]["lemon_trees"] == ORCHARD_TREES_L110
    assert new_cfg["tower"]["level"] == 110
    # Input untouched.
    assert cfg == {"farming": {"speed_reduction": 90}}


def test_apply_rewards_0_to_10_bumps_speed_reduction():
    cfg = {"farming": {"speed_reduction": 90}}
    new_cfg = apply_rewards(cfg, 0, 10)
    assert new_cfg["farming"]["speed_reduction"] == 100
    assert new_cfg["tower"]["level"] == 10
    assert cfg["farming"]["speed_reduction"] == 90


def test_apply_rewards_60_to_70_sets_fishing_trawl_only():
    cfg = {}
    new_cfg = apply_rewards(cfg, 60, 70)
    assert new_cfg["fishing"]["have_fishing_trawl"] is True
    assert new_cfg["tower"]["level"] == 70
    assert "orchard" not in new_cfg
    assert "storehouse" not in new_cfg
    assert "farming" not in new_cfg
    assert cfg == {}


def test_apply_rewards_60_crosses_orchard_trees():
    new_cfg = apply_rewards({}, 50, 60)
    assert new_cfg["orchard"]["apple_trees"] == ORCHARD_TREES_L60
    assert new_cfg["orchard"]["orange_trees"] == ORCHARD_TREES_L60
    assert new_cfg["orchard"]["lemon_trees"] == ORCHARD_TREES_L60


def test_apply_rewards_additive_on_existing_trees():
    cfg = {"orchard": {"apple_trees": 100, "orange_trees": 200, "lemon_trees": 300}}
    new_cfg = apply_rewards(cfg, 50, 60)
    assert new_cfg["orchard"]["apple_trees"] == 100 + ORCHARD_TREES_L60
    assert new_cfg["orchard"]["orange_trees"] == 200 + ORCHARD_TREES_L60
    assert new_cfg["orchard"]["lemon_trees"] == 300 + ORCHARD_TREES_L60


def test_apply_rewards_160_and_170():
    new_cfg = apply_rewards({}, 155, 175)
    assert new_cfg["raptor_pen"]["have_antler_snare"] is True
    assert new_cfg["orchard"]["have_tree_shaker"] is True
    assert new_cfg["tower"]["level"] == 175


def test_apply_rewards_200_storehouse_bonus():
    new_cfg = apply_rewards({}, 190, 200)
    assert new_cfg["storehouse"]["inventory"] == STOREHOUSE_BONUS
    assert new_cfg["tower"]["level"] == 200


def test_apply_rewards_rejects_current_at_or_above_target():
    with pytest.raises(ValueError):
        apply_rewards({}, 10, 10)


# --- REWARD_LEVELS sanity ------------------------------------------------


def test_reward_levels_known_keys():
    assert set(REWARD_LEVELS) == {10, 60, 70, 90, 100, 110, 150, 160, 170, 200}
