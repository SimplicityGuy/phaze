"""I/O layer for the set projection (phaze-x1qr3.3): the missing first step every caller of
``services.set_projection.build_profile`` needs before calling it, plus the ``set_profile`` upsert.

``services/set_projection.py`` (phaze-x1qr3.2) is pure math with no I/O: ``build_profile`` is an
AGGREGATOR over ALREADY-POPULATED per-window fields (``energy`` on coarse rows, ``camelot`` on fine
rows, ``mood_scores`` on coarse rows). Nothing in that module computes those per-window fields from
the raw stored data (``musical_key`` / ``features`` / ``bpm``) -- that is this module's job, shared
by the two writers phaze-x1qr3.3 adds: the live persist path
(``routers/agent_analysis.py::_replace_analysis_windows``) and the backfill
(``services/set_projection_backfill.py``). Both derive from ALREADY-STORED rows; neither re-runs
essentia.

BPM Z-SCORE (an implementer decision, not an operator one -- mirrors how the dispatcher recorded
``ENERGY_WEIGHTS`` on phaze-x1qr3.2 as the implementer's pick). "File-local" means the reference
distribution is THIS FILE's own fine-window BPMs, never the archive's. Per coarse window, ``z`` is
the z-score -- against the file's own fine-window BPM mean/stdev -- of the mean BPM of the fine
windows whose time range overlaps that coarse window. ``z = 0.0`` when the file has no fine BPM at
all, when the file's fine BPMs carry zero variance (a z-score against a zero-width distribution is
undefined), or when no fine window happens to overlap this particular coarse window -- in every one
of those cases ``energy`` reads from the coarse positive-class scores alone, exactly the fallback
:func:`phaze.services.set_projection.build_profile`'s ``sources`` field already names ("bpm": "none").

FAILURE ISOLATION IS THE CALLER'S JOB. Every function here raises on a malformed input rather than
swallowing it -- ``routers/agent_analysis.py`` wraps its calls in its own try/except so a projection
defect can never fail the analysis it rides along with (see that module's docstring); the backfill
wraps per-file so one bad row does not abort the whole run.
"""

from __future__ import annotations

from collections.abc import Mapping
import statistics
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.models.set_profile import SetProfile
from phaze.services.set_projection import MOOD_ORDER, build_profile, camelot_code, energy as energy_scalar, positive_class_vector


if TYPE_CHECKING:
    from collections.abc import Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.analysis import AnalysisWindow
    from phaze.services.set_projection import SetProfileProjection


logger = structlog.get_logger(__name__)

# Bump alongside a change to `ENERGY_WEIGHTS`, `MOOD_ORDER` or this module's BPM-z-score formula
# (see set_projection.py's own ENERGY_WEIGHTS docstring: phaze-x1qr3.12's operator blind check is
# what settles the weights and is what bumps this in practice). `services/set_projection_backfill.py`
# re-derives exactly the rows whose `SetProfile.projection_version` falls behind this value -- never
# the whole corpus -- so a bump here is what schedules a targeted re-backfill, not a schema change.
CURRENT_PROJECTION_VERSION: Final[int] = 1


def _extract(window: Any) -> dict[str, Any]:
    """Read the fields projection needs off either a plain dict row (pre-insert, router path) or
    an :class:`~phaze.models.analysis.AnalysisWindow` ORM instance (backfill path)."""
    get = window.get if isinstance(window, Mapping) else lambda name: getattr(window, name, None)
    return {
        "tier": get("tier"),
        "start_sec": get("start_sec"),
        "end_sec": get("end_sec"),
        "bpm": get("bpm"),
        "musical_key": get("musical_key"),
        "features": get("features"),
    }


def _bpm_stats(fine_bpms: Sequence[float]) -> tuple[float, float] | None:
    """Mean and (population) stdev of a file's fine-window BPMs, or ``None`` when there are fewer
    than 2 values or the values carry zero variance (a z-score is undefined either way)."""
    if len(fine_bpms) < 2:
        return None
    mean = statistics.fmean(fine_bpms)
    stdev = statistics.pstdev(fine_bpms, mu=mean)
    if stdev == 0:
        return None
    return mean, stdev


def _bpm_z_for_range(fine_ranges: Sequence[tuple[float, float, float]], start_sec: float, end_sec: float, stats: tuple[float, float] | None) -> float:
    """The BPM z-score attributed to one coarse window's time range (module docstring)."""
    if stats is None:
        return 0.0
    mean, stdev = stats
    overlapping = [bpm for f_start, f_end, bpm in fine_ranges if f_start < end_sec and f_end > start_sec]
    if not overlapping:
        return 0.0
    return (statistics.fmean(overlapping) - mean) / stdev


def compute_window_projection(windows: Sequence[Any]) -> list[dict[str, Any]]:
    """Per-window ``{"camelot", "energy", "mood_scores"}`` for ``windows``, in the same order.

    Pure: does not mutate ``windows``. A fine window without a ``musical_key`` and a coarse window
    without ``features`` both get a fully-``None`` entry -- the same "not yet knowable" gap every
    other nullable projection field renders, never a manufactured value.
    """
    facts = [_extract(w) for w in windows]
    fine_bpms = [f["bpm"] for f in facts if f["tier"] == "fine" and f["bpm"] is not None]
    stats = _bpm_stats(fine_bpms)
    fine_ranges = [(f["start_sec"], f["end_sec"], f["bpm"]) for f in facts if f["tier"] == "fine" and f["bpm"] is not None]

    results: list[dict[str, Any]] = []
    for fact in facts:
        if fact["tier"] == "fine":
            results.append({"camelot": camelot_code(fact["musical_key"]), "energy": None, "mood_scores": None})
        elif fact["tier"] == "coarse":
            vector = positive_class_vector(fact["features"] or {})
            scores = dict(zip(MOOD_ORDER, vector, strict=True))
            bpm_z = _bpm_z_for_range(fine_ranges, fact["start_sec"], fact["end_sec"], stats)
            results.append({"camelot": None, "energy": energy_scalar(scores, bpm_z), "mood_scores": scores})
        else:
            results.append({"camelot": None, "energy": None, "mood_scores": None})
    return results


def annotate_window_rows(rows: list[dict[str, Any]]) -> None:
    """Merge :func:`compute_window_projection` onto pre-insert row dicts (router path), in place.

    Computes the FULL result list before touching ``rows`` -- if computation raises partway
    through, ``rows`` is left completely untouched rather than half-annotated, so a caller's
    try/except sees an atomic failure and can cleanly skip the projection for this file rather than
    persist a partially-projected window set.
    """
    computed = compute_window_projection(rows)
    for row, fields in zip(rows, computed, strict=True):
        row.update(fields)


def annotate_window_orm_objects(windows: Sequence[AnalysisWindow]) -> None:
    """Merge :func:`compute_window_projection` onto already-loaded ORM instances (backfill path).

    Mutates tracked attributes in place; the caller's own ``session.commit()`` is what turns these
    into an UPDATE (SQLAlchemy's unit of work, not an explicit statement here).
    """
    computed = compute_window_projection(windows)
    for window, fields in zip(windows, computed, strict=True):
        window.camelot = fields["camelot"]
        window.energy = fields["energy"]
        window.mood_scores = fields["mood_scores"]


async def upsert_set_profile(session: AsyncSession, file_id: uuid.UUID, windows: Sequence[AnalysisWindow]) -> SetProfileProjection:
    """``build_profile(windows)`` plus an upsert of the one ``set_profile`` row for ``file_id``.

    ``windows`` must already carry their own per-window ``camelot``/``energy``/``mood_scores``
    (:func:`annotate_window_rows` / :func:`annotate_window_orm_objects`) -- this function does no
    per-window computation of its own, matching ``build_profile``'s own contract. ``sources``
    (provenance the model has no column for -- dispatcher record on phaze-x1qr3.2, item 2: log it,
    since a column would need its own migration and this is diagnostic, not a display value) is
    logged at INFO alongside the write, never persisted.
    """
    projection = build_profile(windows)
    stmt = pg_insert(SetProfile).values(
        file_id=file_id,
        mean_vector=projection.mean_vector,
        arc=projection.arc,
        glyph=projection.glyph,
        camelot_modal=projection.camelot_modal,
        harmonic_discipline=projection.harmonic_discipline,
        peak_sec=projection.peak_sec,
        projection_version=CURRENT_PROJECTION_VERSION,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["file_id"],
        set_={
            "mean_vector": stmt.excluded.mean_vector,
            "arc": stmt.excluded.arc,
            "glyph": stmt.excluded.glyph,
            "camelot_modal": stmt.excluded.camelot_modal,
            "harmonic_discipline": stmt.excluded.harmonic_discipline,
            "peak_sec": stmt.excluded.peak_sec,
            "projection_version": stmt.excluded.projection_version,
        },
    )
    await session.execute(stmt)
    logger.info(
        "set_profile_projected",
        file_id=str(file_id),
        sources=projection.sources,
        projection_version=CURRENT_PROJECTION_VERSION,
    )
    return projection


__all__ = [
    "CURRENT_PROJECTION_VERSION",
    "annotate_window_orm_objects",
    "annotate_window_rows",
    "compute_window_projection",
    "upsert_set_profile",
]
