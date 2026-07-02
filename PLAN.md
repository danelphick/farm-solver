# Plan: multi-objective sourcing solver (time × AP × LN)

Status of this file: living plan for turning the solver from "report mastery
shortfalls" into "find the best way to source everything, trading time against
Arnold Palmers (AP) and Large Nets (LN)." Update as phases land.

## Goal

Every needed item — a mastery/inventory shortfall, or a base material a craft
needs — can be satisfied by one or more **channels**. Two spend a consumable to
buy speed; the rest cost time but no consumables:

| Channel | Consumable | Time cost | Joint production |
|---|---|---|---|
| Passive (buildings) | — | concurrent wait (max) | one cycle yields many items |
| Pets | — | concurrent wait (max) | bundle per pet |
| Farming | — | concurrent grow time (+ grape juice) | harvest yields crop + rare drops |
| Crafting | — | (expands into base-material demand) | — |
| Fishing | **Large Net** | ~instant | one net at a zone yields a bundle of fish |
| Exploring | **Arnold Palmer** | ~instant | one palmer at a location yields a bundle |

Spending an AP or LN pulls items out of the slow farming/passive queues for zero
time but +1 consumable. Sweeping "how much time is a consumable worth" traces a
**Pareto frontier** over `(total_time, AP, LN)`.

## Decisions (locked)

- **Output:** a Pareto frontier of non-dominated plans (~**5** to start; add more
  if short-duration plans need finer AP/LN resolution).
- **Channel choice:** the **optimizer decides** per item — no fixed
  free-channels-first order; the objective alone picks where AP/LN are worth it.
- **Time tracks are concurrent:** farming and all passives run at the same time,
  so `total_time = max(track times)`; fishing/exploring are ~instant.
- **AP mechanics:** one Arnold Palmer yields **200** items in a location, **500**
  with the Lemon Squeezer perk, ×**1.1** with Quandary Chowder (550 with both).
  (Fishing/LN: 250 base, 400 reinforced, +100 trawl, ×1.1 sea pincher.)

## Phases

**1 — Model exploring (AP channel). DONE (`f4c48b7`).** `exploring.py` mirrors
`fishing.py`: `ExploringModifiers.palmer_yield()` (200/500/×1.1) and
`ExploringLocation.explore(yield)` splitting across drop rates.

**2 — Loaders + config wiring for fishing and exploring.** Neither channel loads
its CSV (`fishing.csv`, `exploring.csv`) or reads its `[fishing]`/`[exploring]`
config yet. Add `read_*`/`load_*`/`*_from_config` (following the `crops.py`
loader conventions) so each produces real per-zone/per-location yields. No
sourcing wiring yet — just make the models real from data.

**3 — Unify channels behind one interface.** A `Source` protocol: given
`(item, quantity)` return candidate `Offer`s, each carrying (a) the co-products
the action also yields (the bundle) and (b) a cost signature — `ap`, `ln`, and a
**time-track tag** (`concurrent-pool` / `concurrent-farm` / `instant`). Wrap
passive, pets, farming, fishing, exploring. Crafting stays as the recursion that
expands demand to base materials feeding these sources.

**4 — Time-aggregation model.** Combine a chosen set of offers into one
`(time, AP, LN)`: passive+pets+farming → `max` of their (storehouse-/grape-juice-
aware) track times; fishing/exploring → ~0 time, accumulate AP/LN with **joint
production** so one zone's nets/palmers cover *all* its needed items at once
(cover by the largest requirement). `total_time = max(track times)`.

**5 — Pareto search.** For each item enumerate candidate sources (per-unit time
contribution within its track, consumable cost, co-products). Sweep a price-of-
time `λ` (2-D since fish↔LN and explore↔AP are largely independent); at each `λ`
assign items to the min-blended-cost channel, measure the realized nonlinear
time (bottleneck track), iterate to move the bottleneck's items onto consumables
where that lowers cost, record realized `(time, AP, LN)`. Keep non-dominated
points → ~5-plan frontier.

**6 — Solution + reporting.** Extend `Solution` to hold the frontier (plans with
`AP`, `LN`, `time`, per-item channel assignment). Print it as a table; a flag
(e.g. `--plan N`, or `--max-ap/--max-ln` to pick a point) expands one plan into
the existing `print_farming`/`print_passive` plus new `print_fishing`/
`print_exploring`. Finally set `Solution.arnold_palmers`/`large_nets`.

**7 — Tests + README.** Exploring/fishing loaders, aggregation, joint-production
covering, frontier non-domination; document the frontier output and flags.

## Open questions (revisit as we reach them)

- Frontier granularity may need >5 plans where very short durations cost wildly
  different AP/LN; make the count/spacing tunable.
- Whether any hard AP/LN ceiling should cap the frontier's fast end.
