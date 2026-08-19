"""Shared prelude for the split `tests/shared/routers/pipeline/test_*.py` suite (phaze-7l8jh).

`tests/shared/routers/test_pipeline.py` (3,642 lines, 134 tests) was the single highest
weighted_deficit test file in the repo -- and its production counterpart,
`src/phaze/routers/pipeline.py`, was already split into the `routers/pipeline/` package
(phaze-oau1o: analysis.py, backfill.py, dashboard_stats.py, extraction.py, files.py, lanes.py,
proposals.py, recovery.py, skip.py, tracklists.py). The test file never followed. This package
mirrors that split, one test file per production submodule that this suite actually exercises
(`files.py`, `lanes.py` and `skip.py` already have their own dedicated test files elsewhere in
the tree -- see `tests/integration/test_files_*.py`, `tests/analyze/routers/test_lane_*.py`,
`tests/shared/test_eligibility_trace*.py` -- so no `test_files.py` / `test_lanes.py` /
`test_skip.py` exists here).

This module holds everything that ISN'T a `def test_*`: the shared imports, the autouse
`_cloud_compute_registry` fixture, and every `_make_*` / `_seed_*` / `_persist_*` helper and
stub class the split files import from. Nothing here was rewritten -- every symbol is a
verbatim carry-over from the original file, so no test's behavior changed as a side effect of
the split.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects import postgresql

from phaze.config import get_settings, settings
from phaze.config_backends import ComputeBackend, KubeConfig, KueueBackend, LocalBackend
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus, CloudPhase
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.models.route_control import RouteControl
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.tracklist import Tracklist
from phaze.schemas.agent_tasks import ExtractMetadataPayload, ProcessFilePayload
from tests._background_drain import drain_router_background_tasks
from tests._queue_fakes import (
    DedupFakeQueue,
    DedupFakeTaskRouter,
    install_fake_queues,
    make_agent_live,
    seed_active_agent,
    wire_fakes,
)


# phaze-w55w1: the SAQ `heartbeat` every process_file enqueue must carry, read from the SAME
# derived setting the producer reads. Hard-coding a number here would let the two drift silently
# -- the assertion is that the producer stamps the CONFIGURED deadline, not a literal.
_JOB_HEARTBEAT_SEC = get_settings().analysis_job_heartbeat_sec


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Phase 30 Plan 02: fake named-queue capture harness
#
# The lifespan is NOT run for the test client, so handlers read whatever we
# attach to ``app.state``. ``wire_fakes`` (tests/_queue_fakes.py) attaches a fake
# ``controller_queue`` (named "controller") and a fake ``task_router`` whose
# ``queue_for(agent_id)`` returns a queue named ``phaze-agent-<id>``; every
# ``enqueue`` appends ``(queue_name, task_name, kwargs)`` to a shared capture list
# so tests can assert the exact destination queue per endpoint -- proving the
# v4.0.6 default-queue misrouting is gone.
# ---------------------------------------------------------------------------


# Registry fixtures driving the Phase-67 (D-14, REG-04) reduction the rewired pipeline reads:
# a single compute backend -> cloud_enabled True + active_cloud_kind 'compute' (the v5.0 rsync path);
# a single kueue backend -> active_cloud_kind 'kueue' (the k8s/S3 path); a single local backend ->
# cloud_enabled False (all-local). The pipeline endpoints read the singleton's ``backends`` field via
# the registry-derived ``cloud_enabled`` / ``active_cloud_kind`` properties, so patching ``backends``
# drives every rewired call site through the real property logic.
_COMPUTE_BACKEND = ComputeBackend(kind="compute", id="a1", rank=10, cap=2, agent_ref="cloud-1", scratch_dir="/scratch", push_host="a1.push")
_KUEUE_BACKEND = KueueBackend(kind="kueue", id="k8s", rank=10, cap=2, kube=KubeConfig())
_LOCAL_BACKEND = LocalBackend(kind="local", id="local", rank=99, cap=1)


@pytest.fixture(autouse=True)
def _cloud_compute_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the Phase-49/50 cloud-routing tests to a single compute backend (cloud ON, rsync path).

    The Phase-67 rewire reads the registry: an all-local registry -> cloud_enabled False (all-local
    routing + backfill no-op); a single compute backend -> cloud_enabled True + active_cloud_kind
    'compute' (the live v5.0 rsync path). These regression tests assert the ON behavior (long files
    held in AWAITING_CLOUD, backfill resets+routes), so pin the singleton's ``backends`` to one
    compute backend. The cloud-off / k8s tests override it inside their own bodies.
    """
    from phaze.config import settings

    monkeypatch.setattr(settings, "backends", [_COMPUTE_BACKEND])


def _make_file() -> FileRecord:
    """Create a FileRecord with the given state."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )


async def _seed_analysis_failed(session: AsyncSession, n: int, *, stalled: bool = False) -> list[FileRecord]:
    """Seed ``n`` analyze-FAILED files, each with the DERIVED ``analysis.failed_at`` marker (Phase 90 PR-A).

    The failed-count/list readers now derive terminality from ``failed_clause(Stage.ANALYZE)`` (an
    ``analysis`` row with ``failed_at`` set), not ``files.state`` -- so a bare ``state=ANALYSIS_FAILED``
    file is invisible to them. This helper seeds BOTH the (still-present) legacy state and the marker so
    the corpus is consistent.

    ``stalled=True`` seeds the ``error_message`` with the real ``"timeout: ..."`` prefix
    ``routers/agent_analysis.py::report_analysis_failed`` composes for a heartbeat-stall kill, so
    the file is also counted by :func:`get_analysis_stalled_count` (phaze-g84sk). The default
    (``stalled=False``) uses a non-timeout detail (a crashed/errored shape) so callers that don't
    care about the distinction keep getting failures NOT counted as stalled.
    """
    files = [_make_file() for _ in range(n)]
    session.add_all(files)
    await session.flush()
    error_message = "timeout: no progress for 1800s (last stage: coarse window)" if stalled else "crashed: essentia child exited 1"
    session.add_all([AnalysisResult(id=uuid.uuid4(), file_id=f.id, failed_at=datetime.now(UTC), error_message=error_message) for f in files])
    await session.commit()
    return files


async def _is_awaiting_cloud(session: AsyncSession, file_id: uuid.UUID) -> bool:
    """Phase 90 (D-09): a file is HELD in the cloud-staging queue when it carries a cloud_job row with
    status='awaiting'. The former ``files.state = AWAITING_CLOUD`` dual-write was removed; the cloud_job
    sidecar is the sole derived authority PR-A reads. (A fresh ``execute`` always hits the DB, so no
    ``expire_all`` is needed -- and expiring here would break the caller's later ORM attribute reads.)
    """
    job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
    return job is not None and job.status == CloudJobStatus.AWAITING.value


async def _awaiting_cloud_ids(session: AsyncSession) -> set[uuid.UUID]:
    """The set of file_ids currently HELD in AWAITING_CLOUD, derived from the cloud_job sidecar (Phase 90 D-09)."""
    rows = (await session.execute(select(CloudJob.file_id).where(CloudJob.status == CloudJobStatus.AWAITING.value))).scalars().all()
    return set(rows)


async def _analysis_failed_at(session: AsyncSession, file_id: uuid.UUID) -> datetime | None:
    """Return the file's ``analysis.failed_at`` marker (83-06: a clean cloud-hold clears it to None)."""
    return (await session.execute(select(AnalysisResult.failed_at).where(AnalysisResult.file_id == file_id))).scalar_one()


async def _process_file_ledger_rows(session: AsyncSession, file_id: uuid.UUID) -> list[SchedulingLedger]:
    """Return the ``process_file:<id>`` scheduling-ledger rows for a file (83-06: a clean hold deletes them)."""
    return list((await session.execute(select(SchedulingLedger).where(SchedulingLedger.key == f"process_file:{file_id}"))).scalars().all())


def _make_file_with_convergence() -> tuple[FileRecord, AnalysisResult, FileMetadata]:
    """Create a FileRecord with both AnalysisResult and FileMetadata for convergence gate."""
    uid = uuid.uuid4()
    file_rec = FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )
    # Phase 57.1: a COMPLETED analysis row carries analysis_completed_at -- the tightened
    # proposal-convergence gate (analysis_completed_at IS NOT NULL) excludes in-progress partial rows.
    analysis = AnalysisResult(file_id=uid, bpm=128.0, musical_key="Cm", analysis_completed_at=datetime.now(UTC))
    metadata = FileMetadata(file_id=uid, artist="Test", title="Track")
    return file_rec, analysis, metadata


# ---------------------------------------------------------------------------
# Phase 49 Plan 02: per-file duration router (D-06/D-11/D-02/D-12).
#
# Long (>= cloud_route_threshold_sec) files route to a COMPUTE agent's queue
# (independent of fileserver availability); short/null-duration files route to
# the FILESERVER queue exactly as before; a long file with no compute agent is
# HELD in AWAITING_CLOUD (committed, NEVER silently analyzed locally); short/null
# files with no fileserver are reported "skipped" without aborting the run. The
# Run-analysis response reports the split counts, and the no-active-agent fragment
# is surfaced ONLY when BOTH agent kinds are absent.
# ---------------------------------------------------------------------------

_LONG = 6000.0  # >= cloud_route_threshold_sec default (5400)
_SHORT = 100.0  # < threshold


def _make_file_with_duration(duration: float | None) -> tuple[FileRecord, FileMetadata | None]:
    """Build a DISCOVERED FileRecord plus an optional FileMetadata row carrying ``duration``.

    A ``None`` duration is modeled as the absence of a metadata row (the LEFT OUTER JOIN in
    ``get_discovered_files_with_duration`` yields ``duration=None``) — exercising the
    null-routes-local branch.
    """
    uid = uuid.uuid4()
    file_rec = FileRecord(
        agent_id="test-fileserver",
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )
    md = FileMetadata(file_id=uid, duration=duration) if duration is not None else None
    return file_rec, md


async def _persist_files_with_duration(session: AsyncSession, specs: list[float | None]) -> list[FileRecord]:
    """Persist one DISCOVERED file per duration spec (+ metadata) and return the records."""
    files: list[FileRecord] = []
    mds: list[FileMetadata] = []
    for dur in specs:
        f, md = _make_file_with_duration(dur)
        files.append(f)
        if md is not None:
            mds.append(md)
    session.add_all(files)
    await session.flush()
    if mds:
        session.add_all(mds)
    await session.commit()
    return files


# ---------------------------------------------------------------------------
# Phase 49 Plan 03: POST /pipeline/backfill-cloud — "Backfill to cloud" action
# (D-08/D-09/D-10). Selects EXACTLY the timed-out long files
# (ANALYSIS_FAILED ∧ duration >= cloud_route_threshold_sec), resets them to
# DISCOVERED (committed), and routes each through the SAME per-file duration
# router as "Run Analysis": compute if a compute agent is online, else held in
# AWAITING_CLOUD with an explicit scheduling-ledger row. Never a whole-backlog
# sweep; a double-click is a no-op (the candidates have already left the
# ANALYSIS_FAILED state), and short/never-failed files are never touched.
# ---------------------------------------------------------------------------


async def _persist_failed_with_duration(session: AsyncSession, specs: list[float | None], *, with_ledger: bool = True) -> list[FileRecord]:
    """Persist one ANALYSIS_FAILED file per duration spec (+ metadata) and return the records.

    A ``None`` duration is modeled as the absence of a metadata row — such a file is
    structurally excluded from the backfill candidate set (the INNER JOIN drops it).

    ``with_ledger`` (default ``True``) also seeds a ``process_file:<id>`` scheduling-ledger row
    per file, modelling **previously-scheduled, then timed-out** work: a SAQ timeout abandons the
    job WITHOUT firing ``report_analysis_failed`` (which would clear the row), so the orphaned
    ledger row persists into ``ANALYSIS_FAILED``. Phase 55 (L4) scopes the backfill candidate query
    to exactly these ledgered files. Pass ``with_ledger=False`` to model a never-scheduled (or
    cleanly-reported-failed, row-cleared) file that the EXISTS predicate must exclude.
    """
    from phaze.services.scheduling_ledger import insert_ledger_if_absent

    files: list[FileRecord] = []
    mds: list[FileMetadata] = []
    markers: list[AnalysisResult] = []
    for dur in specs:
        uid = uuid.uuid4()
        files.append(
            FileRecord(
                agent_id="test-fileserver",
                id=uid,
                sha256_hash=uid.hex,
                original_path=f"/music/{uid.hex}.mp3",
                original_filename=f"{uid.hex}.mp3",
                current_path=f"/music/{uid.hex}.mp3",
                file_type="mp3",
                file_size=1000,
            )
        )
        # Phase 90 (PR-A): the backfill candidate query now DERIVES the terminal analyze-failure from
        # the ``analysis.failed_at`` marker (``failed_clause(ANALYZE)``), not ``files.state`` -- so seed
        # the marker alongside the (still-present) legacy state to keep the corpus consistent.
        markers.append(AnalysisResult(id=uuid.uuid4(), file_id=uid, failed_at=datetime.now(UTC), error_message="timed out"))
        if dur is not None:
            mds.append(FileMetadata(file_id=uid, duration=dur))
    session.add_all(files)
    await session.flush()
    session.add_all(markers)
    if mds:
        session.add_all(mds)
    if with_ledger:
        for file in files:
            await insert_ledger_if_absent(
                session,
                key=f"process_file:{file.id}",
                function="process_file",
                kwargs={},
                timeout=7200,
                retries=2,
            )
    await session.commit()
    return files


async def _reset_saq_jobs_minimal(session: AsyncSession) -> None:
    """Give this test a deterministic minimal ``saq_jobs`` schema (key, status).

    A sibling suite may have created ``saq_jobs`` with the full SAQ schema (NOT NULL ``job``/``queue``)
    on the shared connection, so a bare ``CREATE TABLE IF NOT EXISTS`` would no-op and a minimal INSERT
    would violate NOT NULL. DROP + CREATE inside the per-test (rolled-back) transaction pins the schema
    this test controls; the drop is undone at teardown.
    """
    await session.execute(text("DROP TABLE IF EXISTS saq_jobs"))
    await session.execute(text("CREATE TABLE saq_jobs (key TEXT PRIMARY KEY, status TEXT NOT NULL)"))


# --- 83-06 (reverses D-09): backfill produces a CLEAN drainable held file ---------------------
# OPTION A: a backfill that routes a failed long file to a cloud path (compute OR kueue) now clears
# the ``analysis.failed_at`` marker AND deletes the orphaned ``process_file:<id>`` scheduling-ledger
# row in the same transaction, KEEPING only the awaiting ``cloud_job`` row. The held file then looks
# identical to a normal "Run Analysis"-held file, so ``stage_cloud_window`` DISPATCHES it (the retained
# ``failed_at`` + ledger row previously made ``awaiting_candidate_clause`` exclude it -- 83-06). This
# CONSCIOUSLY REVERSES D-09 (the drain, via the awaiting ``cloud_job`` row, is now the single owner;
# D-09's ledger-replay recovery purpose was already dead because ``analysis.failed_at`` put the held
# file in the analyze domain-completed exclusion, so ``recover_orphaned_work`` never replayed it).


class _DrainableStubBackend:
    """A duck-typed cloud ``Backend`` (non-local, so ``select_backend`` treats it as a cloud lane).

    Mirrors ``tests/analyze/tasks/test_release_awaiting_cloud._StubBackend``'s healthy path: a genuine
    stage PROMOTES the file's existing awaiting ``cloud_job`` row to SUBMITTED + stamps ``backend_id``
    (no commit -- the drain owns the single post-loop commit) and returns ``True``.
    """

    def __init__(self, *, id: str, rank: int = 10, cap: int = 5) -> None:
        self.id = id
        self.rank = rank
        self.cap = cap
        self.dispatch_calls = 0

    async def is_available(self, session: AsyncSession) -> bool:  # noqa: ARG002 -- protocol signature
        return True

    async def in_flight_count(self, session: AsyncSession) -> int:  # noqa: ARG002 -- protocol signature
        return 0

    async def dispatch(self, file: FileRecord, session: AsyncSession, task_router: object) -> bool:  # noqa: ARG002 -- protocol signature
        self.dispatch_calls += 1
        await session.execute(update(CloudJob).where(CloudJob.file_id == file.id).values(backend_id=self.id, status=CloudJobStatus.SUBMITTED.value))
        return True

    async def reconcile(self, session: AsyncSession, ctx: dict[str, object] | None = None) -> dict[str, int] | None:  # noqa: ARG002
        return None


class _DrainCfg:
    """Minimal registry-derived cfg the drain reads (cloud on + the two select_backend knobs)."""

    cloud_enabled = True
    cloud_submit_max_attempts = 3
    cloud_spill_to_local_after_seconds = 900


async def _run_stage_cloud_window(monkeypatch: pytest.MonkeyPatch, backend: _DrainableStubBackend) -> dict[str, int]:
    """Run the real ``stage_cloud_window`` drain against a single drainable stub backend.

    Pins the drain's ``get_settings`` + deferred ``resolve_backends`` seams (mirrors the drain-suite
    harness) and wires ``ctx["async_session"]`` from ``phaze.database.async_session`` -- the conftest
    ``_route_stats_fanout`` binds it to the SAME per-test ``_db_connection`` (create_savepoint), so the
    drain SEES the backfill-committed awaiting rows and its promotion is visible to a later read.
    """
    from phaze.database import async_session
    from phaze.tasks.release_awaiting_cloud import stage_cloud_window

    monkeypatch.setattr("phaze.tasks.release_awaiting_cloud.get_settings", lambda: _DrainCfg())
    monkeypatch.setattr("phaze.services.backends.resolve_backends", lambda cfg: [backend])  # noqa: ARG005
    ctx = {"async_session": async_session, "queue": DedupFakeQueue("controller"), "task_router": DedupFakeTaskRouter()}
    return await stage_cloud_window(ctx)


async def _cloud_job_status(session: AsyncSession, file_id: uuid.UUID) -> tuple[str, str | None]:
    """Return ``(status, backend_id)`` for a file's cloud_job sidecar row (fresh DB read)."""
    session.expire_all()
    row = (await session.execute(select(CloudJob.status, CloudJob.backend_id).where(CloudJob.file_id == file_id))).one()
    return row[0], row[1]


# The removed surface's LIVE artifacts. Deliberately token-level and not the bare word
# "deepen": history is allowed to be discussed in prose (several docstrings correctly explain
# that a re-analysis of an already-complete file used to arrive via that path), and a sweep
# that forbade the word would push accurate history out of the codebase to buy nothing. What
# must not survive is anything a browser or Jinja can still resolve.
_DEAD_DEEPEN_ARTIFACTS = (
    "/deepen",  # the route path -- covers the button's hx-post, the poll's hx-get, and the decorators
    "def deepen_",  # the route handlers themselves
    "sampled_badge.html",  # the deleted badge partial, as an {% include %}
    "deepen_response.html",
    "deepen_progress.html",
    "analysis.sampled",  # the dropped column, in a Jinja gate or a query
)


# ---------------------------------------------------------------------------
# Phase 41 (REQ-41-2/REQ-41-4): the bulk match trigger routes to the controller queue (never
# default), skips already-linked rows, and renders the tracklist-unit empty-state.
#
# phaze-2akf: the SEARCH (Phase 39) and SCRAPE (Phase 41) bulk-trigger sections that used to sit
# here are gone with their endpoints. Both fanned one job out per file / per tracklist against a
# host whose entire published budget is ~1 request / 8 s, and the detail-page selectors they
# ultimately reached matched zero nodes -- so every job they enqueued produced an empty tracklist.
# Their routing invariant (a controller task must never land on the consumer-less default queue) is
# still asserted below on the surviving sibling, and the endpoints' ABSENCE is asserted in
# ``test_the_retired_bulk_scrape_triggers_are_gone``.
# ---------------------------------------------------------------------------


def _make_tracklist(n: int) -> Tracklist:
    """Build a bare Tracklist row (no version, no discogs chain) — match pending."""
    uid = uuid.uuid4()
    return Tracklist(id=uid, external_id=f"tl-{n}-{uid.hex}", source_url=f"http://x/{n}")


def _link_tracklist(file_rec: FileRecord) -> Tracklist:
    """Build a Tracklist row linked to ``file_rec`` (marks the file as already-matched)."""
    uid = uuid.uuid4()
    return Tracklist(
        external_id=uid.hex,
        source_url=f"https://www.1001tracklists.com/tracklist/{uid.hex}/x.html",
        file_id=file_rec.id,
    )


def _link_propagated_tracklist(file_rec: FileRecord, *, external_id: str, set_key: str) -> Tracklist:
    """Build a PROPAGATED Tracklist row linked to ``file_rec`` -- a duplicate's projection of
    another file's canonical scrape (phaze-fq9h.7).

    Unlike ``_link_tracklist``, ``external_id`` is a caller-supplied argument rather than a fresh
    uuid, so a test can link this row to the SAME page as a canonical row it seeds separately
    (typically via ``_link_tracklist`` on a different file) -- the combination that reproduces
    phaze-vtovq: refreshing from the DUPLICATE's own file id.
    """
    return Tracklist(
        external_id=external_id,
        source_url=f"https://www.1001tracklists.com/tracklist/{external_id}/x.html",
        file_id=file_rec.id,
        propagated_from_set_key=set_key,
        propagation_confidence="exact",
    )


# ---------------------------------------------------------------------------
# phaze-fq9h.8: per-file prioritize/un-prioritize + the drain progress fragment / manual
# slice trigger. drain_tracklists is a CONTROLLER task (Phase-30 rule); it must never land on
# the consumer-less default queue.
# ---------------------------------------------------------------------------


async def _seed_live_set_file(session: AsyncSession, *, duration: float = 7200.0) -> FileRecord:
    """A long-duration file -- classifies LIVE_SET regardless of its own filename markers."""
    file_rec = _make_file()
    session.add(file_rec)
    await session.flush()
    session.add(FileMetadata(file_id=file_rec.id, duration=duration))
    await session.commit()
    return file_rec


# ---------------------------------------------------------------------------
# phaze-71nz: the operator must be able to tell a recovery that COVERED the work
# from one that knowingly skipped a stage. "Recovery started" cannot be the last word.
#
# On 2026-07-31 a single Recover replayed 430 orphaned s3_upload rows whose payloads carried
# presigned S3 URLs signed at the original enqueue; 428 ran their retries out to terminal `failed`
# (122x HTTP 403, 257x HTTP 400) and zero succeeded. The operator's UI showed 200 + "Recovery
# started — re-enqueuing any orphaned work across all stages", identical to a clean run. Because the
# producer is fire-and-forget, the POST response genuinely cannot know the outcome -- so the fragment
# now polls GET /pipeline/recover/status, which does.
# ---------------------------------------------------------------------------


def _set_recovery_state(*, running: bool = False, failed: bool = False, result: dict[str, object] | None = None) -> None:
    """Pin the in-process last-recovery cell the status fragment renders from."""
    # phaze-oau1o: `routers/pipeline.py` is now a package; `_recovery_state` is read from the
    # `recovery` submodule's namespace, so patching the facade would silently no-op.
    import phaze.routers.pipeline.recovery as pipeline_mod

    pipeline_mod._recovery_state.update(running=running, failed=failed, result=result)


def _recovery_result(**stages: dict[str, int]) -> dict[str, object]:
    """Build a ``recover_orphaned_work``-shaped return value from per-stage tallies."""
    return {
        "detected_loss": True,
        "forced": True,
        "unreplayable": sum(tally.get("unreplayable", 0) for tally in stages.values()),
        "stages": stages,
    }


# ---------------------------------------------------------------------------
# PR4: dashboard activity indicator (green pulse / amber "stalled?")
# ---------------------------------------------------------------------------


async def _seed_running_scan(session: AsyncSession, *, seconds_quiet: int, scan_path: str) -> uuid.UUID:
    """Seed a RUNNING ScanBatch whose heartbeat is `seconds_quiet` seconds old."""
    from datetime import timedelta

    batch_id = uuid.uuid4()
    batch = ScanBatch(
        id=batch_id,
        agent_id="test-fileserver",
        scan_path=scan_path,
        status=ScanStatus.RUNNING.value,
        total_files=0,
        processed_files=0,
        last_progress_at=datetime.now(UTC) - timedelta(seconds=seconds_quiet),
    )
    session.add(batch)
    await session.commit()
    return batch_id


# ---------------------------------------------------------------------------
# Phase 55 (55-05, D-04, KROUTE-06): Cloud admission-state card. Carrier-always /
# body-conditional: the #admission-state-card <section> ALWAYS renders (stable OOB
# target), but the heading + four-tile grid render ONLY when any cloud_phase count
# > 0. a1/local rows have NULL cloud_phase so all-zero leaves a quiet empty carrier.
# Each tile is gated on its own count and finished uses GREEN (not amber/alert).
# ---------------------------------------------------------------------------


async def _seed_cloud_phase(session: AsyncSession, *, cloud_phase: str | None) -> None:
    """Seed one file + its cloud_job row in the given cloud_phase (NULL for a1/local) and commit."""
    file = _make_file()
    session.add(file)
    await session.flush()
    session.add(
        CloudJob(
            id=uuid.uuid4(),
            file_id=file.id,
            s3_key=f"phaze-staging/{file.id}",
            status=CloudJobStatus.SUBMITTED.value,
            cloud_phase=cloud_phase,
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Analyze redesign: stable OOB carriers remain unique while cloud implementation detail lives
# behind progressive disclosure. Structural regression guards use ids and disclosure boundaries,
# not pixel-specific classes.
# ---------------------------------------------------------------------------

_ALL_SIX_CARD_IDS = (
    "admission-state-card",
    "inadmissible-card",
    "localqueue-card",
    "awaiting-cloud-card",
    "staged-pushing-card",
    "analyzing-cloud-card",
)


# ---------------------------------------------------------------------------
# phaze-zyoag acceptance 5: a kueue cloud_job row in each of {uploading, uploaded, submitted, running}
# must describe ITSELF consistently across the Staged / Analyzing / Admission panels on the SAME
# rendered page -- no row may be claimed by two contradictory captions (the original bug report's
# shape: one SUBMITTED row was simultaneously "Queued (quota)" on Admission AND "mid-transfer" on
# Staged).
# ---------------------------------------------------------------------------

_VOX_KUEUE_ONLY_TOML = """
[[backends]]
kind = "kueue"
id = "vox"
rank = 10
cap = 5
buckets = ["burst-vox"]

  [backends.kube]
  api_url = "https://kube.example:6443"
  namespace = "phaze"
  local_queue = "phaze-burst"

[[backends]]
kind = "local"
id = "local"
rank = 99
cap = 1

[[buckets]]
id = "burst-vox"
scope = "cluster-specific"
bucket = "phaze-burst"
endpoint_url = "https://s3.example"
"""


# ---------------------------------------------------------------------------
# phaze-c9w9: multi-fileserver ownership routing -- bulk triggers route each file
# to its OWNING agent, never one most-recently-seen pick for the whole set.
# ---------------------------------------------------------------------------


def _make_file_owned_by(agent_id: str) -> FileRecord:
    """A FileRecord owned by ``agent_id`` (the phaze-c9w9 ownership-routing fixtures)."""
    uid = uuid.uuid4()
    return FileRecord(
        agent_id=agent_id,
        id=uid,
        sha256_hash=uid.hex,
        original_path=f"/music/{uid.hex}.mp3",
        original_filename=f"{uid.hex}.mp3",
        current_path=f"/music/{uid.hex}.mp3",
        file_type="mp3",
        file_size=1000,
    )


__all__ = [
    "TYPE_CHECKING",
    "UTC",
    "_ALL_SIX_CARD_IDS",
    "_COMPUTE_BACKEND",
    "_DEAD_DEEPEN_ARTIFACTS",
    "_JOB_HEARTBEAT_SEC",
    "_KUEUE_BACKEND",
    "_LOCAL_BACKEND",
    "_LONG",
    "_SHORT",
    "_VOX_KUEUE_ONLY_TOML",
    "AnalysisResult",
    "Any",
    "CloudJob",
    "CloudJobStatus",
    "CloudPhase",
    "ComputeBackend",
    "DedupFakeQueue",
    "DedupFakeTaskRouter",
    "ExtractMetadataPayload",
    "FileMetadata",
    "FileRecord",
    "KubeConfig",
    "KueueBackend",
    "LocalBackend",
    "Path",
    "ProcessFilePayload",
    "RouteControl",
    "ScanBatch",
    "ScanStatus",
    "SchedulingLedger",
    "Tracklist",
    "_DrainCfg",
    "_DrainableStubBackend",
    "_analysis_failed_at",
    "_awaiting_cloud_ids",
    "_cloud_compute_registry",
    "_cloud_job_status",
    "_is_awaiting_cloud",
    "_link_propagated_tracklist",
    "_link_tracklist",
    "_make_file",
    "_make_file_owned_by",
    "_make_file_with_convergence",
    "_make_file_with_duration",
    "_make_tracklist",
    "_persist_failed_with_duration",
    "_persist_files_with_duration",
    "_process_file_ledger_rows",
    "_recovery_result",
    "_reset_saq_jobs_minimal",
    "_run_stage_cloud_window",
    "_seed_analysis_failed",
    "_seed_cloud_phase",
    "_seed_live_set_file",
    "_seed_running_scan",
    "_set_recovery_state",
    "datetime",
    "delete",
    "drain_router_background_tasks",
    "get_settings",
    "install_fake_queues",
    "make_agent_live",
    "postgresql",
    "pytest",
    "seed_active_agent",
    "select",
    "settings",
    "text",
    "timedelta",
    "update",
    "uuid",
    "wire_fakes",
]
