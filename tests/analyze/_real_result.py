"""The REAL ``analyze_file`` artifact that the A3 and A7 seam tests are verified against.

**Why this file exists (phaze-qiwdk, seam inventory rows A3 and A7).** Every test that
carried an ``analyze_file`` result carried :func:`tests.analyze._child_stubs._result` — a
hand-built dict of plain Python floats with one fine window and a one-entry ``features``
dict. That stub is a PROXY for what essentia actually produces, and a proxy of exactly the
shape that cannot exhibit the failures the real thing could: a numpy scalar leaf (``_emit``
is ``json.dumps`` with no ``default=``), a non-finite float (``AnalysisWindowPayload``
rejects one anywhere inside ``features``), a ``musical_key`` past ``max_length=10``, or a
line large enough to matter to a 64 KiB pipe. Agreement between two hand-written fixtures
proves only that the two agree with each other — ADR-0012 rule 3.

``real_analyze_file_result.json`` is therefore not written by hand. It is the verbatim
return value of :func:`phaze.services.analysis.analyze_file` executed with the REAL
``essentia-tensorflow`` wheel and the REAL 68-file model set over a REAL archive track.

PROVENANCE (phaze-qiwdk, 2026-08-22, macOS arm64, essentia 2.1-beta6-dev, the pinned
model manifest from ``phaze.scripts.download_models``):

    source                <track-05>, 546.0 s, an archive mp3
    analysis wall clock   330.7 s (0.606x the file's own duration)
    windows               22 = 18 fine + 4 coarse — every natural window, no cap (D-07)
    result JSON           29 246 bytes compact; 115.0 B per fine window, 5 417.2 per coarse
    leaf types            builtins.float / builtins.str / builtins.int ONLY — no numpy scalar
    non-finite leaves     0 (no NaN, no Infinity)
    NUL / lone surrogate  0
    longest musical_key   8 chars ("F# minor") against the wire's max_length=10
    longest style         29 chars ("Electronic/Progressive Breaks") against max_length=50
    longest mood          10 chars ("electronic") against max_length=50
    danceability range    0.6214 .. 0.9895, inside the wire's 0..1
    bpm range             85.5 .. 172.3

Those figures are this one track. The same walk over FIVE real archive tracks (201.4 s to
546.0 s, four artists, rock / pop / electronic) agrees on every categorical claim: 3 031 leaves
across the five, all ``builtins.float`` / ``builtins.str`` / ``builtins.int``, zero non-finite,
zero NUL. Per-window serialized size is near-constant because the coarse feature dict has fixed
shape — 11 model sets x 3 variants x 2 classes plus the genre head's top 10 — at 114.0-115.0 B
per fine window and 5 351.0-5 417.2 per coarse across all five.

The archive identity is scrubbed to ``<track-05>`` per CLAUDE.md's no-local-identifiers
rule; every measured quantity above is exact and unscrubbed. The artifact itself carries no
filename, path or digest — ``analyze_file`` returns none — so it is committable as-is.

REFRESHING IT is a deliberate act, not routine maintenance: re-run ``analyze_file`` against
a real track with a real model set and replace the file wholesale. Do NOT hand-edit it to
make a test pass — a hand-edit turns it back into the proxy it exists to replace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Iterator


_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real_analyze_file_result.json"


def real_analysis_result() -> dict[str, Any]:
    """The real ``analyze_file`` dict, freshly decoded so a caller may mutate its copy."""
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def iter_leaves(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(dotted_path, leaf)`` for every scalar in a JSON-shaped value.

    List indices collapse to ``[]`` so a path names a POSITION IN THE SHAPE rather than one
    element — a failure report then reads ``windows[].features.genre.predictions[].confidence``
    instead of a coordinate nobody can act on. Dict KEYS are yielded too (as their own leaf),
    because a NUL in a key survives ``_summarize_dict_to_string`` into a ``String(50)`` column
    while a value-only walk never sees it (the hazard ``services/pg_text.py`` documents).
    """
    if isinstance(value, dict):
        for key, item in value.items():
            yield (f"{path}<key>", key)
            yield from iter_leaves(item, f"{path}.{key}" if path else str(key))
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_leaves(item, f"{path}[]")
        return
    yield (path, value)
