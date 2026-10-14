import timing


def test_solve_timer_counts_cpu_reported_by_worker_processes():
    # A worker process's CPU never shows in this process's own process_time, so
    # whatever the workers report during the timed span is added on top.
    timer = timing.SolveTimer()
    timing.add_worker_cpu(2.5)
    measured = timer.stop()
    assert measured.cpu >= 2.5
    assert measured.wall >= 0


def test_timing_reports_the_average_cores_busy():
    measured = timing.Timing(wall=2.0, cpu=5.0)
    assert measured.cores == 2.5
    assert measured.as_json() == {"wall_ms": 2000, "cpu_ms": 5000, "cores": 2.5}
    assert measured.text() == "wall 2.00s, CPU 5.00s (2.5 cores on average)"
