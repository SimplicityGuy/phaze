"""BEUI-03 docs-content guard (Phase 71).

Locks the operator-facing documentation deliverables of BEUI-03 so a future edit cannot
silently drop them:

* ``docs/runbook.md`` exists and covers the four operator topics — the force-local incident
  revert (incl. the A4 held-file note), reading the N backend lanes, spillover, and per-backend
  ``_FILE`` secrets — and prints **no literal secret value** (T-71-11).
* ``docs/configuration.md`` reconciles the ``cloud_target`` contradiction (T-71-12): it states the
  flat selector was **removed in Phase 67** (not "deprecated but still works") and carries the
  trivial 1:1 ``cloud_target`` -> ``backends`` equivalence.

This is a pure filesystem structural guard mirroring the repo's established hermetic idiom
(``tests/shared/core/test_requirements_traceability.py`` / ``test_docs_ia_current.py``):
repo-root ``Path`` constants, ``read_text`` parse-then-assert, one assertion per behavior, every
assert carries a precise offender message, and ZERO ``phaze.*`` imports -> hermetic. Being
import-free it is immune to the ``get_settings`` lru_cache leak / ``saq_jobs`` stub cross-test
poison and passes in isolation via ``just test-bucket shared``. It needs no DB fixture.
"""

from __future__ import annotations

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/shared/core/X.py -> repo root
_DOCS = _REPO_ROOT / "docs"
_RUNBOOK = _DOCS / "runbook.md"
_CONFIGURATION = _DOCS / "configuration.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _contains_all(haystack: str, needles: list[str]) -> list[str]:
    """Return the needles (case-insensitive) that are MISSING from haystack."""
    lowered = haystack.lower()
    return [n for n in needles if n.lower() not in lowered]


# --- runbook presence + coverage --------------------------------------------------------


def test_runbook_exists() -> None:
    """``docs/runbook.md`` must exist — it is the BEUI-03 operator runbook deliverable."""
    assert _RUNBOOK.is_file(), f"missing operator runbook — create {_RUNBOOK.relative_to(_REPO_ROOT)}"


def test_runbook_covers_force_local_incident_revert() -> None:
    """The runbook documents the force-local master toggle / incident revert."""
    text = _read(_RUNBOOK)
    missing = _contains_all(
        text,
        [
            "force-local",  # the toggle by name
            "FORCED LOCAL",  # engaged pill copy
            "CLOUD ROUTING",  # normal pill copy
            "route_control",  # the durable row it writes
            "reversible",  # no-redeploy incident semantics
            "drain",  # gated leg 1
            "duration router",  # gated leg 2
        ],
    )
    assert not missing, f"runbook.md is missing force-local incident-revert coverage: {missing}"


def test_runbook_documents_held_file_behavior() -> None:
    """A4: already-held AWAITING_CLOUD files stay held while forced — must be documented."""
    text = _read(_RUNBOOK)
    assert "held" in text.lower(), "runbook.md must document the A4 held-file behavior (contains 'held')"
    missing = _contains_all(text, ["AWAITING_CLOUD", "stay held"])
    assert not missing, f"runbook.md held-file note is incomplete: {missing}"


def test_runbook_covers_reading_the_lanes() -> None:
    """The runbook explains reading the N lanes: rank order, in-flight/cap, offline, admission."""
    text = _read(_RUNBOOK)
    missing = _contains_all(
        text,
        [
            "rank",  # rank ascending = dispatch preference
            "dispatch preference",
            "in_flight",  # {in_flight}/{cap}
            "cap",
            "offline",  # lane offline state
            "quota",  # Kueue quota-wait
            "Inadmissible",  # vs Inadmissible
        ],
    )
    assert not missing, f"runbook.md is missing lane-reading coverage: {missing}"


def test_runbook_covers_spillover() -> None:
    """The runbook explains spillover across backends by rank and cap."""
    text = _read(_RUNBOOK)
    assert "spillover" in text.lower() or "spill" in text.lower(), "runbook.md must cover spillover behavior"
    missing = _contains_all(text, ["rank", "cap", "eligible"])
    assert not missing, f"runbook.md spillover coverage is incomplete: {missing}"


def test_runbook_covers_file_secrets() -> None:
    """The runbook cross-references the per-backend ``_FILE`` secret convention."""
    text = _read(_RUNBOOK)
    missing = _contains_all(text, ["_FILE", "configuration.md", "never print a secret"])
    assert not missing, f"runbook.md is missing per-backend _FILE secret coverage: {missing}"


def test_runbook_prints_no_literal_secret_values() -> None:
    """T-71-11: the runbook references secrets by NAME only — never a literal value.

    Guards against a future edit pasting a real token / key / PEM block into the runbook. We
    forbid the obvious literal-secret shapes: a PEM header, a real ``phaze_agent_<token>`` value
    (a bare ``phaze_agent_`` prefix in prose is fine; the guard fires only when 12+ token
    characters follow it), and an inline ``password=<value>`` / ``secret=<value>`` assignment.
    """
    text = _read(_RUNBOOK)
    offenders: list[str] = []
    if "-----BEGIN" in text:
        offenders.append("a PEM/private-key block (-----BEGIN ...)")
    if re.search(r"phaze_agent_[A-Za-z0-9_\-]{12,}", text):
        offenders.append("a literal phaze_agent_ bearer token value")
    if re.search(r"(?i)(password|secret_access_key|sa_token)\s*=\s*['\"]?[A-Za-z0-9/+]{8,}", text):
        offenders.append("an inline secret assignment (password/secret_access_key/sa_token = <value>)")
    assert not offenders, f"runbook.md appears to embed a literal secret value — reference secrets by name only: {offenders}"


# --- configuration.md contradiction reconciled ------------------------------------------


def test_configuration_states_cloud_target_removed() -> None:
    """T-71-12: configuration.md says cloud_target was REMOVED in Phase 67 (no live-selector).

    The ``### Cloud target`` section must present it as removed and point at the registry — it must
    NOT still describe ``PHAZE_CLOUD_TARGET`` as a live "single routing selector".
    """
    text = _read(_CONFIGURATION)
    lowered = text.lower()
    assert "removed in" in lowered and "phase 67" in lowered, "configuration.md must state cloud_target was removed in Phase 67"
    assert "backend registry" in lowered or "backends.toml" in lowered, "configuration.md must point cloud_target readers at the backend registry"
    # The stale live-selector framing must be gone from the Cloud target section heading.
    assert "### Cloud target (`PHAZE_CLOUD_TARGET`)" not in text, (
        "the stale '### Cloud target (PHAZE_CLOUD_TARGET)' live-selector heading must be reconciled"
    )


def test_configuration_documents_cloud_target_to_backends_equivalence() -> None:
    """configuration.md carries the trivial 1:1 cloud_target -> backends equivalence."""
    text = _read(_CONFIGURATION)
    missing = _contains_all(
        text,
        [
            "equivalence",  # the 1:1 mapping is called out
            'kind="compute"',  # a1 -> compute
            'kind="kueue"',  # k8s -> kueue
            'kind="local"',  # local -> implicit local
        ],
    )
    assert not missing, f"configuration.md is missing the 1:1 cloud_target->backends equivalence: {missing}"
