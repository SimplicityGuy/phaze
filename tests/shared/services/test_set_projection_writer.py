"""Unit-level tests for ``services/set_projection_writer.py`` (phaze-x1qr3.3) -- the I/O layer's
PURE half (``compute_window_projection`` and its helpers), exercised directly on plain dict rows
with no database. DB-backed round trips (the router path, the backfill path) live in
``tests/shared/tasks/test_analysis_persist_projection.py`` and
``tests/shared/cli/test_backfill_projection.py``; this module targets the BPM-z-score edge cases
those corpus-level tests do not naturally hit -- zero-variance fine BPMs, a coarse window with no
overlapping fine window, and ``annotate_window_rows``' atomic-on-failure contract.

phaze-aswsz adds the plausible-tempo band's own cases at the end of the file. Those run the real
writer over real ``AnalysisWindow`` instances carrying real coarse ``features`` from
``tests/analyze/_real_result.py``, because the property under test is what a whole FILE's energies
do when one window is junk -- not what a helper returns for one value.
"""

from __future__ import annotations

import itertools
import math
from typing import Any
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from phaze.models.analysis import AnalysisWindow
from phaze.services.set_projection import MOOD_ORDER, SetProfileProjection, energy as energy_scalar, positive_class_vector
from phaze.services.set_projection_writer import (
    MAX_PLAUSIBLE_BPM,
    MIN_PLAUSIBLE_BPM,
    _bpm_stats,
    _bpm_z_for_range,
    _plausible_bpm,
    annotate_window_orm_objects,
    annotate_window_rows,
    build_set_profile_upsert_statement,
    compute_window_projection,
    logged_sources,
)
from tests.analyze._real_result import real_analysis_result


def test_bpm_stats_is_none_below_two_values() -> None:
    assert _bpm_stats([]) is None
    assert _bpm_stats([120.0]) is None


def test_bpm_stats_is_none_when_every_value_is_identical() -> None:
    """Zero variance -- a z-score against a zero-width distribution is undefined, not zero."""
    assert _bpm_stats([120.0, 120.0, 120.0]) is None


def test_bpm_stats_reports_mean_and_stdev_for_a_real_spread() -> None:
    stats = _bpm_stats([100.0, 120.0, 140.0])
    assert stats is not None
    mean, stdev = stats
    assert mean == 120.0
    assert stdev > 0.0


def test_bpm_z_for_range_is_zero_with_no_stats() -> None:
    assert _bpm_z_for_range([(0.0, 30.0, 120.0)], 0.0, 180.0, None) == 0.0


def test_bpm_z_for_range_is_zero_when_no_fine_window_overlaps() -> None:
    """Stats exist (the file has usable fine BPM elsewhere), but none of the fine windows fall
    inside THIS coarse window's time range -- z reads 0.0 rather than reaching outside the range."""
    stats = (120.0, 10.0)
    fine_ranges = [(0.0, 30.0, 100.0), (30.0, 60.0, 140.0)]
    assert _bpm_z_for_range(fine_ranges, 500.0, 680.0, stats) == 0.0


def test_bpm_z_for_range_scores_the_overlapping_windows_average() -> None:
    stats = (120.0, 10.0)
    fine_ranges = [(0.0, 30.0, 130.0), (30.0, 60.0, 130.0)]
    assert _bpm_z_for_range(fine_ranges, 0.0, 60.0, stats) == 1.0


def test_compute_window_projection_gives_a_featureless_coarse_window_a_fully_none_entry() -> None:
    """Review finding 3: a coarse window with no ``features`` at all (``None`` or ``{}``) must
    read as a gap, never a manufactured ``energy=0.0`` computed from a bpm_z term alone or an
    all-null ``mood_scores`` dict that ``_mean_vector`` (set_projection.py) would otherwise count
    as real data."""
    for empty_features in (None, {}):
        windows = [{"tier": "coarse", "start_sec": 0.0, "end_sec": 180.0, "bpm": None, "musical_key": None, "features": empty_features}]

        result = compute_window_projection(windows)

        assert result == [{"camelot": None, "energy": None, "mood_scores": None}]


def test_compute_window_projection_gives_a_fully_none_entry_for_an_unrecognised_tier() -> None:
    """A window whose ``tier`` is neither ``fine`` nor ``coarse`` (a malformed row this module has
    never been asked to handle) gets the same all-``None`` gap as any other "not yet knowable"
    field, never an exception -- ``compute_window_projection`` must stay total over its input."""
    windows = [{"tier": "unknown", "start_sec": 0.0, "end_sec": 30.0, "bpm": None, "musical_key": None, "features": None}]

    result = compute_window_projection(windows)

    assert result == [{"camelot": None, "energy": None, "mood_scores": None}]


def test_annotate_window_rows_leaves_rows_untouched_when_computation_raises() -> None:
    """A coarse row whose ``features`` is not a mapping (e.g. a raw string) makes
    ``positive_class_vector`` raise; ``annotate_window_rows`` must not partially merge onto the
    rows it was given -- the atomicity ``annotate_window_rows``' own docstring promises."""
    rows: list[dict[str, object]] = [
        {"tier": "fine", "start_sec": 0.0, "end_sec": 30.0, "bpm": 120.0, "musical_key": "A minor", "features": None},
        {"tier": "coarse", "start_sec": 30.0, "end_sec": 210.0, "bpm": None, "musical_key": None, "features": "not-a-mapping"},
    ]

    with pytest.raises(AttributeError):
        annotate_window_rows(rows)

    assert "camelot" not in rows[0]
    assert "energy" not in rows[1]
    assert "mood_scores" not in rows[1]


def test_annotate_window_orm_objects_sets_the_three_attributes_from_dict_like_rows() -> None:
    """``annotate_window_orm_objects`` reads via ``getattr`` (not ``.get``), so a lightweight
    stand-in object -- not a real ``AnalysisWindow`` -- exercises the non-Mapping branch of
    ``_extract`` directly, mirroring what the real ORM instances the backfill loads look like."""

    class _FakeWindow:
        def __init__(self, **kwargs: object) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    fine = _FakeWindow(tier="fine", start_sec=0.0, end_sec=30.0, bpm=120.0, musical_key="A minor", features=None)
    windows = [fine]

    annotate_window_orm_objects(windows)  # type: ignore[arg-type]

    assert fine.camelot == "8A"
    assert fine.energy is None
    assert fine.mood_scores is None


def test_build_set_profile_upsert_statement_stamps_updated_at_explicitly() -> None:
    """Review finding 2: the ORM's ``onupdate=func.now()`` never fires on this Core
    ``ON CONFLICT DO UPDATE`` path, so the SET clause must stamp ``updated_at`` explicitly or a
    re-projection after a ``projection_version`` bump would leave it frozen at the row's first
    write (the same defect class already fixed at ``scheduling_ledger.py`` / ``cloud_budget.py``).
    """
    projection = SetProfileProjection(
        mean_vector=None,
        arc=None,
        glyph=None,
        camelot_modal=None,
        harmonic_discipline=None,
        peak_sec=None,
        sources={"bpm": "none"},
    )

    stmt = build_set_profile_upsert_statement(uuid.uuid4(), projection)

    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "updated_at" in compiled
    assert "on conflict" in compiled.lower()


def test_logged_sources_reads_none_when_bpm_stats_has_no_usable_distribution() -> None:
    """Review finding 5: the LOGGED ``sources`` must say ``"none"`` whenever :func:`_bpm_stats`
    could not build a usable reference distribution -- fewer than 2 fine BPMs, or zero variance --
    not merely "no fine window had a bpm at all" (``set_projection.build_profile``'s own
    presence-only ``_bpm_source``, which this module's docstring explicitly says NOT to trust for
    this purpose)."""
    single_bpm = [{"tier": "fine", "start_sec": 0.0, "end_sec": 30.0, "bpm": 120.0, "musical_key": None, "features": None}]
    assert logged_sources(single_bpm) == {"bpm": "none"}

    zero_variance = [
        {"tier": "fine", "start_sec": 0.0, "end_sec": 30.0, "bpm": 120.0, "musical_key": None, "features": None},
        {"tier": "fine", "start_sec": 30.0, "end_sec": 60.0, "bpm": 120.0, "musical_key": None, "features": None},
    ]
    assert logged_sources(zero_variance) == {"bpm": "none"}


def test_logged_sources_reads_fine_with_a_real_usable_spread() -> None:
    real_spread = [
        {"tier": "fine", "start_sec": 0.0, "end_sec": 30.0, "bpm": 100.0, "musical_key": None, "features": None},
        {"tier": "fine", "start_sec": 30.0, "end_sec": 60.0, "bpm": 140.0, "musical_key": None, "features": None},
    ]
    assert logged_sources(real_spread) == {"bpm": "fine"}


# ---------------------------------------------------------------------------
# phaze-aswsz: the plausible-tempo band on the BPM z-score's reference population
# ---------------------------------------------------------------------------

_SILENT_WINDOW_BPM = 738.3
"""What ``RhythmExtractor2013(method="multifeature")`` returns for 44.1 kHz digital silence,
measured in this environment (essentia 2.1-beta6-dev, macOS arm64): bpm 738.3, at confidence 0.0
over a 5 s buffer and 4.69 over a 30 s one. The confidence never reaches the database, so this
number arrives at the writer indistinguishable from a tempo. The REAL extractor is what pins the
out-of-band property -- `tests/analyze/services/pipeline/test_rhythm_silence_bpm.py`, per
`docs/design/0012-verification-fidelity-and-operator-attribution.md` rule 3. This module
deliberately stays essentia-free (see `tests/shared/core/test_task_split.py`) and takes the
measured value as a literal."""

_ENERGY_TOLERANCE = 1e-9
"""The named tolerance the acceptance criterion asks for, set at float-noise scale on purpose.

The band removes the junk window from BOTH halves of the computation -- the file's reference
mean/stdev and each coarse window's overlap set -- so the two window sets below feed
``_bpm_stats`` and ``_bpm_z_for_range`` byte-identical inputs and the energies come out exactly
equal. Stating a tolerance rather than ``==`` keeps the assertion about the PROPERTY the bead is
buying (one silent window must not move the file's energy arc) rather than about float identity,
which a later change to the z-score's arithmetic could break without breaking the property."""

_FINE_WINDOW_SEC = 30.0
_COARSE_WINDOW_SEC = 180.0
_MUSICAL_FINE_COUNT = 60
_SILENT_FINE_INDEX = 30


def _musical_bpm(index: int) -> float:
    """A ~128 BPM set with the small window-to-window jitter a real fine tier produces.

    Deterministic and deliberately non-constant: a constant-BPM control would carry zero variance,
    ``_bpm_stats`` would return ``None``, every ``bpm_z`` would be 0.0 and the acceptance test
    below would pass without the ``bpm_z`` term ever contributing to an energy -- vacuously green
    against the very defect it exists to catch. Measured spread of this sequence: mean 127.96,
    population stdev 0.7957.
    """
    return round(128.0 + (index % 7 - 3) * 0.4, 1)


def _real_coarse_features() -> list[Any]:
    """The four coarse ``features`` dicts from the REAL ``analyze_file`` artifact.

    Real essentia over a real archive track and the real 68-file model set
    (``tests/analyze/_real_result.py``), not a hand-built stub: the energies below then come out at
    0.3985..0.5933, well clear of both ``_clamp01`` edges, so a distorted ``bpm_z`` shows up as a
    moved number rather than being silently absorbed by the clamp.
    """
    return [window["features"] for window in real_analysis_result()["windows"] if window["tier"] == "coarse"]


def _window_set(*, include_silent: bool) -> list[AnalysisWindow]:
    """A 1830 s set: 60 musical fine windows, 11 coarse windows, optionally one silent fine window.

    Real :class:`~phaze.models.analysis.AnalysisWindow` instances (unflushed -- this exercises the
    computation, not the database), so ``_extract``'s ``getattr`` branch reads the same column
    names the backfill's loaded rows carry. The silent window sits at 900..930 s, inside the coarse
    window spanning 900..1080 s, so it is on BOTH sides of the defect: in the file-wide reference
    distribution and in one coarse window's own overlap set.
    """
    file_id = uuid.uuid4()
    features_cycle = itertools.cycle(_real_coarse_features())
    windows: list[AnalysisWindow] = []

    fine_index = 0
    for slot in range(_MUSICAL_FINE_COUNT + 1):
        silent = slot == _SILENT_FINE_INDEX
        if silent and not include_silent:
            continue
        windows.append(
            AnalysisWindow(
                id=uuid.uuid4(),
                file_id=file_id,
                tier="fine",
                window_index=slot,
                start_sec=slot * _FINE_WINDOW_SEC,
                end_sec=(slot + 1) * _FINE_WINDOW_SEC,
                bpm=_SILENT_WINDOW_BPM if silent else _musical_bpm(fine_index),
                musical_key="A minor",
            )
        )
        if not silent:
            fine_index += 1

    total_sec = (_MUSICAL_FINE_COUNT + 1) * _FINE_WINDOW_SEC
    start = 0.0
    coarse_index = 0
    while start < total_sec:
        windows.append(
            AnalysisWindow(
                id=uuid.uuid4(),
                file_id=file_id,
                tier="coarse",
                window_index=coarse_index,
                start_sec=start,
                end_sec=min(start + _COARSE_WINDOW_SEC, total_sec),
                features=next(features_cycle),
            )
        )
        start += _COARSE_WINDOW_SEC
        coarse_index += 1
    return windows


def _coarse_energies(windows: list[AnalysisWindow]) -> list[float]:
    return [window.energy for window in windows if window.tier == "coarse"]


def test_plausible_bpm_accepts_the_band_inclusively_and_rejects_everything_else() -> None:
    """Both bounds are edges of the range the extractor searches, not values it cannot return."""
    assert _plausible_bpm(MIN_PLAUSIBLE_BPM) == MIN_PLAUSIBLE_BPM
    assert _plausible_bpm(MAX_PLAUSIBLE_BPM) == MAX_PLAUSIBLE_BPM
    assert _plausible_bpm(128.0) == 128.0

    assert _plausible_bpm(None) is None
    assert _plausible_bpm(0.0) is None
    assert _plausible_bpm(MIN_PLAUSIBLE_BPM - 0.1) is None
    assert _plausible_bpm(MAX_PLAUSIBLE_BPM + 0.1) is None
    assert _plausible_bpm(_SILENT_WINDOW_BPM) is None


def test_plausible_bpm_rejects_non_finite_values() -> None:
    """A ``NaN`` reaching ``statistics.fmean`` poisons the whole FILE's reference distribution, not
    one window's -- the band's comparisons reject it (and ``inf``) without a separate check."""
    assert _plausible_bpm(math.nan) is None
    assert _plausible_bpm(math.inf) is None
    assert _plausible_bpm(-math.inf) is None


def test_the_band_is_what_keeps_one_junk_value_from_swamping_the_reference() -> None:
    """The magnitude the gate prevents, stated against ``_bpm_stats`` itself.

    Not a re-implementation of the writer: it reads the same helper the writer reads, once over the
    population the band admits and once over the population it rejects, and pins the two-orders-of-
    magnitude gap between them. That gap is the whole defect -- an inflated stdev divides every
    real window's deviation down to nothing.
    """
    musical = [_musical_bpm(index) for index in range(_MUSICAL_FINE_COUNT)]

    gated = _bpm_stats(musical)
    ungated = _bpm_stats([*musical, _SILENT_WINDOW_BPM])

    assert gated is not None
    assert ungated is not None
    assert gated[1] == pytest.approx(0.7957, abs=1e-4)
    assert ungated[1] == pytest.approx(77.5069, abs=1e-4)


def test_one_silent_fine_window_leaves_every_coarse_energy_undisturbed() -> None:
    """THE acceptance case (phaze-aswsz): one silent window (bpm 738.3) among sixty ~128 BPM
    windows leaves the coarse energies within ``_ENERGY_TOLERANCE`` of the same set without it.

    Runs the REAL writer (``annotate_window_orm_objects`` -> ``compute_window_projection`` ->
    ``_bpm_stats`` / ``_bpm_z_for_range`` / ``set_projection.energy``) over real ``AnalysisWindow``
    instances carrying real coarse ``features``. Nothing about the z-score is mocked or stubbed
    (`docs/design/0012-verification-fidelity-and-operator-attribution.md` rule 3): a test that
    patched the z-score would prove the patch, not the gate.

    Measured against the pre-fix writer (band removed, everything else identical): the file's
    population stdev goes from 0.7957 to 77.5069, the eleven coarse windows' z-scores collapse from
    a real -0.201..+0.553 spread onto a near-uniform -0.13, and the one window the silent window
    overlaps is pushed the other way, +0.553 to +1.188. At the 0.10 `bpm_z` weight that is a
    maximum energy shift of 0.0635, seven orders of magnitude past the tolerance below. Removing
    either half of the gate (the reference or the overlap) also fails this test.
    """
    control = _window_set(include_silent=False)
    treatment = _window_set(include_silent=True)

    annotate_window_orm_objects(control)
    annotate_window_orm_objects(treatment)

    control_energies = _coarse_energies(control)
    treatment_energies = _coarse_energies(treatment)

    # Vacuity guards: the comparison is only meaningful if the control actually exercised the
    # `bpm_z` term (a usable reference distribution) and produced a varying, unclamped arc.
    assert _bpm_stats([w.bpm for w in control if w.tier == "fine"]) is not None
    assert len(control_energies) == 11
    assert all(energy is not None for energy in control_energies)
    assert min(control_energies) > 0.0
    assert max(control_energies) < 1.0
    assert len(set(control_energies)) > 1

    deltas = [abs(a - b) for a, b in zip(control_energies, treatment_energies, strict=True)]
    assert max(deltas) <= _ENERGY_TOLERANCE, f"one silent fine window moved a coarse energy by {max(deltas)} (tolerance {_ENERGY_TOLERANCE})"


def test_the_band_gates_the_coarse_overlap_set_and_not_only_the_reference() -> None:
    """The half a reference-only gate would miss.

    The junk window is the ONLY fine window overlapping the second coarse window. Gating just the
    file-wide reference distribution would keep 738.3 out of the mean and stdev while still letting
    ``_bpm_z_for_range`` average it into THAT window's own overlap -- z would read (738.3 - 128) / 8
    = about +76 there, and the energy would clamp to 1.0. Gated on both sides, that coarse window
    has no usable overlapping fine window at all, which is the documented ``z = 0.0`` case.

    The first coarse window is the control in the same set: it overlaps one real fine window at 120
    BPM against a file mean of 128 and stdev of 8, so it must still carry a genuine ``z = -1.0``.
    Without it the test would pass just as well against a gate that flattened the whole file.
    """
    features = _real_coarse_features()[0]
    windows = [
        {"tier": "fine", "start_sec": 0.0, "end_sec": 30.0, "bpm": 120.0, "musical_key": None, "features": None},
        {"tier": "fine", "start_sec": 180.0, "end_sec": 210.0, "bpm": _SILENT_WINDOW_BPM, "musical_key": None, "features": None},
        {"tier": "fine", "start_sec": 360.0, "end_sec": 390.0, "bpm": 136.0, "musical_key": None, "features": None},
        {"tier": "coarse", "start_sec": 0.0, "end_sec": 180.0, "bpm": None, "musical_key": None, "features": features},
        {"tier": "coarse", "start_sec": 180.0, "end_sec": 360.0, "bpm": None, "musical_key": None, "features": features},
    ]

    computed = compute_window_projection(windows)

    scores = dict(zip(MOOD_ORDER, positive_class_vector(features), strict=True))
    assert computed[3]["energy"] == pytest.approx(energy_scalar(scores, -1.0))
    assert computed[4]["energy"] == pytest.approx(energy_scalar(scores, 0.0))


def test_a_file_whose_only_fine_bpms_are_out_of_band_reports_no_usable_reference() -> None:
    """``logged_sources`` reads from the SAME banded population the z-score does.

    Before the band it read ``"fine"`` here -- claiming the file's energies were informed by its
    own tempo when every value feeding that claim was silence. Two junk windows, so the "fewer than
    two values" branch of ``_bpm_stats`` is not what produces the verdict.
    """
    windows = [
        {"tier": "fine", "start_sec": 0.0, "end_sec": 30.0, "bpm": _SILENT_WINDOW_BPM, "musical_key": None, "features": None},
        {"tier": "fine", "start_sec": 30.0, "end_sec": 60.0, "bpm": 700.0, "musical_key": None, "features": None},
    ]

    assert logged_sources(windows) == {"bpm": "none"}
