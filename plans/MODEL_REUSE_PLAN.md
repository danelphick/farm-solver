# Sweep model-reuse plan

Follow-on to [PERF_PLAN.md](PERF_PLAN.md) (all four items there are DONE). That
plan's final profiling finding, after the HiGHS backend swap, was that solving
itself is now only ~25% of the run; the rest is **Python LP model building**
(`pulp.lpSum`/`addInPlace`, `LpVariable.__init__`, offer/item term scans) —
items 2–3 trimmed pieces of that, but the fundamental cost remains: every one
of the ~80 `minimize` calls in a single `optimizer.frontier` sweep rebuilds an
LP from scratch in Python, even though almost all of that structure is
identical across the sweep. This plan attacks that redundancy directly.

## Where the redundancy is

Within one `frontier()` call, the epsilon-constraint sweep and the
`favour_passive` sweep each call `minimize` ~40 times ([optimizer.py:1273](../optimizer.py#L1273),
[optimizer.py:1306](../optimizer.py#L1306)) with the **same** `offers`,
`demand`, `recipes`, `crafting_bonus`, `openables`, `inventory`,
`cookie_sets`, `prices`, `pj_gain`, `pj_price`, `pj_limit` — only `time_cap`
(and, for the two regimes, `favour_passive`/`gold_weight`) differs call to
call.

Every one of those calls independently rebuilds, from scratch, the part of
`minimize` that depends only on the shared inputs:

- the `usage`/`craft`/`opens`/`buy`/`pj`/`makespan` `LpVariable`s
  ([optimizer.py:465](../optimizer.py#L465)–468)
- the `yield_terms`/`consume_terms` per-item indexes ([optimizer.py:544](../optimizer.py#L544)–552)
- every per-item balance constraint, in `sorted(items)` order
  ([optimizer.py:563](../optimizer.py#L563)–602)
- the track (concurrency) constraints ([optimizer.py:604](../optimizer.py#L604)–609)

What actually varies per call is small: the stage-1 objective (weighted-sum,
`gold`, or `spend_value`), an optional `makespan <= time_cap` constraint, and
the refinement tower's own pinning constraints
([optimizer.py:637](../optimizer.py#L637)–665) — which are already added to
and solved against **one reused `problem` object across the 3 refinement
stages within a call**. That reuse-within-a-call already works today; this
plan extends it **across calls** in a sweep.

## Step 1 — spike: DONE (2026-07-10)

Prototyped standalone (not committed; a throwaway script that monkeypatched
`optimizer.minimize` to record real call args from a live `python main.py`
run against this repo's actual `./mydata` state, then replicated
`minimize`'s body split into a one-time `build_base()` and a per-cap
`solve_configured()`). Findings:

**Correctness.** Across all 41 harvested epsilon-sweep calls and all 42
harvested favour_passive-sweep calls (cap range ~9.3k–42.4k minutes, 104
offers, 261 recipes, 6 demanded items — this repo's real current state), the
mutate-and-resolve prototype matched the real, unmodified `optimizer.minimize`
exactly (`makespan` within 0.5 min, `gold` within 1%/1g) under **both**
`--solver highs` and `--solver cbc`. Deleting a named constraint from
`problem.constraints` and calling `.solve()` again (with a fresh solver
object each time, exactly as today's code already does per-call) behaves
correctly on both backends — confirms the core mechanism.

**Timing** (this machine, same real sweep, `problem.solve()` per call
including a fresh solver object each time — no backend-specific incremental
API used):

| Sweep | Backend | Fresh rebuild (N calls) | Build-once + mutate | Speedup |
| --- | --- | --- | --- | --- |
| epsilon (41 calls) | highs | 0.222s | 0.078s | **2.83x** |
| epsilon (41 calls) | cbc | 1.225s | 1.007s | 1.22x |
| favour_passive (42 calls) | highs | 0.267s | 0.132s | **2.03x** |
| favour_passive (42 calls) | cbc | 1.597s | 1.420s | 1.12x |

As PERF_PLAN's own diagnosis predicted: the win is substantial under the
default HiGHS backend (Python model-building was the dominant residual cost
there), and marginal under CBC (its ~12ms-per-solve subprocess spawn tax
dominates regardless of whether the Python-side model was freshly built or
reused — this change doesn't touch that cost). **Verdict: proceed to steps
2–3**, understood as a HiGHS-tier win primarily; don't expect much under CBC.

**Design correction found while prototyping** (would have been a silent
correctness bug otherwise): the real `minimize` only creates `pj` (Pumpkin
Juice) variables `if pj_gain and pj_price > 0 and not favour_passive`
([optimizer.py:497](../optimizer.py#L497)) — pj is supposed to be entirely
off in favour_passive mode. A single shared base can't gate a *variable's
existence* on the per-call mode (the base is built once, before any call
picks its mode). Fix: build `pj` unconditionally in the base (whenever
`pj_gain`/`pj_price` allow it at all), and in every `favour_passive=True`
call add an ephemeral `pj[item] <= 0` constraint per pj item before solving,
neutralising it exactly like the original code's variable-absence did. This
was exercised in the spike's harvested run (`pj_gain` was empty there —
`use_pumpkin_juice = false` in this repo's `config.toml` — so it was inert
in practice) but the mechanism is in place and should be covered by a test
with `--use-pumpkin-juice` on. See the reference implementation below.

## Design

Two shared bases per `frontier()` invocation, not one — `pj` variable
existence is gated on `favour_passive` in the original code (see finding
above), so one base serves every `favour_passive=False` call (fast endpoint,
cheap endpoint, the epsilon-cap sweep) and a second serves every
`favour_passive=True` call (the probe, the extension-cap sweep). Both bases
are built from the same `offers`/`demand`/`recipes`/... — they differ only in
whether `pj` variables are neutralised per call.

1. **`_build_base(offers, demand, recipes, crafting_bonus, openables,
   inventory, cookie_sets, prices, pj_gain, pj_price, pj_limit)`** — runs once
   per base (so twice per `frontier()` invocation; once for single-shot
   callers). Builds the `problem`, all variables (`usage`, `craft`, `opens`,
   `buy`, `buy_cookie`, `pj`, `makespan`), the `gold` and `spend_value`
   expressions, the `yield_terms`/`consume_terms` indexes, every balance and
   track constraint — everything listed above — and returns a bundle holding
   all of that plus the bookkeeping needed later to assemble a `SourcingPlan`
   (`used`, `crafted`, `opened`, `wanted`, `remainder`, `pj_gain`, `pj_price`).
   Also holds `ephemeral: list[str]`, the names of constraints the last
   `_solve_configured` call added.
2. **`_solve_configured(base, *, gold_weight=None, time_cap=None,
   favour_passive=False)`** — the per-call entry point. At its **top** (not
   its end — see the leakage risk below), pops every name in
   `base.ephemeral` from `base.problem.constraints` and resets
   `base.ephemeral = []`; a call that raises partway through never corrupts
   the base for calls that never happen. Then:
   - If `favour_passive` and `base.pj`: add `pj[item] <= 0` per pj item,
     named e.g. `f"pj_off_{item}"`, appended to `ephemeral`.
   - Add the `makespan <= time_cap` constraint if given, named `"cap"`.
   - Set the stage-1 objective (`gold_weight * makespan + gold_weight_term`
     weighted-sum / `gold` / `spend_value` per mode) and solve.
   - Run the existing stage-2/3 refinement **verbatim**
     ([optimizer.py:637](../optimizer.py#L637)–665), naming every constraint
     it adds (`"stage1_pin"`, `"stage2_pin"`) and appending each to
     `ephemeral`.
   - Read the solution into a `SourcingPlan` exactly as today's tail of
     `minimize` does ([optimizer.py:667](../optimizer.py#L667)–718).

Top-level `minimize(...)` keeps its exact signature and behavior — it becomes
`_build_base` followed by one `_solve_configured` call — so `fastest_endpoint`,
`self_sourced_endpoint`, and `main.py`'s calendar fixed-point loop (all
single-shot callers) need no changes. `frontier()` is the only caller that
changes: it builds the two bases once and drives every endpoint/cap solve in
its sweeps through `_solve_configured`, instead of calling `minimize` fresh
each time.

This deliberately stays inside PuLP's object model — still one `problem.solve()`
call per cap, on whatever backend `--solver`/`[solver] backend` selected, with
a fresh solver object per solve exactly like today (the spike confirmed this
is what makes the mutation safe — no persistent solver-side state to go
stale). It does **not** need or rely on any backend-specific incremental/
warm-start API, so it doesn't touch the pluggable-backend abstraction
(`optimizer.set_solver_backend`, `pulp.getSolver` bolt-ons) and doesn't
revisit PERF_PLAN's rejected "bypass PuLP for direct highspy" item.

### Reference implementation (validated by the step-1 spike)

The spike's `build_base`/`solve_configured` split, ready to adapt directly
into `optimizer.py` (variable/constraint construction is a verbatim copy of
`minimize`'s existing body — only the split point and ephemeral-naming are
new):

```python
def _build_base(offers, demand, recipes, crafting_bonus, openables,
                 inventory, cookie_sets, prices, pj_gain, pj_price, pj_limit):
    # ... optimizer.minimize's lines ~399-525 verbatim, ending with `pj` built
    # unconditionally (drop the `and not favour_passive` gate -- favour_passive
    # calls neutralise pj per-call instead, see below) ...
    return _Base(problem=problem, usage=usage, craft=craft, opens=opens,
                 makespan=makespan, buy=buy, buy_cookie=buy_cookie, pj=pj,
                 gold=gold, spend_value=spend_value, used=used, crafted=crafted,
                 opened=opened, wanted=wanted, remainder=remainder,
                 pj_gain=pj_gain, pj_price=pj_price, ephemeral=[])


def _solve_configured(base, *, gold_weight=None, time_cap=None,
                       favour_passive=False):
    for name in base.ephemeral:
        base.problem.constraints.pop(name, None)
    base.ephemeral = []
    problem = base.problem

    if favour_passive and base.pj:
        for item, var in base.pj.items():
            problem += (var <= 0, f"pj_off_{item}")
            base.ephemeral.append(f"pj_off_{item}")

    if time_cap is not None:
        problem += (base.makespan <= _minutes(time_cap), "cap")
        base.ephemeral.append("cap")

    if favour_passive:
        problem.setObjective(base.spend_value)
    elif time_cap is not None:
        problem.setObjective(base.gold)
    else:
        problem.setObjective(base.makespan + gold_weight * base.gold)
    problem.solve(_solver())

    # ... stage-2/3 refinement, verbatim from optimizer.py:637-665, with
    # every `problem += constraint` given an explicit name and appended to
    # base.ephemeral instead of left anonymous ...

    # ... tail, verbatim from optimizer.py:667-718, building the SourcingPlan ...
```

## Steps

Each step separately committable and verifiable, per this project's usual
discipline. Written to be picked up and executed without further design
work — the spike above resolved the open design questions.

1. ~~Spike~~ — **DONE**, see above.
2. ~~Refactor `minimize` into `_build_base` + `_solve_configured`~~ — **DONE**
   (commit `fb29bed`). Also fixed the `pj`-gating design correction the spike
   found (built unconditionally in the base now, not gated on
   `favour_passive`). Verified by golden diffs alone (see Verification) before
   step 3 introduced the actual reuse.
3. ~~Change `frontier()`~~ — **DONE** (commit `b78dd59`): builds
   `base_active`/`base_passive` once each and drives every sweep solve through
   `_solve_configured`. Surfaced and fixed a real bug along the way: PuLP's
   `LpConstraint.name` setter sanitises names (spaces/punctuation → `_`), so
   an ephemeral name built from an item string (`f"pj_off_{item}"` for
   `"fancy violin"`) didn't match what `problem.constraints` actually stored
   it under — the next call's pop missed it, and a later call's re-add raised
   `PulpError: overlapping constraint names`. Fixed by reading the sanitised
   name back off the constraint object before recording it in `ephemeral`.
   `--use-pumpkin-juice`'s golden (the one exercising this path) is what
   caught it.
4. ~~Re-profile~~ — **DONE**, see the Expected effect section below.
5. ~~Update ARCHITECTURE.md~~ — **DONE**, bundled into commit `b78dd59` with
   step 3, per CLAUDE.md's "keep the map current" rule.

## Verification

Same discipline as PERF_PLAN.md, not repeated once at the end:

1. Capture goldens **twice** before starting (`python main.py`,
   `--model-calendar`, `--cheapest-plan`, `--model-calendar --plan 3`, and
   `--use-pumpkin-juice` — the last one specifically exercises the pj-
   neutralisation path the spike found, which this repo's default `mydata`/
   `config.toml` state does not) to see the natural CBC/HiGHS alternate-optima
   flap (memory: `cbc-nondeterminism` — never diff against a stale golden).
2. After each step, re-run and diff: the frontier's **time and net-gold
   columns must match exactly**; the same difference class as before
   (equivalent-zone/draw splits, GJ tally, passive-line ordering, ±1-unit
   rounding) is acceptable, nothing else.
3. `uv run pytest -q` and `uvx ruff check .` must pass.
4. Commit each step separately to `main` with the Co-Authored-By trailer.

## Risks / open questions

- ~~**Constraint-removal correctness.**~~ **Resolved by the spike**: popping
  a named constraint from `problem.constraints` and re-solving (fresh solver
  object per `.solve()`, as today's code already does) produced results
  identical to a full rebuild, under both `highs` and `cbc`, across 83 real
  calls. Not separately tested against a `pulp.getSolver` bolt-on backend —
  low risk, since the mechanism doesn't touch backend-specific state at all
  (it mutates the `LpProblem` before any backend ever sees it), but worth a
  quick smoke test in step 2/3's verification if a bolt-on solver is
  available.
- ~~**`pj` variable-existence gating on `favour_passive`.**~~ **Resolved by
  the spike**: build `pj` unconditionally in the base; neutralise it with a
  per-call `pj <= 0` ephemeral constraint when `favour_passive=True`. Verify
  with `--use-pumpkin-juice` in step 2/3, since the harvested spike run had
  no pj demand to exercise it against.
- ~~**Ephemeral-constraint leakage.**~~ **Hit this, not hypothetically**: a
  name-sanitisation mismatch (see step 3 above) meant a `pj_off_*` constraint
  was never actually popped, so it accumulated instead of being replaced —
  but this particular instance *did* crash loudly (`PulpError: overlapping
  constraint names`) rather than silently corrupting a result, since PuLP
  rejects a duplicate name outright. A leak that instead **failed to add** a
  needed pin (rather than failing to remove one) would be the silent,
  dangerous version this note originally warned about — not hit here, but the
  golden time/net-gold diff remains the safety net for it.
- **Determinism.** The `sorted(items)` constraint-insertion-order comment
  ([optimizer.py:556](../optimizer.py#L556)) that keeps alternate-optima
  tie-breaking reproducible must keep applying to the base model; only the
  ephemeral per-call additions may vary, and they already do today.
- **Bounded upside, and backend-dependent.** This doesn't reduce the number
  of LP solves (still ~80 per sweep) — only the redundant *construction*
  preceding each one. The spike measured ~2-2.8x on the sweep groups under
  HiGHS (the default) and ~1.1-1.2x under CBC — expect the win to matter for
  the common case and be marginal for CBC users.
- **PuLP deprecation risk (found in post-merge review, not by the spike).**
  `_solve_configured`'s `problem.constraints.pop(name, None)` uses PuLP's
  dict-style `.constraints` access, which is deprecated — PuLP 4.0 returns a
  list from `prob.constraints()` instead, and there is no replacement removal
  API yet as of PuLP 3.3.2. Confirmed working correctly today on both `highs`
  and `cbc` (this is the entire reuse mechanism, so it's well-exercised), but
  it is a forward-compatibility risk sitting at the core of this plan's
  design — watch for it on the next PuLP upgrade. Missed during
  implementation because the `DeprecationWarning` only surfaces under `uv run
  pytest -q` (pytest overrides the default warning filter); a plain `python
  main.py` run stays silent, since Python suppresses `DeprecationWarning`
  outside `__main__` by default. Now noted in `optimizer.py` and
  ARCHITECTURE.md's gotchas.

## Rejected / deferred (carried over, still applicable)

- **Bypassing PuLP for direct incremental highspy calls** — bigger
  theoretical win (true warm start, no per-call solver translation at all),
  but loses the free multi-backend abstraction item 1 of PERF_PLAN leans on.
  The step-1 spike's ~2-2.8x under HiGHS is a solid win without this; only
  worth revisiting if steps 2-3's integrated re-profile (step 4) falls well
  short of the spike's standalone numbers.
- **Thread-parallelizing the sweep** — PERF_PLAN already rejected this under
  GIL-bound Python model building; still not worth it after this plan, since
  reuse directly targets that cost instead of trying to parallelize it.

## Expected effect

Step-1 spike measured (standalone prototype, not yet integrated — see table
above): epsilon sweep 2.83x faster under HiGHS / 1.22x under CBC;
favour_passive sweep 2.03x under HiGHS / 1.12x under CBC, on this repo's real
current state (104 offers, 261 recipes, 6 demanded items, 41+42 sweep calls).

## Step 4 — re-profile: DONE (2026-07-10)

Measured on this machine, `--model-calendar`, HiGHS backend, same interpreter/
venv for both sides (swapped only `optimizer.py` between the pre-plan commit
`13884b8` and the post-step-3 commit, caches warmed with two throwaway runs
before timing — the first naive attempt compared across two different `uv`-
provisioned venvs, one of which silently resolved to a free-threaded 3.14t
build with a cold bytecode cache, producing a bogus ~5x; the controlled
same-venv comparison below is the real number):

| Metric | Before (13884b8) | After (steps 2-3) | Speedup |
| --- | --- | --- | --- |
| Wall clock, warm cache, best-of-3 (`/usr/bin/time -p`) | 0.68s | 0.37s | **1.84x** |
| `cProfile` total run time | 1.808s | 0.666s | 2.72x |
| `cProfile` `optimizer.frontier` cumtime | 1.622s | 0.482s | **3.37x** |
| PuLP model-construction calls (`LpVariable`/`LpAffineExpression.__init__`, `__add__`, `__radd__`, `__mul__`, `__rmul__`, `addInPlace`, `lpSum`) | 1,061,616 calls / 0.573s | 108,939 calls / 0.058s | **9.7x calls, 9.9x time** |
| `solve`/`actualSolve` cumtime (unchanged work — same ~88 solves, same backend) | 0.354s | 0.319s | ~1x (expected) |

Beats the step-1 spike's standalone 2-2.8x estimate (3.37x on `frontier()`
itself) — the integrated reuse also collapses the `_obtainable`/`_reachable`/
craftable-set graph work in `_build_base` from ~88 calls to 2, which the
spike's isolated LP-construction-only prototype didn't capture. Model
building is no longer the dominant cost: solving now accounts for ~66% of
`frontier`'s time (was ~22%), the reverse of PERF_PLAN's original diagnosis.
Lands below PERF_PLAN's own "~0.4s" estimate for the default HiGHS backend, as
expected; not re-measured under `--solver cbc` (the plan's own risk note
already flagged this item as HiGHS-tier, marginal-to-CBC).

## Post-merge review (2026-07-10)

Independently re-verified: golden diff against `13884b8` (time/net-gold match
exactly on every plan; the one label difference seen — the frontier table's
"defining passive resource" name on one plan — reproduced identically on
*unmodified* `13884b8` across repeated runs, confirming it's the pre-existing
alternate-optima flap, not a regression), `pytest -q` (599 passed), `ruff
check` (clean), cross-backend diff (`highs` vs `cbc`, matching).

Two gaps found and fixed:

- `fastest_endpoint`'s docstring claimed "`frontier` calls this too, so the
  two can never drift apart" — no longer true since step 3 (`frontier` now
  calls `_solve_configured(base_active, gold_weight=_FAST_PRICE)` directly).
  Still behaviorally equivalent, but no longer enforced by shared code.
  Docstring corrected to say so explicitly.
- The PuLP `.constraints` deprecation risk (see the Risks section's new
  bullet above) — not caught during implementation since the warning is
  silent outside `pytest`. Documented in `optimizer.py` (inline comment) and
  ARCHITECTURE.md (gotchas).
