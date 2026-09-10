"""The REAL ``RhythmExtractor2013`` against digital silence -- the premise
``services/set_projection_writer.MIN_PLAUSIBLE_BPM`` is built on (phaze-aswsz).

WHY THIS FILE EXISTS. The band on the BPM z-score's reference population rests on two claims about
the deployed extractor: that silence produces a value far outside any musical tempo, and that the
band never rejects a tempo the extractor was configured to search for. Both are claims about REAL
essentia, and `docs/design/0012-verification-fidelity-and-operator-attribution.md` rule 3 says a
claim about real essentia is not discharged by a mocked one. The whole ``tests/shared`` half of
this bead takes 738.3 as a literal, which proves nothing about the extractor. This module is where
the literal is earned.

MEASURED (2026-09-09, macOS arm64, essentia 2.1-beta6-dev, ``method="multifeature"``, 44.1 kHz
float32 zeros): bpm 738.3 at confidence 0.0 over a 5 s buffer and 4.69 over a 30 s one. The 30 s
buffer is the deployed fine-window length (``analysis._DEFAULT_FINE_WINDOW_SEC``), and its
confidence of 4.69 is why the persisted number cannot be distinguished from a tempo downstream --
it is not a zero-confidence reading, and the confidence is dropped before persistence anyway.

WHAT IS AND IS NOT ASSERTED. The assertions pin the PROPERTIES the band depends on -- out-of-band,
and above the ceiling rather than below the floor -- not the exact float 738.3, which is an
artifact of one essentia build and would make this a version pin rather than a behaviour test. The
exact value is recorded above as a measurement instead.
"""

from __future__ import annotations

import numpy as np

from phaze.services.set_projection_writer import MAX_PLAUSIBLE_BPM, MIN_PLAUSIBLE_BPM


_SAMPLE_RATE = 44100
_FINE_WINDOW_SEC = 30.0  # `analysis._DEFAULT_FINE_WINDOW_SEC`, the buffer the fine tier really hands over


def _silence(seconds: float) -> np.ndarray:
    """A float32 buffer of digital silence, the shape ``_analyze_one_fine_window`` passes along."""
    return np.zeros(int(_SAMPLE_RATE * seconds), dtype=np.float32)


def test_real_rhythm_extractor_returns_an_out_of_band_bpm_for_digital_silence() -> None:
    """Silence at the deployed fine-window length lands outside the plausible-tempo band.

    This is the exact value ``services/analysis.py`` stores -- ``round(float(bpm), 1)``, with the
    confidence discarded -- so if this assertion ever fails, the band is no longer the thing
    keeping junk out of the projection's reference distribution and the gate needs re-deriving.
    """
    import essentia.standard as es

    bpm, _beats, confidence, _estimates, _intervals = es.RhythmExtractor2013(method="multifeature")(_silence(_FINE_WINDOW_SEC))
    stored = round(float(bpm), 1)

    assert stored > MAX_PLAUSIBLE_BPM, f"silence returned {stored} BPM, inside the band {MIN_PLAUSIBLE_BPM}..{MAX_PLAUSIBLE_BPM}"
    # The reading is not self-identifying as junk: a nonzero confidence is why a downstream reader
    # gating on "confidence != 0" would have kept this window too.
    assert float(confidence) > 0.0


def test_the_plausible_band_contains_the_extractors_own_search_range() -> None:
    """The band must never reject a tempo the deployed extractor was configured to look for.

    Excluding a REAL window distorts the same file-local reference distribution the band exists to
    protect, so this is the direction the gate must fail open in. Asserting containment rather than
    equality is deliberate: essentia is free to widen or narrow its own defaults, and only a change
    that pushed the search range OUTSIDE the band would actually invalidate the constants.
    """
    import essentia.standard as es

    extractor = es.RhythmExtractor2013(method="multifeature")

    assert float(extractor.paramValue("minTempo")) >= MIN_PLAUSIBLE_BPM
    assert float(extractor.paramValue("maxTempo")) <= MAX_PLAUSIBLE_BPM
