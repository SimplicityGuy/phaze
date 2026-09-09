"""Guard: `docs/design/0018-set-projection-and-file-viewer.md`'s weights table and
``services.set_projection.ENERGY_WEIGHTS`` cannot drift apart.

phaze-x1qr3.4. The ADR names the initial energy weights explicitly and labels them the
implementer's decision pending the operator blind check in `phaze-x1qr3.12` -- see the ADR's
§4. That labelling is only trustworthy if the table it labels is the table the code actually
uses; a hand-transcribed number in prose has no mechanism keeping it in sync with a constant
that is free to change in a later bead (including `phaze-x1qr3.12` itself, if the blind check
changes a value). This test reads both sources and fails on drift in either the NAME set or
the VALUES -- never by re-deriving one from the other, since a self-referential check proves
nothing about whether the two independently-authored artifacts still agree.

WHAT THIS DOES NOT COVER. The ADR's prose framing (that these are the implementer's own choice,
not yet weighed in on by the operator) is not machine-checked here -- that is a human-review
concern for `docs/design/0018-set-projection-and-file-viewer.md` itself, not something a
table-diff can verify. This test only pins the numbers.
"""

from __future__ import annotations

from pathlib import Path
import re

from phaze.services.set_projection import ENERGY_WEIGHTS


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADR_PATH = _REPO_ROOT / "docs" / "design" / "0018-set-projection-and-file-viewer.md"

# Matches one Markdown table data row under the "## 4." weights table, e.g.:
#   | `danceability` | `0.30` | raises energy |
#   | `bpm_z` | `0.10` | a faster-than-the-file's-own-average window raises energy |
# Deliberately tolerant of a leading `-` sign inside the backticks (`` `-0.20` ``) and of
# arbitrary trailing prose in the third column, which is not part of what this test pins.
_WEIGHT_ROW_RE = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*`(-?\d+(?:\.\d+)?)`\s*\|")


def _adr_weights() -> dict[str, float]:
    """Parse the §4 weights table out of the ADR's own Markdown, by name -> float value."""
    text = _ADR_PATH.read_text(encoding="utf-8")
    weights: dict[str, float] = {}
    for line in text.splitlines():
        match = _WEIGHT_ROW_RE.match(line)
        if match:
            weights[match.group(1)] = float(match.group(2))
    return weights


def test_adr_0018_exists() -> None:
    assert _ADR_PATH.is_file(), f"expected {_ADR_PATH} to exist"


def test_adr_weights_table_is_non_empty() -> None:
    """A guard comparing against an empty parse would pass vacuously on a table-format change."""
    assert _adr_weights(), (
        f"found no weight rows in {_ADR_PATH}'s §4 table -- either the table's Markdown shape "
        "changed (update _WEIGHT_ROW_RE in this test) or the table was accidentally removed"
    )


def test_adr_0018_weight_names_match_energy_weights() -> None:
    adr_names = set(_adr_weights())
    code_names = set(ENERGY_WEIGHTS)
    assert adr_names == code_names, (
        f"docs/design/0018-set-projection-and-file-viewer.md's §4 table names {adr_names} but "
        f"ENERGY_WEIGHTS names {code_names} -- keep the ADR's table and the module's table in "
        "sync (add, remove, or rename in both, in the same change)."
    )


def test_adr_0018_weight_values_match_energy_weights() -> None:
    adr_weights = _adr_weights()
    mismatches = {
        name: (adr_weights[name], ENERGY_WEIGHTS[name])
        for name in adr_weights
        if name in ENERGY_WEIGHTS and adr_weights[name] != ENERGY_WEIGHTS[name]
    }
    assert not mismatches, (
        f"docs/design/0018-set-projection-and-file-viewer.md's §4 table disagrees with "
        f"ENERGY_WEIGHTS on {sorted(mismatches)} (adr_value, code_value): {mismatches}. If "
        "phaze-x1qr3.12's operator blind check changed a weight, update BOTH the ADR's §4 "
        "table and ENERGY_WEIGHTS, and record the change as a dated amendment in the ADR's "
        "§10 rather than editing §4 silently."
    )
