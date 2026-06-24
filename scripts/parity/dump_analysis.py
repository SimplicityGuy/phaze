"""Shared analysis-dump CLI for arm64↔x86 numeric parity (Phase 47 / CLOUDIMG-03).

Runs the REAL ``phaze.services.analysis.analyze_file`` over one audio file + a
models directory and emits the parity-comparable JSON. This SAME tool runs inside
both the x86 image (producing the *golden*) and the arm64 image (producing the
*actual*) in plan 47-04's CI parity job, so both sides share a byte-for-byte
identical schema — there is exactly one place the dump shape is defined.

Projection: the emitted JSON keeps ONLY the parity-relevant keys from the
``analyze_file`` return dict (src/phaze/services/analysis.py:575-588):

    bpm, musical_key, mood, style, danceability, features

The run-to-run variable keys (``windows`` and the ``fine_windows_analyzed`` /
``fine_windows_total`` / ``coarse_windows_analyzed`` / ``coarse_windows_total`` /
``sampled`` coverage counts) are DROPPED — they are strided/count artifacts, not
parity signal. ``features`` retains its real nested model-score structure; the
companion ``compare_analysis.compare`` flattens it for the epsilon comparison
(the comparator is the single flatten authority, and its unit tests exercise this
exact nested shape).

faulthandler is enabled so a native essentia/TensorFlow segfault dumps a C
stack instead of dying silently — the same defensive pattern as the arm64 spike
smoke test.

CLI:
    dump_analysis.py <audio_path> <models_dir> [--out PATH]

Writes the JSON to ``--out`` (default: stdout).
"""

from __future__ import annotations

import argparse
import faulthandler
import json
from pathlib import Path
from typing import Any


# The parity-relevant scalar keys copied verbatim from the analyze_file return dict.
_PARITY_SCALAR_KEYS = ("bpm", "musical_key", "mood", "style", "danceability")


def project(result: dict[str, Any]) -> dict[str, Any]:
    """Project a full ``analyze_file`` result down to the parity key set.

    Keeps ``bpm``/``musical_key``/``mood``/``style``/``danceability`` plus the
    nested ``features`` model-score map; drops the variable ``windows`` and
    coverage-count keys. Uses ``.get(...)`` so a model whose schema changed still
    serializes (the comparator then flags it) rather than raising.
    """
    projected: dict[str, Any] = {key: result.get(key) for key in _PARITY_SCALAR_KEYS}
    projected["features"] = result.get("features", {})
    return projected


def dump(audio_path: str, models_dir: str) -> dict[str, Any]:
    """Run ``analyze_file`` over ``audio_path`` with ``models_dir`` and project the result."""
    from phaze.services.analysis import analyze_file  # noqa: PLC0415  # defer heavy essentia import past --help/arg-parse

    return project(analyze_file(audio_path, models_dir))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: analyze one file and emit the parity-comparable JSON."""
    parser = argparse.ArgumentParser(description="Run analyze_file over one audio file and emit the parity-comparable JSON.")
    parser.add_argument("audio_path", help="path to the audio file to analyze")
    parser.add_argument("models_dir", help="directory holding the essentia .pb/.json model weights")
    parser.add_argument("--out", default=None, help="write JSON here (default: stdout)")
    args = parser.parse_args(argv)

    faulthandler.enable()  # dump the native C stack if essentia/TF segfaults

    payload = json.dumps(dump(args.audio_path, args.models_dir), indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
