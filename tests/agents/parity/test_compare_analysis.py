"""Unit tests for ``scripts.parity.compare_analysis.compare`` (Phase 47 / CLOUDIMG-03).

The comparator encodes the arm64↔x86 numeric-parity contract: ``bpm`` and
``musical_key`` (plus the dominant ``mood``/``style`` labels) must match EXACTLY;
``danceability`` and every model score under ``features`` must agree within an
absolute tolerance ``atol`` (``math.isclose(abs_tol=atol)``).

These tests are pure dict-in / list-of-strings-out — NO essentia, NO models, NO
audio. The input dicts mirror the REAL ``phaze.services.analysis.analyze_file``
return schema (``bpm``/``musical_key``/``mood``/``style``/``danceability``/
``features`` plus the variable ``windows``/count keys the comparator must ignore),
NOT the RESEARCH example's non-existent ``model_scores`` key.

A None-vs-number, or a model-score key present on one side but missing on the
other, is a FAILURE — never a silent pass and never a ``KeyError`` (T-47-07).
"""

from __future__ import annotations

import copy
from typing import Any

from scripts.parity.compare_analysis import compare


def _golden() -> dict[str, Any]:
    """A representative ``analyze_file`` output dict (the parity golden side)."""
    return {
        "bpm": 120.0,
        "musical_key": "C major",
        "mood": "happy",
        "style": "rock",
        "danceability": 0.5,
        "features": {
            "mood_happy": {
                "musicnn_msd": [
                    {"label": "happy", "prediction": 0.80},
                    {"label": "non_happy", "prediction": 0.20},
                ],
                "vggish": [
                    {"label": "happy", "prediction": 0.75},
                    {"label": "non_happy", "prediction": 0.25},
                ],
            },
            "genre": {
                "predictions": [
                    {"label": "rock", "confidence": 0.90},
                    {"label": "pop", "confidence": 0.10},
                ],
            },
        },
        # Variable run-to-run keys the comparator MUST ignore for parity.
        "windows": [{"idx": 0, "bpm": 119.0}],
        "fine_windows_analyzed": 3,
        "fine_windows_total": 5,
        "coarse_windows_analyzed": 2,
        "coarse_windows_total": 7,
        "sampled": True,
    }


def test_identical_inputs_pass() -> None:
    """compare(g, g) == [] — identical inputs mean parity holds."""
    g = _golden()
    assert compare(g, copy.deepcopy(g)) == []


def test_variable_count_keys_are_ignored() -> None:
    """Differing windows / *_analyzed / *_total / sampled keys never break parity."""
    g = _golden()
    a = copy.deepcopy(g)
    a["windows"] = [{"idx": 9, "bpm": 200.0}, {"idx": 10}]
    a["fine_windows_analyzed"] = 999
    a["coarse_windows_total"] = 1
    a["sampled"] = False
    assert compare(g, a) == []


def test_bpm_mismatch_fails() -> None:
    """A bpm difference (exact match) returns a failure naming 'bpm'."""
    g = _golden()
    a = copy.deepcopy(g)
    a["bpm"] = 120.1
    fails = compare(g, a)
    assert any("bpm" in f for f in fails)


def test_musical_key_mismatch_fails() -> None:
    """A musical_key string difference returns a failure naming 'key'."""
    g = _golden()
    a = copy.deepcopy(g)
    a["musical_key"] = "A minor"
    fails = compare(g, a)
    assert any("key" in f for f in fails)


def test_mood_label_mismatch_fails() -> None:
    """A dominant mood-label difference fails (exact label parity)."""
    g = _golden()
    a = copy.deepcopy(g)
    a["mood"] = "sad"
    fails = compare(g, a)
    assert any("mood" in f for f in fails)


def test_style_label_mismatch_fails() -> None:
    """A dominant style-label difference fails (exact label parity)."""
    g = _golden()
    a = copy.deepcopy(g)
    a["style"] = "pop"
    fails = compare(g, a)
    assert any("style" in f for f in fails)


def test_model_score_within_epsilon_passes() -> None:
    """A model score within atol (0.80 vs 0.80005, atol=1e-3) is not a failure."""
    g = _golden()
    a = copy.deepcopy(g)
    a["features"]["mood_happy"]["musicnn_msd"][0]["prediction"] = 0.80005
    assert compare(g, a, atol=1e-3) == []


def test_model_score_outside_epsilon_fails() -> None:
    """A model score outside atol (0.80 vs 0.83, atol=1e-3) fails, naming the score."""
    g = _golden()
    a = copy.deepcopy(g)
    a["features"]["mood_happy"]["musicnn_msd"][0]["prediction"] = 0.83
    fails = compare(g, a, atol=1e-3)
    assert any("mood_happy" in f for f in fails)


def test_danceability_within_epsilon_passes() -> None:
    """danceability is compared with epsilon, not exact."""
    g = _golden()
    a = copy.deepcopy(g)
    a["danceability"] = 0.50005
    assert compare(g, a, atol=1e-3) == []


def test_danceability_outside_epsilon_fails() -> None:
    """A danceability difference beyond atol fails, naming 'danceability'."""
    g = _golden()
    a = copy.deepcopy(g)
    a["danceability"] = 0.7
    fails = compare(g, a, atol=1e-3)
    assert any("danceability" in f for f in fails)


def test_none_vs_number_fails() -> None:
    """A None on one side where the other is a number is a FAILURE (no silent pass)."""
    g = _golden()
    a = copy.deepcopy(g)
    a["danceability"] = None
    fails = compare(g, a)
    assert any("danceability" in f for f in fails)


def test_missing_model_score_key_fails_not_raises() -> None:
    """A model-score key present in golden but absent in actual fails (never KeyErrors)."""
    g = _golden()
    a = copy.deepcopy(g)
    del a["features"]["mood_happy"]["vggish"]
    fails = compare(g, a)
    assert any("vggish" in f for f in fails)


def test_extra_model_score_key_fails() -> None:
    """A model-score key present in actual but absent in golden fails (no silent pass)."""
    g = _golden()
    a = copy.deepcopy(g)
    a["features"]["mood_happy"]["effnet"] = [{"label": "x", "prediction": 0.42}]
    fails = compare(g, a)
    assert any("effnet" in f for f in fails)


def test_missing_top_level_field_fails_not_raises() -> None:
    """A missing top-level field (e.g. musical_key) surfaces as a failure, not a KeyError."""
    g = _golden()
    a = copy.deepcopy(g)
    del a["musical_key"]
    fails = compare(g, a)
    assert any("key" in f for f in fails)
