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
windows whose time range overlaps that coarse window. ``z = 0.0`` when the file has no usable fine
BPM at all, when the file's usable fine BPMs carry zero variance (a z-score against a zero-width
distribution is undefined), or when no usable fine window happens to overlap this particular coarse
window -- in every one of those cases ``energy`` reads from the coarse positive-class scores alone.

"USABLE" is :data:`MIN_PLAUSIBLE_BPM`..:data:`MAX_PLAUSIBLE_BPM`, not merely "not ``None``"
(phaze-aswsz). The fine tier stores whatever ``RhythmExtractor2013`` returned and drops its
confidence before persistence, so digital silence arrives here as a real-looking ``bpm`` of 738.3;
see that constant for the measurement, why the gate is a band rather than a confidence threshold,
and why it is applied to the coarse-window OVERLAP set as well as to the reference distribution.

``sources`` (the field :func:`upsert_set_profile` logs) is NOT simply
:func:`phaze.services.set_projection.build_profile`'s own ``sources`` output: that function's
``_bpm_source`` is a PRESENCE check ("did any fine window carry a real bpm at all"), which reads
``"fine"`` even when this module's z-score fell back to ``0.0`` for the zero-variance or
no-overlap reasons above -- a real disagreement between what the field claims and what actually
fed a given window's energy (phaze-x1qr3.3 review finding 5). :func:`upsert_set_profile` instead
derives the LOGGED ``sources`` from whether :func:`_bpm_stats` found a USABLE reference
distribution at all (``"fine"`` only then, ``"none"`` otherwise), which is the honest claim this
module can actually make about its own BPM z-score.

FAILURE ISOLATION IS THE CALLER'S JOB, split by failure DOMAIN. The computation in this module
(``annotate_window_rows`` / ``annotate_window_orm_objects`` / :func:`build_set_profile_upsert_statement`)
raises on a malformed input rather than swallowing it, and a caller wraps that in a plain
try/except -- a projection defect must never fail the analysis it rides along with. A DATABASE-level
failure on the ``set_profile`` upsert (the vanished-file race: the file's row can be deleted
concurrently between this transaction starting and this statement running, same class as
``analysis``/``analysis_window``) is a SEPARATE domain this module does not swallow either: a bare
try/except around the execute would let a caught ``IntegrityError`` poison the rest of the caller's
transaction (request_guards.py rule 5), so :func:`build_set_profile_upsert_statement` returns the
Core statement UNEXECUTED and leaves execution to the caller, which runs it through the SAME
SAVEPOINT guard (``routers.request_guards.execute_guarding_vanished_file``) the aggregate
``AnalysisResult`` upsert already uses (phaze-x1qr3.3 review finding 1). :func:`upsert_set_profile`
(the backfill's own entry point) executes the statement directly, because ``run_backfill`` already
rolls back and counts per-file failures at its own layer with no larger transaction to protect.
"""

from __future__ import annotations

from collections.abc import Mapping
import statistics
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
import structlog

from phaze.models.set_profile import SetProfile
from phaze.services.set_projection import MOOD_ORDER, build_profile, camelot_code, energy as energy_scalar, positive_class_vector


if TYPE_CHECKING:
    from collections.abc import Sequence
    import uuid

    from sqlalchemy.dialects.postgresql import Insert
    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.analysis import AnalysisWindow
    from phaze.services.set_projection import SetProfileProjection


logger = structlog.get_logger(__name__)

# Bump alongside a change to `ENERGY_WEIGHTS`, `MOOD_ORDER` or this module's BPM-z-score formula
# (see set_projection.py's own ENERGY_WEIGHTS docstring: phaze-x1qr3.12's operator blind check is
# what settles the weights and is what bumps this in practice). `services/set_projection_backfill.py`
# re-derives exactly the rows whose `SetProfile.projection_version` falls behind this value -- never
# the whole corpus -- so a bump here is what schedules a targeted re-backfill, not a schema change.
#
# 1 -> 2 (phaze-aswsz): the BPM z-score's reference distribution gained the plausible-tempo band
# below, which changes `energy` for any file carrying at least one out-of-band fine window. The
# corpus backfill has not run, but that is NOT the same as "no rows exist": the LIVE path
# (`routers/agent_analysis.py::_replace_analysis_windows`) has written a `set_profile` row at
# version 1 on every analysis completed since phaze-x1qr3.3 landed, and those rows carry the
# distortion. The bump is what puts exactly them -- and nothing else -- in the backfill's
# `projection_version <` predicate. Bumping when the affected population turns out to be empty
# costs nothing; not bumping would leave a distorted row indistinguishable from a correct one.
CURRENT_PROJECTION_VERSION: Final[int] = 2

# The plausible-tempo band the BPM z-score's reference distribution is drawn from (phaze-aswsz).
#
# WHAT WENT WRONG WITHOUT IT. `services/analysis.py` stores every fine window's `bpm` unconditionally
# (`round(float(bpm), 1)`), and the extractor's own `confidence` never reaches the database --
# `FineWindow.as_payload_dict` omits it and `analysis_window` has no confidence column -- so the
# number itself is the only signal this module can read. Measured IN THIS ENVIRONMENT (essentia
# 2.1-beta6-dev, macOS arm64, the deployed `RhythmExtractor2013(method="multifeature")`, 44.1 kHz
# digital silence): bpm 738.3, at confidence 0.0 over a 5 s buffer and 4.69 over a 30 s one.
# `tests/analyze/services/pipeline/test_rhythm_silence_bpm.py` runs the real extractor and holds
# that measurement. ONE such window is enough to distort a whole file's `energy`, `arc` and
# `peak_sec` -- and the distortion is persisted. Measured over the 61-window fixture in
# `tests/shared/services/test_set_projection_writer.py` (sixty ~128 BPM fine windows plus one
# silent one, eleven coarse windows carrying real `features`): the file's population stdev goes
# from 0.7957 to 77.5069, which flattens every coarse window's z-score from a real -0.201..+0.553
# spread onto a near-uniform -0.13, and pushes the one coarse window the silent one overlaps from
# +0.553 to +1.188. In `energy` that is up to 0.0635 on this file. Every other BPM reader already
# gates (`analysis_windows.aggregate_bpm` on confidence, `analysis_timeline._valid_bpm` and
# `record_facts.median_bpm` on `> 0`); this reader gated on `is not None` alone, and `> 0` would
# not have caught 738.3 anyway.
#
# WHY A BAND RATHER THAN CONFIDENCE. Confidence is the sharper instrument -- 4.69 over 30 s of
# silence is a real reading no band can see -- but it is not available here at any price this bead
# can pay: it needs a nullable column on `analysis_window`, a migration, a wire-payload field, and
# a backfill that could not recover the confidence of any already-analysed window, because the
# value was never persisted. The band needs none of that and rejects the measured failure by a
# factor of 3.4. This is the IMPLEMENTER's decision, not the operator's. Persisting confidence
# stays the better fix and stays open, and is what a junk value landing INSIDE the band would call
# for.
#
# WHY THESE BOUNDS. The deployed extractor declares its own search range through its `minTempo` /
# `maxTempo` parameters -- read off the constructed algorithm in this environment as 40 and 208 --
# so a value outside that range is not an estimate the extractor claims to have made. The ceiling
# here is deliberately 220 rather than 208: wrongly EXCLUDING a real window distorts the very
# reference distribution this constant exists to protect, so the gate carries twelve BPM of
# headroom and fails open on the musical side while still rejecting the measured junk value by a
# wide margin. The invariant that matters -- the band contains the extractor's own search range --
# is asserted against the live algorithm in `test_rhythm_silence_bpm.py`, not restated here.
MIN_PLAUSIBLE_BPM: Final[float] = 40.0
MAX_PLAUSIBLE_BPM: Final[float] = 220.0


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


def _plausible_bpm(value: float | None) -> float | None:
    """A fine window's stored ``bpm`` when it falls inside the plausible-tempo band, else ``None``.

    ``None`` covers the non-finite cases for free: ``NaN`` compares false against both bounds and
    ``inf`` fails the ceiling, and either one reaching :func:`statistics.fmean` would poison the
    whole FILE's reference distribution rather than one window's. This is
    ``services/analysis_timeline._valid_bpm``'s "finite and physically meaningful, or an explicit
    absence" contract with :data:`MIN_PLAUSIBLE_BPM` / :data:`MAX_PLAUSIBLE_BPM` in place of that
    reader's ``> 0`` -- which admits 738.3 and is why a shared helper was not reused here.

    Both bounds are INCLUSIVE: they are the edges of the range the extractor searches, not values
    it cannot return.
    """
    if value is None or not MIN_PLAUSIBLE_BPM <= value <= MAX_PLAUSIBLE_BPM:
        return None
    return float(value)


def _fine_bpm_ranges(facts: Sequence[Mapping[str, Any]]) -> list[tuple[float, float, float]]:
    """``(start_sec, end_sec, bpm)`` for every fine window whose BPM is inside the band.

    THE population, derived ONCE. :func:`compute_window_projection` takes both the z-score's
    reference distribution and each coarse window's overlap set from this single list, and
    :func:`logged_sources` reports on the same list. Two separate comprehensions over the same
    facts is exactly how the band could come to be applied to the reference but not to the
    overlap -- which would keep the junk window out of the file's mean and stdev while still
    letting it drag the average of whichever coarse window it happens to sit in.
    """
    return [
        (fact["start_sec"], fact["end_sec"], bpm) for fact in facts if fact["tier"] == "fine" and (bpm := _plausible_bpm(fact["bpm"])) is not None
    ]


def _bpm_stats(fine_bpms: Sequence[float]) -> tuple[float, float] | None:
    """Mean and (population) stdev of a file's fine-window BPMs, or ``None`` when there are fewer
    than 2 values or the values carry zero variance (a z-score is undefined either way).

    ``fine_bpms`` must already have passed :func:`_plausible_bpm` -- this function has no view of
    which values are tempi and which are the extractor's junk, and a single out-of-band value moves
    the stdev it returns by two orders of magnitude (:data:`MIN_PLAUSIBLE_BPM`)."""
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
    fine_ranges = _fine_bpm_ranges(facts)
    fine_bpms = [bpm for _start, _end, bpm in fine_ranges]
    stats = _bpm_stats(fine_bpms)

    results: list[dict[str, Any]] = []
    for fact in facts:
        if fact["tier"] == "fine":
            results.append({"camelot": camelot_code(fact["musical_key"]), "energy": None, "mood_scores": None})
        elif fact["tier"] == "coarse" and fact["features"]:
            vector = positive_class_vector(fact["features"])
            scores = dict(zip(MOOD_ORDER, vector, strict=True))
            bpm_z = _bpm_z_for_range(fine_ranges, fact["start_sec"], fact["end_sec"], stats)
            results.append({"camelot": None, "energy": energy_scalar(scores, bpm_z), "mood_scores": scores})
        else:
            # Either an unrecognised tier, or a coarse window with no `features` at all (None or
            # {}) -- a fully-None entry, never a manufactured `energy=0.0` / all-null `mood_scores`
            # dict computed from nothing (phaze-x1qr3.3 review finding 3). The latter used to reach
            # `positive_class_vector({})` and `energy_scalar(all-None scores, bpm_z)`, which does
            # NOT reliably return 0.0 -- a nonzero file-local `bpm_z` alone would clamp01 to a small
            # nonzero "energy" for a window this module measured NOTHING about. It also fed
            # `_mean_vector` (set_projection.py) a non-empty all-`None` dict, which that function's
            # own truthiness check treated as real data (see its docstring fix, same review).
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


def _bpm_source(fine_bpms: Sequence[float]) -> str:
    """ "fine" only when :func:`_bpm_stats` found a USABLE reference distribution for THIS module's
    own z-score, never merely "some fine window had a bpm at all" (module docstring, finding 5)."""
    return "fine" if _bpm_stats(fine_bpms) is not None else "none"


def build_set_profile_upsert_statement(file_id: uuid.UUID, projection: SetProfileProjection) -> Insert:
    """The ``set_profile`` upsert as a Core statement -- built, NEVER executed.

    Deliberately returns rather than runs the statement: ``set_profile.file_id`` carries the same
    ``ON DELETE CASCADE`` FK to ``files.id`` that ``analysis_window`` does, so the insert can lose
    the vanished-file race (phaze-wn1l) exactly like the aggregate ``AnalysisResult`` upsert does.
    A caller with a request-scoped transaction to protect (the router) must run this through
    ``routers.request_guards.execute_guarding_vanished_file`` -- the SAME SAVEPOINT guard that
    upsert already uses -- rather than this module choosing a recovery a bare try/except cannot
    honestly provide (review finding 1: a caught DB-level exception still leaves the transaction
    poisoned unless the failing statement ran inside its own SAVEPOINT). ``updated_at`` is stamped
    explicitly in the SET clause because the ORM's ``TimestampMixin.onupdate=func.now()`` never
    fires on a Core ``ON CONFLICT DO UPDATE`` path (review finding 2; same fix as
    ``scheduling_ledger.py`` / ``cloud_budget.py``) -- without it, a re-projection after a
    ``projection_version`` bump would leave ``updated_at`` frozen at the row's first write.
    """
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
    return stmt.on_conflict_do_update(
        index_elements=["file_id"],
        set_={
            "mean_vector": stmt.excluded.mean_vector,
            "arc": stmt.excluded.arc,
            "glyph": stmt.excluded.glyph,
            "camelot_modal": stmt.excluded.camelot_modal,
            "harmonic_discipline": stmt.excluded.harmonic_discipline,
            "peak_sec": stmt.excluded.peak_sec,
            "projection_version": stmt.excluded.projection_version,
            "updated_at": func.now(),
        },
    )


def logged_sources(windows: Sequence[Any]) -> dict[str, str]:
    """The honest ``sources`` to LOG alongside a ``set_profile`` write -- derived from whether
    this module's own :func:`_bpm_stats` found a usable reference distribution, never from
    ``SetProfileProjection.sources`` (``set_projection.build_profile``'s own field, a presence-only
    check that can disagree with what actually fed a given window's z-score; module docstring,
    review finding 5)."""
    facts = [_extract(w) for w in windows]
    return {"bpm": _bpm_source([bpm for _start, _end, bpm in _fine_bpm_ranges(facts)])}


async def upsert_set_profile(session: AsyncSession, file_id: uuid.UUID, windows: Sequence[AnalysisWindow]) -> SetProfileProjection:
    """``build_profile(windows)`` plus a DIRECT execute of the ``set_profile`` upsert.

    ``windows`` must already carry their own per-window ``camelot``/``energy``/``mood_scores``
    (:func:`annotate_window_rows` / :func:`annotate_window_orm_objects`) -- this function does no
    per-window computation of its own, matching ``build_profile``'s own contract.

    This is the BACKFILL's entry point: ``services.set_projection_backfill.run_backfill`` already
    rolls back and counts a per-file failure at its own layer, with no larger request transaction
    to protect, so executing directly here (rather than through the router's SAVEPOINT guard) is
    correct for that caller. The router builds its OWN statement via
    :func:`build_set_profile_upsert_statement` and runs it through
    ``routers.request_guards.execute_guarding_vanished_file`` instead (see the module docstring).
    """
    projection = build_profile(windows)
    stmt = build_set_profile_upsert_statement(file_id, projection)
    await session.execute(stmt)
    logger.info(
        "set_profile_projected",
        file_id=str(file_id),
        sources=logged_sources(windows),
        projection_version=CURRENT_PROJECTION_VERSION,
    )
    return projection


__all__ = [
    "CURRENT_PROJECTION_VERSION",
    "MAX_PLAUSIBLE_BPM",
    "MIN_PLAUSIBLE_BPM",
    "annotate_window_orm_objects",
    "annotate_window_rows",
    "build_set_profile_upsert_statement",
    "compute_window_projection",
    "logged_sources",
    "upsert_set_profile",
]
