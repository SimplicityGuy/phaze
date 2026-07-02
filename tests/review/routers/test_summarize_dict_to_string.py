"""Parametrized unit tests for `phaze.routers.agent_analysis._summarize_dict_to_string` (Phase 26 W6).

The helper converts ``dict[str, float]`` to a ``"k=v,k=v,k=v"`` summary string,
top-3 keys by score (descending), with a hard 50-char cap to fit the existing
``AnalysisResult.mood``/``style`` ``String(50)`` columns.

Tiebreak semantics: when scores are identical, the helper sorts by key
ascending (alphabetical) for determinism across Python implementations -- the
``identical_scores_alphabetical_tiebreak`` case below pins that contract.
"""

from __future__ import annotations

import pytest

from phaze.routers.agent_analysis import _summarize_dict_to_string


@pytest.mark.parametrize(
    "input_dict, expected",
    [
        # empty dict -> empty string
        ({}, ""),
        # single-key dict -> one entry, no comma
        ({"happy": 0.85}, "happy=0.85"),
        # 3-key dict -> top-3 sorted by score descending
        (
            {"happy": 0.7, "energetic": 0.8, "calm": 0.3},
            "energetic=0.80,happy=0.70,calm=0.30",
        ),
        # 10-key dict -> top-3 only (m10=1.00, m9=0.90, m8=0.80)
        (
            {f"m{i}": 0.1 * i for i in range(1, 11)},
            "m10=1.00,m9=0.90,m8=0.80",
        ),
        # identical scores -> alphabetical tiebreak (ascending)
        (
            {"zeta": 0.5, "alpha": 0.5, "mu": 0.5},
            "alpha=0.50,mu=0.50,zeta=0.50",
        ),
    ],
)
def test_summarize_dict_to_string(input_dict: dict[str, float], expected: str) -> None:
    """Verify summary string output for documented edge cases + 50-char invariant."""
    result = _summarize_dict_to_string(input_dict)
    assert result == expected
    assert len(result) <= 50  # W6 hard cap invariant


def test_summarize_dict_to_string_length_cap() -> None:
    """50-char cap fires when 3 top-keys would overflow."""
    long_keys = {
        "first_classifier_label": 0.99,
        "second_classifier_label": 0.88,
        "third_classifier_label": 0.77,
    }
    result = _summarize_dict_to_string(long_keys)
    assert len(result) <= 50, f"output {result!r} exceeded 50-char cap"
