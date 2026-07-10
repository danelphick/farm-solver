# Performance plan

Profiled 2026-07-10 (M-series Mac, Python 3.14, PuLP + bundled CBC). Baselines,
plus a measured proof-of-concept of the item-1 backend swap (monkeypatching
`pulp.PULP_CBC_CMD` → `pulp.HiGHS`, no other change):

| Run | CBC (today) | HiGHS (measured PoC) |
| --- | --- | --- |
| `python main.py` | ~4.3 s | **0.62 s** |
| `python main.py --model-calendar` | ~8.2 s | **1.22 s** |

Why: the run makes 343 LP solves (682 with `--model-calendar` — the seasonal
fixed point runs the whole frontier twice). Under CBC each solve is a
**subprocess launch** (~12 ms: write MPS file, spawn the CBC binary, read the
solution back) — ~75% of the wall time for LPs that are individually tiny.
`pulp.HiGHS` solves in-process via `highspy` (wheels install cleanly on
3.14), eliminating that per-solve tax.

Where the 343 solves come from: `optimizer.frontier` calls `minimize` 86
times (2 weighted endpoints, ~44 epsilon-sweep caps, 1 favour_passive probe,
and ~39 favour_passive caps); each `minimize` runs a 3-stage lexicographic
refinement = 3 solves, plus one small surplus-value LP per frontier plan.

Re-profiling the HiGHS PoC shows the residual is **Python LP model building**
in `optimizer.minimize` — `pulp.lpSum`/`addInPlace`, `LpVariable.__init__`,
and O(items × offers) generator scans (`sources.Offer.yield_of` is called 1M
times per base run, 2M with the calendar) — with actual solving down to ~25%.
Items 2–4 attack that remainder.

Four items, in order of value. Each is separately committable and verifiable.
Output-wise all are behaviour-preserving up to the known alternate-optima flap
(see Verification).

---

## Item 1 — Pluggable solver backend, default HiGHS (~7× measured) — DONE

Implemented 2026-07-10 (`optimizer.set_solver_backend`, `--solver`,
`[solver] backend`; measured 0.73 s base / 1.35 s calendar). Details below kept
for reference.

PuLP already **is** the solver abstraction — the code just hardcodes
`pulp.PULP_CBC_CMD(msg=0)` at 4 call sites (`optimizer.py` ~lines 566, 604,
613 in `minimize`, and ~852 in `surplus_sale_value`). Make the backend a
module-level factory and default it to in-process HiGHS.

1. **Dependency:** `uv add highspy` (pure wheel, no build step; verified on
   this machine's 3.14).
2. **`optimizer.py`:** add a small backend registry + factory and replace the
   4 hardcoded calls with `problem.solve(_solver())`:

   ```python
   _SOLVER_BACKENDS = {
       "highs": lambda: pulp.HiGHS(msg=False),
       "cbc": lambda: pulp.PULP_CBC_CMD(msg=0),
   }
   _backend: str = "highs" if pulp.HiGHS(msg=False).available() else "cbc"

   def set_solver_backend(name: str) -> None:
       """Select the LP backend: a key in _SOLVER_BACKENDS, or any PuLP
       solver name (pulp.listSolvers()) for bolt-on solvers."""
       ...

   def _solver() -> pulp.LpSolver:
       factory = _SOLVER_BACKENDS.get(_backend)
       return factory() if factory else pulp.getSolver(_backend, msg=0)
   ```

   The `pulp.getSolver(name)` fallback is the generic bolt-on hook: any
   installed PuLP backend (GLPK, Gurobi, COPT, HIGHS_CMD, …) works by name
   with zero further code.
3. **Config + CLI:** read `[solver] backend` from `config.toml` in `main.run`
   and add a `--solver NAME` flag (flag wins), calling
   `optimizer.set_solver_backend` before any solve. Unknown/unavailable
   backend → a clear error listing `pulp.listSolvers(onlyAvailable=True)`.
   The CLI flag also makes cross-backend verification trivial
   (`--solver cbc` vs `--solver highs` in the same session).
4. **Fallback:** if `highspy` is not importable, the default stays CBC
   (PuLP bundles the binary), so a bare checkout still runs. Keep
   `available()`-probing cheap: compute the default once at import.
5. **Docs:** README config section for `[solver] backend`; rewrite the
   ARCHITECTURE.md "Dependency" paragraph — it currently documents CBC as the
   deliberate choice; the rationale (pure-wheel install) now extends to
   highspy, with CBC as the zero-dep fallback and the registry as the
   extension point. Note `highspy` in pyproject.
6. Run `uv run pytest -q` once with each backend (the suite runs whatever the
   default is; use the env of the machine — a quick
   `python main.py --solver cbc` smoke is enough for the fallback).

Measured effect: base 4.3 s → 0.62 s, calendar 8.2 s → 1.22 s.

## Item 2 — Skip the provably redundant stage-2 solve in epsilon mode — DONE

Implemented 2026-07-10. Details below kept for reference.

**File:** `optimizer.py`, `minimize`, the refinement block (~lines 592–613).

In epsilon-constraint mode (`time_cap is not None and not favour_passive`) the
**stage-1 objective already is `gold`**. Stage 1 minimises gold over
`makespan ≤ cap`; stage 2 re-minimises gold over the *tighter* set
`makespan ≤ t_star` (t_star = achieved makespan ≤ cap) which still contains
the stage-1 optimum — so its optimum is exactly the stage-1 gold value and
the solve is a no-op.

Edit: in the `if buy or buy_cookie is not None or pj:` stage-2 block, when
`time_cap is not None and not favour_passive`, don't
`setObjective(gold)`/`solve` — just pin from the stage-1 solution:

```python
problem += gold <= (pulp.value(gold) or 0.0) * (1 + 1e-9) + 1e-9
```

Stage 3 still needs gold locked, so the constraint stays. Leave the
weighted-sum and favour_passive paths untouched — the long comment above the
block explains why stage 2 is essential there (objective-scale degeneracy /
different stage-1 objective). Add a short comment with the argument above.

Removes 1 of 3 solves for the ~44 epsilon minimizes per frontier (~13% of
solves, and the same share of their model-translation overhead).

## Item 3 — Index offers per item in `minimize` — DONE

Implemented 2026-07-10. Details below kept for reference.

**File:** `optimizer.py`, `minimize`, the per-item balance loop (~line 512).

Every item's balance scans **all** offers twice via `o.yield_of(item)`
(filter + value) and again for `consumes`. Offer `yields`/`consumes` keys are
already lower-cased (see `sources.Offer` docstring), as are the members of
`items`, so prebuild indexes once, before the loop:

```python
yield_terms: dict[str, list] = {}
consume_terms: dict[str, list] = {}
for o, x in zip(used, usage):
    for item, rate in o.yields.items():
        if rate > 0:
            yield_terms.setdefault(item, []).append(rate * x)
    for item, qty in o.consumes.items():
        if qty > 0:
            consume_terms.setdefault(item, []).append(qty * x)
```

then in the loop body:

```python
gathered = pulp.lpSum(yield_terms.get(item, []))
used_up = pulp.lpSum(consume_terms.get(item, []))
```

Also cheap while there: the `used = [o for o in usable if
any(o.yield_of(i) > 0 for i in items)]` filter (~line 409) becomes
`any(rate > 0 and i in items for i, rate in o.yields.items())`. Leave the
small openables scans (`unpacked`/`emptied`) alone.

Term order inside each `lpSum` changes from items-major to offer order — the
constraints are mathematically identical and offer order is preserved, so the
determinism comment at `for item in sorted(items)` still holds.

## Item 4 — Cheap fixed-point iterations for `--model-calendar`

**File:** `main.py`, the `_MAX_SEASON_ITERS` loop in `run` (~line 1057).

The calendar model iterates: build seasonal rates for a horizon → **full
frontier** → take the selected plan's makespan as the new horizon → repeat
until `_makespans_close`. It converges in 2 iterations today, so the calendar
run costs exactly 2 full frontiers — but the first frontier is discarded
except for that one makespan.

In the **default case** the selected plan is `shown[0]` = the frontier's
fastest plan, whose makespan equals the `fast` endpoint's (a single `minimize`
with `gold_weight=_FAST_PRICE`; refinement pins the makespan, so it's stable).
So iterate the horizon on that one `minimize` (3 solves) instead of a full
frontier (~250), then run one full frontier at the converged horizon.

Gate the fast path on all of: `model_calendar`, `args.plan is None`,
`not args.cheapest_plan`, and every limit unset (`time_limit is None and
args.max_ap is None and args.max_ln is None` and the resolved max-gold is
None — the same expressions `within_limits` receives). With `--plan K`,
`--cheapest-plan`, or any limit, the selected plan needs the full frontier —
keep the existing loop for those.

Sketch (a seed loop before the existing one, which then breaks on its first
pass as today):

```python
if fast_path:
    for _ in range(_MAX_SEASON_ITERS):
        production = config.passive_production(cfg, today=start, start=start,
                                               horizon=horizon,
                                               monthly_grants=monthly_grants)
        offers = optimizer.all_offers(production, fish, explore, farm)
        fast = optimizer.fastest_endpoint(offers, demand, recipes,
                                          args.crafting_bonus, prices=prices,
                                          openables=open_flows,
                                          inventory=inventory,
                                          cookie_sets=cookie_sets,
                                          pj_gain=pj_gain, pj_price=pj_price,
                                          pj_limit=pj_limit)
        if not fast.draws and not fast.crafts:   # nothing to source
            break
        if horizon is not None and _makespans_close(fast.time, horizon):
            break
        horizon = fast.time
```

Add `optimizer.fastest_endpoint(...)` as a thin wrapper around the exact
`minimize` call `frontier` makes for its `fast` endpoint (~line 1163) — same
kwargs, same `_FAST_PRICE` — and have `frontier` use it too, so the two can
never drift.

Behaviour-preserving up to the existing convergence tolerance
(`_makespans_close`: 1 h or 0.1%): the displayed frontier is still built from
rates the loop has verified as a fixed point. Expected: calendar ≈ base
cost + a few %.

---

## Verification (per item, not once at the end)

Memory note (`cbc-nondeterminism`): solver output **flaps across runs** for
degenerate plans (alternate optimal vertices), so never compare against stale
goldens — capture before/after in the same session.

1. Before starting, capture goldens (run each **twice** to see the natural
   flap): `python main.py`, `python main.py --model-calendar`,
   `python main.py --cheapest-plan`, `python main.py --model-calendar --plan 3`.
   Save outputs and `time` figures.
2. After each item: re-run, diff. The frontier's **time and net-gold columns
   must match exactly**; acceptable differences are confined to unpinned
   metrics — which equivalent zones/draws fill the same time (AC/AP splits),
   the GJ tally (not pinned by the refinement tower), passive-line ordering,
   and ±1-unit rounding. This is exactly the difference class observed in the
   CBC↔HiGHS PoC diff and in CBC's own run-to-run flap.
3. `uv run pytest -q` and `uvx ruff check .` must pass (CLAUDE.md).
4. For item 1, also diff `--solver cbc` vs `--solver highs` in one session
   (same acceptance rule), and re-profile at the end:
   `uv run python -m cProfile -o /tmp/p.prof main.py --model-calendar`.

Commit each item separately to `main` with the Co-Authored-By trailer. Items
1, 2 and 4 touch the LP/frontier and orchestration logic — **update
ARCHITECTURE.md in the same commit** (Dependency section for item 1; the
minimize refinement note for item 2; the pipeline's fixed-point note for
item 4).

## Expected end state

| Run | Before | After item 1 (measured) | After all (est.) |
| --- | --- | --- | --- |
| `python main.py` | ~4.3 s | 0.62 s | ~0.4 s |
| `python main.py --model-calendar` | ~8.2 s | 1.22 s | ~0.5 s |

## Rejected / deferred

- **Thread-parallelizing the sweep solves** — was the top item when CBC
  subprocesses dominated (~2× wall). Under in-process HiGHS the residual cost
  is GIL-bound Python model building, which threads don't parallelize; not
  worth the complexity now. Revisit only if CBC must stay the default.
- **Free-threaded Python (3.14t)** — measured 2026-07-10: it *works*
  (highspy 1.15.1 opts into free-threading, the GIL stays disabled after
  import; PuLP is pure Python and ran fine; no single-thread penalty), but
  threaded build+solve of solver-shaped LPs scales ~2× at 4 workers and
  **collapses to ~1× at 8** (object-churn contention in PuLP's model
  building), vs 1.3× the GIL build already gets free (HiGHS releases the GIL
  during the native solve). highspy also has **no cp314t wheel** — it builds
  from source, needing a C++ toolchain, against the pure-wheel install
  rationale. Best case saves ~0.3–0.6 s on a 0.6–1.2 s run; items 2–4 are
  cheaper and stack. Not worth it.
- **Merging refinement stages 2+3 with a weighted objective** — reintroduces
  the objective-scale degeneracy the 3-stage tower exists to avoid (see the
  long comment in `minimize`). Don't.
- **Caching LP solutions across calendar iterations** — offers (seasonal
  rates) change between iterations, so solutions don't transfer.
- **Bypassing PuLP for direct highspy model building** — would cut the
  remaining `lpSum`/translation overhead, but loses the free multi-backend
  abstraction item 1 leans on. Only worth it if sub-0.3 s ever matters.
