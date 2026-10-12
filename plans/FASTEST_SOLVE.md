# Fastest solve (branch `perf/fastest-solve`)

Goal: make the two web views -- the **solver page** (`/api/solve`:
`webapi.solve` + `webapi.render`) and the **mastery-costs page**
(`/api/mastery-costs`: `webapi.mastery_costs`) -- and their CLI twins
(`python main.py`, `python main.py --mastery-costs`) as fast as possible,
**without changing any result**.

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

Every step must leave the output byte-identical to `main`'s, checked three
ways:

* a harness dumps **every LP `_build_base` builds** (every row in order, every
  coefficient, bound and variable) and the printed report, for twelve CLI
  scenarios -- the default tower goal, `-i`/`-m` goals (incl. Pumpkin Juice,
  `--self-sourced`, AP-trading and seed-limited crops, the Sugar Cane Mill),
  `--mastery-costs` (both ways), `--ascend` (incl. the fresh-account 0->200
  climb) and Veggie Juice off -- run on `main` and on the branch with the same
  `PYTHONHASHSEED`, then diffed;
* `uv run pytest -q`;
* `uv run --group web python web/manage.py test solverapp`.

A step that cannot keep the output identical says so, and why, below.

## Results

| step | solve | mastery | cli | cli-mc |
| --- | --- | --- | --- | --- |
| `main` (`0875ebf`) | 1.079s | 5.646s | 1.068s | 2.904s |
| 1. `FastHiGHS`: a fast PuLP -> HiGHS hand-over | 0.770s | 5.155s | 0.716s | 2.386s |
| 2. HiGHS without presolve before the CBC fallback | 0.684s | 3.019s | 0.703s | 2.365s |

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

The CLI scenarios never hit the fallback and are unchanged. The web views now
take those items' optima from HiGHS instead of CBC -- equally optimal, but where
the optimum is not unique the reported plan can differ at solver-tolerance level:

* mastery-costs page: every time identical, and the "cannot" lists; 61 of 192
  gold figures differ, all by under 0.0001% except Sunflower Oil's Megamaster
  (665,219.36 -> 665,249.62 gold, +0.005%: the stage-2 tie-break minimises total
  value, not gold, so equally-valued plans can spend different gold);
* solver page: one frontier plan's time moves by 0.002 seconds and its gold by
  0.0002 (both print the same); the selected plan's Small Spring explores
  yield 1 Wood fewer, Gary's Crushroom 3 Mushroom Paste fewer, the Surplus
  section 5 Hammer fewer, and the passive-production rows tied on quantity and
  time now list alphabetically instead of in the CBC solution's order.
