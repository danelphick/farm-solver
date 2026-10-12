# Fastest solve (branch `perf/fastest-solve`)

Goal: make the two web views -- the **solver page** (`/api/solve`:
`webapi.solve` + `webapi.render`) and the **mastery-costs page**
(`/api/mastery-costs`: `webapi.mastery_costs`) -- and their CLI twins
(`python main.py`, `python main.py --mastery-costs`) as fast as possible,
**without changing any result**.

## Summary

| | solve | mastery | cli | cli-mc |
| --- | --- | --- | --- | --- |
| `main` | 1.079s | 5.646s | 1.068s | 2.904s |
| branch, 14-core Mac (defaults) | 0.193s | 0.225s | 0.277s | 0.401s |
| speed-up | **5.6x** | **25x** | **3.9x** | **7.2x** |
| branch, one CPU (`--threads 1`; the 1-vCPU deployment) | 0.359s | 1.24s | 0.48s | 1.30s |
| speed-up on one CPU | **3.0x** | **4.5x** | **2.2x** | **2.2x** |

Where it came from: a PuLP -> HiGHS hand-over that converts each LP once and
warm-starts the refinement stages (steps 1, 10); HiGHS's presolve failures sent
to a presolve-free HiGHS instead of CBC (2) and its MIP feasibility-jump
heuristic off (6); much less Python per model -- shared build work cached,
leftovers in one pass, the surplus LP straight to HiGHS, the activity objective
kept (7, 8, 14, 15); and the CPUs used -- threads for the frontier, worker
processes for the mastery sweep, independent solves started early, all capped
at the CPUs actually available (3-5, 9, 12, 13). Lazy imports help the CLI (11).

What changed in the output: **every CLI scenario is byte-identical** to `main`
(including the fresh-account `--ascend` climb). The **web views**, on their own
config, differ in three ways, all explained below:

* step 2 -- where HiGHS's presolve wrongly rejects a model, the presolve-free
  HiGHS answer replaces CBC's: equally optimal, but on the mastery-costs page
  one row's gold moves (Sunflower Oil -> Megamaster, 665,219.4 -> 665,249.6) and
  the solver page's selected plan shifts by a few units in places;
* step 6 -- two mastery-costs rows were **wrong** on `main` (HiGHS's presolve
  returned a plan ~3x slower than optimal as "optimal") and are now right:
  Runestone 10 -> Grandmaster 51d 2h -> 17d 9h, Runestone 16 -> Grandmaster 82d
  23h -> 27d 22h;
* step 10 -- some mastery-costs gold figures move in their last digits (at most
  0.0002 gold, never visible).

The Runestone rows are also wrong on `main` today -- the feasibility-jump fix
(step 6, which needs step 2's fallback) is worth taking even without the rest.

## How it is measured

`uv run python scripts/bench_solve.py` against the newest `mydata/` pastes:

| case | what | config |
| --- | --- | --- |
| `solve` | `webapi.solve` + `webapi.render`, default params (the tower goal), warm process | `web_default_config.toml` |
| `mastery` | `webapi.mastery_costs`, warm process | `web_default_config.toml` |
| `cli` | `python main.py`, a fresh process (imports included) | `config.toml` |
| `cli-mc` | `python main.py --mastery-costs`, a fresh process | `config.toml` |

Each figure is the median of 5 runs (the in-process cases after a warm-up run)
on a 14-core Apple Silicon Mac, Python 3.14.2, highspy 1.15.1.

## How each step is verified

Every step must leave the output byte-identical to `main`'s, checked these
ways:

* a harness dumps **every LP `_build_base` builds** (every row in order, every
  coefficient, bound and variable) and the printed report, for twelve CLI
  scenarios -- the default tower goal, `-i`/`-m` goals (incl. Pumpkin Juice,
  `--self-sourced`, AP-trading and seed-limited crops, the Sugar Cane Mill),
  `--mastery-costs` (both ways), `--ascend` (incl. the fresh-account 0->200
  climb) and Veggie Juice off -- run on `main` and on the branch with the same
  `PYTHONHASHSEED`, then diffed;
* the two web views' payloads (the mastery-costs rows, the solver page's
  rendered sections), dumped and diffed the same way;
* `uv run pytest -q`, `uv run --group web python web/manage.py test solverapp`
  and the web unit tests;
* for the threaded and multi-process steps, repeated runs at high thread
  counts compared with each other (no run-to-run differences).

A step that cannot keep the output identical says so, and why, below.

## Results

| step | solve | mastery | cli | cli-mc |
| --- | --- | --- | --- | --- |
| `main` (`0875ebf`) | 1.079s | 5.646s | 1.068s | 2.904s |
| 1. `FastHiGHS`: a fast PuLP -> HiGHS hand-over | 0.770s | 5.155s | 0.716s | 2.386s |
| 2. HiGHS without presolve before the CBC fallback | 0.684s | 3.019s | 0.703s | 2.365s |
| 3. Mastery costs' items on a thread pool | 0.684s | 0.951s | 0.716s | 1.025s |
| 4. The frontier's sweeps and exact finish on a thread pool | 0.486s | 0.943s | 0.497s | 1.032s |
| 5. Every exact-finish plan concurrently | 0.426s | 0.946s | 0.466s | 1.018s |
| 6. HiGHS's feasibility-jump heuristic off | 0.338s | 0.901s | 0.448s | 1.007s |
| 7. Surplus leftovers in one pass over the touched items | 0.306s | 0.886s | 0.414s | 0.999s |
| 8. Reuse what builds share; re-tune the thread counts | 0.283s | 0.526s | 0.387s | 0.632s |
| 9. Thread counts capped at the CPUs available | 0.275s | 0.521s | 0.374s | 0.625s |
| 10. Warm-start the refinement stages | 0.252s | 0.512s | 0.360s | 0.613s |
| 11. Import rich and multiprocessing only where used | 0.249s | 0.512s | 0.335s | 0.588s |
| 12. Mastery-costs sweep on worker processes | 0.248s | 0.220s | 0.333s | 0.405s |
| 13. Run known work ahead: the confirmed frontier, the reference endpoint | 0.216s | 0.224s | 0.309s | 0.405s |
| 14. The surplus LP straight to HiGHS, from a cached shape | 0.200s | 0.226s | 0.284s | 0.402s |
| 15. Build the stage-3 objective once per base | 0.193s | 0.225s | 0.277s | 0.401s |

`main` already includes the coefficient-dict row building (`afe461d`), which
took `--mastery-costs` from 3.85s to 2.81s on its own.

### 1. `FastHiGHS` -- output identical

Profiling the solver page after the row-building fix: of ~1.6s (profiled), PuLP's
HiGHS backend spent ~0.9s *handing the model over* -- `LpProblem.variables()`
(called three times per solve, each walking every constraint's terms and
re-sorting the variables), then one `addCol`/`addRow` call per column and row,
then reading every value, dual and slack back -- against ~0.4s in HiGHS itself.
The same `_Base` is re-solved dozens of times with only the objective, a few
pins and a time cap changing, yet every solve rebuilt it from nothing.

`highs_backend.FastHiGHS` (the "highs" backend) gives HiGHS the *identical*
model -- columns in PuLP's order (sorted by name), rows in constraint order with
each row's nonzero entries in order, the same bounds, costs, integrality and
options, a fresh `Highs` per solve -- but caches the converted rows on the
problem and re-converts only the constraints added since the last solve (the
shared prefix is reused; a base's ephemeral pins come and go at the end), then
passes the model in one `passModel`, and reads back only the primal values (no
caller reads duals or slacks). Verified identical on every scenario and on both
web views.

### 2. HiGHS without presolve before the CBC fallback -- web output differs at solver tolerance

`_assert_optimal` re-solved any non-optimal first solve on CBC (a subprocess,
~20ms a solve) before believing the demand unsatisfiable, and the CBC solver
then carried the rest of that call's refinement. On the web config that was not
rare: **62 of the mastery-costs page's 192 items** (37 "Infeasible", 24
"Unbounded", 1 "UnboundedOrInfeasible" from HiGHS -- all feasible, CBC solved
every one) and 3 of the solver page's solves -- ~45% of the mastery-costs page's
time. Every one of those models solves to optimality in HiGHS with presolve off
-- presolve mis-flags them; the costs reach 6e-11 against a 1e-7..1e5 matrix --
once a MIP (the models carry one integer, `days_available`) also stops presolving
at its search nodes (`mip_root_presolve_only`; without it 12 of the 62 still
failed). So `_assert_optimal` now tries `FastHiGHS(presolve="off",
mip_root_presolve_only=True)` first and keeps CBC as the last resort; neither web
view calls CBC any more.

The CLI's normal and `--mastery-costs` scenarios never hit the fallback and are
unchanged. The `--ascend` mode does -- 115 times in the fresh-account 0->200 climb
-- and there it is kept CBC-only (`cbc_only`, added after the fact): the climb
rounds fractional LP solutions stage by stage and carries each stage's plan into
the next, so a different but equally optimal vertex at one fallback (the two
solvers' objectives agree to ~1e-3 minutes at all 115) sent the whole climb down
another path -- its fastest plan came out at 92,440 gold instead of 49,444, at
the same 0m. With CBC kept there, the climb's output is identical to `main`'s
again (and it still drops from 68s to 60s).

The web views now take those items' optima from HiGHS instead of CBC --
equally optimal, but where the optimum is not unique the reported plan can
differ at solver-tolerance level:

* mastery-costs page: every time identical, and the "cannot" lists; 61 of 192
  gold figures differ, all by under 0.0001% except Sunflower Oil's Megamaster
  (665,219.36 -> 665,249.62 gold, +0.005%: the stage-2 tie-break minimises total
  value, not gold, so equally-valued plans can spend different gold);
* solver page: one frontier plan's time moves by 0.002 seconds and its gold by
  0.0002 (both print the same); the selected plan's Small Spring explores
  yield 1 Wood fewer, Gary's Crushroom 3 Mushroom Paste fewer, the Surplus
  section 5 Hammer fewer, and the passive-production rows tied on quantity and
  time now list alphabetically instead of in the CBC solution's order.

### 3. Mastery costs' items on a thread pool -- output identical

The mastery-costs sweep is 192 independent `fastest_endpoint` solves, and
highspy releases the GIL inside `Highs.run` (8 threads ran a batch of HiGHS
solves 5.8x faster than one). `solve_mastery_costs` now walks the candidates
first (status, notes, which tiers to solve -- no solving), then runs each item's
tier climb on a `ThreadPoolExecutor` and gathers the results back in candidate
order, so the table is identical at any worker count (checked at 1, 2, 4, 6, 8,
10 and 14). Each item builds its own LP; the threads share only read-only
inputs.

The CLI sweep by worker count (`--jobs`): 1 -> 2.34s, 2 -> 1.45s, 4 -> 1.07s, 6
and up -> 1.01s. Past ~6 the Python model building between solves -- which
still holds the GIL -- is the limit, so the default is 8 (`MASTERY_COST_WORKERS`;
re-tuned to 6 in step 8).

### 4. The frontier's sweeps and exact finish on a thread pool -- output identical

After step 2, three quarters of a solver-page request was HiGHS itself: ~220
solves (192 LPs at ~1.4ms, 27 MIPs at ~9ms). Most of them are independent: the
epsilon sweep's ~45 capped solves, the slow regime's ~45, and the exact
(integer) re-solves of the plans that survive thinning.

`frontier` now runs them on a `ThreadPoolExecutor` (`FRONTIER_WORKERS` = 4; 5
after step 8):

* `_sweep` splits its caps into contiguous runs solved concurrently -- the first
  on the given base, each other on its own copy (`make_base`: `_build_base` from
  the same inputs; a base is one mutable LP, so threads cannot share one) -- and
  returns a collector instead of a list, so the epsilon sweep runs while the
  main thread builds the slow regime's base and solves its probe. `post`
  (quantize + surplus credit) runs on each plan on its sweep thread.
* the exact finish re-solves each thinned plan on the base it came from; plans
  sharing a base take turns, plans on different bases run concurrently.

A capped solve is a cold solve of the same model whichever base copy runs it
(same rows, same order, same columns), so the plans -- and their order, which
the dedup's first-wins relies on -- are unchanged; `post` is per plan. By worker
count (solver page / CLI): 1 -> 0.684s / 0.705s, 2 -> 0.533s / 0.556s, 3 ->
0.520s / 0.526s, 4 -> 0.474s / 0.489s, 6 -> 0.482s / 0.491s, 8 -> 0.497s /
0.500s; past 4 each extra base copy's build (Python, so GIL-bound) costs more
than the extra overlap saves.

### 5. Every exact-finish plan concurrently -- output identical

A timeline of the threads showed the exact finish's critical path was the
plans sharing a base (the fast and cheap endpoints' plans both live on
`base_active`), re-solved one after another as integer MIPs (~25-60ms each).
Now every plan is its own task: the first plan from each base re-solves on
that base, any other on a fresh copy -- the same cold solve of the same model.

### 6. HiGHS's feasibility-jump heuristic off -- output identical, except two wrong rows put right

The integer (MIP) solves -- the fixed point's fastest endpoints, the exact
finish, the reference endpoint -- were now the critical path: 27 MIPs a solver
page at ~9ms against ~1.4ms for an LP, though each has a single integer
(`days_available`; a few Pumpkin Juices when on). Half of each MIP's time was
HiGHS's feasibility-jump heuristic hunting for a first incumbent, which the root
LP all but hands over on these models: with it off the MIPs took 0.128s instead
of 0.274s and returned bit-identical solutions on all 27.

Across the CLI scenarios and the solver page the output is identical. On the
mastery-costs page two rows change -- and both were wrong before:

| row | before | after |
| --- | --- | --- |
| Runestone 10 -> Grandmaster | 51d 2h, 0 gold | 17d 9h, 2,536 gold |
| Runestone 16 -> Grandmaster | 82d 23h, 0 gold | 27d 22h, 1,522 gold |

With the heuristic on, HiGHS's presolve returned these two "fastest" plans as
*optimal* at ~3x the true optimum (objective 121,976 vs 40,259 minutes for
Runestone 16) -- the same numerical fragility as step 2, silently this time.
With it off, presolve calls those models infeasible instead, so step 2's
fallback re-solves them without presolve and finds the real optimum. A check of
all 192 first solves of the sweep, presolve on vs off, finds no other model
where both claim optimal but disagree by more than a minute.

### 7. Surplus leftovers in one pass over the touched items -- output identical

With the solves overlapping, the Python between them -- which takes turns on
the GIL -- set the sweeps' pace, and the largest piece was `post`'s surplus
credit on every frontier plan. `accumulated_leftovers` called `plan_balance`
twice and walked the whole inventory (thousands of items on a real save) three
more times, to subtract from each leftover the held share that cancels it
exactly for every item the plan never touches. It now computes the balance
once and walks only the touched items (needed, made, opened, gathered or
passively produced); the arithmetic per item is unchanged -- the result was
checked equal, float for float, on all 148 frontier plans of three web solves.

### 8. Reuse what builds share; re-tune the thread counts -- output identical

The threaded mastery-costs sweep was now paced by its GIL-bound model building:
`_build_base` was over half of a sweep's Python. Two of its parts depend only on
inputs every build in a sweep shares (the offers, recipes and openables -- and
the frontier's base copies share all of them):

* **`_obtainable`** and what derives from it (craftable recipes, usable offers
  and openables, the reachability indexes), recomputed per build -- now cached
  per `(offers, recipes, openables, buyable, held)`;
* **the offer scan**: every build walked every usable offer's whole bundle (on a
  real save, ~12,000 item-offer pairs) to find the offers yielding a reachable
  item and to index their terms -- now a per-item index of producing/consuming
  offers is built once per offers list, and a build reads just its reachable
  items' entries.

`_cached` keys an entry by the inputs' identity and keeps the inputs alive in
it (so an `id` cannot be reused while cached), under a lock since builds run on
solver threads. Variables are now created with `problem.add_variable`, PuLP 4.0's
form, which skips a deprecation warning per variable.

The models are unchanged -- every row of every build dumps identical -- and so
is every output. With building cheaper, the thread counts were re-measured: the
mastery sweep is flat from 6 to 8 threads (0.524s / 0.530s) and slower past it,
the frontier best at 5 (0.285s against 0.301s at 4), so the defaults are now
`MASTERY_COST_WORKERS = 6` and `FRONTIER_WORKERS = 5`.

### 9. Thread counts capped at the CPUs available -- output identical

The deployment (`deploy/cloudrun.sh`) runs on **1 vCPU** by default, where the
solver threads of steps 3-5 can only take turns -- and the frontier's per-thread
base copies would be wasted builds. `optimizer.solver_threads(preferred)` now
caps every thread count at `available_cpus()` -- the process's affinity, then a
Linux cgroup v2 CPU quota (a container's `--cpu`) -- with `FARM_SOLVER_THREADS`
as an explicit override; at 1 the frontier skips its pool and copies entirely
(the original sequential path) and the mastery sweep runs in turn. Outputs are
identical either way (checked with `FARM_SOLVER_THREADS=1` and at the default).
`scripts/bench_solve.py --threads 1` measures the single-CPU case.

## On one CPU

What a 1-vCPU instance (the deployment's default) gains -- `bench_solve.py
--threads 1`. `main` never used threads, so its figures are the same:

| | solve | mastery | cli | cli-mc |
| --- | --- | --- | --- | --- |
| `main` (`0875ebf`) | 1.079s | 5.646s | 1.068s | 2.904s |
| branch at step 9, 1 thread | 0.508s | 1.247s | 0.614s | 1.331s |
| branch at step 10, 1 thread | 0.395s | 1.258s | 0.556s | 1.336s |
| branch at step 11, 1 thread | 0.395s | 1.258s | 0.531s | 1.303s |
| branch at step 14, 1 thread | 0.371s | 1.24s | 0.491s | 1.30s |
| branch at step 15, 1 thread | 0.359s | 1.24s | 0.48s | 1.30s |
| speed-up | 3.0x | 4.5x | 2.2x | 2.2x |

Everything except steps 3-5 (the threads) counts here: the PuLP hand-over, the
CBC fallback gone, the MIP heuristic, the leftovers pass, the build reuse and
the warm starts.
With more vCPUs each request also uses the threads -- the main table's figures
are this 14-core machine at the defaults (5 frontier threads, 6 mastery).

### 10. Warm-start the refinement stages -- displayed output identical

On one CPU the solver page was now three quarters HiGHS: ~190 LPs at ~1.1ms
and ~27 MIPs. Two thirds of those LPs are `_solve_configured`'s refinement
stages, each re-optimising the previous stage's model with its optimum pinned --
the same rows plus a pin or two, a new objective -- yet each started from
scratch (fresh `Highs`, presolve, a full simplex).

`FastHiGHS` now keeps the `Highs` of an optimal LP solve on the problem, and when
the next solve differs only by its objective, column bounds and *added* rows, it
adds the rows, swaps the costs and bounds and runs on with the basis still
loaded (`_extend`). Every call's first solve is still cold (the previous call's
pins were removed, so the rows no longer extend the loaded ones), as is every
MIP, every solve after a non-optimal one, and any change of solver options; the
warm instance is per problem, so per base, so per thread.

A warm re-optimisation reaches the same optimum, so every CLI scenario, the
solver page and the mastery-costs page's times are byte-identical; 34 of the
mastery-costs page's gold figures move in their last digits (at most 0.0002
gold), so every displayed figure and the row order are unchanged. Solver page
on one CPU 0.508s -> 0.395s; at the default threads 0.275s -> 0.252s. (The
mastery sweep's refinements are mostly MIPs, which stay cold.)

### 11. Import rich and multiprocessing only where used -- output identical

Every CLI run paid for importing rich (its Markdown renderer pulls in
markdown-it) and `concurrent.futures.process` (multiprocessing), though rich only
renders a `--format markdown` report to a terminal and the process pool only
serves `--ascend`. Both are now imported where they are used. `import main`
drops from ~72ms to ~50ms, and each CLI run by ~25-30ms; the web views (a
warm process) are unaffected.

### 12. Mastery-costs sweep on worker processes -- output identical

On threads the sweep was bound by its Python (a third of each item, taking
turns on the GIL): six threads went no faster than 0.51s. A full sweep now runs
on worker processes -- a `ProcessPoolExecutor` ("spawn" context, never "fork":
the parent may hold threads and live HiGHS state) kept for the process's life,
so a web server pays the ~0.1s start-up once, not per request. Worker `k` takes
items `k`, `k + n`, ... (interleaved, so the costly ones spread out) with the
shared inputs (`_ClimbInputs`: offers, recipes, prices, quantizer, ...) shipped
once per batch; results come back in item order, so the table is identical.
Measured by worker count on this machine (web page / CLI): 4 -> 0.37s / 0.55s,
6 -> 0.32s / 0.51s, 8 -> 0.24s / 0.42s, 10 -> 0.22s / 0.41s; the default is now
`MASTERY_COST_WORKERS = 10`, capped at the CPUs available as before.

Caveats, and what guards them:

* **Spawned workers re-run the caller's main script** (as `__mp_main__`) before
  they can run anything, so a script that solves at its top level, without an
  `if __name__ == "__main__"` guard, would run again in every worker. Processes
  are only used when `_spawn_safe()`: an interactive session, or a known-guarded
  entry point (`main.py`, `manage.py`, `gunicorn`, `bench_solve.py`); anything
  else stays on threads. `FARM_SOLVER_PROCESSES=0`/`1` overrides.
* A pool that breaks (a worker dies) falls back to threads for that sweep.
* Each worker is a full interpreter with the solver imported (~60-80MB), so up
  to 10 of them on a big machine; on the 1-vCPU deployment the worker count is 1
  and no pool is ever started.
* A handful of items -- the expand toggle's further tiers -- stays on threads,
  which cost nothing to start.

### 13. Run known work ahead -- output identical

Two solves on the solver page's critical path had their inputs long before
their turn came:

* the fixed point's **confirming iteration**: once an iteration sets a horizon,
  the next one usually only confirms it (`_makespans_close`), after which the
  frontier runs at exactly that horizon -- so `solve_frontier` now starts that
  frontier (`frontier_at`) on a look-ahead thread as soon as the horizon is set,
  and uses it if the confirmation comes (else the loop goes on as before, and
  the early start is discarded);
* the **reference endpoint** `derive_report_inputs` judges passive completion
  against depends only on the final frontier's offers, so it now starts beside
  that frontier and rides along on `FrontierResult.reference`.

Both are the same deterministic solves, only started earlier, so nothing
changes. They need spare CPUs, so they only run ahead when `solver_threads`
allows more than one (`_LOOKAHEAD`, two threads); on one CPU the order is as it
was.

### 14. The surplus LP straight to HiGHS, from a cached shape -- output identical

`post` values every frontier plan's surplus with a small LP (`surplus_sale_value`:
craft the leftovers into the sellable goods for the most gold), built in PuLP per
plan though its shape depends only on the recipes, prices and crafting bonus --
the leftovers are just its right-hand sides. `_surplus_model` now builds that
shape once (cached like the build caches, step 8) exactly as `FastHiGHS` would lay
out the PuLP formulation -- rows per item in order, each row's terms in PuLP's
order, columns `s0`, `s1`, ... in name order, the maximised objective negated --
and each call only fills in the right-hand sides, solves, and reads back each
good's net value the way `LpAffineExpression.value()` sums it. The PuLP
formulation stays as `_surplus_sale_value_pulp`, used on any other backend, and
was checked equal to the direct one -- every returned row, float for float -- on
all 191 calls of four web solves.

### 15. Build the stage-3 objective once per base -- output identical

The refinement's last stage minimises activity -- the sum of every craft, draw,
open and buy variable, ~600 terms -- and rebuilt that sum with `lpSum` on every
call, though it is the same expression on every call on a base. `_Base.activity`
now keeps it (built on first use; the stage only runs when the base has such
variables, so the expression is never empty and PuLP never folds a dummy
variable into it).

### Memory: release the bases with the plans -- output identical

The web app caches each solve's frontier (`SessionCache`, 64 entries), and every
plan carried `_origin` -- its whole base: the LP, its `Highs` instance, and with
solver threads one base copy per sweep thread -- though nothing reads it once
`frontier` returns. Twelve cached sessions cost +198 MB of RSS at the default
threads against +52 MB on `main`; on a multi-vCPU instance with the default
512Mi a full cache would not fit. `frontier` now returns its plans with the
origin dropped (`_released`), as does the early-started reference endpoint.
Python-level memory held per cached session is now the same at any thread
count (~1.6 MB), and 12 sessions cost +48 MB of RSS on one thread (below
`main`); threads add a roughly constant ~50-60 MB of allocator arenas and
stacks, which does not grow with the cache.
