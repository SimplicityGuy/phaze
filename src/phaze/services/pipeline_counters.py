"""Maintained Redis per-pipeline-function counters (Phase 35, D-02 / D-03).

Two durable cumulative counters per pipeline function, keyed in a fixed, bounded
namespace:

- ``phaze:pipeline:enqueued:<function>``  -- bumped from the central
  :func:`phaze.tasks._shared.deterministic_key.apply_deterministic_key`
  ``before_enqueue`` hook (one INCR per enqueue *attempt*).
- ``phaze:pipeline:completed:<function>`` -- bumped from the
  :func:`phaze.tasks._shared.deterministic_key.increment_completed`
  ``after_process`` hook, only on a ``Status.COMPLETE`` terminal outcome.

Unlike :func:`phaze.services.proposal.check_rate_limit` (a rolling 60s window that
sets ``EXPIRE``), these are **durable** caches: plain ``INCR`` with no TTL. They are
a fast cache for the per-job-type progress UI, NOT the rendering authority -- the DB
reconcile (``get_stage_progress``, 35-03) owns every rendered ``done`` value (D-03:
DB is truth, counters are a cache). The counter cardinality is bounded to the 8 fixed
function names below -- no user-controlled key component, so no unbounded growth
(threat T-35-02, accepted).
"""

from __future__ import annotations

from typing import Any


_NAMESPACE = "phaze:pipeline"

# The 8 pipeline functions that carry a deterministic key + maintained counters.
# MUST stay in sync with ``deterministic_key._KEY_BUILDERS`` -- the drift-guard test
# (tests/test_deterministic_key.py) enforces the routable-task universe; this tuple
# is the read-side enumeration ``read_counters`` reports over.
PIPELINE_FUNCTIONS: tuple[str, ...] = (
    "process_file",
    "extract_file_metadata",
    "fingerprint_file",
    "scan_live_set",
    "search_tracklist",
    "scrape_and_store_tracklist",
    "match_tracklist_to_discogs",
    "generate_proposals",
)


def _enqueued_key(function: str) -> str:
    """Return the durable enqueued-counter key for ``function``."""
    return f"{_NAMESPACE}:enqueued:{function}"


def _completed_key(function: str) -> str:
    """Return the durable completed-counter key for ``function``."""
    return f"{_NAMESPACE}:completed:{function}"


def _to_int(value: Any) -> int:
    """Coerce a Redis return value (``None`` / ``bytes`` / ``str`` / ``int``) to ``int``.

    A missing key reads back ``None`` -> ``0``. ``bytes`` (the default when the SAQ
    queue's Redis client is not ``decode_responses=True``) is decoded before parsing.
    """
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return int(value.decode())
    return int(value)


async def incr_enqueued(redis: Any, function: str) -> None:
    """``INCR phaze:pipeline:enqueued:<function>`` (durable, no EXPIRE)."""
    await redis.incr(_enqueued_key(function))


async def incr_completed(redis: Any, function: str) -> None:
    """``INCR phaze:pipeline:completed:<function>`` (durable, no EXPIRE)."""
    await redis.incr(_completed_key(function))


async def read_counters(redis: Any) -> dict[str, dict[str, int]]:
    """Return ``{function: {"enqueued": N, "completed": M}}`` for the 8 known functions.

    Reads both namespaces with two pipelined ``MGET`` calls (one round-trip each).
    Missing keys read back ``0``. The result is a fast cache reconciled against
    DB-truth on read (D-03) -- it never overrides a DB-rendered ``done``.
    """
    enqueued_keys = [_enqueued_key(fn) for fn in PIPELINE_FUNCTIONS]
    completed_keys = [_completed_key(fn) for fn in PIPELINE_FUNCTIONS]

    enqueued_vals = await redis.mget(enqueued_keys)
    completed_vals = await redis.mget(completed_keys)

    return {
        fn: {
            "enqueued": _to_int(enqueued_vals[i]),
            "completed": _to_int(completed_vals[i]),
        }
        for i, fn in enumerate(PIPELINE_FUNCTIONS)
    }


__all__ = [
    "PIPELINE_FUNCTIONS",
    "incr_completed",
    "incr_enqueued",
    "read_counters",
]
