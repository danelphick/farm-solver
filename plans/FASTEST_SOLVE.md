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
