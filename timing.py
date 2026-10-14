"""Wall-clock and CPU time of a solve, across every core it used.

The OS already sums a process's CPU over all its threads, so
:func:`time.process_time` covers the HiGHS threads and the frontier's thread
pool. What it misses is the ``--mastery-costs`` worker processes
(``main._climb_processes``): their pool outlives a request, so the OS's
"children" totals would not include them until the workers exit. Each worker
batch therefore measures its own CPU and the parent adds it here
(:func:`add_worker_cpu`), so :class:`SolveTimer` can count it.

In a server running several solves at once the process-wide figure mixes them;
it is exact while one solve runs at a time (``SOLVE_SEMAPHORE``'s normal case).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_worker_cpu = 0.0
_worker_cpu_lock = threading.Lock()


def add_worker_cpu(seconds: float) -> None:
    """Record CPU seconds a worker process spent on this process's behalf."""
    global _worker_cpu
    with _worker_cpu_lock:
        _worker_cpu += seconds


def worker_cpu() -> float:
    """CPU seconds worker processes have reported so far (see
    :func:`add_worker_cpu`)."""
    with _worker_cpu_lock:
        return _worker_cpu


@dataclass(frozen=True)
class Timing:
    """``wall`` and ``cpu`` seconds; ``cpu`` summed over every core used."""

    wall: float
    cpu: float

    @property
    def cores(self) -> float:
        """Average cores busy: CPU per wall-clock second."""
        return self.cpu / self.wall if self.wall > 0 else 0.0

    def text(self) -> str:
        return (
            f"wall {self.wall:.2f}s, CPU {self.cpu:.2f}s "
            f"({self.cores:.1f} cores on average)"
        )

    def as_json(self) -> dict[str, float]:
        return {
            "wall_ms": round(self.wall * 1000),
            "cpu_ms": round(self.cpu * 1000),
            "cores": round(self.cores, 1),
        }


class SolveTimer:
    """Measure the wall-clock and CPU time from construction to :meth:`stop`."""

    def __init__(self) -> None:
        self._wall = time.perf_counter()
        self._cpu = time.process_time()
        self._workers = worker_cpu()

    def stop(self) -> Timing:
        cpu = time.process_time() - self._cpu + worker_cpu() - self._workers
        return Timing(wall=time.perf_counter() - self._wall, cpu=cpu)
