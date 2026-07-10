"""In-process session cache + solve concurrency guards, per gunicorn worker.

No state is shared across workers or processes: each fingerprint's
``SolveSession`` lives only in the worker that computed it, so a request
landing on a different worker (or after a restart/eviction) is simply a cache
miss -- ``webapi.solve``/``render`` re-run in ~0.5-3s (see ARCHITECTURE.md's
"Concurrency" note). This is a deliberate simplicity trade-off for a small
deployment: no sticky sessions, no shared cache to keep consistent -- a miss
just costs one re-solve, never a wrong answer.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

import webapi

MAX_ENTRIES = 64
TTL_SECONDS = 30 * 60

# Guards both _entries (the session cache) and _key_locks (below); every
# critical section under it is a plain dict operation, never a solve.
_lock = threading.Lock()
_entries: OrderedDict[str, tuple[webapi.SolveSession, float]] = OrderedDict()


class SessionCache:
    """A small LRU (``MAX_ENTRIES``) + TTL (``TTL_SECONDS``) SolveSession cache."""

    def get(self, key: str) -> webapi.SolveSession | None:
        with _lock:
            entry = _entries.get(key)
            if entry is None:
                return None
            session, expiry = entry
            if time.monotonic() > expiry:
                del _entries[key]
                return None
            _entries.move_to_end(key)
            return session

    def put(self, key: str, session: webapi.SolveSession) -> None:
        with _lock:
            _entries[key] = (session, time.monotonic() + TTL_SECONDS)
            _entries.move_to_end(key)
            while len(_entries) > MAX_ENTRIES:
                _entries.popitem(last=False)


CACHE = SessionCache()

# One LP solve at a time per process: PuLP/HiGHS hold solver state that isn't
# safe under concurrent solves in the same process (see ARCHITECTURE.md).
# Acquired even on a cache hit -- render()'s horizon guard can still trigger
# a re-solve (see webapi.render) -- so every request funnels through this.
SOLVE_SEMAPHORE = threading.BoundedSemaphore(1)

_key_locks: dict[str, threading.Lock] = {}


def key_lock(key: str) -> threading.Lock:
    """A lock scoped to one fingerprint.

    Two concurrent requests for the same fingerprint (a user double-clicking,
    or two tabs) would otherwise both take a cache miss and both solve the
    same thing (a dogpile); serialising on this lets the second wait for the
    first's result and then hit the cache instead. Never released here by
    eviction -- a small, unbounded (but very lightweight: one ``Lock`` per
    distinct fingerprint ever seen) growth, acceptable for this deployment
    size; see ARCHITECTURE.md if this ever needs revisiting.
    """
    with _lock:
        return _key_locks.setdefault(key, threading.Lock())
