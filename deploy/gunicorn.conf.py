"""Gunicorn config for the farm-solver web app, tuned for Google Cloud Run.

The README's "Deploying it" recipe (2 gthread workers x 4 threads) assumes a
box with cores to spare. A Cloud Run instance is sized in whole vCPUs and
billed by the vCPU-second, so the defaults here are per-*instance* and scale
out horizontally instead: one worker per vCPU, and Cloud Run starts another
instance when concurrency is exceeded (see deploy/cloudrun.sh).

Every value is env-overridable so the same image can run on a bigger box.
"""

import os

# Cloud Run injects $PORT and routes to it; 8080 is its default and matches the
# Dockerfile's EXPOSE.
bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"

# One worker per vCPU, deliberately. solverapp.cache.SOLVE_SEMAPHORE already
# limits each *process* to one LP solve at a time (PuLP/HiGHS hold solver state
# that is not safe to touch from two threads), so on a 1-vCPU instance a second
# worker buys no extra solve throughput -- it just puts two CPU-bound frontier
# sweeps on one core and makes both about twice as slow. Raise this together
# with the instance's --cpu, not on its own.
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))

# Threads, on the other hand, are what keep the app responsive *while* a solve
# holds the semaphore: a frontier row click is a refilter (webapi.render against
# a cached SolveSession), a few milliseconds of pure Python, and it should not
# queue behind someone else's ~1-3s re-solve.
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

# Must exceed the slowest thing a request can legitimately do, or gunicorn kills
# the worker mid-solve. The ceiling is views._MASTERY_SEMAPHORE_TIMEOUT_SECONDS
# (60s waiting for the semaphore) plus the mastery-costs sweep itself, which is
# one LP per remaining (item, tier) pair -- seconds on a well-progressed save,
# and slower on a shared-core cloud vCPU than on a desktop.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30

# Cloud Run's frontend holds the client connection, so keepalive here only
# affects the hop from that proxy.
keepalive = 5

# Never recycle workers on a request count. Each worker owns its own
# solverapp.cache.SessionCache (an in-process LRU(64)/TTL(30 min) dict); killing
# one throws that cache away, turning every user's next click from a
# milliseconds-long refilter back into a full re-solve.
max_requests = 0

# Fork *before* importing the app. SolverappConfig.ready() pins the HiGHS
# backend at startup, and preloading would run that in the master and then fork
# a process holding live highspy state -- the same non-fork-safety that makes
# main.py's ascension sweep use ProcessPoolExecutor rather than threads. With
# one worker there is nothing to save by preloading anyway.
preload_app = False

# Cloud Run collects stdout/stderr into Cloud Logging.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
