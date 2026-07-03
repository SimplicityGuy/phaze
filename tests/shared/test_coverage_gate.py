"""Coverage-gate consistency guard (Phase 64-03, COV-02 / D-05, RESEARCH Pitfall 3).

The global coverage gate lives at TWO edit sites that must move together:

1. ``pyproject.toml`` ``[tool.coverage.report] fail_under`` — the config default.
2. ``justfile`` ``coverage-combine`` ``coverage report --fail-under=<N>`` — the CLI value.

``coverage report``'s ``--fail-under`` CLI flag **silently overrides** the pyproject
config value, so if the two drift the CLI number wins and the config number becomes a
dead, misleading placeholder. Phase 64 raised the gate above the 90.38% baseline (D-05);
this guard is the tripwire that fails loud if the two numbers ever diverge OR either one
regresses back to/below the baseline — a silent gate weakening (RESEARCH Pitfall 3).

It also asserts the per-module floor wiring stays inside ``coverage-combine``: the recipe
must still emit ``coverage json`` (the floor script's input) and invoke
``scripts/coverage_floor.py`` (the per-module 85% floor, COV-01/D-02). Dropping either
would re-open a false-green hole in the combined-coverage gate.

This guard is DB-free and subprocess-free: it parses ``justfile`` as text and
``pyproject.toml`` with stdlib ``tomllib``. It lives in ``tests/shared/`` so it rides the
``shared`` bucket (see ``test_partition_guard.py`` for why bucket placement matters),
mirroring ``test_ci_workflow_wiring.py``.
"""

from __future__ import annotations

from pathlib import Path
import re
import tomllib


# The baseline combined coverage the raised gate must stay strictly above (D-05).
_BASELINE = 90.38

# tests/shared/test_coverage_gate.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _REPO_ROOT / "justfile"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _extract_recipe(justfile_text: str, name: str) -> str:
    """Return the indented body of the top-level `just` recipe `name`.

    Anchoring the header match to the start of a line (``re.MULTILINE`` + ``^``) is
    load-bearing: it stops a recipe *name* merely mentioned inside a comment from being
    mistaken for the recipe's own header. Mirrors ``test_ci_workflow_wiring._extract_recipe``.
    """
    pattern = re.compile(rf"^{re.escape(name)}\b[^\n]*:\n((?:[ \t]+.*\n?)*)", re.MULTILINE)
    match = pattern.search(justfile_text)
    assert match is not None, f"recipe {name!r} not found as a top-level header in {_JUSTFILE}"
    return match.group(1)


def _justfile_gate() -> int:
    recipe_body = _extract_recipe(_JUSTFILE.read_text(encoding="utf-8"), "coverage-combine")
    match = re.search(r"coverage report --fail-under=(\d+)", recipe_body)
    assert match is not None, f"coverage-combine recipe has no `coverage report --fail-under=<N>`:\n{recipe_body}"
    return int(match.group(1))


def _pyproject_gate() -> float:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return float(data["tool"]["coverage"]["report"]["fail_under"])


def test_global_gate_sites_agree_and_beat_the_baseline() -> None:
    """The two ``fail_under`` sites are EQUAL and BOTH strictly > 90.38 (D-05, Pitfall 3).

    ``--fail-under`` on the CLI overrides the pyproject config value, so the config and
    the recipe number must stay in lockstep or the CLI silently wins. Both must also stay
    above the 90.38% baseline, or the gate has been silently weakened back to (or below)
    where Phase 64 started.
    """
    justfile_gate = _justfile_gate()
    pyproject_gate = _pyproject_gate()

    assert pyproject_gate == justfile_gate, (
        f"coverage gate drift: pyproject fail_under={pyproject_gate} but "
        f"justfile coverage-combine --fail-under={justfile_gate}; the CLI value silently wins"
    )
    assert pyproject_gate > _BASELINE, f"pyproject fail_under={pyproject_gate} must be strictly > {_BASELINE} baseline (D-05)"
    assert justfile_gate > _BASELINE, f"justfile --fail-under={justfile_gate} must be strictly > {_BASELINE} baseline (D-05)"


def test_coverage_combine_keeps_the_per_module_floor_wiring() -> None:
    """``coverage-combine`` must still emit ``coverage json`` AND run the floor script.

    The per-module floor (COV-01/D-02) rides inside this one recipe on the combined
    coverage. It needs ``coverage json`` as its input and ``scripts/coverage_floor.py`` as
    the check; dropping either silently re-opens a false-green hole in the gate.
    """
    recipe_body = _extract_recipe(_JUSTFILE.read_text(encoding="utf-8"), "coverage-combine")
    assert "coverage json" in recipe_body, f"coverage-combine no longer emits coverage.json for the floor script:\n{recipe_body}"
    assert "scripts/coverage_floor.py" in recipe_body, f"coverage-combine no longer invokes the per-module floor script:\n{recipe_body}"
