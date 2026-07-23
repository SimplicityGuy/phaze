"""DB-free unit tests for `repair_mojibake` (phaze-x4ux).

Bucket: ``shared``. Stdlib-only module, no Postgres needed. The known production example ("Sven
VÃƒÂ¤th" -> "Sven Väth", lux, 2026-07-18) is exercised verbatim, alongside a battery of negatives:
IDEMPOTENCY AND THE NO-OP-ON-LEGITIMATE-TEXT CASE ARE TESTED AT LEAST AS HARD AS THE REPAIR CASE
per the bead's own instruction -- a fix that mangles already-correct names is strictly worse than
the bug it repairs.
"""

from __future__ import annotations

import pytest

from phaze.services.text_repair import repair_mojibake


# ---------------------------------------------------------------------------
# The known production case (phaze-x4ux), verbatim.
# ---------------------------------------------------------------------------


def test_repairs_the_known_production_case_double_encoded() -> None:
    """'Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - LIVE @ Timewarp 2003.mp3' (lux, 2026-07-18)."""
    assert repair_mojibake("Sven VÃƒÂ¤th") == "Sven Väth"


def test_repairs_the_full_known_production_filename() -> None:
    garbled = "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven VÃƒÂ¤th - LIVE @ Timewarp 2003.mp3"
    expected = "Carl Cox, Umek, Dj Rush, Chris Liebing, Sven Väth - LIVE @ Timewarp 2003.mp3"
    assert repair_mojibake(garbled) == expected


def test_repairs_single_encoded_mojibake_in_one_effective_pass() -> None:
    """A single (not double) mis-decode also repairs -- the loop must not require exactly 2 passes."""
    assert repair_mojibake("Sven VÃ¤th") == "Sven Väth"


# ---------------------------------------------------------------------------
# Idempotency: repair_mojibake(repair_mojibake(x)) == repair_mojibake(x).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Sven VÃƒÂ¤th",
        "Sven VÃ¤th",
        "Sven Väth",
        "Björk",
        "Sigur Rós",
        "Hello World",
        "",
        "façade",
    ],
)
def test_idempotent(value: str) -> None:
    once = repair_mojibake(value)
    twice = repair_mojibake(once)
    assert once == twice


# ---------------------------------------------------------------------------
# Negative: already-correct accented Unicode text must NOT be touched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "clean",
    [
        "Sven Väth",
        "Björk",
        "Sigur Rós",
        "São Paulo",
        "Straße",
        "Königsberg",
        "façade",
        "naïve café",
        "Émilie",
        "Håkan",
        "Zürich",
        "Møller",
        "Kraków",
        "Île-de-France",
        "Škoda",
    ],
)
def test_no_op_on_legitimate_accented_text(clean: str) -> None:
    """A correctly-encoded accented name must survive completely unchanged -- see module docstring."""
    assert repair_mojibake(clean) == clean


def test_no_op_on_pure_ascii() -> None:
    for value in ("Hello World", "Carl Cox - Live @ Timewarp 2003.mp3", "", "123-456_789.flac"):
        assert repair_mojibake(value) == value


def test_no_op_on_legitimate_a_with_tilde_in_context() -> None:
    """A literal 'Ã' appearing legitimately (not as a mojibake artifact) must not be "repaired"."""
    legit = "MAÇÃ smoothie"  # Portuguese "maçã" (apple), capitalized
    assert repair_mojibake(legit) == legit


# ---------------------------------------------------------------------------
# Never raises, even on input that cannot be repaired.
# ---------------------------------------------------------------------------


def test_never_raises_on_arbitrary_unicode() -> None:
    tricky = "emoji \U0001f3b5 CJK 中文 astral \U0001d49c mixed"
    assert repair_mojibake(tricky) == tricky


def test_never_raises_on_empty_string() -> None:
    assert repair_mojibake("") == ""


def test_never_raises_on_lone_surrogate() -> None:
    """A lone surrogate cannot even be UTF-8 encoded; repair_mojibake must return it unchanged, not raise."""
    assert repair_mojibake("bad\ud800frame") == "bad\ud800frame"


# ---------------------------------------------------------------------------
# General double-encoding shape (not hardcoded to the one production string).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("garbled", "expected"),
    [
        ("MoÃ«t", "Moët"),
        ("naÃ¯ve", "naïve"),
        ("cafÃ©", "café"),
        ("BjÃ¶rk", "Björk"),
        ("SÃ£o Paulo", "São Paulo"),
    ],
)
def test_repairs_single_encoded_general_shapes(garbled: str, expected: str) -> None:
    assert repair_mojibake(garbled) == expected


@pytest.mark.parametrize(
    ("double_garbled", "expected"),
    [
        ("MoÃƒÂ«t", "Moët"),
        ("BjÃƒÂ¶rk", "Björk"),
        ("cafÃƒÂ©", "café"),
    ],
)
def test_repairs_double_encoded_general_shapes(double_garbled: str, expected: str) -> None:
    assert repair_mojibake(double_garbled) == expected
