"""The Analyze workspace read model -- the bounded working set, the paged full listing, the
shared row projection and the per-file lane derivation.

Extracted from the former monolithic ``services/pipeline.py`` (phaze-vsqpr).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import TYPE_CHECKING, Any, cast as type_cast

from sqlalchemy import and_, false, func, or_, select
import structlog

from phaze.config import get_settings
from phaze.enums.stage import Stage
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.models.metadata import FileMetadata
from phaze.services.agent_liveness import non_local_backend_kinds
from phaze.services.pagination import DEFAULT_PAGE_SIZE, clamp_page, clamp_page_size, paged_stmt, split_sentinel
from phaze.services.stage_status import (
    inflight_clause,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import Select

    from phaze.config import ControlSettings


logger = structlog.get_logger(__name__)


# --- Phase 58 (58-04, WORK-04 / D-03) all-in-stage Analyze file table read ----------------
#
# The rows surfaced in the D-03 "one table of ALL in-stage Analyze files" table are now DERIVED
# (Phase 90 PR-A, no ``files.state`` read): any analysis row (``AnalysisResult.id IS NOT NULL`` --
# which by the builders' definitions covers the done + failed + partial-57.1 buckets), the analyze
# in-flight ledger (``inflight_clause(ANALYZE)``), and the active ``cloud_job`` lanes
# (awaiting / pushing / pushed, D-12), so a running or cloud-held file appears even before it has an
# analysis row.


# Phase 95 (phaze-zqvh.2, CONSOLE-04): the per-file table is BOUNDED at the source. The
# Phase-58 ``get_analyze_stage_files`` returned the ENTIRE analyze-stage membership -- which as the
# archive converges monotonically approaches the whole corpus (92,335 rows / ~105MB HTML at the seeded
# 200K scale, phaze-zqvh.1 baseline). It is SPLIT here into two bounded reads that share ONE row
# projection (identical per-row dict shape, so ``analyze_workspace.html`` row-building is unchanged):
#
#   * :func:`get_analyze_working_set` -- the DEFAULT view: the active-first working set (in-flight,
#     awaiting-cloud, failed -- everything that is NOT a finished completion, naturally bounded by lane
#     concurrency / the failure backlog) PLUS a LIMIT-ed recent-completions window. The dominant,
#     monotonically-growing completed set is windowed, not rendered whole.
#   * :func:`get_analyze_files_page` -- the full corpus, reachable via the status-filter bar, served as
#     bounded OFFSET pages with a ``page_size + 1`` sentinel for ``has_next`` (never a whole-corpus
#     COUNT -- the same T-87-11 DoS mitigation ``get_files_page`` uses).
#
# The membership semantics are UNCHANGED from Phase 90 (PR-A): DERIVED, never ``files.state``. A file is
# in the Analyze stage iff it carries ANY analysis row (``AnalysisResult.id IS NOT NULL`` -- SUPERSETS
# done_clause + failed_clause + any partial 57.1 row) OR its analyze is in-flight (``inflight_clause``
# over ``scheduling_ledger``) OR it carries an ACTIVE ``cloud_job`` sidecar. The correlated builders are
# NOT composed against the OUTER-JOINED columns (SQLAlchemy would auto-correlate them out of the inner
# ``exists(...)`` -- the Phase 90 blocking-fix); membership is spelled against the joined columns using
# the builders' EXACT semantics while ``inflight_clause`` (over the un-joined ledger) is composed verbatim.

# Bounded recent-completions window on the DEFAULT view (phaze-zqvh.2). Small enough that the operator
# sees "what just finished" without the whole (corpus-scale) completed set landing in the DOM.
_ANALYZE_COMPLETIONS_WINDOW = 50

# The ACTIVE cloud statuses that place a file in the Analyze working set -- the SAME five the Phase-58
# membership listed (awaiting/uploading/submitted/uploaded/running; NOT the terminal ``succeeded``,
# which the completed-window / paged listing covers instead).
_ANALYZE_ACTIVE_CLOUD_STATUSES: tuple[str, ...] = (
    CloudJobStatus.AWAITING.value,
    CloudJobStatus.UPLOADING.value,
    CloudJobStatus.SUBMITTED.value,
    CloudJobStatus.UPLOADED.value,
    CloudJobStatus.RUNNING.value,
)

# The status-filter allowlist for the paged full listing. Validated as a SET (T-87-14 / T-57-01: a
# filter value is NEVER spliced into SQL or a template path -- an unknown value degrades to the
# unfiltered "all" membership, never a 422 into the render). ``None`` (no filter) => the DEFAULT
# working-set view; ``"all"`` => the full analyze-stage membership, paged.
ANALYZE_FILTER_ALL = "all"
ANALYZE_FILTER_IN_FLIGHT = "in_flight"
ANALYZE_FILTER_AWAITING = "awaiting_cloud"
ANALYZE_FILTER_FAILED = "failed"
ANALYZE_FILTER_COMPLETED = "completed"
ANALYZE_FILTERS: frozenset[str] = frozenset(
    {
        ANALYZE_FILTER_ALL,
        ANALYZE_FILTER_IN_FLIGHT,
        ANALYZE_FILTER_AWAITING,
        ANALYZE_FILTER_FAILED,
        ANALYZE_FILTER_COMPLETED,
    }
)


@dataclass
class AnalyzeFilesPage:
    """A bounded, projected page of analyze-stage files. ``has_next`` rides a +1 sentinel -- never a COUNT."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    page: int = 1
    page_size: int = 50
    has_next: bool = False
    status: str | None = None


def _analyze_files_select() -> Select[Any]:
    """The shared analyze-file SELECT: the 11 display columns + the three degrade-safe LEFT joins.

    Extracted so the working-set, completions-window, and paged reads all project the IDENTICAL row
    shape (:func:`_project_analyze_rows`), keeping ``analyze_workspace.html`` row-building unchanged.
    LEFT JOINs the per-file ``cloud_job`` sidecar (lane derivation), the 1:1 ``analysis`` aggregate
    (windowed coverage / the 57.1 mid-flight signal + the done/failed markers), and ``metadata``
    (duration). No WHERE / ORDER here -- each caller composes its own bounded predicate + order.
    """
    return (
        select(
            FileRecord.id,
            FileRecord.original_filename,
            FileRecord.original_path,
            CloudJob.id,
            CloudJob.status,
            CloudJob.backend_id,
            AnalysisResult.fine_windows_analyzed,
            AnalysisResult.fine_windows_total,
            AnalysisResult.analysis_completed_at,
            AnalysisResult.failed_at,
            FileMetadata.duration,
        )
        .select_from(FileRecord)
        .outerjoin(CloudJob, CloudJob.file_id == FileRecord.id)
        .outerjoin(AnalysisResult, AnalysisResult.file_id == FileRecord.id)
        .outerjoin(FileMetadata, FileMetadata.file_id == FileRecord.id)
    )


def _analyze_active_where() -> Any:
    """The DEFAULT working-set predicate: analyze-stage membership MINUS finished completions.

    In-flight (a partial analysis row -- ``analysis`` row present with NO ``analysis_completed_at``,
    which also covers a ``failed_at`` row -- OR the ledger ``inflight_clause``) plus awaiting-cloud
    (an active ``cloud_job``). A completed file (``analysis_completed_at`` set) is EXCLUDED here and
    surfaced via the bounded completions window instead -- so this set never grows with the corpus.
    """
    return or_(
        and_(AnalysisResult.id.is_not(None), AnalysisResult.analysis_completed_at.is_(None)),
        inflight_clause(Stage.ANALYZE),
        CloudJob.status.in_(_ANALYZE_ACTIVE_CLOUD_STATUSES),
    )


def _analyze_status_where(status: str | None) -> Any:
    """Map a validated status filter to its WHERE predicate (the paged full-listing lens).

    ``None`` / unknown -> the full analyze-stage membership ("all", unfiltered). Each branch is a pure
    ORM bound-param comparison over the already-joined columns (never f-string SQL, never a request
    value in a path -- T-87-14 / T-57-01); the router validates ``status`` against :data:`ANALYZE_FILTERS`.
    """
    if status == ANALYZE_FILTER_IN_FLIGHT:
        return or_(
            and_(
                AnalysisResult.id.is_not(None),
                AnalysisResult.analysis_completed_at.is_(None),
                AnalysisResult.failed_at.is_(None),
            ),
            inflight_clause(Stage.ANALYZE),
        )
    if status == ANALYZE_FILTER_AWAITING:
        return CloudJob.status.in_(_ANALYZE_ACTIVE_CLOUD_STATUSES)
    if status == ANALYZE_FILTER_FAILED:
        return AnalysisResult.failed_at.is_not(None)
    if status == ANALYZE_FILTER_COMPLETED:
        return AnalysisResult.analysis_completed_at.is_not(None)
    # ANALYZE_FILTER_ALL / None / unknown -> the full analyze-stage membership (the Phase-90 predicate).
    return or_(
        AnalysisResult.id.is_not(None),
        inflight_clause(Stage.ANALYZE),
        CloudJob.status.in_(_ANALYZE_ACTIVE_CLOUD_STATUSES),
    )


def derive_file_lane(cloud_job_id: Any, backend_id: str | None, kinds: dict[str, str]) -> tuple[str, str]:
    """The COMPUTE-03 lane derivation off a file's (possibly absent) ``CloudJob`` -- the ONE place
    "which lane did this file run on" is answered, so every per-file lane badge (analyze rows,
    RECORD-01's facts grid, ...) reads the same truth instead of each growing its own copy
    (phaze-lljfx: ``record_body.html`` hardcoded ``local`` because this derivation was inlined only
    in :func:`_project_analyze_rows` and never reused).

    No ``cloud_job`` -> local; a stamped ``backend_id`` -> the id + its registry ``lane_kind`` via
    ``non_local_backend_kinds`` (falling back to ``"cloud"`` for a deregistered cluster); a NULL
    ``backend_id`` on a stamped job -> the truthful unattributed ``"cloud"`` fallback, NEVER the
    stale ``"a1"`` heuristic. ``kinds`` is the caller's once-per-call registry projection (never a
    per-row lookup).
    """
    if cloud_job_id is None:
        return "local", "local"
    if backend_id is not None:
        return backend_id, kinds.get(backend_id, "cloud")
    # Stamped cloud_job with no backend_id yet (not attributed to a registry cluster) -- the
    # truthful "cloud, unattributed" fallback. NEVER the stale "a1" heuristic label.
    return "cloud", "cloud"


def _project_analyze_rows(rows: Sequence[Any], kinds: dict[str, str]) -> list[dict[str, Any]]:
    """Project raw :func:`_analyze_files_select` rows into the per-file dict the template renders.

    The IDENTICAL shape the Phase-58 ``get_analyze_stage_files`` produced (so ``analyze_workspace.html``
    row-building is unchanged): the RECORD-01 ``file_id`` opener key, the DERIVED boolean flags
    (``awaiting_cloud`` / ``analysis_failed`` / ``completed`` -- never a raw ``files.state``), the
    COMPUTE-03 lane derivation (:func:`derive_file_lane`), and the 57.1 windowed coverage. ``kinds``
    is the once-per-call registry projection (never a per-row lookup).
    """
    files: list[dict[str, Any]] = []
    for file_id, filename, path, cloud_job_id, cloud_status, backend_id, fine_done, fine_total, completed_at, failed_at, duration in rows:
        lane, lane_kind = derive_file_lane(cloud_job_id, backend_id, kinds)
        files.append(
            {
                # Phase 61 (RECORD-01): the row->record slide-in opener keys on this file_id
                # (hx-get="/record/{file_id}"); str() so the template renders the UUID inline.
                "file_id": str(file_id),
                "filename": filename,
                "path": path,
                # Phase 90 (PR-A): derived boolean flags REPLACE the raw ``state`` key -- the template
                # renders off these, never a FileState string.
                "awaiting_cloud": cloud_status == CloudJobStatus.AWAITING.value,
                "analysis_failed": failed_at is not None,
                "lane": lane,
                "lane_kind": lane_kind,
                "fine_done": fine_done,
                "fine_total": fine_total,
                "duration": duration,
                # completed derives from the joined analysis_completed_at (done_clause(ANALYZE)), not state==ANALYZED.
                "completed": completed_at is not None,
            }
        )
    return files


async def get_analyze_working_set(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    completions_limit: int = _ANALYZE_COMPLETIONS_WINDOW,
) -> AnalyzeFilesPage:
    """Return ONE BOUNDED page of the default Analyze view: the active-first working set, then a completions window.

    phaze-5462 -- THIS READ USED TO BE UNBOUNDED, and its docstring said the opposite. The retired
    text claimed the working set was "Naturally bounded (lane concurrency + the failure backlog);
    NEVER the whole corpus". That was FALSE in production and is the entire bug: a file joins the
    working set merely by having a ``scheduling_ledger`` row OR a partial/failed ``analysis`` row, and
    ORPHANED work never leaves it on its own. With a large stuck backlog the branch rendered 10,132
    rows / 12.7 MB inline -- ~180x the sibling metadata tab. The prior fix (phaze-zqvh)
    bounded only the completions window and trusted this assertion for the other half. An assumption
    is not a bound; the LIMIT below is.

    Both reads follow the paging contract in :mod:`phaze.services.pagination` -- OFFSET paging, the
    shared :data:`~phaze.services.pagination.DEFAULT_PAGE_SIZE`, a ``page_size + 1`` sentinel for
    ``has_next`` (NEVER a whole-corpus COUNT), and the MANDATORY unique ``FileRecord.id`` tiebreaker
    (``created_at`` alone ties -- Postgres timestamp defaults are transaction-time constant -- so
    without it OFFSET paging would silently skip and duplicate rows across pages).

      1. The active working set (:func:`_analyze_active_where`) -- in-flight / awaiting-cloud /
         failed, newest-first, PAGED.
      2. The recent-completions window, appended ONLY on the final page (``has_next`` False) so the
         "active work first, then what just finished" reading survives while every page stays
         bounded. For a working set that fits one page this is byte-identical to the prior behaviour.

    Degrade-safe under ONE SAVEPOINT: any error rolls back the nested scope alone, logs, and returns
    an EMPTY page -- this rides the hot workspace render and must NEVER 500 the page.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    completions_limit = min(max(completions_limit, 0), 500)
    try:
        async with session.begin_nested():
            active_raw = (
                await session.execute(
                    paged_stmt(
                        _analyze_files_select().where(_analyze_active_where()),
                        page=page,
                        page_size=page_size,
                        order_by=(FileRecord.created_at.desc(),),
                        tiebreaker=(FileRecord.id.desc(),),
                    )
                )
            ).all()
            active_rows, has_next = split_sentinel(active_raw, page_size)
            # The completions window is a TAIL garnish, not part of the paged set -- read it only when
            # there is no further active page to show.
            window_rows = (
                (
                    await session.execute(
                        _analyze_files_select()
                        .where(AnalysisResult.analysis_completed_at.is_not(None))
                        # phaze-wiz1: exclude anything the active section would also claim -- a
                        # re-analysis-in-flight completed file (analysis_completed_at stays set through a
                        # re-run per the migration-033 XOR check, while the re-run's enqueue recreates
                        # the scheduling_ledger row / an active cloud_job) or an orphaned, never-cleared
                        # ledger row on an already-completed file. The Python `seen` dedup below only
                        # ever covered the FINAL page's active rows, which structurally cannot exclude
                        # an overlapping file that sorted onto an earlier page -- excluding at the
                        # query level (mirroring how _analyze_active_where already excludes completed
                        # rows from the active section) is correct regardless of which page it landed on.
                        # NULL-safe: _analyze_active_where()'s CloudJob.status disjunct is NULL (not
                        # False) for the common case of no cloud_job row at all (a LEFT JOIN miss), so
                        # a bare `~_analyze_active_where()` would evaluate to NULL -- and therefore
                        # WHERE-exclude -- every ordinary completed local file. coalesce(..., false())
                        # forces that NULL to False before negating.
                        .where(~func.coalesce(_analyze_active_where(), false()))
                        .order_by(AnalysisResult.analysis_completed_at.desc(), FileRecord.id.desc())
                        .limit(completions_limit)
                    )
                ).all()
                if not has_next
                else []
            )
    except Exception:
        logger.warning("analyze_working_set_degraded", page=page, page_size=page_size, exc_info=True)
        return AnalyzeFilesPage(rows=[], page=page, page_size=page_size, has_next=False, status=None)

    # COMPUTE-03: the registry projection is looked up ONCE per call (not per row).
    kinds = non_local_backend_kinds(type_cast("ControlSettings", get_settings()))
    active = _project_analyze_rows(active_rows, kinds)
    seen = {row["file_id"] for row in active}
    window = [row for row in _project_analyze_rows(window_rows, kinds) if row["file_id"] not in seen]
    return AnalyzeFilesPage(rows=active + window, page=page, page_size=page_size, has_next=has_next, status=None)


async def get_analyze_files_page(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    status: str | None = None,
) -> AnalyzeFilesPage:
    """Return ONE bounded page of the full analyze-stage listing under a validated status filter.

    Follows the paging contract in :mod:`phaze.services.pagination`: OFFSET paging, the shared
    clamps, a ``page_size + 1`` sentinel for ``has_next`` (NEVER a whole-corpus COUNT -- T-87-11), and
    the MANDATORY unique ``FileRecord.id`` tiebreaker after the non-unique ``created_at`` display
    order. ``status`` is validated against :data:`ANALYZE_FILTERS` (unknown -> the unfiltered "all"
    membership, never a 422 into the render). SAVEPOINT degrade-safe: ANY error rolls back the nested
    scope alone, logs a warning, and returns a safe EMPTY page. Rows are the SAME projected shape as
    :func:`get_analyze_working_set`, so the template renders both identically.
    """
    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    status = status if status in ANALYZE_FILTERS else None
    try:
        async with session.begin_nested():
            stmt = paged_stmt(
                _analyze_files_select().where(_analyze_status_where(status)),
                page=page,
                page_size=page_size,
                order_by=(FileRecord.created_at.desc(),),
                tiebreaker=(FileRecord.id.desc(),),
            )
            raw = (await session.execute(stmt)).all()
    except Exception:
        logger.warning("analyze_files_page_degraded", page=page, page_size=page_size, exc_info=True)
        return AnalyzeFilesPage(rows=[], page=page, page_size=page_size, has_next=False, status=status)
    rows, has_next = split_sentinel(raw, page_size)
    kinds = non_local_backend_kinds(type_cast("ControlSettings", get_settings()))
    return AnalyzeFilesPage(rows=_project_analyze_rows(rows, kinds), page=page, page_size=page_size, has_next=has_next, status=status)


def analyze_lanes_content_hash(lanes: list[dict[str, Any]], selected_lane: str | None) -> str:
    """Return a stable content hash of the #analyze-lanes grid's render inputs (phaze-zqvh.3).

    A deterministic digest over the lane snapshot + the selected-lane highlight -- the ONLY inputs that
    change what ``_analyze_lanes.html`` renders. Emitted as ``data-lanes-hash`` on the grid so a client
    ``htmx:oobBeforeSwap`` hook can SKIP the 5s OOB grid swap when the incoming state is byte-identical to
    what is already mounted -- bounding per-tick destroy-and-recreate churn (+ the Alpine re-init it
    triggers) on a long-lived, mostly-idle tab, WITHOUT a second poll loop or any change to the OOB
    store-seed fan-out (phaze-zqvh.3). Pure + degrade-safe: any serialization error collapses to ``""``
    (an empty hash never matches, so the swap always proceeds -- the fail-safe default is "always swap").
    """
    try:
        payload = json.dumps({"lanes": lanes, "selected": selected_lane}, sort_keys=True, default=str)
    except Exception:
        logger.warning("analyze_lanes_hash_degraded", exc_info=True)
        return ""
    return hashlib.sha256(payload.encode()).hexdigest()[:16]
