"""Control-side restart / queue-loss recovery for the WHOLE pipeline.

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). These tasks need both
PostgreSQL via ``ctx["async_session"]`` AND the per-agent enqueuer via
``ctx["task_router"]`` (plus the controller queue ``ctx["queue"]``) -- wired in
``phaze.tasks.controller.startup``. The agent worker is deliberately Postgres-free (the
import-boundary test ``tests/shared/core/test_task_split.py`` enforces this), so this module MUST
NEVER be imported or registered by ``phaze.tasks.agent_worker`` or anything under
``phaze.tasks._shared``. Register it ONLY in ``phaze.tasks.controller``.

THE DURABILITY REFRAME (Phase 42, 42-RESEARCH §Q2 -- READ THIS BEFORE "RESTORING" ANYTHING):
Phase 36 migrated the SAQ broker from Redis to Postgres (``saq_jobs`` table, ``PostgresQueue``).
Queued and active jobs are now DURABLE across a controller restart -- SAQ re-dequeues the
surviving ``saq_jobs`` rows itself, and reclaims timed-out ``active`` jobs on its own. A genuine
"queue-loss" is now the rare, DETECTABLE asymmetry "``saq_jobs`` has zero queued/active rows while
the durable scheduling ledger still records scheduled work" (a truncate / restore-from-backup /
fresh migration). Steady state produces ZERO automatic enqueues -- DO NOT re-introduce a
steady-state auto-advance cron or the deleted ``reenqueue_discovered`` producer. (Phase 80 left this
durability contract untouched; it only changed HOW ``domain-completed`` is derived -- see below.)

THE PHASE-45 LEDGER REFRAME (45-CONTEXT, the operator spec -- READ THIS BEFORE TOUCHING RECOVERY):
Pre-Phase-45 recovery derived its work from the ``services/pipeline.py`` COMPLEMENT-OF-DONE
pending-set queries (``get_files_by_state(DISCOVERED)``, ``get_untracked_files``, ...): "everything
that has not finished this stage." There was NO record that a stage was ever SCHEDULED for an item,
so clicking "Recover" swept in ~11,400 never-scheduled ``DISCOVERED`` files and detonated the queue
to ~44,500 jobs (the 2026-06-18 incident). The operator principle: **recovery must only re-queue
work that was previously scheduled and then lost**; a never-scheduled file is not yet orphaned.

:func:`recover_orphaned_work` now drives off the DURABLE scheduling ledger (Plan 01:
``scheduling_ledger`` table, written at the single ``before_enqueue`` chokepoint, cleared on every
terminal outcome). It re-enqueues exactly::

    orphaned = (ledger rows) MINUS (live saq_jobs keys) MINUS (domain-completed)

replaying each orphaned row's STORED payload through the SAME keyed producer that originally
enqueued it (``ctx["queue"].enqueue`` for controller rows; the active agent's per-agent queue for
agent rows). A never-scheduled ``DISCOVERED`` file has NO ledger row, so the incident sweep CANNOT
recur. ``force=True`` flips to "reconcile the ledger now", bypassing ONLY the no-op DETECT gate --
never the per-item deterministic-key dedup, so a forced reconcile over a live queue stays idempotent
(no doubling, Phase-32 class). Phase 80 (READ-03) preserved this ledger-recovery contract verbatim
while cutting the ``domain-completed`` exclusion over to the derived predicate layer (see the third
reframe below): the WORK set is still, exactly, ``ledger MINUS live MINUS domain-completed``.

THE PER-STAGE DOMAIN-COMPLETED PREDICATE (the SECONDARY net for Plan 02's residual gap):
Phase 80 (READ-03) CUT this predicate over from ``FileRecord.state`` reads to the Phase-78/81
single-source derivation layer (``services/stage_status.py``): recovery now derives "done" DIRECTLY
from the per-stage output tables via the LOCKED ``domain_completed_clause`` builder,
with ZERO ``FileRecord.state`` reads. This lands AFTER migration ``036`` backfilled
``analysis.analysis_completed_at`` (so ``done(analyze)`` reads a complete corpus) and BEFORE Phase 82
redefines "pending", closing the double-negation (D-05): the enrich branches flip from
``absent-from-pending`` (which would silently become ``done OR in_flight`` once pending = ``NOT done AND
NOT in_flight``) to a DIRECT ``in done-set`` test. The exclusion stays EXPLICIT and TOTAL per stage
(asserted in test_recovery.py):

- predicate-covered (:data:`_DOMAIN_COMPLETED_STAGES`): ``process_file`` (analyze; done via
  ``domain_completed_clause(ANALYZE)`` == done OR terminal-failed -- FAILURE_IS_TERMINAL[analyze],
  so an un-analyzable file is NEVER auto-re-driven), ``extract_file_metadata`` (done via
  ``domain_completed_clause(METADATA)`` with the D-10 ``enqueued_at <= failed_at`` gate applied at the
  call site), and ``push_file`` (done
  via ``cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE)`` -- D-07, no backend-kind
  resolution needed because a ``push_file`` ledger row implies compute).
- live-keys-only (everything else): the four controller stages (``generate_proposals`` /
  ``search_tracklist`` / ``scrape_and_store_tracklist`` / ``match_tracklist_to_discogs``; Plan 01's
  after_process clears them on every terminal status).

All done-sets are LEDGER-SCOPED (D-06): recovery only ever asks about files that appear in the ledger,
so every done-set query binds the ledger's file-ids as a SINGLE Postgres array (``= ANY(:ids)``), never
a bare ``.in_(fids)`` (the asyncpg 32767 param cap -- the ledger reached ~44.5K rows in the 2026-06-18
incident). Deriving "done" directly inverts the set-size characteristic (the pending sets were small;
the done set is most of a 200K corpus), which the ledger scope keeps at O(|ledger|).

Routing carries forward the Phase-32 pitfalls: agent rows route to EACH row's OWNING fileserver
agent (``payload["agent_id"]``, phaze-fjii) via ``select_agent_by_id`` +
``ctx["task_router"].queue_for(agent.id, lane)`` -- NEVER the consumer-less controller queue
(Pitfall 1), and never a single "most-recently-seen" fileserver that would misroute other owners'
rows; controller rows route to ``ctx["queue"]``. An offline / non-fileserver owner (common right
after a cold reboot; Pitfall 3) logs a warning and skips THAT owner's agent-routed rows instead of
raising -- never rerouting them onto another agent. The cached ``task_router`` is reused, never
reconstructed per call (Pitfall 4).

THE REPLAY-SAFETY INVARIANT (phaze-71nz -- READ THIS BEFORE ADDING A KEYED PRODUCER):
**A ``scheduling_ledger`` payload must be replayable at an ARBITRARY FUTURE TIME.** Verbatim replay
is correct only for a TIME-INVARIANT payload; a payload carrying material minted against a clock
replays DEAD material. Measured 2026-07-31: one operator Recover replayed 430 orphaned ``s3_upload``
rows whose payloads embedded presigned S3 PUT URLs signed at the ORIGINAL enqueue; 428 ran their
retries out to terminal ``failed`` at the object store (122x HTTP 403, 257x HTTP 400) and ZERO
succeeded -- while the same run's 2,512 ``process_file`` rows replayed fine, because that payload
carries nothing that expires. Recovery is an INCIDENT tool, so "replayed and guaranteed to fail" had
to stop being indistinguishable from "replayed and will succeed".

The invariant is now carried in THREE enforced pieces (see ``tasks/_shared/replay_safety.py``):

1. **Classification.** Every keyed producer is declared either ``LEDGER_REPLAY_TIME_INVARIANT``
   (replay the stored payload verbatim) or ``LEDGER_REPLAY_REGENERATED`` (payload carries
   time-limited material -- NEVER replay it; re-derive from durable inputs). The split is TOTAL and
   DISJOINT over ``_KEY_BUILDERS``, so the NEXT producer to store expiring material cannot slip
   through by omission the way ``s3_upload`` did.
2. **Regeneration (D-71nz, option (a)).** ``s3_upload`` -- today's only regenerated member -- is
   re-derived through :data:`_REPLAY_REGENERATORS` by calling the SAME live producer that mints the
   URLs (``cloud_staging.redrive_upload`` -> ``_stage_file_to_s3``: abort the prior multipart,
   initiate a fresh one, presign fresh part URLs, re-park the enqueue), keyed off durable inputs only
   (``file_id`` + the row's recorded ``cloud_job.staging_bucket``). Recovery duplicates NO staging
   logic and therefore cannot drift from it. When the durable inputs are not there (no ``FileRecord``,
   no resolvable staging bucket, no online fileserver), the row is tallied ``unreplayable`` and
   SKIPPED -- never faked with stale credentials.
3. **A general replay-time refusal.** :func:`_replay_row` runs ``find_time_limited_paths`` over EVERY
   payload it is about to replay verbatim and REFUSES (tally ``unreplayable`` + an ERROR log naming
   the offending payload paths) if it finds presign/token/expiry material. This is deliberately NOT
   pinned to ``s3_upload``: a future producer that stores expiring material and is mis-classified as
   time-invariant is caught by the substrate rather than by an incident.

The per-stage tally therefore distinguishes ``unreplayable`` (knowingly skipped -- the work is NOT
re-enqueued and needs another owner) from ``skipped`` (deterministic-key dedup -- the work is already
live), and ``POST /pipeline/recover`` surfaces the difference instead of always reading "Recovery
started".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
import json
from typing import TYPE_CHECKING, Any, cast
import uuid

from sqlalchemy import ARRAY, Select, bindparam, func, select, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
import structlog

from phaze.config import get_settings
from phaze.enums.stage import Stage
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services import cloud_staging
from phaze.services.backends import IN_FLIGHT
from phaze.services.cloud_staging import NoCloudJobToRedriveError
from phaze.services.enqueue_router import NoActiveAgentError, lane_for_task, select_agent_by_id
from phaze.services.pipeline import count_inflight_jobs, get_live_job_keys
from phaze.services.s3_staging import S3StagingError
from phaze.services.scheduling_ledger import get_ledger_rows, insert_ledger_if_absent
from phaze.services.stage_status import CLOUD_LANE_FUNCTIONS, cloud_lane_completed_clause, domain_completed_clause, skipped_clause
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS
from phaze.tasks._shared.replay_safety import LEDGER_REPLAY_REGENERATED, find_time_limited_paths


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.config import ControlSettings
    from phaze.models.scheduling_ledger import SchedulingLedger


logger = structlog.get_logger(__name__)


# --- Phase 45: ledger-driven, gated, idempotent recovery producer -----------------------
#
# Recovery replays ``ledger MINUS live MINUS domain-completed``. A replay of any item still in
# saq_jobs dedups against its deterministic key (apply_deterministic_key, the single
# before_enqueue chokepoint) and returns None -> counted as skipped. SAFETY BACKSTOP
# (T-45-09): even if the live-key filter false-NEGATIVES (a stale read) and re-enqueues a
# still-live item, the deterministic-key dedup collapses it to a skipped no-op, so a forced
# reconcile can NEVER double the queue (the Phase-32 doubling class is closed). Every enqueue
# goes strictly through the keyed producers (never a raw random-key queue.enqueue).
#
# The THREE agent stages whose ledger clear is NOT reliable on every terminal outcome get an
# explicit domain-completed predicate; the four controller stages are live-keys-only (their clear
# IS reliable). The classification is TOTAL: predicate-covered XOR live-keys-only, asserted in a
# test against _KEY_BUILDERS so no stage is silently undefined (T-45-17).
_DOMAIN_COMPLETED_STAGES: frozenset[str] = frozenset(
    {
        "process_file",  # analyze: domain_completed_clause(ANALYZE) == done OR terminal-failed
        "extract_file_metadata",  # domain_completed_clause(METADATA) + the D-10 enqueued_at gate at the call site
        "push_file",  # D-07: cloud_lane_completed_clause() == cloud_job 'succeeded' OR domain_completed_clause(ANALYZE)
        "s3_upload",  # phaze-k95r7: the SAME cloud-lane predicate as push_file -- see below
    }
)
"""Keyed functions that carry a per-stage domain-completed predicate (the SECONDARY exclusion).

This set is exactly THE FILE-KEYED AGENT TASKS -- every keyed function that is both routed to an agent
(``enqueue_router.AGENT_TASKS``) and keyed on a ``file_id`` (``deterministic_key._KEY_BUILDERS``). That
is the population whose ledger clear is NOT reliable, because the work runs off-controller and the row
is cleared by a control-side callback that a crash, a restart or a swept ``saq_jobs`` row can lose.
(``write_file_tags`` is agent-routed but keyed on a ``log_id``, not a file, so no per-file completion
predicate can exist for it; the remaining keyed functions are CONTROLLER tasks whose clear rides the
broker's own ``after_process`` and needs no domain net.)

phaze-k95r7 ADDED ``s3_upload``, which had been the one file-keyed agent task with NO completion
exclusion of any kind -- an omission, not a decision. Measured on the live deployment 2026-08-08: 17
``s3_upload`` rows dated 2026-07-07/14, every one of them for a file whose analysis had SUCCEEDED and
whose ``cloud_job`` row had since been cleaned up, were still being handed to the regenerator on every
single recovery run and reported ``unreplayable`` forever. Nothing else could have excluded them: the
two cloud exclusions (:func:`_in_flight_cloud_job_ids` / :func:`_awaiting_cloud_job_ids`) both key off a
``cloud_job`` row these files no longer had.

Phase 80 (READ-03) cut every predicate over to the derived layer -- all four are derived from the
per-stage output tables via the LOCKED ``domain_completed_clause`` / ``cloud_lane_completed_clause``
builders, with ZERO ``FileRecord.state`` reads (the old FileState-derived "done" is gone).
Kept in sync with ``deterministic_key._KEY_BUILDERS`` by a totality test in test_recovery.py.
"""


@dataclass(frozen=True)
class _DoneSets:
    """The ledger-scoped per-stage domain-completed derivation, computed ONCE per recovery run (D-06).

    Every set/dict is keyed by file-id STRING and scoped to the ledger's files only (``= ANY(:ids)``),
    so membership is O(1) in :func:`is_domain_completed` and the derivation is O(|ledger|), never
    O(200K corpus). The metadata cell is split into a domain-completed set + a ``failed_at`` map so the
    call site can apply the D-10 ``enqueued_at <= failed_at`` gate (which needs the per-row ledger
    timestamp) to the failed subset -- and a SEPARATE ``metadata_skipped`` set (phaze-3m5n) so the call
    site can short-circuit to domain-complete on force-skip membership BEFORE ever reaching the D-10
    gate. Without this separate cell, a file that is force-skipped AFTER a failure (the additive-only
    ``force_skip_stage`` contract, T-87-20, never clears ``failed_at``) still carries a non-NULL
    ``metadata_failed_at`` entry, so the D-10 gate would wrongly re-subject it to the
    ``enqueued_at <= failed_at`` comparison and re-drive an orphaned post-failure retry row that the
    operator explicitly terminated -- exactly the bug phaze-3m5n fixes.
    """

    analyze_done: set[str]  # domain_completed_clause(ANALYZE): done OR terminal-failed
    metadata_domain_completed: set[str]  # domain_completed_clause(METADATA): done OR skipped OR failed (D-10 gate refines the failed-only subset)
    metadata_failed_at: dict[str, datetime]  # failed_clause(METADATA) rows -> failed_at (D-10 gate)
    metadata_skipped: set[str]  # skipped_clause(METADATA) alone: force-skip is domain-complete UNCONDITIONALLY, never gated
    cloud_lane_done: set[str]  # D-07 / phaze-k95r7: cloud_lane_completed_clause() -- covers BOTH push_file and s3_upload


TALLY_KEYS: tuple[str, ...] = ("reenqueued", "skipped", "errored", "unreplayable")
"""The per-stage tally counters, in render order. FOUR outcomes, all semantically distinct:

- ``reenqueued`` -- a fresh job landed on the queue; the work WILL run.
- ``skipped``    -- the deterministic key deduped against a still-live job; the work is ALREADY
  running. Benign; the stage is covered.
- ``errored``    -- the replay itself blew up (a transient enqueue/connect failure, an unmapped
  legacy function). The ledger row survives, so the next pass retries it.
- ``unreplayable`` -- phaze-71nz. The row was DELIBERATELY not replayed, because replaying it would
  enqueue a job guaranteed to fail: its payload carries time-limited material (presigned URL / token
  / expiry) and either the regeneration inputs are unavailable or the producer is mis-classified as
  time-invariant. The work is NOT covered by this run and needs another owner (the staging drain, a
  later recovery once the fileserver is back). This is the counter the 2026-07-31 incident had no
  way to express: 430 ``s3_upload`` rows were reported as ``reenqueued`` while every one of them was
  guaranteed to 403.
"""


def _zero() -> dict[str, int]:
    """Return a fresh zero per-stage tally (every counter in :data:`TALLY_KEYS`)."""
    return dict.fromkeys(TALLY_KEYS, 0)


def _ledger_fids(rows: Sequence[SchedulingLedger]) -> list[uuid.UUID]:
    """Extract the distinct, UUID-parseable natural file-ids from the run's ledger rows (D-06 scope).

    The done-set queries only ever ask about files that appear in the ledger, so this is the exact
    ``fids`` bound as the ``= ANY(array)`` scope. A row whose payload carries no ``file_id`` (or a
    non-UUID natural id -- e.g. a controller set-hash) is skipped: it can never be a per-file
    output-table match, so excluding it from the scope is a pure optimization with no behavior change.
    """
    out: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for row in rows:
        nid = _natural_id(row)
        if nid is None:
            continue
        try:
            fid = uuid.UUID(nid)
        except ValueError:
            continue
        if fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


def _fids_scope(fids: list[uuid.UUID], name: str) -> Any:
    """``FileRecord.id = ANY(:name)`` -- a SINGLE Postgres array bind, never a bare ``.in_(fids)``.

    D-06 / Landmine 5: a bare ``.in_(fids)`` expands to one bind per element and crashes asyncpg past
    the 32767-param wire cap (the ledger reached ~44.5K rows in the 2026-06-18 incident). Binding the
    whole id list as ONE ``uuid[]`` array param sidesteps the ceiling entirely while the Phase-77
    partial indexes still drive each per-stage probe. There is NO ``= ANY(array)`` idiom elsewhere in
    the codebase (RESEARCH §e); this is the phase's one genuinely new query pattern.
    """
    return FileRecord.id == func.any(bindparam(name, value=fids, type_=ARRAY(PGUUID(as_uuid=True))))


async def _build_done_sets(session: AsyncSession, fids: list[uuid.UUID]) -> _DoneSets:
    """Compute the ledger-scoped per-stage domain-completed derivation ONCE (D-05/D-06/D-07/D-10).

    Derives "done" DIRECTLY from the Phase-78/81 predicate layer -- ZERO ``FileRecord.state`` reads:

    - ``analyze_done``: ``domain_completed_clause(ANALYZE)`` (done OR terminal-failed -- an
      un-analyzable file stays terminal, FAILURE_IS_TERMINAL[analyze]).
    - ``metadata_domain_completed`` + ``metadata_failed_at`` + ``metadata_skipped``: ``domain_completed_clause(METADATA)``
      gives the done-OR-skipped-OR-failed set; the failed rows' ``failed_at`` is read alongside so the
      call site can REFINE ONLY the failed-only subset with the D-10 gate
      (``done OR skipped OR (failed AND enqueued_at <= failed_at)``) -- both twins stay ledger-agnostic.
      Deriving the set via ``domain_completed_clause`` (not a bare ``done_clause``) is what makes the
      D-11 lock bite: a ``~inflight_clause`` conjunct wrongly added to it would drop every
      failed-and-inflight candidate. ``metadata_skipped`` is read SEPARATELY (``skipped_clause(METADATA)``
      alone, not folded into the merged set) so the call site can test skip membership BEFORE the D-10
      gate -- a force-skipped file that ALSO carries a stale ``failed_at`` (the force-skip writer's
      additive-only contract, T-87-20, never clears it) must stay domain-complete UNCONDITIONALLY,
      never re-subjected to the failed-only ``enqueued_at`` comparison (phaze-3m5n).
    - ``cloud_lane_done``: ``cloud_lane_completed_clause()`` -- ``cloud_job.status='succeeded' OR
      domain_completed_clause(ANALYZE)`` (D-07, generalised by phaze-k95r7 to cover ``s3_upload`` as
      well as ``push_file``). SUCCEEDED covers the landed-but-not-yet-analyzed window and
      ``domain_completed(analyze)`` covers the onward advance; a SUBMITTED / AWAITING / no-row file is
      NOT cloud-lane-done and re-drives. No backend-kind resolution is needed: both producers name a
      file and a cloud destination, and both are moot the moment the analysis they exist to feed has
      reached a terminal state.

    Every query keeps its own per-stage shape (one targeted probe per stage) so each Phase-77 partial
    index drives its own scan, and every probe is scoped to ``fids`` via a single ``= ANY(array)`` bind
    (:func:`_fids_scope`). An empty ledger short-circuits to empty sets (no pointless round-trips).
    """
    if not fids:
        return _DoneSets(set(), set(), {}, set(), set())

    analyze_done = {str(fid) for fid in (await session.scalars(_select_done_analyze_ids(fids))).all()}
    metadata_domain_completed = {
        str(fid)
        for fid in (await session.scalars(select(FileRecord.id).where(_fids_scope(fids, "m_ids"), domain_completed_clause(Stage.METADATA)))).all()
    }
    metadata_failed_at = {
        str(file_id): failed_at
        for file_id, failed_at in (
            await session.execute(
                select(FileMetadata.file_id, FileMetadata.failed_at).where(
                    FileMetadata.file_id == func.any(bindparam("mf_ids", value=fids, type_=ARRAY(PGUUID(as_uuid=True)))),
                    FileMetadata.failed_at.isnot(None),
                )
            )
        ).all()
    }
    metadata_skipped = {
        str(fid) for fid in (await session.scalars(select(FileRecord.id).where(_fids_scope(fids, "ms_ids"), skipped_clause(Stage.METADATA)))).all()
    }
    cloud_lane_done = {str(fid) for fid in (await session.scalars(_select_cloud_lane_done_ids(fids))).all()}

    return _DoneSets(
        analyze_done=analyze_done,
        metadata_domain_completed=metadata_domain_completed,
        metadata_failed_at=metadata_failed_at,
        metadata_skipped=metadata_skipped,
        cloud_lane_done=cloud_lane_done,
    )


def _select_done_analyze_ids(fids: list[uuid.UUID]) -> Select[tuple[uuid.UUID]]:
    """Build the ledger-scoped SELECT for file ids whose analyze stage is DOMAIN-COMPLETE (D-01/D-06).

    ``domain_completed_clause(ANALYZE)`` == ``done OR failed`` -- both terminal, because
    ``FAILURE_IS_TERMINAL[analyze]`` is True: a genuinely un-analyzable file (analyze FAILED) is
    domain-complete and is NEVER auto-looped by ``recover_orphaned_work``. The operator-gated
    ``POST /pipeline/analysis-failed/retry`` (``routers/pipeline.py`` ``retry_analysis_failed``) is the
    manual counterpart -- it CLEARS ``analysis.failed_at`` BEFORE re-enqueuing (the Phase-81 CR-01 fix),
    so a re-driven file leaves the failed disjunct and is no longer domain-complete (and analyze has no
    ambiguous D-10 cell, unlike metadata). Scoped to the ledger's ``fids`` via a single array bind.
    """
    return select(FileRecord.id).where(_fids_scope(fids, "a_ids"), domain_completed_clause(Stage.ANALYZE))


def _select_cloud_lane_done_ids(fids: list[uuid.UUID]) -> Select[tuple[uuid.UUID]]:
    """Build the ledger-scoped SELECT for file ids whose CLOUD LANE is done (D-07, sidecar-derived).

    ``cloud_lane_done = cloud_job.status='succeeded' OR domain_completed_clause(ANALYZE)`` -- composed
    from the LOCKED :func:`~phaze.services.stage_status.cloud_lane_completed_clause` builder rather
    than re-spelled here, so recovery's exclusion and the reaper's target set are the SAME predicate by
    construction (phaze-k95r7: a row excluded from recovery but invisible to the reaper is immortal
    stale state, which is how the 17 ``s3_upload`` rows survived a month).

    Covers BOTH file-keyed cloud producers. A ``push_file`` row is created only by
    ``ComputeAgentBackend.dispatch``'s parked enqueue and an ``s3_upload`` row only by
    ``cloud_staging.stage_file_to_s3``, so no backend-kind resolution is needed: SUCCEEDED means
    "landed, now analyzing" for both, and ``domain_completed(analyze)`` covers the onward advance
    (behavior-identical to the retired ``state IN (PUSHED, ANALYZED, ANALYSIS_FAILED)``). A file at
    SUBMITTED / AWAITING / no-row is NOT cloud-lane-done and its row correctly re-drives. NO
    ``FileRecord.state`` read. Scoped to the ledger's ``fids`` via a single array bind.
    """
    return select(FileRecord.id).where(_fids_scope(fids, "p_ids"), cloud_lane_completed_clause())


# phaze-fc2l: the ONLY functions a file's cloud_job actually owns the re-drive of -- the analyze/push
# pipeline. The SCHED-05 (in-flight) and 83-06 (awaiting) cloud exclusions exist to keep exactly ONE
# recovery owner for these rows (the backend reconcile/`/pushed` callback for in-flight, the
# stage_cloud_window drain for awaiting), and CLOUDROUTE-02 for process_file (a held long file is never
# routed to a fileserver for LOCAL analysis). Because ``_natural_id`` is function-AGNOSTIC (it returns
# ``payload['file_id']`` for EVERY file-keyed row -- extract_file_metadata / search_tracklist
# included), an UNSCOPED cloud exclusion silently dropped those unrelated stages' recovery for any
# file that merely happened to also have a cloud_job. The cloud callback/drain re-drives ONLY these
# four; metadata/tracklist have no cloud second owner, so scoping the two exclusions to this set lets their orphaned rows recover normally.
_CLOUD_OWNED_FUNCTIONS: frozenset[str] = frozenset({"process_file", "push_file", "s3_upload", "submit_cloud_job"})


async def _awaiting_cloud_job_ids(session: AsyncSession) -> set[str]:
    """File-id strings for files HELD in AWAITING_CLOUD (a ``cloud_job(status='awaiting')`` sidecar row).

    83-06 (CONSCIOUSLY REVERSES D-09): a held awaiting-cloud file is owned SOLELY by the
    :func:`~phaze.tasks.release_awaiting_cloud.stage_cloud_window` drain (via its awaiting ``cloud_job``
    row), so ``recover_orphaned_work`` EXCLUDES it -- exactly as it excludes an in-flight cloud file
    (:func:`_in_flight_cloud_job_ids`). This REPLACES the former ``_get_awaiting_cloud_ids`` +
    ``held_agent_rows`` compute-only partition, which existed only because the D-09 held-file backfill
    SEEDED a ``process_file:<id>`` ledger row for every held compute file. 83-06 removed that seed (and
    deletes the orphaned row), so no held file carries a ``process_file`` ledger row any more; the
    partition became unreachable. Excluding awaiting-cloud files here preserves the CLOUDROUTE-02 safety
    invariant ROBUSTLY (a held long file is NEVER routed kind-agnostically to a fileserver and analyzed
    locally) even for a LEGACY row seeded by the pre-83-06 backfill -- the drain, not the ledger, re-drives
    it. ``'awaiting'`` is deliberately OUT of :data:`IN_FLIGHT`, so the two exclusion sets are disjoint and
    each is read ONCE per recovery run.
    """
    return {str(fid) for fid in (await session.scalars(select(CloudJob.file_id).where(CloudJob.status == CloudJobStatus.AWAITING.value))).all()}


async def _in_flight_cloud_job_ids(session: AsyncSession) -> set[str]:
    """File-id strings for files that currently carry an in-flight ``cloud_job`` row (Phase 69, SCHED-05).

    After Phase-68 BACK-03 a cloud-burst file has BOTH an in-flight ``cloud_job`` row (any
    ``backend_id``) AND a ``process_file`` / ``push_file`` scheduling-ledger row. Both the backend
    reconcile/``/pushed`` callback and this ledger recovery could otherwise claim ownership of that
    file's re-drive -- a double-owner vector that is exactly the 44.5k over-enqueue incident class.
    Excluding every file with a live ``cloud_job`` row from the ledger orphan set makes the backend
    reconcile/callback the SINGLE owner for cloud-backed files. 83-06 (reverses D-09): a HELD
    AWAITING_CLOUD file (``'awaiting'`` ∉ ``IN_FLIGHT``) is likewise excluded, but via the disjoint
    :func:`_awaiting_cloud_job_ids` set -- the ``stage_cloud_window`` drain owns it. A file with NEITHER
    an in-flight NOR an awaiting ``cloud_job`` row (a genuinely-orphaned local re-drive) keeps its
    recovery path.

    ``IN_FLIGHT`` = {UPLOADING, UPLOADED, SUBMITTED, RUNNING} (terminal SUCCEEDED/FAILED excluded);
    the set is small and bounded (in-flight rows only), read ONCE per recovery run alongside the
    done-sets. Mirrors :func:`_awaiting_cloud_job_ids`.
    """
    return {str(fid) for fid in (await session.scalars(select(CloudJob.file_id).where(CloudJob.status.in_([s.value for s in IN_FLIGHT])))).all()}


def _natural_id(row: SchedulingLedger) -> str | None:
    """Return the file-id natural id from a predicate-covered row's stored payload, or None.

    The three predicate-covered functions are all file-keyed (``_KEY_BUILDERS`` uses ``file_id``),
    so the natural id is ``payload["file_id"]``. A missing/empty payload field yields None (treated
    as NOT domain-completed -- replay rather than silently drop).
    """
    payload = row.payload or {}
    fid = payload.get("file_id")
    return str(fid) if fid is not None else None


def is_domain_completed(row: SchedulingLedger, done_sets: _DoneSets) -> bool:
    """Return True only for a predicate-covered row whose file is DOMAIN-COMPLETE for that stage.

    ALWAYS False for the four live-keys-only controller functions: their ledger clear is reliable
    on every terminal outcome, so the live-key filter is the sole exclusion and any surviving row
    is genuinely orphaned. For the three predicate-covered
    agent stages the derivation reads DIRECTLY from the done-sets (D-05 -- the double-negation cut:
    the enrich branches now test ``fid in done_set``, never ``fid not in pending_set``, so once
    Phase 82 redefines pending as ``NOT done AND NOT in_flight`` a genuinely-orphaned in-flight-ledger
    file cannot be silently mis-classified as done):

    - ``process_file``: file id in ``analyze_done`` (domain_completed(analyze) == done OR terminal-failed).
      NOTE the discriminator this reads (DERIV-03, and the thing phaze-k95r7's live measurement has to
      be re-read against): ``done(analyze)`` is ``analysis_completed_at IS NOT NULL``, NOT "an
      ``analysis`` row exists" and NOT "``failed_at IS NULL``". A PARTIAL row -- the shape
      ``POST /agent/analysis/{id}/progress`` writes, carrying real window counters and no completion
      stamp -- is correctly NOT domain-complete and IS re-driven: the analysis never finished.
    - ``push_file`` / ``s3_upload``: file id in ``cloud_lane_done`` (cloud_job succeeded OR
      domain_completed(analyze); D-07, extended to ``s3_upload`` by phaze-k95r7).
    - ``extract_file_metadata``: skip-first, THEN the D-10 cell. A force-SKIPPED file (``metadata_skipped``)
      is domain-complete UNCONDITIONALLY and returns True BEFORE the D-10 gate is ever consulted
      (phaze-3m5n) -- the force-skip writer's additive-only contract (T-87-20) never clears
      ``failed_at``, so a file skipped after a failed operator retry still carries a stale
      ``metadata_failed_at`` entry, and the D-10 gate must never be applied to it (that would
      re-classify the skip as "genuinely pending" and re-drive a stage the operator explicitly
      terminated). Absent a skip marker, domain-complete when ``done(metadata)`` OR the metadata FAILED
      AND the ledger row's ``enqueued_at <= metadata.failed_at``. metadata is the ONLY stage with this
      ambiguous failed-only cell: ``retry_metadata_failed`` LEAVES ``failed_at`` set (81 D-11) then
      re-enqueues, so ``(ledger row AND failed_at)`` is ambiguous. ``enqueued_at > failed_at`` (an
      orphaned OPERATOR retry) re-drives; ``enqueued_at < failed_at`` (a callback that wrote the marker
      but crashed before clearing the ledger) stays terminal. ``analysis.failed_at`` is cleared on retry
      (CR-01), so analyze has no such cell -- the asymmetry is intentional (D-10).
    """
    function = row.function
    if function not in _DOMAIN_COMPLETED_STAGES:
        return False
    fid = _natural_id(row)
    if fid is None:
        return False
    if function == "process_file":
        return fid in done_sets.analyze_done
    if function in CLOUD_LANE_FUNCTIONS:  # push_file / s3_upload -- one lane, one predicate (phaze-k95r7)
        return fid in done_sets.cloud_lane_done
    # extract_file_metadata -- force-skip is checked BEFORE the D-10 gate (phaze-3m5n): the gate must
    # refine ONLY the subset of metadata_domain_completed whose membership derives solely from the
    # failed disjunct, never a row that is ALSO domain-complete via the skipped disjunct.
    if fid in done_sets.metadata_skipped:
        return True  # force-skipped -> domain-complete unconditionally, D-10 gate never applies
    # D-10 call-site gate via SchedulingLedger.enqueued_at.
    if fid not in done_sets.metadata_domain_completed:
        return False  # neither done, skipped, nor failed -> genuinely pending -> re-drive
    failed_at = done_sets.metadata_failed_at.get(fid)
    if failed_at is None:
        return True  # done (a metadata row present, failed_at NULL) -> domain-complete
    # failed -> the D-10 cell: terminal only when the ledger row PRE-DATES the failure marker.
    # This coercion was load-bearing when ``scheduling_ledger.enqueued_at`` was ``TIMESTAMP WITHOUT TIME
    # ZONE``: asyncpg returned it NAIVE while ``metadata.failed_at`` came back aware, and a bare
    # ``naive <= aware`` raises TypeError, aborting the whole recovery run (CR-02). phaze-cz3m /
    # migration 049 made every timestamp column ``timestamptz``, so a DB-read row is now aware and this
    # is a no-op on the production path. KEPT deliberately: it costs one attribute check, and it is the
    # difference between a TypeError that kills a recovery run and a correct comparison should a naive
    # stamp ever reach here from a caller that did not come through the DB.
    enqueued_at = row.enqueued_at if row.enqueued_at.tzinfo is not None else row.enqueued_at.replace(tzinfo=UTC)
    return enqueued_at <= failed_at


async def _replay_row(queue: Any, row: SchedulingLedger, tally: dict[str, int]) -> None:
    """Replay one orphaned ledger row through its keyed producer, updating ``tally``.

    The STORED payload is replayed verbatim with the deterministic key re-stamped from the ledger
    key (``key=row.key`` -- exactly what the ``before_enqueue`` hook would stamp from the payload,
    so a still-live item dedups to None). NEVER a raw random-key enqueue. A None return (dedup)
    counts as skipped; otherwise reenqueued. extra='forbid' agent schemas re-validate the stored
    payload on dequeue, so a malformed row dead-letters rather than executing (T-45-10).

    The stored SAQ Job policy (``row.timeout`` / ``row.retries``) is replayed too when present. A
    NULL column (legacy/backfilled row written before the Phase-45 capture columns existed, or a
    producer that set no explicit policy) is left out of the enqueue kwargs entirely -- the queue's
    ``apply_project_job_defaults`` before_enqueue hook then fills the ROLE default (600s/4
    retries).

    phaze-w55w1: for ``process_file`` the replayed ``timeout`` no longer matters either way. That
    function now runs ``timeout=0`` (no wall clock at all -- exhaustive analysis legitimately runs
    for hours, ADR-0007 §7) plus a progress ``heartbeat``, and BOTH are PINNED by
    ``apply_project_job_defaults`` on every enqueue including this replay. So a row carrying the
    old 7200s bound is corrected rather than honoured, which is the point: replaying that bound
    would put a wall clock back on a job that must not have one. The ledger never captured
    ``heartbeat``, and pinning is what closes that gap -- see
    ``queue_defaults._FUNCTION_HEARTBEAT_POLICY``.

    phaze-plpnf: that role-default fallback used to be the FULL story, and it is exactly what
    turned "no captured bounds" into a live incident -- one queued-at cluster of legacy
    NULL-bounds ``process_file`` rows replayed straight onto the 600s/4-retries role default and
    died on every attempt with a SAQ ``TimeoutError`` (12x short of the bound a long file needed).
    ``apply_project_job_defaults`` now ALSO enforces a per-function policy
    (``queue_defaults._FUNCTION_JOB_POLICY`` / ``_FUNCTION_HEARTBEAT_POLICY``) after its generic
    fill, so a NULL-bounds ``process_file`` replay lands on the current policy regardless -- the
    omission above is now a defense-in-depth choice (replay the captured bound when we have it)
    rather than the only thing standing between a legacy row and the 600s role default.

    phaze-71nz -- THE REPLAY-SAFETY REFUSAL (the general guard, deliberately NOT pinned to any one
    function). Before the enqueue, the payload is screened by ``find_time_limited_paths``. A hit
    means the payload carries material minted against a clock (a presigned URL, a bearer token, an
    explicit expiry), so replaying it verbatim would enqueue a job that is GUARANTEED to fail at
    whatever authenticates it -- exactly the 428-of-430 ``s3_upload`` outcome. Such a row is REFUSED:
    tallied ``unreplayable``, logged at ERROR with the offending payload paths (never the values --
    they are credentials), and left in the ledger for an owner that can regenerate it.

    A function declared ``LEDGER_REPLAY_REGENERATED`` never reaches here at all -- it is routed to
    its regenerator in :data:`_REPLAY_REGENERATORS`. So a refusal HERE always means a producer wrote
    expiring material into the ledger without declaring it, which is the root-cause class this guard
    exists to make loud instead of silent.
    """
    violations = find_time_limited_paths(row.payload)
    if violations:
        logger.error(
            "recover_orphaned_work: REFUSING to replay a ledger payload carrying time-limited material -- "
            "a scheduling_ledger payload must be replayable at an arbitrary future time (phaze-71nz). "
            "Replaying it would enqueue a job guaranteed to fail at the credential check. Declare this "
            "producer in replay_safety.LEDGER_REPLAY_REGENERATED and register a regenerator in "
            "reenqueue._REPLAY_REGENERATORS, or stop storing the derived material in the payload.",
            key=row.key,
            function=row.function,
            # PATHS only -- the values are live credentials and must never reach a log sink.
            payload_paths=violations,
        )
        tally["unreplayable"] += 1
        return

    # Job-control kwargs only when the ledger captured them (NULL => fall back to queue defaults).
    policy: dict[str, Any] = {}
    if row.timeout is not None:
        policy["timeout"] = row.timeout
    if row.retries is not None:
        policy["retries"] = row.retries
    await queue.connect()
    job = await queue.enqueue(row.function, key=row.key, **policy, **(row.payload or {}))
    if job is None:
        tally["skipped"] += 1
    else:
        tally["reenqueued"] += 1


async def _replay_row_isolated(queue: Any, row: SchedulingLedger, stages: dict[str, dict[str, int]]) -> None:
    """Replay one row via :func:`_replay_row`, isolating any failure to THIS row's tally (phaze-o1xx).

    The pre-fix loop ran every orphaned row through ``_replay_row`` with zero per-row error
    handling: a single transient failure -- a Postgres blip on ``queue.enqueue``, a per-agent lane
    ``connect()`` failure -- propagated out of :func:`recover_orphaned_work` and killed the ENTIRE
    replay after an arbitrary prefix had already been enqueued. Re-populating ``saq_jobs`` with that
    prefix then defers the next boot's DETECT gate (``count_inflight_jobs == 0``) until the drained
    prefix idles out, silently stranding the un-replayed suffix in the meantime (see the module's
    "Degrade-safe" contract, which previously covered ONLY an offline owning agent).

    ``stages.setdefault(row.function, _zero())`` also guards a legacy ledger row whose ``function``
    was since renamed/removed (not currently reachable -- every keyed row's function is a member of
    :data:`_ALL_KEYED_FUNCTIONS` by construction -- but a poison row here must never abort the run
    either, so the guard costs nothing and closes the latent gap).
    """
    tally = stages.setdefault(row.function, _zero())
    try:
        await _replay_row(queue, row, tally)
    except Exception:
        logger.exception(
            "recover_orphaned_work: row replay failed -- row skipped this run, ledger entry remains for the next pass",
            key=row.key,
            function=row.function,
        )
        tally["errored"] += 1


# --- phaze-71nz: regenerating replay for the time-limited producers ------------------------


class UnreplayableRow(Exception):
    """This ledger row CANNOT be replayed correctly RIGHT NOW -- skip it, never fake it.

    Raised by a regenerator when the durable inputs it needs are unavailable (the ``FileRecord`` is
    gone, no staging bucket resolves, no fileserver is online). It is NOT an error: the correct
    outcome is a deliberate, TALLIED skip (``unreplayable``) with the reason logged, so the operator
    can see that a stage was knowingly left uncovered. The alternative -- replaying the stored
    payload anyway -- is precisely the 2026-07-31 defect (a stage burned into ``failed`` while the
    HTTP layer reported success).

    Distinct from a bare ``Exception`` out of a regenerator, which is a genuine fault and tallies
    ``errored`` so the next pass retries the row.
    """


class StaleLedgerRow(UnreplayableRow):
    """This ledger row describes work that is NOT PENDING -- there is nothing to regenerate (phaze-k95r7).

    A strict refinement of :class:`UnreplayableRow`, split off it because the parent's wording is a
    statement about the CLOCK ("cannot be replayed correctly RIGHT NOW", "its payload is time-limited")
    and this case has nothing to do with the clock. The row points at a staging attempt that does not
    exist: no ``cloud_job`` row for the file at all. Waiting does not help, a fileserver coming online
    does not help, and a later recovery pass will report exactly the same thing -- which is what 17
    live ``s3_upload`` rows did on every run for a month while claiming to be an expiry problem.

    Tallied under the SAME ``unreplayable`` counter (the operator-facing meaning -- "this run did not
    cover that work" -- is unchanged, and the tally shape stays total), but logged with its own message
    so the two are never again read as one thing. Subclassing keeps every existing
    ``except UnreplayableRow`` handler correct; handlers that care catch this one FIRST.
    """


@dataclass(frozen=True)
class _RegenTarget:
    """A ledger row SNAPSHOT, detached from the ORM identity map before regeneration runs.

    Unlike every replay path, a regenerator OWNS a transaction boundary: it commits on success and
    ROLLS BACK on failure. Both expire the ORM instances loaded on that session -- so a
    :class:`SchedulingLedger` held across the call becomes a lazy-load landmine. The next attribute
    read (``row.key`` in the very log line reporting the skip, or the NEXT row's ``row.function`` on
    the following loop iteration) fires IO from sync attribute access and raises ``MissingGreenlet``,
    turning a deliberate, isolated skip into an exception that escapes the whole recovery run.

    Snapshotting the three fields a regenerator needs before the loop starts removes the hazard by
    construction rather than by remembering to re-fetch.
    """

    key: str
    function: str
    payload: dict[str, Any]


async def _regenerate_s3_upload(session: AsyncSession, task_router: Any, target: _RegenTarget, tally: dict[str, int]) -> None:
    """Re-drive an orphaned ``s3_upload`` row with FRESHLY presigned part URLs (D-71nz option (a)).

    The stored payload's ``part_urls`` are presigned PUT URLs bounded by ``s3_presign_put_ttl_sec``
    and signed at the ORIGINAL enqueue, so they are dead by the time a row has been orphaned long
    enough to matter. Rather than replay them, this calls the SAME producer that mints them on the
    live path -- ``cloud_staging.redrive_upload`` -> ``_stage_file_to_s3``: best-effort abort the
    prior multipart, initiate a fresh one, presign fresh part URLs, refresh the ``cloud_job`` row and
    PARK a fresh ``s3_upload`` enqueue. Recovery therefore duplicates ZERO staging logic and cannot
    drift from the live path (the reason option (a) was chosen over excluding the stage outright).

    The only inputs consumed are DURABLE: the row's ``payload["file_id"]`` and the recorded
    ``cloud_job.staging_bucket`` / ``backend_id`` (``cloud_staging._redrive_bucket``, MKUE-02). Nothing
    time-limited is read from the ledger payload at all.

    Transaction discipline mirrors ``stage_file_to_s3``'s committing wrapper, because
    ``redrive_upload`` deliberately calls the NO-COMMIT core (phaze-j2tm) and PARKS its enqueue
    (phaze-grzo): commit the ``cloud_job`` UPLOADING row FIRST, then flush the parked enqueue, so the
    worker-visible job can never precede the committed row it reads. On any failure the parked
    enqueue is DROPPED (which also best-effort aborts the fresh multipart, phaze-cws5) and the
    session is rolled back so the remaining rows in the recovery loop still see a usable session.

    ``fired == 0`` means SAQ deduped the fresh enqueue against a still-incomplete ``s3_upload:<id>``
    job (``flush_pending_s3_enqueues`` logs it) -- the same ``skipped`` semantics a verbatim replay's
    ``None`` return carries: the work is already live, not lost.

    KNOWN INHERITED BEHAVIOR (not a regression of this fix): ``_stage_file_to_s3`` resolves the
    destination via ``select_active_agent(kind="fileserver")`` rather than the row's own
    ``payload["agent_id"]``, so on a multi-fileserver deployment the regenerated job follows the LIVE
    staging path's agent choice, not phaze-fjii's per-owner rule. That is deliberate here: recovery
    must land on exactly the destination the live producer would pick, and fixing the live producer's
    choice belongs with the staging path, not with recovery.
    """
    raw_fid = target.payload.get("file_id")
    if raw_fid is None:
        raise UnreplayableRow("s3_upload ledger row carries no payload['file_id'] -- nothing durable to regenerate from")
    try:
        file_uuid = uuid.UUID(str(raw_fid))
    except ValueError as exc:
        raise UnreplayableRow(f"s3_upload ledger row carries a non-UUID file_id {raw_fid!r}") from exc
    fid = str(raw_fid)

    file = await session.get(FileRecord, file_uuid)
    if file is None:
        raise UnreplayableRow(f"file {fid} no longer exists -- the staged upload has no source to re-presign")

    try:
        await cloud_staging.redrive_upload(session, file, task_router)
        await session.commit()
    except NoCloudJobToRedriveError as exc:
        # phaze-k95r7: NOT "cannot regenerate right now" -- there is no staging attempt to regenerate.
        # Caught BEFORE the S3StagingError branch below (it is a subclass) so this row is reported as
        # the stale ledger row it is, never as a time-limited payload.
        await session.rollback()
        await cloud_staging.drop_pending_s3_enqueues(session)
        raise StaleLedgerRow(str(exc)) from exc
    except (NoActiveAgentError, S3StagingError) as exc:
        # Both are "the inputs to regenerate are not available right now", not faults: no online
        # fileserver to run the upload (cold boot), or no staging bucket recorded/resolvable for this
        # file. Skip deliberately; the staging drain / a later recovery re-drives it.
        await session.rollback()
        await cloud_staging.drop_pending_s3_enqueues(session)
        raise UnreplayableRow(str(exc)) from exc
    except BaseException:
        await session.rollback()
        await cloud_staging.drop_pending_s3_enqueues(session)
        raise

    fired = await cloud_staging.flush_pending_s3_enqueues(session)
    if fired:
        tally["reenqueued"] += 1
    else:
        tally["skipped"] += 1


_REPLAY_REGENERATORS: dict[str, Callable[[AsyncSession, Any, _RegenTarget, dict[str, int]], Awaitable[None]]] = {
    "s3_upload": _regenerate_s3_upload,
}
"""Keyed function -> the coroutine that RE-DERIVES its job from durable inputs instead of replaying
the stored payload (phaze-71nz).

Keys MUST equal ``replay_safety.LEDGER_REPLAY_REGENERATED`` exactly -- asserted by a totality test.
A member of that frozenset with no regenerator here would silently drop the stage from recovery
altogether (every row ``unreplayable``, forever), which is a quieter version of the same bug; a
regenerator here for an unlisted function would leave the classification lying about what recovery
actually does.
"""

# FAIL-LOUD at import: both sides are module-level constants, so a mismatch is a programming error
# that can only exist during development -- and it is exactly the kind that would otherwise ship
# silently. A declared-regenerated function with no regenerator drops its stage from recovery
# entirely (every row ``unreplayable``, forever); a regenerator for an undeclared function means
# ``_replay_row``'s refusal and the classification disagree about the same payload.
if set(_REPLAY_REGENERATORS) != LEDGER_REPLAY_REGENERATED:
    raise RuntimeError(
        "replay-safety drift (phaze-71nz): reenqueue._REPLAY_REGENERATORS "
        f"{sorted(_REPLAY_REGENERATORS)} != replay_safety.LEDGER_REPLAY_REGENERATED {sorted(LEDGER_REPLAY_REGENERATED)}"
    )


async def _regenerate_row_isolated(
    session: AsyncSession,
    task_router: Any,
    target: _RegenTarget,
    stages: dict[str, dict[str, int]],
) -> None:
    """Run ``row``'s regenerator, isolating both outcomes to THIS row's tally (phaze-71nz/o1xx).

    Three outcomes, three counters:

    - success -> the regenerator tallies ``reenqueued`` (fresh job) or ``skipped`` (SAQ deduped
      against a still-live job);
    - :class:`StaleLedgerRow` -> ``unreplayable`` + a WARNING saying the row points at work that is
      not pending. Checked FIRST (it is a subclass of the next branch) so a stale row is never
      described as a time-limited payload -- the mis-description that cost the 2026-08-08
      investigation (phaze-k95r7);
    - :class:`UnreplayableRow` -> ``unreplayable`` + a WARNING carrying the reason. The row stays in
      the ledger; nothing is enqueued. This is the deliberate, VISIBLE skip;
    - any other exception -> ``errored`` + an exception log, and the loop moves to the next row
      (phaze-o1xx: one bad row must never abort the remaining replay).
    """
    tally = stages.setdefault(target.function, _zero())
    regenerator = _REPLAY_REGENERATORS[target.function]
    try:
        await regenerator(session, task_router, target, tally)
    except StaleLedgerRow as exc:
        logger.warning(
            "recover_orphaned_work: row NOT replayed -- it points at work that is not pending, so there is nothing to "
            "regenerate (this is NOT a time-limited/expired payload; the ledger entry is stale and the reaper clears it "
            "once the work reads domain-complete)",
            key=target.key,
            function=target.function,
            reason=str(exc),
        )
        tally["unreplayable"] += 1
    except UnreplayableRow as exc:
        logger.warning(
            "recover_orphaned_work: row NOT replayed -- its payload is time-limited and cannot be regenerated right now "
            "(skipped deliberately, never replayed with stale credentials; ledger entry remains for the next pass)",
            key=target.key,
            function=target.function,
            reason=str(exc),
        )
        tally["unreplayable"] += 1
    except Exception:
        logger.exception(
            "recover_orphaned_work: row regeneration failed -- row skipped this run, ledger entry remains for the next pass",
            key=target.key,
            function=target.function,
        )
        tally["errored"] += 1


async def _replay_agent_rows_by_owner(
    session: AsyncSession,
    task_router: Any,
    rows: list[SchedulingLedger],
    stages: dict[str, dict[str, int]],
    *,
    required_kind: str | None,
) -> None:
    """Replay agent-routed ledger rows onto EACH row's OWNING agent (phaze-fjii / phaze-5dkgp).

    phaze-c9w9 fixed the LIVE enqueue paths (``resolve_queue_for_task`` /
    ``resolve_queues_for_owned_files``) to route every file-keyed agent task to the FILE's owning
    agent (``payload["agent_id"]``) iff that agent is live -- never to "the single most-recently-seen
    fileserver". Recovery was never updated: it picked ONE ``select_active_agent(kind="fileserver")``
    and replayed every orphaned agent row onto it, so in a multi-fileserver deployment every recovered
    row landed on ONE agent's lanes regardless of the payload's real owner. That agent's mount lacks
    the other owners' paths, so ``process_file`` FileNotFounds into terminal ANALYSIS_FAILED and the
    meta lane wedges on a consumer-less key -- a silent misroute of everyone else's work.

    This mirrors phaze-c9w9's contract for the recovery path: group ``rows`` by their stored
    ``payload["agent_id"]`` (encounter order preserved within each group), resolve each owner
    independently via :func:`select_agent_by_id`, and replay each row onto that owner's per-function
    lane queue. An owner that is offline / revoked / never-seen / wrong-kind is SKIPPED with a
    WARNING -- its rows are NEVER rerouted onto another agent (the exact misroute this fixes); a later
    recovery re-drives them once the owner is back. A row whose payload carries no ``agent_id``
    (malformed / legacy) is likewise skipped rather than blind-routed, since its true owner is
    unknowable.

    ``required_kind`` pins the accepted ``Agent.kind`` -- ``"fileserver"`` for ``push_file`` rows
    (Phase 50 / D-10: a re-driven push reads the media mount, so it MUST route to the rsync
    initiator, never a compute agent). ``None`` accepts whatever live kind the row's stored owner
    actually is (phaze-5dkgp): the compute-lane ``process_file`` enqueued by the ``/pushed`` callback
    (``routers/agent_push.py``) is owned by a COMPUTE agent, and pinning ``kind="fileserver"`` there
    made :func:`select_agent_by_id` raise :class:`NoActiveAgentError` on every recovery pass -- even
    with the owning compute agent live and healthy -- permanently stranding the row. Resolving by id
    without a kind filter and letting :func:`~phaze.services.agent_task_router.AgentTaskRouter.queue_for`
    route on the resolved agent's own id (kind-agnostic) fixes that without weakening the push_file
    guarantee, which still passes its own ``required_kind="fileserver"``.
    """
    if not rows:
        return

    owners: dict[str | None, list[SchedulingLedger]] = {}
    for row in rows:
        owner_id = (row.payload or {}).get("agent_id")
        owners.setdefault(owner_id, []).append(row)

    for owner_id, owned in owners.items():
        if owner_id is None:
            logger.warning(
                "recover_orphaned_work: agent-routed ledger row has no owning agent_id -- skipped, never rerouted (phaze-fjii)",
                functions=sorted({r.function for r in owned}),
                rows=len(owned),
            )
            continue
        try:
            # phaze-fjii (mirrors phaze-c9w9 / enqueue_router.resolve_queue_for_task's per-file
            # ``agent_id`` form): the destination is THIS owner iff it is live (and, when pinned, of
            # ``required_kind``) -- never "some other live fileserver" whose mount lacks the path.
            agent = await select_agent_by_id(session, owner_id, kind=required_kind)
        except NoActiveAgentError:
            logger.warning(
                "recover_orphaned_work: owning agent offline -- rows skipped, not rerouted (phaze-fjii)",
                agent_id=owner_id,
                required_kind=required_kind,
                functions=sorted({r.function for r in owned}),
                rows=len(owned),
            )
            continue
        # phaze-266lc: ``select_agent_by_id`` above autobegins a new transaction on this shared
        # session; commit it here, before the per-row enqueue loop below, so THIS owner's group of
        # network-dependent replay calls does not run with the session idle-in-transaction (same
        # class + same fix shape as the read-phase commit above and agent_analysis.py phaze-7jfgi).
        # ``agent`` stays usable after the commit -- ``expire_on_commit=False`` -- and only its
        # plain ``.id`` column is read below, never a lazy-loaded relationship.
        await session.commit()
        # quick-260707-dh1: derive the LANE per row via lane_for_task(row.function) (push_file -> io;
        # process_file -> analyze; ...). An unmapped function raises loudly (never a bad queue).
        for row in owned:
            try:
                agent_queue = task_router.queue_for(agent.id, lane_for_task(row.function))
            except Exception:
                # phaze-o1xx: routing itself (an unmapped legacy function) must not abort the whole
                # replay -- isolate it exactly like a failed _replay_row and move to the next row.
                logger.exception(
                    "recover_orphaned_work: agent row lane routing failed -- row skipped this run, ledger entry remains for the next pass",
                    key=row.key,
                    function=row.function,
                    agent_id=owner_id,
                )
                stages.setdefault(row.function, _zero())["errored"] += 1
                continue
            await _replay_row_isolated(agent_queue, row, stages)


async def recover_orphaned_work(ctx: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Gated, ledger-driven, idempotent restart/queue-loss recovery producer (Phase 45).

    Re-enqueues exactly ``ledger MINUS live-saq_jobs-keys MINUS domain-completed`` by replaying each
    orphaned row's STORED payload through the SAME keyed producer that originally enqueued it. Both
    the controller startup hook and the manual "Recover" button (force=True) call THIS one function,
    so the automatic and manual paths cannot drift (D-03).

    Flow:

    1. DETECT gate (skipped when ``force``): if :func:`count_inflight_jobs` reports any queued/active
       ``saq_jobs`` row, this is a DURABLE Phase-36 restart -- nothing was lost. Returns a structured
       no-op ``{"detected_loss": False, "forced": False, "stages": {}}`` and enqueues NOTHING (D-02).
    2. RECOVER (when ``saq_jobs`` is empty, OR ``force=True``): read the ledger rows + the live keys +
       the per-stage done sets ONCE; ``orphaned = [r for r in rows if r.key not in live and not
       is_domain_completed(r, done_sets)]``. Partition by ``r.routing``: controller rows replay on
       ``ctx["queue"]``; agent rows replay onto EACH row's OWNING agent (``payload["agent_id"]``,
       phaze-fjii -- mirroring phaze-c9w9's live-path per-owner routing), resolved via
       :func:`select_agent_by_id`. ``push_file`` rows require the owner to be a live FILESERVER
       (Phase 50, D-10 -- the re-drive reads the media mount); every other agent function accepts
       whatever live kind the stored owner actually is (phaze-5dkgp -- a compute-lane
       ``process_file`` row is owned by a COMPUTE agent, and pinning ``kind="fileserver"`` there
       stranded it forever). An offline / wrong-kind owner (cold boot, D-05) has its rows SKIPPED
       with a WARNING (zero counts) -- never rerouted onto another agent -- while the controller
       rows still replay. Each producer's ``None`` return (deterministic-key dedup)
       counts as ``skipped``, otherwise ``reenqueued``. A row whose replay RAISES (a transient
       ``queue.enqueue``/``connect()`` failure, or a routing failure for a legacy row) is isolated
       via :func:`_replay_row_isolated` -- logged, tallied under ``errored``, and the loop continues
       with the NEXT row rather than aborting the whole run (phaze-o1xx).
    3. REGENERATE, ahead of both partitions above (phaze-71nz): a row whose function is declared
       ``LEDGER_REPLAY_REGENERATED`` never has its stored payload replayed at all -- it goes to its
       :data:`_REPLAY_REGENERATORS` entry, which re-derives the job from durable inputs. Today that is
       ``s3_upload``, whose payload embeds presigned PUT URLs signed at the original enqueue. A row
       whose regeneration inputs are unavailable is tallied ``unreplayable`` and skipped DELIBERATELY,
       never replayed with stale credentials. Verbatim replay additionally REFUSES any payload that
       screens positive for time-limited material (:func:`_replay_row`), so a mis-classified future
       producer is caught by the substrate rather than by an incident.

    ``force=True`` bypasses ONLY the no-op DETECT gate (the manual-button path) -- it never bypasses
    the per-item deterministic-key dedup, so a forced reconcile over a live queue is still idempotent.

    Returns ``{"detected_loss": bool, "forced": bool, "unreplayable": T, "stages": {<function>:
    {"reenqueued": N, "skipped": M, "errored": K, "unreplayable": U}, ...}}`` keyed per keyed function
    (all initialized to zero so the shape is total). ``unreplayable`` is hoisted to the top level as
    the run-wide total, because it is the one outcome the OPERATOR must see: it means work was
    knowingly NOT re-enqueued, so "Recovery started" would otherwise read identically to a run that
    silently left a whole stage uncovered (phaze-71nz, acceptance 3). Degrade-safe: agent-stage
    absence skips rather than raises, and (phaze-o1xx) a per-row replay failure is isolated rather
    than aborting the remaining rows.
    """
    # Control-only task: get_settings() returns the ControlSettings in the controller role, so the
    # cast safely narrows BaseSettings -> ControlSettings (kept for parity with the control-side
    # producers; recovery itself no longer reads a settings field, but the role contract holds).
    _ = cast("ControlSettings", get_settings())

    async with ctx["async_session"]() as session:
        inflight = await count_inflight_jobs(session)
        detected_loss = inflight == 0

        if not force and not detected_loss:
            logger.info("recover_orphaned_work no-op: queue durable (Phase-36 restart)", inflight=inflight)
            # phaze-71nz: carry the run-wide ``unreplayable`` total on the no-op shape too, so every
            # caller (the startup log, the operator status fragment) can read it unconditionally.
            return {"detected_loss": False, "forced": False, "unreplayable": 0, "stages": {}}

        rows = await get_ledger_rows(session)
        live = await get_live_job_keys(session)
        # D-06: the done-sets are scoped to the ledger's files ONLY (O(|ledger|), never O(200K)). Read
        # the fids once from the same rows and bind them as a single Postgres array (:func:`_fids_scope`).
        done_sets = await _build_done_sets(session, _ledger_fids(rows))
        # SCHED-05: a file with an in-flight cloud_job row (any backend_id) is owned SOLELY by its
        # backend reconcile/`/pushed` callback -- excluding it here keeps exactly one recovery owner
        # per backend kind, so a compute-backed cloud file gains no second recovery path (no replay
        # of the 44.5k over-enqueue incident class). Read ONCE, alongside live/done_sets.
        in_flight = await _in_flight_cloud_job_ids(session)
        # 83-06 (CONSCIOUSLY REVERSES D-09): a file HELD in AWAITING_CLOUD is owned SOLELY by the
        # stage_cloud_window drain (via its awaiting cloud_job row) -- exclude it so recovery never
        # re-drives it (and never routes a held long file kind-agnostically to a fileserver for LOCAL
        # analysis, the CLOUDROUTE-02 invariant). This REPLACES the former _get_awaiting_cloud_ids +
        # held_agent_rows compute-only partition, which was reachable only via the removed D-09 held-file
        # ledger seed. Read ONCE, alongside in_flight (the two sets are disjoint -- 'awaiting' ∉ IN_FLIGHT).
        awaiting_cloud = await _awaiting_cloud_job_ids(session)

        # phaze-fc2l: SCOPE the two cloud exclusions to the functions the cloud_job actually owns
        # (_CLOUD_OWNED_FUNCTIONS). _natural_id is function-agnostic, so an unscoped exclusion dropped an
        # orphaned extract_file_metadata / search_tracklist row for
        # ANY file that merely also had an in-flight/awaiting cloud_job -- silently skipping recovery the
        # cloud callback/drain will never perform. A non-cloud-owned row for a cloud-busy file recovers
        # normally; process_file/push_file/s3_upload/submit_cloud_job still defer to their single owner.
        orphaned = [
            r
            for r in rows
            if r.key not in live
            and not is_domain_completed(r, done_sets)
            and (r.function not in _CLOUD_OWNED_FUNCTIONS or (_natural_id(r) not in in_flight and _natural_id(r) not in awaiting_cloud))
        ]

        # phaze-266lc: the read phase above (get_ledger_rows / get_live_job_keys / _build_done_sets /
        # _in_flight_cloud_job_ids / _awaiting_cloud_job_ids) is done -- ``orphaned`` is materialized
        # in-memory and nothing below reads any of those queries' results again. Commit here to close
        # the read transaction BEFORE the enqueue replay loops (controller_rows / push_rows /
        # other_agent_rows / regenerated_targets), each of which spans a per-row network-dependent
        # enqueue and, on a large orphaned set, minutes of wall time. Left open, this control-engine
        # connection sat idle-in-transaction across the whole replay -- the same phaze-1v37 pool-drain
        # class fixed at the other request/task-scoped session sites (agent_analysis.py phaze-7jfgi,
        # tags.py phaze-7bjjj). ``ctx["async_session"]`` is ``expire_on_commit=False`` (controller.py),
        # so ``rows``/``orphaned``'s ORM instances stay usable after the commit; ``select_agent_by_id``
        # and the regenerators below reopen a transaction on demand exactly as they already did on
        # every subsequent call.
        await session.commit()

        # Initialize every keyed function to zero so the return shape is TOTAL (and a stage with no
        # orphaned rows reads as an explicit zero, not a missing key the startup-log/UI must guess at).
        stages: dict[str, dict[str, int]] = {fn: _zero() for fn in _ALL_KEYED_FUNCTIONS}

        # phaze-71nz: peel the REGENERATED functions off FIRST -- their stored payload carries
        # time-limited material and must never be replayed verbatim by either partition below. They
        # are SNAPSHOT here (:class:`_RegenTarget`) because a regenerator commits/rolls back, which
        # expires every ORM instance on this session -- see the dataclass docstring.
        regenerated_targets = [
            _RegenTarget(key=r.key, function=r.function, payload=dict(r.payload or {})) for r in orphaned if r.function in _REPLAY_REGENERATORS
        ]
        replayable = [r for r in orphaned if r.function not in _REPLAY_REGENERATORS]

        controller_rows = [r for r in replayable if r.routing == "controller"]
        agent_rows = [r for r in replayable if r.routing == "agent"]

        # 83-06 (CONSCIOUSLY REVERSES D-09): the former compute-only ``held_agent_rows`` partition is GONE.
        # It caught a ``process_file`` ledger row whose file was HELD in AWAITING_CLOUD and routed it to a
        # COMPUTE agent only (CLOUDROUTE-02: never a fileserver -> never local analysis). That partition was
        # reachable ONLY because the D-09 held-file backfill SEEDED a ``process_file:<id>`` ledger row for
        # every held compute file. 83-06 removed that seed (backfill now DELETES the orphaned row and keeps
        # only the awaiting ``cloud_job`` row as the sole registry), so no held file carries a process_file
        # ledger row any more -- the partition was provably empty. The CLOUDROUTE-02 invariant is now held
        # ROBUSTLY (even for a LEGACY pre-83-06 row) by the ``awaiting_cloud`` orphan-set exclusion above:
        # a held awaiting-cloud file never reaches ANY agent partition, so it can never be analyzed locally.
        # Phase 50 (D-10): a re-driven push_file reads the media mount, so it MUST route to a FILESERVER
        # agent (the rsync initiator), never the compute agent -- partition push rows onto their own path.
        push_rows = [r for r in agent_rows if r.function == "push_file"]
        other_agent_rows = [r for r in agent_rows if r.function != "push_file"]

        # Controller rows replay regardless of agent presence (D-05). phaze-o1xx: each row is
        # isolated -- one row's failure (transient enqueue error) is tallied under "errored" and the
        # loop continues, instead of the whole replay aborting after an arbitrary prefix.
        for row in controller_rows:
            await _replay_row_isolated(ctx["queue"], row, stages)

        # Phase 50 (D-10): push_file re-drives route to a FILESERVER (the media-mount owner that runs
        # the rsync); an offline owner skips with a WARNING (the next staging-cron tick / a later
        # recovery re-drives the still-PUSHING file -- never enqueue it onto a compute agent).
        await _replay_agent_rows_by_owner(session, ctx["task_router"], push_rows, stages, required_kind="fileserver")

        # Remaining agent rows re-drive onto their OWNING agent, whatever kind it actually is
        # (phaze-5dkgp: e.g. a compute-lane process_file row owned by a compute agent) -- cold
        # boot / offline owner -> skip, never raise.
        await _replay_agent_rows_by_owner(session, ctx["task_router"], other_agent_rows, stages, required_kind=None)

        # phaze-71nz: the time-limited rows, re-derived from durable inputs rather than replayed.
        # Run LAST so a regenerator's commit boundary (it owns one, unlike every replay above) can
        # never interleave with the read-only replay partitions.
        for target in regenerated_targets:
            await _regenerate_row_isolated(session, ctx["task_router"], target, stages)

    unreplayable = sum(tally["unreplayable"] for tally in stages.values())
    if unreplayable:
        # phaze-k95r7: state the COUNT and the STAGES here, never the cause. This summary used to
        # assert "its payload is time-limited and could not be regenerated" for every unreplayable row,
        # which is one of at least two causes (the other being a row that points at work which is not
        # pending at all) -- and stating the wrong one at run level sent the 2026-08-08 investigation
        # after an expiry problem that did not exist. The per-row WARNING above carries the actual
        # reason for each row; this line only says how much was left uncovered.
        logger.warning(
            "recover_orphaned_work: some orphaned work was NOT re-enqueued (phaze-71nz). These stages are NOT covered by "
            "this run -- see the per-row warnings above for each row's reason.",
            unreplayable=unreplayable,
            stages=sorted(fn for fn, tally in stages.items() if tally["unreplayable"]),
        )
    logger.info("recover_orphaned_work complete", detected_loss=detected_loss, forced=force, unreplayable=unreplayable, stages=stages)
    return {"detected_loss": detected_loss, "forced": force, "unreplayable": unreplayable, "stages": stages}


# The eight keyed function names, sourced from ``deterministic_key._KEY_BUILDERS`` (a Postgres-free
# ``_shared`` module) so the recovery return shape can never drift from the real keyed-task universe.
# ``deterministic_key`` is import-safe here -- this module is control-only and never loaded by the
# agent worker (tests/test_task_split.py enforces the reverse direction).
_ALL_KEYED_FUNCTIONS: tuple[str, ...] = tuple(_KEY_BUILDERS)


# --- Phase 45 Plan 04: one-time idempotent startup ledger backfill (locked decision #3) --
#
# Between the 022 migration landing and the before_enqueue WRITE hook starting to populate the
# ledger, jobs ALREADY in ``saq_jobs`` (the in-flight cohort + any residual incident jobs) have no
# ledger row, so recovery could not see them. ``backfill_ledger_from_saq_jobs`` closes that gap ONCE
# by seeding the ledger from the live queued/active ``saq_jobs`` rows. It is a CONTROL-SIDE runtime
# reconcile -- NEVER an Alembic data step (Alembic must never read/write the SAQ-owned saq_jobs
# table; T-45-15). It is idempotent (``insert_ledger_if_absent`` == ON CONFLICT DO NOTHING) so it is
# safe to run on every boot and becomes a cheap no-op once the transition cohort drains.
#
# Read-only probe of the SAQ-owned table: SELECT only ``job`` (the serialized blob) + ``key``. The
# SAQ default serializer is ``json.dumps`` (build_pipeline_queue sets no custom dump/load), so the
# blob is a JSON object carrying top-level ``function`` / ``kwargs`` / ``key`` -- we parse it with
# the SAME tolerant idiom as ``pipeline._job_started_ms`` (no ``saq.Job`` construction, which would
# need the live queue object and raise on a queue-name mismatch). Only the status allowlist literal
# is in the SQL -- no operator input is interpolated (T-44-05 discipline).
_BACKFILL_SAQ_JOBS_SQL = text("SELECT job, key FROM saq_jobs WHERE status IN ('queued', 'active')")


def _parse_job_blob(blob: object) -> dict[str, Any] | None:
    """Deserialize a SAQ ``saq_jobs.job`` blob to its dict, or None if unreadable (T-45-12).

    Mirrors ``pipeline._job_started_ms``: ``json.loads`` a str/bytes blob (the default json.dumps
    serializer), pass a pre-decoded dict through, and treat anything that is not JSON / not a dict
    as None so one malformed/malicious row skips ALONE instead of aborting the batch.
    """
    try:
        data = json.loads(blob) if isinstance(blob, (str, bytes, bytearray)) else blob
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def backfill_ledger_from_saq_jobs(session: AsyncSession) -> dict[str, int]:
    """Seed the scheduling ledger from the live queued/active ``saq_jobs`` rows (idempotent).

    For each ``saq_jobs`` row with status in ``('queued', 'active')``: deserialize its job blob to
    recover ``function`` / ``kwargs`` / ``key``; if the function is a KEYED pipeline function
    (in :data:`deterministic_key._KEY_BUILDERS`) insert a ledger row with ON CONFLICT (key) DO
    NOTHING (via the Plan-01-owned :func:`insert_ledger_if_absent`, routing stamped via
    :func:`routing_for_function`). A non-keyed / random-key row is SKIPPED (no ledger row).

    The DO NOTHING conflict clause makes this:

    - idempotent -- a second call over the same broker state inserts 0 (every key already present);
    - non-clobbering -- a row already written by the before_enqueue WRITE hook is left UNTOUCHED, so
      the (possibly fresher) hook payload always wins (T-45-13).

    Degrade-safe (T-45-14): the read runs inside a SAVEPOINT (``session.begin_nested()``); a missing
    ``saq_jobs`` table (a pre-migration env) rolls the nested scope back ALONE and returns an empty
    tally. The caller commits. NEVER raises -- a backfill failure must not abort controller boot.

    Returns ``{"inserted": N, "skipped": M}`` where ``inserted`` counts ledger ``insert_if_absent``
    calls issued for keyed rows and ``skipped`` counts rows that were not keyed or whose blob/key
    could not be parsed. (DO NOTHING makes ``inserted`` an UPPER bound on rows actually written --
    a row already present is a no-op INSERT; the integration test asserts the row count, not this
    tally, for the no-overwrite case.)
    """
    tally = {"inserted": 0, "skipped": 0}

    try:
        async with session.begin_nested():
            rows = (await session.execute(_BACKFILL_SAQ_JOBS_SQL)).all()
    except Exception:
        logger.warning("ledger_backfill_degraded: saq_jobs read failed (pre-migration env?)", exc_info=True)
        return tally

    for row in rows:
        blob, key = row[0], row[1]
        data = _parse_job_blob(blob)
        if data is None:
            tally["skipped"] += 1
            continue
        function = data.get("function")
        # Belt-and-suspenders: trust the blob's function, but fall back to the saq_jobs key prefix
        # (``<function>:<natural_id>``) so a row missing the field is still classified correctly.
        if not isinstance(function, str) and isinstance(key, str):
            function = key.split(":", 1)[0]
        if not isinstance(function, str) or function not in _KEY_BUILDERS or not isinstance(key, str):
            tally["skipped"] += 1
            continue
        kwargs = data.get("kwargs")
        if not isinstance(kwargs, dict):
            kwargs = {}
        # The SAQ default json.dumps serializer writes timeout/retries (Job dataclass fields) at the
        # blob top level. Carry them through so an in-flight transition cohort recovers with its
        # real bound, not the 600s default. (For process_file this is belt-and-braces since
        # phaze-w55w1: apply_project_job_defaults pins its timeout=0 + heartbeat regardless.)
        timeout = data.get("timeout") if isinstance(data.get("timeout"), int) else None
        retries = data.get("retries") if isinstance(data.get("retries"), int) else None
        await insert_ledger_if_absent(session, key=key, function=function, kwargs=dict(kwargs), timeout=timeout, retries=retries)
        tally["inserted"] += 1

    return tally
