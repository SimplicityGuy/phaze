"""Numeric-parity comparator for the arm64 essentia agent image (Phase 47 / CLOUDIMG-03).

This is the gate the arm64 build hinges on: it compares two
``phaze.services.analysis.analyze_file`` output dicts — an x86 *golden* and an
arm64 *actual* — and returns a list of human-readable failure strings (``[]`` ==
parity OK). Plan 47-04's CI parity job shells out to the ``__main__`` CLI and
fails the build on any non-zero exit.

Tolerance contract:
  * ``bpm`` and ``musical_key`` — EXACT match (bpm is already rounded to 0.1 by
    ``analyze_file``; key is a string).
  * ``mood`` / ``style`` dominant labels — EXACT string match.
  * ``danceability`` and every numeric model score under ``features`` —
    ``math.isclose(abs_tol=atol)``. ``atol`` defaults to a conservative ``1e-4``
    placeholder; plan 47-04 tunes it empirically from observed x86↔arm64 deltas.

Anti-silent-pass (T-47-07): a ``None`` on one side where the other is a number,
or a model-score key present on one side but missing on the other, is a FAILURE
— never a silent pass and never a ``KeyError``. A parity gate that passes on
absent data is worse than no gate.

This module keys STRICTLY on ``analyze_file``'s REAL return dict
(``bpm``/``musical_key``/``mood``/``style``/``danceability``/``features`` —
src/phaze/services/analysis.py:575-588). It deliberately does NOT use the
RESEARCH example's ``model_scores`` key, which does not exist in the real output
and would ``KeyError``. Model scores are reached THROUGH ``features`` via a
recursive numeric flatten, so a renamed/missing score becomes a failure string
rather than an exception. The run-to-run variable keys (``windows`` and the
``*_analyzed``/``*_total``/``sampled`` coverage counts) are ignored.

CLI:
    compare_analysis.py <golden.json> <actual.json> [--atol X]

Exits non-zero (1) if any parity failure is found, 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


# Conservative default tolerance; plan 47-04 tunes this from real x86↔arm64 deltas.
_DEFAULT_ATOL = 1e-4

# Scalar fields compared for EXACT equality (bpm is pre-rounded; the rest are labels/strings).
_EXACT_FIELDS = ("bpm", "musical_key", "mood", "style")


def _flatten_scores(node: Any, prefix: str = "") -> dict[str, float]:
    """Recursively flatten every numeric leaf under ``node`` to a ``path -> float`` map.

    ``features`` is a nested dict of model sets → variants → ``[{label, prediction}]``
    lists (plus ``genre`` → ``{predictions: [{label, confidence}]}``). Walking it
    generically — dict keys and list indices build the path — yields one stable
    key per numeric score (e.g. ``mood_happy.musicnn_msd[0].prediction``) without
    hard-coding the schema, so a renamed/added/removed score surfaces as a key-set
    difference for the caller to flag. String labels are dropped (ordering is
    deterministic on both sides, so index-based alignment is sufficient); ``bool``
    is skipped (it is an ``int`` subclass but never a model score).
    """
    out: dict[str, float] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten_scores(value, child))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            out.update(_flatten_scores(value, f"{prefix}[{idx}]"))
    elif isinstance(node, bool):
        pass
    elif isinstance(node, (int, float)):
        out[prefix] = float(node)
    return out


def _compare_score(name: str, golden_value: float | None, actual_value: float | None, atol: float, fails: list[str]) -> None:
    """Append an epsilon-comparison failure for ``name`` to ``fails`` if the two values disagree.

    Both ``None`` → parity (no failure). Exactly one ``None`` → failure (no silent
    pass on absent data). Otherwise ``math.isclose(abs_tol=atol)``.
    """
    if golden_value is None and actual_value is None:
        return
    if golden_value is None or actual_value is None:
        fails.append(f"{name}: {golden_value} vs {actual_value} (one side missing/None)")
        return
    if not math.isclose(golden_value, actual_value, abs_tol=atol):
        fails.append(f"score {name}: {golden_value} vs {actual_value} (atol={atol})")


def compare(golden: dict[str, Any], actual: dict[str, Any], *, atol: float = _DEFAULT_ATOL) -> list[str]:
    """Compare two ``analyze_file`` output dicts; return ``[]`` iff numeric parity holds.

    ``bpm``/``musical_key``/``mood``/``style`` are compared EXACTLY; ``danceability``
    and every numeric leaf under ``features`` are compared with ``math.isclose(abs_tol=atol)``.
    Uses ``.get(...)`` throughout so a missing key becomes a failure string, never a
    ``KeyError`` (T-47-07). The variable ``windows``/``*_analyzed``/``*_total``/
    ``sampled`` keys are not consulted and are therefore ignored.
    """
    fails: list[str] = []

    for field in _EXACT_FIELDS:
        golden_value = golden.get(field)
        actual_value = actual.get(field)
        if golden_value != actual_value:
            fails.append(f"{field}: {golden_value!r} != {actual_value!r}")

    _compare_score("danceability", golden.get("danceability"), actual.get("danceability"), atol, fails)

    golden_scores = _flatten_scores(golden.get("features", {}))
    actual_scores = _flatten_scores(actual.get("features", {}))
    for name in sorted(set(golden_scores) | set(actual_scores)):
        _compare_score(f"features.{name}", golden_scores.get(name), actual_scores.get(name), atol, fails)

    return fails


def _load(path: str) -> dict[str, Any]:
    """Load a JSON object from ``path``; raise ``SystemExit`` if it is not a dict."""
    with Path(path).open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        msg = f"{path}: expected a JSON object, got {type(data).__name__}"
        raise SystemExit(msg)
    return data


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: compare two JSON dumps and return a process exit code."""
    parser = argparse.ArgumentParser(description="Compare two analyze_file JSON dumps for arm64↔x86 numeric parity.")
    parser.add_argument("golden", help="path to the golden (x86) analyze_file JSON")
    parser.add_argument("actual", help="path to the actual (arm64) analyze_file JSON")
    parser.add_argument("--atol", type=float, default=_DEFAULT_ATOL, help=f"absolute tolerance for model scores (default {_DEFAULT_ATOL})")
    args = parser.parse_args(argv)

    fails = compare(_load(args.golden), _load(args.actual), atol=args.atol)
    if fails:
        print(f"PARITY FAIL ({len(fails)} mismatch(es), atol={args.atol}):")
        for line in fails:
            print(f"  - {line}")
        return 1
    print(f"PARITY OK (atol={args.atol})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
