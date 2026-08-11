"""Central deterministic-key ``before_enqueue`` hook + completion ``after_process`` hook.

Generalizes the Phase-32 ``process_file:<file_id>`` deterministic-key fix to the WHOLE
pipeline, enforced at the single SAQ ``before_enqueue`` chokepoint so no call site can
drift back to a random-uuid key (D-05, locked decision A). The 2026-06-11 queue-doubling
incident proved that random-uuid jobs cannot dedup against a deterministic re-enqueue;
centralizing key construction here makes every routable task schedule-safe by construction.

Two hooks live here:

- :func:`apply_deterministic_key` (``before_enqueue``): for any function registered in
  :data:`_KEY_BUILDERS`, sets ``job.key = "<function>:<natural_id>"`` UNCONDITIONALLY
  (overriding any caller-supplied key -- anti-drift, threat T-35-01) and folds in the
  best-effort ``enqueued`` counter INCR. Functions absent from the registry keep SAQ's
  random-uuid default key.
- :func:`increment_completed` (``after_process``): bumps the ``completed`` counter only
  on a ``Status.COMPLETE`` terminal outcome.

The ``process_file`` builder MUST compute the IDENTICAL string the existing
:func:`phaze.services.analysis_enqueue.process_file_job_key` produces
(``process_file:<file_id>``), so the already-keyed path stays a no-op-equivalent
(35-RESEARCH Q1).

NOTE -- ACCEPTED enqueued upward drift (plan-checker W3): this hook runs PRE-dedup, so a
duplicate-key re-enqueue that Redis dedup later no-ops STILL bumps ``enqueued``. That drift
is ACCEPTED: ``enqueued`` is a NON-AUTHORITATIVE soft hint only -- the UI renders the
em-dash ``-`` as the real denominator and ``get_stage_progress`` (DB-truth, 35-03) owns
every rendered ``done``. Do NOT add pre-dedup detection to "fix" this.

NOTE -- intent of the ``completed`` counter (plan-checker W4): :func:`increment_completed`
maintains ``phaze:pipeline:completed:<function>`` to satisfy D-02's mandate for MAINTAINED
per-function counters. No node renders it directly (every ``done`` renders from DB-truth
per D-03); it is a deliberate reconcile/backstop cache, NOT dead code. 35-04 documents how
``read_counters`` feeds reconcile-on-read without overriding the DB-truth ``done``.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from saq.job import TERMINAL_STATUSES, Status
import structlog

from phaze.services.pipeline_counters import incr_completed, incr_enqueued
from phaze.tasks._shared.replay_safety import LEDGER_REPLAY_REGENERATED, find_time_limited_paths


if TYPE_CHECKING:
    from collections.abc import Callable

    from saq import Job


logger = structlog.get_logger(__name__)


def _hash_ids(file_ids: Any) -> str:
    """Return an order-independent sha256 hex digest of a batch of ids.

    ``generate_proposals`` is a batch task (35-RESEARCH Q3): its job identity is the
    SET of ``file_ids``, not any single file. Sorting before hashing makes
    ``[A, B, C]``, ``[C, B, A]`` and ``[B, A, C]`` collapse to the SAME key, so a
    re-enqueue of the same batch dedups regardless of caller ordering. Per-file
    idempotency lives in the proposals upsert (35-02), not in this key.
    """
    joined = ",".join(sorted(str(i) for i in file_ids))
    return hashlib.sha256(joined.encode()).hexdigest()


# Each builder maps a job's kwargs (the task payload) to the natural id that makes a re-enqueue
# of the same logical work dedup. Natural ids VERIFIED present in each payload (35-RESEARCH Q1
# table). MUST cover the drift-guard test's routable universe (CONTROLLER_TASKS | AGENT_TASKS)
# minus the documented _UNKEYED_TASKS exemptions. The first entries also back
# ``pipeline_counters.PIPELINE_FUNCTIONS`` (dashboard counters); ``s3_upload`` is keyed here for
# scheduling-ledger dedup/re-drive (Phase 53, Plan 04) ahead of its dashboard-counter wiring,
# which lands with the live routing seam (Phase 55).
_KEY_BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "process_file": lambda k: str(k["file_id"]),
    "extract_file_metadata": lambda k: str(k["file_id"]),
    "match_tracklist_to_discogs": lambda k: str(k["tracklist_id"]),
    "generate_proposals": lambda k: _hash_ids(k["file_ids"]),
    # Phase 50 (CLOUDPIPE-05): push_file:<file_id> dedup collapses a double-tick of the
    # bounded cloud-window staging cron to a no-op (T-50-double-enqueue).
    "push_file": lambda k: str(k["file_id"]),
    # Phase 53 (KSTAGE-02): s3_upload:<file_id> dedup collapses a re-driven staging upload to a
    # no-op and persists the attempt counter the Plan-04 callback re-drive loop reads.
    "s3_upload": lambda k: str(k["file_id"]),
    # Phase 54 (KSUBMIT-01): submit_cloud_job:<file_id> dedup collapses a double enqueue of an
    # already-submitting file to a no-op, mirroring s3_upload; the reconcile re-drive loop relies
    # on this key so a still-submitting file is never double-submitted.
    "submit_cloud_job": lambda k: str(k["file_id"]),
    # phaze-6bkk: keyed on the pre-minted TagWriteLog id, NOT file_id. A re-enqueue of the SAME
    # audit row (a duplicate dispatch of one operator click) dedups to a no-op; a genuinely new
    # write -- or an undo, which is a second write of the same file -- mints a new log_id and is
    # correctly a distinct job. Keying on file_id would make an undo silently collapse into the
    # write it is reverting.
    "write_file_tags": lambda k: str(k["log_id"]),
    # phaze-5fta.3: a CONSTANT key -- the whole (release_group, date_order) key space is one
    # logical unit of work, and the task's only kwarg (batch_size) is a memory knob that does not
    # change what is computed. Keying it collapses an operator double-click into one full-corpus
    # sweep instead of two concurrent ones queueing behind the write phase's advisory lock. The key
    # frees itself the moment the job reaches a terminal status, so a genuine "recompute now that
    # the corpus changed" re-run is never blocked. The key text is a LITERAL rather than an import
    # of the learner's CONVENTION_SCOPE / CONVENTION_KIND: this module is ``_shared`` and the agent
    # worker imports it, so pulling in a service that imports ``phaze.models`` would break the
    # Postgres-free agent boundary (test_task_split). ``test_learner_key_matches_the_learner_module``
    # asserts the two agree, so the duplication cannot drift silently.
    "learn_filename_conventions": lambda _kwargs: "release_group:date_order",
}


def _warn_if_payload_is_time_limited(function: str, key: str, kwargs: dict[str, Any]) -> None:
    """Log LOUDLY when a producer writes time-limited material into the durable ledger (phaze-71nz).

    THE WRITE-SIDE HALF of the replay-safety invariant: a ``scheduling_ledger`` payload must be
    replayable at an arbitrary future time. This is the exact point where that stops being true --
    the payload is about to become durable -- so it is where the violation is cheapest to see. The
    2026-07-31 incident had NOTHING at this layer: 430 ``s3_upload`` rows carrying presigned URLs
    were written, orphaned, replayed and burned into terminal ``failed`` without a single log line
    distinguishing them from the 2,512 rows that replayed fine.

    DETECTS, never blocks. The ledger write itself is best-effort by contract (T-45-03) and an
    enqueue must never fail on a bookkeeping opinion; a producer that legitimately needs expiring
    material declares itself in ``replay_safety.LEDGER_REPLAY_REGENERATED`` (and is exempt here),
    and recovery's own :func:`phaze.tasks.reenqueue._replay_row` is the hard refusal.

    Only PATHS are logged -- the values are live credentials and must never reach a log sink.
    """
    if function in LEDGER_REPLAY_REGENERATED:
        return
    violations = find_time_limited_paths(kwargs)
    if not violations:
        return
    logger.error(
        "scheduling-ledger payload carries TIME-LIMITED material but its producer is declared replay-safe -- "
        "recovery will refuse to replay this row (phaze-71nz). A scheduling_ledger payload must be replayable "
        "at an arbitrary future time: store the durable inputs and re-derive, or declare the function in "
        "replay_safety.LEDGER_REPLAY_REGENERATED with a regenerator in reenqueue._REPLAY_REGENERATORS.",
        function=function,
        key=key,
        payload_paths=violations,
    )


async def apply_deterministic_key(job: Job) -> None:
    """SAQ ``before_enqueue`` hook -- set ``job.key`` deterministically + bump ``enqueued``.

    For a function in :data:`_KEY_BUILDERS`, sets ``job.key`` to
    ``"<function>:<natural_id>"`` UNCONDITIONALLY (overriding any caller-supplied key --
    anti-drift) so SAQ's per-queue ``incomplete``-set dedup collapses a repeat enqueue of
    the same logical work to a no-op. Functions NOT in the registry are left untouched
    (they keep SAQ's random-uuid default key).

    The ``enqueued`` counter INCR is folded in here (one hook does key + counter) and is
    strictly best-effort: the Redis handle is read from ``job.queue.cache_redis`` and any
    failure is logged, never raised -- a counter hiccup must never block an enqueue.
    """
    builder = _KEY_BUILDERS.get(job.function)
    if builder is None:
        return

    job.key = f"{job.function}:{builder(job.kwargs or {})}"

    # Best-effort enqueued counter. Phase 36: the broker is Postgres now, so the cache client
    # is the decoupled ``cache_redis`` handle the factory attaches to the queue object (NOT
    # ``job.queue.redis`` -- PostgresQueue has no such attribute). Degrade silently if it is
    # absent (e.g. a test fake without a wired cache_redis).
    try:
        redis = getattr(job.queue, "cache_redis", None)
        if redis is not None:
            await incr_enqueued(redis, job.function)
    except Exception:
        # Counter is a cache; never block the enqueue on a Redis hiccup.
        logger.warning("pipeline enqueued-counter increment failed", function=job.function, exc_info=True)

    # Phase 45 (L-01): durable scheduling-ledger WRITE at the single before_enqueue chokepoint.
    # The DB handle hangs off the queue (symmetric with cache_redis); it is ONLY present on the
    # control-side queues (controller + per-agent router queues). On the agent worker queue (and
    # test fakes) it is absent, so this whole block degrades to a logged no-op -- the agent
    # boundary stays Postgres-free (T-45-02). The import is function-LOCAL so the module-level
    # graph never pulls phaze.services.scheduling_ledger (and thus phaze.models / sqlalchemy.ext
    # .asyncio) on the agent path -- it executes only when ledger_sessionmaker is present.
    try:
        sm = getattr(job.queue, "ledger_sessionmaker", None)
        if sm is not None:
            # INTENTIONAL function-local import (PLC0415 suppressed on the import line below):
            # this module is _shared (the agent worker imports it); a top-level import of the
            # ledger service would drag phaze.models / sqlalchemy.ext.asyncio into the agent graph
            # and break the Postgres-free boundary (test_task_split). It runs only when a
            # control-side ledger_sessionmaker is present.
            from phaze.services.scheduling_ledger import upsert_ledger_entry  # noqa: PLC0415

            kwargs = dict(job.kwargs or {})
            _warn_if_payload_is_time_limited(job.function, job.key, kwargs)
            async with sm() as session:
                # apply_project_job_defaults is registered BEFORE this hook (queue_factory), so
                # job.timeout / job.retries are the FINAL effective policy here -- capture them so
                # recovery replays the SAME bound, else a recovered long job falls back to the 600s
                # role default and times out. NOTE (phaze-w55w1): `heartbeat` is NOT captured here,
                # and process_file's liveness now depends on it (it runs timeout=0). That gap is
                # closed at the other end -- apply_project_job_defaults PINS the heartbeat on every
                # enqueue, replays included -- rather than by widening this table.
                await upsert_ledger_entry(
                    session,
                    key=job.key,
                    function=job.function,
                    kwargs=kwargs,
                    timeout=job.timeout,
                    retries=job.retries,
                )
                await session.commit()
    except Exception:
        # The ledger is best-effort here; a hiccup degrades to "row not written" (recovered by the
        # Plan-04 backfill / next recovery) and must NEVER block an enqueue (T-45-03).
        logger.warning("scheduling-ledger upsert failed", function=job.function, key=job.key, exc_info=True)


async def increment_completed(ctx: dict[str, Any]) -> None:
    """SAQ ``after_process`` hook -- bump ``completed`` on COMPLETE + clear the ledger on terminal.

    Wired as a Worker constructor kwarg (``"after_process"``) in both worker settings dicts
    (35-RESEARCH Q2). ``after_process`` runs in a ``finally`` after EVERY outcome, so
    ``job.status`` is the authoritative terminal/non-terminal signal: ``finish()`` sets a
    terminal status, ``retry()`` sets ``Status.QUEUED``.

    Two best-effort actions, both gated on ``job.function in _KEY_BUILDERS`` and never raising:

    1. completed-counter INCR -- only on ``Status.COMPLETE`` (preserves the Phase-35 contract).
    2. Phase 45 (L-02, controller half) scheduling-ledger CLEAR -- on ``job.status in
       TERMINAL_STATUSES`` {COMPLETE, FAILED, ABORTED}, NOT on a retry (Status.QUEUED). Locked
       decision #1: a terminal ``failed`` clears the row (no poison re-queue) just like success.
       The clear only reaches Postgres when ``ledger_sessionmaker`` is present (controller
       worker); on the agent worker (no handle) it is a logged no-op -- agent-stage clears are
       Plan 02's job (the control-side callback handlers).

       phaze-3yln: this call is NOT an unconditional delete-by-key. ``clear_ledger_entry`` itself
       carries an ownership guard -- it no-ops when a same-key re-enqueue has already landed a
       LIVE (queued/active) ``saq_jobs`` row for this key between THIS job going terminal and this
       clear running (SAQ re-queues a terminal key via ``ON CONFLICT (key) DO UPDATE``, so that
       interleaving is reachable). See ``phaze.services.scheduling_ledger``'s module docstring
       INVARIANT paragraph and ``clear_ledger_entry``'s own docstring for the exact guard shape;
       this hook does not need to know about it, it just calls the (now-safe) primitive.
    """
    job = ctx.get("job")
    if job is None or job.function not in _KEY_BUILDERS:
        return

    if job.status == Status.COMPLETE:
        try:
            redis = getattr(job.queue, "cache_redis", None)
            if redis is not None:
                await incr_completed(redis, job.function)
        except Exception:
            # Counter is a cache; never block job teardown on a Redis hiccup.
            logger.warning("pipeline completed-counter increment failed", function=job.function, exc_info=True)

    if job.status in TERMINAL_STATUSES:
        # Function-LOCAL import (mirrors the WRITE hook) so the agent import graph stays
        # Postgres-free; it executes only when ledger_sessionmaker is present (control-side).
        try:
            sm = getattr(job.queue, "ledger_sessionmaker", None)
            if sm is not None:
                # INTENTIONAL function-local import (see apply_deterministic_key; PLC0415 suppressed
                # on the import line): keeps the ledger service out of the agent's _shared import
                # graph; runs only when a control-side ledger_sessionmaker is present.
                from phaze.services.scheduling_ledger import clear_ledger_entry  # noqa: PLC0415

                async with sm() as session:
                    await clear_ledger_entry(session, job.key)
                    await session.commit()
        except Exception:
            # Best-effort: a clear hiccup leaves the row for the next recovery; never raise (T-45-03).
            logger.warning("scheduling-ledger clear failed", function=job.function, key=job.key, exc_info=True)


__all__ = ["apply_deterministic_key", "increment_completed"]
