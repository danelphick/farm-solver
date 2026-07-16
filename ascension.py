"""Ascension: climbing the tower by accumulating Ascension Knowledge (AK).

Climbing from tower level ``L`` to ``L+1`` costs :data:`AK_PER_LEVEL` (100) AK.
AK is earned from three sources: mastering an item (reaching 10,000 progress)
pays :data:`AK_PER_MASTER` (10), grandmastering it (100,000) pays a further
:data:`AK_PER_GRANDMASTER` (100), and daily chores pay +1/day plus a bonus on
the 7th/14th/28th of each calendar month (see :func:`chore_ak`). Levels
101..200 additionally require a cumulative count of megamasteries (reaching
1,000,000 progress on an item -- see :func:`megas_required_by`).

This module is the pure-domain arithmetic behind the "ascension" solver mode:
AK/chore/mega bookkeeping, candidate mastery steps, splitting a climb into
stages at the levels that grant a reward (:func:`stage_bounds`), and applying
those rewards to a config (:func:`apply_rewards`). It knows nothing about the
LP or CLI -- it imports only :mod:`farm_solver` types and the standard
library. Milestone 1's ``config.current_tower_level``/``crafting_bonus_for``/
``effective_grape_juices`` are how the level-90 (Resource Saver 3) and
level-150 (Grape Juice doubling) rewards actually take effect once
``apply_rewards`` sets ``[tower] level``; later milestones wire this module
into the optimiser (:class:`AscensionSpec`/:class:`AscensionOutcome` are that
handoff, defined here but consumed there).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from farm_solver import GameState, Mastery

# Mastery thresholds (FarmRPG's tier denominators -- see Mastery.tier).
MASTER_THRESHOLD = 10_000
GRANDMASTER_THRESHOLD = 100_000
MEGA_THRESHOLD = 1_000_000

# All the power-of-ten thresholds a mastery can sit at, smallest first.
_THRESHOLDS: tuple[int, ...] = (10, 100, 1_000, 10_000, 100_000, 1_000_000)

AK_PER_MASTER = 10
AK_PER_GRANDMASTER = 100
AK_PER_LEVEL = 100
MAX_ASCEND_LEVEL = 200

# Day-of-month -> bonus AK the daily chore earns on top of its usual +1.
CHORE_BONUS_DAYS: dict[int, int] = {7: 7, 14: 14, 28: 28}

# Orchard tree-count rewards (added to EACH of apple/orange/lemon trees).
ORCHARD_TREES_L60 = 400
ORCHARD_TREES_L110 = 1500

# Storehouse capacity reward, granted at both level 100 and level 200.
STOREHOUSE_BONUS = 2500


def ak_cost(current_level: int, target_level: int) -> int:
    """AK required to climb from ``current_level`` to ``target_level``.

    Flat :data:`AK_PER_LEVEL` (100) per level climbed. Raises ``ValueError``
    unless ``0 <= current_level < target_level <= MAX_ASCEND_LEVEL``.
    """
    if not (0 <= current_level < target_level <= MAX_ASCEND_LEVEL):
        raise ValueError(
            "ak_cost requires 0 <= current_level < target_level <= "
            f"{MAX_ASCEND_LEVEL}, not current_level={current_level!r}, "
            f"target_level={target_level!r}"
        )
    return AK_PER_LEVEL * (target_level - current_level)


def megas_required_by(level: int) -> int:
    """Cumulative megamasteries required by the time ``level`` is climbed.

    Levels 1..100 need none. Levels 101..200 need ``ceil((level - 100) / 4)``,
    i.e. one more megamastery every 4 levels: 101 needs 1, 104 still needs 1,
    105 needs 2, ..., 200 needs 25.
    """
    if level <= 100:
        return 0
    return math.ceil((level - 100) / 4)


def chore_rate_per_day() -> float:
    """The smeared average daily AK from chores, for steady-state modelling.

    The daily +1 plus each bonus day's AK (see :data:`CHORE_BONUS_DAYS`)
    averaged over a calendar month (``365.25 / 12`` days): ``1 + 49 /
    30.4375`` ≈ 2.6098/day. :func:`chore_ak` is the calendar-exact version;
    this is its long-run average, for a stage whose span isn't yet known.
    """
    days_per_month = 365.25 / 12
    return 1 + sum(CHORE_BONUS_DAYS.values()) / days_per_month


def chore_ak(start: date, days: float) -> float:
    """Calendar-exact chore AK earned in the continuous window ``[start, start + days)``.

    Two components:

    - **Daily +1** accrues continuously at a rate of 1/day, so it is simply
      ``days`` (a half-day window earns 0.5, matching a whole day earning 1).
    - **Bonus days** (:data:`CHORE_BONUS_DAYS`: +7/+14/+28 on the 7th/14th/28th
      of the month) land **the moment their calendar date begins**, in full,
      never pro-rated: calendar date ``d`` earns its bonus iff ``(d -
      start).days < days`` -- i.e. the window need only reach the instant that
      date starts, not cover the whole day. ``start`` itself is checked at
      offset 0, so a window opening on a bonus date earns it for any
      ``days > 0``; a zero-length window earns nothing at all, even then.

    Raises ``ValueError`` if ``days`` is negative.
    """
    if days < 0:
        raise ValueError(f"days must be >= 0, not {days!r}")
    total = float(days)
    # Candidate dates are start, start+1, ... -- offset < days holds for
    # offset in range(ceil(days)), so that bounds the loop exactly.
    for offset in range(math.ceil(days)):
        d = start + timedelta(days=offset)
        total += CHORE_BONUS_DAYS.get(d.day, 0)
    return total


@dataclass(frozen=True)
class StepCandidate:
    """One item's remaining mastery steps, as seen from its current progress.

    ``item`` is the lower-cased item name. Each ``delta_*`` is the remaining
    mastery progress to reach that step from wherever the item currently
    stands (``None`` when the step is already done or moot -- see
    :func:`candidates`).
    """

    item: str
    delta_master: float | None
    delta_grandmaster: float | None
    delta_mega: float | None


def candidates(state: GameState) -> list[StepCandidate]:
    """Every masterable item's remaining mastery steps, sorted by item name.

    Built from ``state.masteries`` -- every item in the pasted mastery block
    is masterable. An item already fully mega'd (``Mastery.required is
    None``) contributes nothing at all. For an item at progress ``p``:

    - ``delta_master = MASTER_THRESHOLD - p`` while ``p < MASTER_THRESHOLD``,
      else ``None`` (already mastered).
    - ``delta_grandmaster = GRANDMASTER_THRESHOLD - max(p, MASTER_THRESHOLD)``
      while ``p < GRANDMASTER_THRESHOLD``, else ``None``.
    - ``delta_mega = MEGA_THRESHOLD - max(p, GRANDMASTER_THRESHOLD)`` while
      ``p < MEGA_THRESHOLD``, else ``None``.

    An item with no remaining step at all (``p >= MEGA_THRESHOLD``, or
    ``required is None``) is omitted from the result entirely.
    """
    result: list[StepCandidate] = []
    for key, mastery in state.masteries.items():
        if mastery.required is None:
            continue
        p = mastery.progress
        delta_master = MASTER_THRESHOLD - p if p < MASTER_THRESHOLD else None
        delta_grandmaster = (
            GRANDMASTER_THRESHOLD - max(p, MASTER_THRESHOLD)
            if p < GRANDMASTER_THRESHOLD
            else None
        )
        delta_mega = (
            MEGA_THRESHOLD - max(p, GRANDMASTER_THRESHOLD)
            if p < MEGA_THRESHOLD
            else None
        )
        if delta_master is None and delta_grandmaster is None and delta_mega is None:
            continue
        result.append(
            StepCandidate(
                item=key,
                delta_master=delta_master,
                delta_grandmaster=delta_grandmaster,
                delta_mega=delta_mega,
            )
        )
    result.sort(key=lambda c: c.item)
    return result


def mega_count(state: GameState) -> int:
    """How many items are already fully megamastered (tier 6, ``required is None``)."""
    return sum(1 for m in state.masteries.values() if m.required is None)


def mega_remaining(masteries: dict[str, Mastery], item: str) -> float:
    """Remaining mastery points needed to reach :data:`MEGA_THRESHOLD` for
    ``item``, from its current progress in ``masteries``.

    Mirrors :func:`candidates`' ``delta_mega`` formula exactly (progress is
    measured from ``max(progress, GRANDMASTER_THRESHOLD)``, so the result is
    "how much more is needed once grandmaster's own gap is closed" whether or
    not it actually is yet -- consistent with the deltas summing to the full
    climb when every step is selected together), but as a standalone per-item
    lookup rather than a full candidate scan, for milestone 6's scheduling/
    floor helpers below.
    ``0`` once the item is at or past mega (``required is None``); the full
    threshold for an item with no entry at all (never interacted with, so
    its progress is 0).
    """
    mastery = masteries.get(item.lower())
    if mastery is None:
        return float(MEGA_THRESHOLD)
    if mastery.required is None:
        return 0.0
    return float(MEGA_THRESHOLD - max(mastery.progress, GRANDMASTER_THRESHOLD))


def schedule_megas(
    chosen: list[str],
    bounds: list[tuple[int, int]],
    already: int,
    per_item_remaining: dict[str, float],
) -> dict[str, int]:
    """Assign each item in ``chosen`` a DUE STAGE INDEX (0-based, into
    ``bounds``) -- the first stage whose end level requires it.

    Stage ``k`` (ending at level ``bounds[k][1]``) requires
    ``megas_required_by(bounds[k][1]) - already`` cumulative megas complete
    by its end. Items are assigned to due stages in that increasing-
    cumulative-requirement order, **cheapest-remaining-supply first**
    (smallest ``per_item_remaining``, ties broken by item name for
    determinism) -- so the cheapest megas land on the earliest deadlines and
    the dearest get the most stages to spread their production across (see
    :func:`mega_stage_floors`, which does the actual spreading).

    ``chosen`` must have exactly ``megas_required_by(bounds[-1][1]) -
    already`` items -- the LP's mega-count constraint guarantees this by
    construction (the aggregate selection solve picks exactly enough
    candidates to cover the target's requirement) -- raising ``ValueError``
    otherwise rather than silently stranding a requirement or scheduling an
    item nothing needs.
    """
    required_total = megas_required_by(bounds[-1][1]) - already
    if len(chosen) != required_total:
        raise ValueError(
            f"schedule_megas: chosen has {len(chosen)} item(s) but "
            f"{required_total} are required by the target"
        )
    ordered = sorted(chosen, key=lambda item: (per_item_remaining.get(item, 0.0), item))
    due: dict[str, int] = {}
    for j, item in enumerate(ordered):
        needed_cumulative = j + 1
        due_stage = len(bounds) - 1
        for k, (_, b) in enumerate(bounds):
            if megas_required_by(b) - already >= needed_cumulative:
                due_stage = k
                break
        due[item] = due_stage
    return due


def mega_stage_floors(
    masteries: dict[str, Mastery], schedule: dict[str, int], stage_index: int
) -> dict[str, float]:
    """This stage's mega supply floors (in MASTERY POINTS, matching
    :attr:`AscensionSpec.mega_supply_floors`' units), one entry per
    ``schedule`` item still due at or after ``stage_index``.

    Each item's floor is its remaining mastery-points-to-mega -- recomputed
    from ``masteries`` (so overshoot in an earlier stage shrinks a later
    stage's floor, and undershoot correspondingly grows it) -- split evenly
    across the stages from ``stage_index`` to its due stage inclusive
    (``due - stage_index + 1`` stages). At the due stage itself that span is
    1, so the floor is exactly the full remaining amount: the item's mega
    must complete there. An item already at or past mega (remaining <= 0) is
    omitted -- nothing left to force.
    """
    floors: dict[str, float] = {}
    for item, due in schedule.items():
        if due < stage_index:
            continue
        remaining = mega_remaining(masteries, item)
        if remaining <= 0:
            continue
        span = due - stage_index + 1
        floors[item] = remaining / span
    return floors


@dataclass
class AscensionSpec:
    """One stage's ascension requirements, handed to the optimiser (milestone 3).

    ``ak_required`` and ``megas_required`` are net of whatever carries over
    from earlier stages, so a stage's own solve only has to clear what's left.
    ``mega_supply_floors`` (mastery points, milestone 6: :func:`mega_stage_floors`)
    forces a chosen mega item's gross supply this stage even when its own
    ``y_mega`` isn't selected yet -- see optimizer.py's "Ascension extension"
    for how the LP constraint combines it with the step terms; empty means no
    floor (every stage before milestone 6, and any stage with no mega due).
    """

    candidates: list[StepCandidate]
    ak_required: float
    chore_rate_per_day: float
    megas_required: int
    mastery_bonus: float = 1.1
    mega_supply_floors: dict[str, float] = field(default_factory=dict)


@dataclass
class AscensionOutcome:
    """One stage's plan, as produced by the optimiser (milestone 3)."""

    masters: list[str]
    grandmasters: list[str]
    megas: list[str]
    ak_from_steps: float
    ak_from_chores: float
    mastery_supply: dict[str, float]


def _required_for(progress: int) -> int | None:
    """The threshold ``progress`` sits below, or ``None`` once fully mega'd."""
    if progress >= MEGA_THRESHOLD:
        return None
    for threshold in _THRESHOLDS:
        if progress < threshold:
            return threshold
    return None  # pragma: no cover - unreachable, MEGA_THRESHOLD is the last threshold


def advance(masteries: dict[str, Mastery], outcome: AscensionOutcome) -> dict[str, Mastery]:
    """Apply a stage's ``outcome`` to ``masteries``, returning a new dict.

    Pure: ``masteries`` is deep-copied, never mutated. For each item in
    ``outcome.mastery_supply``, its progress increases by ``round(supply)``
    and ``required`` is recomputed to the next power-of-ten threshold above
    the new progress (``None`` once progress reaches :data:`MEGA_THRESHOLD`).
    An item in ``mastery_supply`` but absent from ``masteries`` gets a fresh
    entry starting from 0 progress.
    """
    result = copy.deepcopy(masteries)
    for item, supply in outcome.mastery_supply.items():
        key = item.lower()
        mastery = result.get(key)
        if mastery is None:
            mastery = Mastery(item=item, progress=0)
            result[key] = mastery
        mastery.progress += round(supply)
        mastery.required = _required_for(mastery.progress)
    return result


# Tower levels that grant a reward, and a human-readable description of each
# -- used to split a climb into stages (stage_bounds) and to apply the
# reward's config deltas (apply_rewards). Levels 90 and 150 have no config
# delta of their own here: they take effect via config.crafting_bonus_for/
# effective_grape_juices reading [tower] level, which apply_rewards always
# sets.
REWARD_LEVELS: dict[int, str] = {
    10: "crop growth +10% of base",
    60: f"+{ORCHARD_TREES_L60} trees per orchard fruit",
    70: "Fishing Trawl",
    90: "Resource Saver 3 (crafting bonus 1.45)",
    100: f"+{STOREHOUSE_BONUS} inventory cap",
    110: f"+{ORCHARD_TREES_L110} trees per orchard fruit",
    150: "Grape Juice uses doubled",
    160: "Antler Snare",
    170: "Tree Shaker",
    200: f"+{STOREHOUSE_BONUS} inventory cap",
}


def stage_bounds(current: int, target: int) -> list[tuple[int, int]]:
    """Split ``(current, target]`` into stages at every reward threshold crossed.

    Each ``REWARD_LEVELS`` key strictly between ``current`` and ``target``
    ends one stage and starts the next, so a stage never straddles a reward
    (the reward is available for every stage after it). No zero-length
    stages; validates like :func:`ak_cost`.

    ``stage_bounds(0, 200)`` -> ``[(0,10),(10,60),(60,70),(70,90),(90,100),
    (100,110),(110,150),(150,160),(160,170),(170,200)]``.
    """
    if not (0 <= current < target <= MAX_ASCEND_LEVEL):
        raise ValueError(
            "stage_bounds requires 0 <= current < target <= "
            f"{MAX_ASCEND_LEVEL}, not current={current!r}, target={target!r}"
        )
    thresholds = sorted(t for t in REWARD_LEVELS if current < t < target)
    bounds: list[tuple[int, int]] = []
    prev = current
    for t in thresholds:
        bounds.append((prev, t))
        prev = t
    bounds.append((prev, target))
    return bounds


def apply_rewards(cfg: dict, from_level: int, to_level: int) -> dict:
    """A deep copy of ``cfg`` with the deltas for every reward crossed climbing to ``to_level``.

    Applies the config-visible delta for each ``REWARD_LEVELS`` threshold
    ``t`` with ``from_level < t <= to_level`` (levels 90 and 150 have no
    delta here -- see :data:`REWARD_LEVELS`'s docstring note), then always
    sets ``cfg["tower"]["level"] = to_level`` regardless of what was crossed
    (creating the section if absent), since that is what makes the level-90/
    150 rewards take effect via ``config.crafting_bonus_for``/
    ``effective_grape_juices``. A missing section is created; an additive
    delta on a missing key starts from that key's own consuming-code default
    (0 for ``speed_reduction``/tree counts/``inventory``). ``cfg`` is never
    mutated.
    """
    if not (0 <= from_level < to_level <= MAX_ASCEND_LEVEL):
        raise ValueError(
            "apply_rewards requires 0 <= from_level < to_level <= "
            f"{MAX_ASCEND_LEVEL}, not from_level={from_level!r}, "
            f"to_level={to_level!r}"
        )
    new_cfg = copy.deepcopy(cfg)
    crossed = [t for t in REWARD_LEVELS if from_level < t <= to_level]
    for t in sorted(crossed):
        if t == 10:
            farming = new_cfg.setdefault("farming", {})
            farming["speed_reduction"] = farming.get("speed_reduction", 0) + 10
        elif t == 60:
            orchard = new_cfg.setdefault("orchard", {})
            for key in ("apple_trees", "orange_trees", "lemon_trees"):
                orchard[key] = orchard.get(key, 0) + ORCHARD_TREES_L60
        elif t == 70:
            fishing = new_cfg.setdefault("fishing", {})
            fishing["have_fishing_trawl"] = True
        elif t == 100:
            storehouse = new_cfg.setdefault("storehouse", {})
            storehouse["inventory"] = storehouse.get("inventory", 0) + STOREHOUSE_BONUS
        elif t == 110:
            orchard = new_cfg.setdefault("orchard", {})
            for key in ("apple_trees", "orange_trees", "lemon_trees"):
                orchard[key] = orchard.get(key, 0) + ORCHARD_TREES_L110
        elif t == 160:
            raptor_pen = new_cfg.setdefault("raptor_pen", {})
            raptor_pen["have_antler_snare"] = True
        elif t == 170:
            orchard = new_cfg.setdefault("orchard", {})
            orchard["have_tree_shaker"] = True
        elif t == 200:
            storehouse = new_cfg.setdefault("storehouse", {})
            storehouse["inventory"] = storehouse.get("inventory", 0) + STOREHOUSE_BONUS
        # 90 and 150: no direct config delta -- tower.level (set below) is
        # what makes config.crafting_bonus_for/effective_grape_juices react.
    tower = new_cfg.setdefault("tower", {})
    tower["level"] = to_level
    return new_cfg
