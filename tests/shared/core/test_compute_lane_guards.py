"""Epic phaze-zlv verification: static grep-level + contract guards (fast, no DB).

These guards lock the epic's structural invariants so a future edit cannot silently reintroduce a
second compute-lane derivation path or resurrect the retired ``'a1'`` heuristic:

* **No stale ``'a1'`` lane key** -- the old file-badge heuristic mapped a NULL-``cloud_phase`` cloud
  job to a hardcoded ``lane = "a1"`` badge (``☁️ A1``). The epic replaced it with the registry-kind
  projection, so no template renders the ``☁️ A1`` badge, no template uses ``'a1'`` as a lane-badge
  dict key, and no source assigns the ``"a1"`` literal as a lane label. (Prose/comments that merely
  MENTION the historical A1 lane are unaffected -- the guards target rendered badges + code
  assignments, not documentation.)
* **Retired symbols gone** -- ``classify_compute_lanes`` (the deleted pre-epic derivation) and the
  ``compute_lane_state`` template context key are absent from ``src/`` entirely.
* **Backends contract present** -- a positive-existence check pins the three dispatch-contract
  symbols (``get_backend_lane_snapshot`` / ``derive_cloud_hold_reason`` / the per-backend
  ``dispatch`` routing) as still live in ``src/phaze/services/backends.py``.

Pure filesystem/regex -- no Postgres, no app, runs everywhere.
"""

from __future__ import annotations

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "phaze"
_TEMPLATES = _SRC / "templates"
_BACKENDS = _SRC / "services" / "backends.py"


def _src_python_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _template_files() -> list[Path]:
    return sorted(_TEMPLATES.rglob("*.html"))


# ---------------------------------------------------------------------------
# Guard 1: the stale 'a1' lane key / badge is gone from templates + lane logic.
# ---------------------------------------------------------------------------

# The old rendered A1 badge glyph string, e.g. `'a1': ('☁️ A1', ...)`.
_A1_BADGE = "☁️ A1"
# ``'a1'`` / ``"a1"`` used as a dict KEY (the removed lane_badges map key).
_A1_DICT_KEY = re.compile(r"""['"]a1['"]\s*:""")
# ``lane = "a1"`` / ``lane, lane_kind = ..., "a1"`` -- the retired literal lane assignment. Matches an
# ``a1`` string literal on the RHS of an assignment; prose/comments (no ``=`` before the literal on the
# same logical assignment) never match.
_A1_ASSIGN = re.compile(r"""=\s*['"]a1['"]""")


def test_no_a1_badge_string_in_templates() -> None:
    """No template renders the retired ``☁️ A1`` compute-lane badge."""
    offenders = [t.relative_to(_REPO_ROOT).as_posix() for t in _template_files() if _A1_BADGE in t.read_text(encoding="utf-8")]
    assert not offenders, f"stale '☁️ A1' badge still rendered in: {offenders}"


def test_no_a1_lane_badge_dict_key_in_templates() -> None:
    """No template uses ``'a1'`` as a lane-badge dict key (the removed lane_badges map)."""
    offenders = [t.relative_to(_REPO_ROOT).as_posix() for t in _template_files() if _A1_DICT_KEY.search(t.read_text(encoding="utf-8"))]
    assert not offenders, f"stale 'a1' lane-badge dict key still present in: {offenders}"


def test_no_a1_literal_lane_assignment_in_src() -> None:
    """No source file assigns the retired ``"a1"`` string literal (the deleted lane heuristic)."""
    offenders = [p.relative_to(_REPO_ROOT).as_posix() for p in _src_python_files() if _A1_ASSIGN.search(p.read_text(encoding="utf-8"))]
    assert not offenders, f"stale '= \"a1\"' lane assignment still present in: {offenders}"


# ---------------------------------------------------------------------------
# Guard 2: the retired derivation symbol + template context key are gone.
# ---------------------------------------------------------------------------


def test_classify_compute_lanes_symbol_removed_from_src() -> None:
    """The pre-epic ``classify_compute_lanes`` derivation is deleted -- absent from all of src/."""
    offenders = [p.relative_to(_REPO_ROOT).as_posix() for p in _src_python_files() if "classify_compute_lanes" in p.read_text(encoding="utf-8")]
    assert not offenders, f"deleted symbol 'classify_compute_lanes' still referenced in: {offenders}"


def test_compute_lane_state_context_key_removed_from_src() -> None:
    """The old ``compute_lane_state`` template context key is gone from src/ (routers + templates)."""
    offenders = [
        p.relative_to(_REPO_ROOT).as_posix()
        for p in (*_src_python_files(), *_template_files())
        if "compute_lane_state" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"retired context key 'compute_lane_state' still present in: {offenders}"


# ---------------------------------------------------------------------------
# Guard 3: the backends.py contract is untouched by the epic.
# ---------------------------------------------------------------------------


def test_backends_contract_symbols_still_present() -> None:
    """The three dispatch-contract symbols still live in backends.py."""
    text = _BACKENDS.read_text(encoding="utf-8")
    assert "async def get_backend_lane_snapshot" in text
    assert "async def derive_cloud_hold_reason" in text
    assert "async def dispatch" in text  # the per-backend routing seam
