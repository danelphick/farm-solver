"""Time the solver's two web views and their CLI twins.

Against the newest ``mydata/`` pastes, times:

* ``solve`` -- ``webapi.solve`` + ``webapi.render`` with default params: the
  solver page's first (uncached) request, the tower goal on the web config;
* ``mastery`` -- ``webapi.mastery_costs``: the mastery-costs page's request;
* ``cli`` -- ``python main.py`` in a fresh process (``config.toml``, imports
  and all);
* ``cli-mc`` -- ``python main.py --mastery-costs`` in a fresh process.

The two in-process cases run once to warm up (a web server's process is warm),
then ``--repeat`` times; the CLI cases are cold every time, like a real run.
Reports the fastest and the median of each. ``--threads N`` sets
``FARM_SOLVER_THREADS`` for every case -- ``--threads 1`` stands in for a
single-CPU host (the Cloud Run deployment's default ``--cpu 1``), where the
solver threads cannot help. ``plans/FASTEST_SOLVE.md`` records what each
optimisation step did to these numbers.

Run with: ``uv run python scripts/bench_solve.py [--repeat N] [--threads N] [--only NAME ...]``
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import highspy  # noqa: E402

import main  # noqa: E402
import webapi  # noqa: E402

CASES = ("solve", "mastery", "cli", "cli-mc")


def _pastes() -> tuple[str, str]:
    data = ROOT / "mydata"
    mastery = main._latest_dated_file(data, "mastery-")
    inventory = main._latest_dated_file(data, "inventory-")
    if mastery is None or inventory is None:
        sys.exit("bench_solve: needs mydata/mastery-*.txt and mydata/inventory-*.txt")
    return mastery.read_text(), inventory.read_text()


def _in_process(fn: Callable[[], object], repeat: int) -> list[float]:
    with contextlib.redirect_stdout(io.StringIO()):
        fn()  # warm-up
        times = []
        for _ in range(repeat):
            started = time.perf_counter()
            fn()
            times.append(time.perf_counter() - started)
    return times


def _fresh_process(argv: list[str], repeat: int) -> list[float]:
    times = []
    for _ in range(repeat):
        started = time.perf_counter()
        subprocess.run(
            [sys.executable, str(ROOT / "main.py"), *argv],
            cwd=ROOT, stdout=subprocess.DEVNULL, check=True,
        )
        times.append(time.perf_counter() - started)
    return times


def main_() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--only", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--threads", type=int, default=None)
    opts = parser.parse_args()
    if opts.threads is not None:
        os.environ["FARM_SOLVER_THREADS"] = str(opts.threads)  # inherited by the CLIs

    mastery_text, inventory_text = _pastes()

    def web_solve() -> None:
        args = webapi.params_to_args({})
        session = webapi.solve(mastery_text, inventory_text, args)
        webapi.render(session, args)

    def web_mastery() -> None:
        webapi.mastery_costs(mastery_text, inventory_text, webapi.params_to_args({}))

    runs = {
        "solve": lambda: _in_process(web_solve, opts.repeat),
        "mastery": lambda: _in_process(web_mastery, opts.repeat),
        "cli": lambda: _fresh_process([], opts.repeat),
        "cli-mc": lambda: _fresh_process(["--mastery-costs"], opts.repeat),
    }
    print(
        f"python {platform.python_version()}, highspy {highspy.Highs().version()}, "
        f"{os.cpu_count()} cpus, repeat {opts.repeat}, "
        f"threads {os.environ.get('FARM_SOLVER_THREADS', 'default')}"
    )
    for name in opts.only:
        times = runs[name]()
        print(
            f"{name:8s} min {min(times):6.3f}s  median {statistics.median(times):6.3f}s"
        )


if __name__ == "__main__":
    main_()
