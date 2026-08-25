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
DB is truth, counters are a cache). The counter cardinality is bounded to the 9 fixed
function names below -- no user-controlled key component, so no unbounded growth
(threat T-35-02, accepted).
"""

from __future__ import annotations

from typing import Any


_NAMESPACE = "phaze:pipeline"

# The pipeline functions that carry a deterministic key + maintained counters.
# MUST stay in sync with ``deterministic_key._KEY_BUILDERS`` -- the drift-guard test
# (tests/test_deterministic_key.py) enforces the routable-task universe; this tuple
# is the read-side enumeration ``read_counters`` reports over.
PIPELINE_FUNCTIONS: tuple[str, ...] = (
    "process_file",
    "extract_file_metadata",
    # phaze-2akf: search_tracklist / scrape_and_store_tracklist are gone with the legacy scrape
    # path. Their durable Redis counters are deliberately NOT deleted -- they are never-reset
    # INCRs that record work genuinely completed, and read_counters simply stops enumerating them.
    "match_tracklist_to_discogs",
    "generate_proposals",
    "push_file",
)


def _enqueued_key(function: str) -> str:
    """Return the durable enqueued-counter key for ``function``."""
    return f"{_NAMESPACE}:enqueued:{function}"


def _completed_key(function: str) -> str:
    """Return the durable completed-counter key for ``function``."""
    return f"{_NAMESPACE}:completed:{function}"


def _to_int(value: Any) -> int:
    """Coerce a Redis return value (``None`` / ``str`` / ``int``) to ``int``. REFUSE ``bytes``.

    A missing key reads back ``None`` -> ``0``.

    IMPLEMENTER'S DECISION, not an operator decision (phaze-ooe68, dev/redismode, 2026-08-24;
    labelled per ADR-0012 rule 2 so it invites the review an operator label would suppress).
    This helper used to ACCEPT ``bytes`` and decode them, on the stated grounds that that is
    "the default when the SAQ queue's Redis client is not ``decode_responses=True``". The
    code-side client-mode measurement recorded on phaze-ooe68 refutes the premise:

    * ``phaze:pipeline:{enqueued,completed}:*`` has exactly ONE production reader --
      :func:`phaze.routers.pipeline.dashboard_stats._read_pipeline_counters`, which passes
      ``app.state.redis`` (``main.py:161``, ``decode_responses=True``). That yields ``str``.
    * The byte-mode reader the decode branch was written for was the ``controller_queue.redis``
      fallback, DELETED in Phase 36 -- ``dashboard_stats._read_pipeline_counters``'s own
      docstring says so ("the former ``controller_queue.redis`` fallback is gone").

    So the bytes branch had no caller, and its only live effect was to make a client-mode
    mismatch succeed SILENTLY -- consuming the very signal that a mismatch had occurred. The
    WRITERS are genuinely byte-mode (``queue.cache_redis``, ``queue_factory.py:95``), but an
    ``INCR`` is mode-independent on the wire: client mode changes what a READ decodes to and
    nothing else, so a byte-mode writer is not a reason to accept byte-mode reads.

    Refusing is loud but never fatal. The sole caller wraps ``read_counters`` in a broad
    ``except`` that logs ``pipeline_counters_degraded`` with ``exc_info`` and renders the
    dashboard from DB-truth instead (D-03: the DB owns every rendered ``done``; these counters
    are only a cache). A future rewiring onto a byte-mode client therefore surfaces as a warning
    carrying a traceback, rather than as numbers nobody can tell are stale or wrong.

    Contrast :func:`phaze.routers.execution._coerce_int`, the reader on the OTHER half of this
    same bytes-vs-str boundary (the ``exec:{batch_id}`` hash): it also refuses ``bytes``, but
    SILENTLY, by returning its default. Both dispositions are now pinned by tests --
    ``tests/analyze/core/test_pipeline_counters_client_mode.py`` and
    ``tests/review/routers/test_exec_hash_client_mode.py`` -- against real Redis.
    """
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        msg = (
            f"pipeline counters read back {type(value).__name__}, not str: read_counters requires the "
            "decode_responses=True client (app.state.redis, main.py:161), never a byte-mode client "
            "(queue.cache_redis, queue_factory.py:95). See phaze-ooe68."
        )
        raise TypeError(msg)
    return int(value)


async def incr_enqueued(redis: Any, function: str) -> None:
    """``INCR phaze:pipeline:enqueued:<function>`` (durable, no EXPIRE)."""
    await redis.incr(_enqueued_key(function))


async def incr_completed(redis: Any, function: str) -> None:
    """``INCR phaze:pipeline:completed:<function>`` (durable, no EXPIRE)."""
    await redis.incr(_completed_key(function))


async def read_counters(redis: Any) -> dict[str, dict[str, int]]:
    """Return ``{function: {"enqueued": N, "completed": M}}`` for the 9 known functions.

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
