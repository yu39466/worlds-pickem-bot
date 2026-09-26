"""Small TTL cache shared by the source modules.

Leaguepedia rate-limits hard - two uncached queries back to back can trip it -
so this is a correctness requirement, not an optimisation.

Deliberately thread-based rather than async: the upstream clients are
synchronous, and the MCP tools call them through anyio.to_thread.
"""

import threading
import time

_values = {}            # key -> (expires_at_monotonic, value)
_locks = {}             # key -> Lock
_registry_lock = threading.Lock()


def _lock_for(key):
    with _registry_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _fresh(entry):
    return entry is not None and entry[0] > time.monotonic()


def get(key, ttl, fn):
    """Return a cached value, calling fn() only when the entry is missing or stale.

    If several threads ask for the same cold key at once, one fetches and the
    others wait on the lock, then read what it stored. Without this, ten users
    running a command in the same second would send ten identical queries.
    """
    entry = _values.get(key)
    if _fresh(entry):
        return entry[1]

    with _lock_for(key):
        entry = _values.get(key)
        if _fresh(entry):
            return entry[1]
        value = fn()
        _values[key] = (time.monotonic() + ttl, value)
        return value


def get_stale(key):
    """Last known value regardless of age, or None.

    Used as a fallback when the upstream rate-limits us: serving four-minute-old
    picks beats telling the user nothing.
    """
    entry = _values.get(key)
    return entry[1] if entry else None


def invalidate(key=None):
    """Drop one key, or everything when key is None. Mainly for tests."""
    if key is None:
        _values.clear()
    else:
        _values.pop(key, None)
