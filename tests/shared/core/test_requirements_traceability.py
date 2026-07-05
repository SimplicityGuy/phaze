"""DOCS-01 requirements-traceability drift guard (Phase 66).

Closes the manual REQUIREMENTS/ROADMAP sync gap called out across the retrospectives:
a passed phase whose mapped requirement is left unmarked, a requirement marked Complete
without a passed phase, or a per-requirement checkbox that disagrees with its Traceability
table Status must all fail CI loudly and precisely.

This is a pure filesystem structural guard mirroring the repo's established idiom
(``tests/shared/core/test_docs_ia_current.py`` / ``test_dead_template_guard.py``): repo-root
path constants, ``read_text`` parse-then-assert, one assertion function per behavior, every
assert carries a precise offender message, and ZERO ``phaze.*`` imports -> hermetic. Being
import-free it is immune to the ``get_settings`` lru_cache leak / ``saq_jobs`` stub
cross-test poison and passes in isolation via ``just test-bucket shared``.

Scope + degradation rule (D-04):

* **Active milestone** (``.planning/REQUIREMENTS.md`` + ``.planning/ROADMAP.md`` +
  ``.planning/phases/``) gets full gating: a phase is *passed* iff its ``- [x] **Phase NN**``
  line in ROADMAP.md is checked **AND** its ``{NN}-VERIFICATION.md`` frontmatter is
  ``status: passed`` (D-01). The cross-check is bidirectional (D-02) and also requires the
  per-requirement checkbox and the Traceability Status column to agree (D-03).
* **Archived milestones** (``.planning/milestones/vN.M-REQUIREMENTS.md``) predate the
  gsd-verifier and frequently have NO VERIFICATION files (v7.0 has zero; v4.0 phases 39-42
  have none). They are validated for **internal consistency only** — per-requirement
  checkbox <-> Traceability Status <-> Complete/Deferred — and are NEVER gated on missing
  VERIFICATION files. Requirement IDs that appear in only one encoding (e.g. v5.0's
  ``CLOUDIMG-01..03`` range rows, or deferred rows listed without a checkbox) are tolerated:
  only the intersection of both encodings is cross-checked, so legacy formatting variance
  never false-fails.

* **In-flight tolerance** (D-05): a not-yet-passed phase whose mapped requirements are still
  unmarked (checkbox ``[ ]`` + Status ``Pending``) is NOT drift — it PASSES. Phase 66 itself
  is exactly this state while this guard is being built, so the guard must stay green.

* **Between-milestones tolerance:** ``/gsd:complete-milestone`` deliberately removes
  ``.planning/REQUIREMENTS.md`` at milestone close (a fresh one is created by the next
  ``/gsd:new-milestone``). An absent active REQUIREMENTS.md is therefore a valid
  no-active-milestone state, NOT drift — the active-milestone checks below skip while it is
  gone. The archived-milestone consistency check + the hermetic parser unit tests still run.

Status-vocabulary is normalized before comparing ({complete, done} -> COMPLETE;
{deferred} -> DEFERRED; everything else -> PENDING) because the active milestone uses
Complete/Pending while v7.0 uses Done/Deferred (Pitfall 1).
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/shared/core/X.py -> repo root
_PLANNING = _REPO_ROOT / ".planning"
_REQUIREMENTS = _PLANNING / "REQUIREMENTS.md"
_ROADMAP = _PLANNING / "ROADMAP.md"
_MILESTONES = _PLANNING / "milestones"
_PHASES = _PLANNING / "phases"

# Between-milestones tolerance: complete-milestone removes REQUIREMENTS.md; new-milestone
# recreates it. When it is absent there is no active milestone to gate, so the active-milestone
# checks skip (the archived-consistency + hermetic parser tests still run).
_NO_ACTIVE_MILESTONE = not _REQUIREMENTS.exists()
_NO_ACTIVE_MILESTONE_REASON = "no active .planning/REQUIREMENTS.md — between milestones (removed at close, recreated by /gsd:new-milestone); active-milestone traceability checks are N/A"

# Verbatim parser regexes (RESEARCH Deep-Dive 1). All line-anchored patterns use re.MULTILINE.
_REQ_CHECKBOX = re.compile(r"^- \[([ x])\] \*\*([A-Z]+-\d+)\*\*", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\|\s*([A-Z]+-\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
_PHASE_IN_CELL = re.compile(r"Phase (\d+(?:\.\d+)?)")
_ROADMAP_PHASE = re.compile(r"^- \[([ x])\] \*\*Phase (\d+(?:\.\d+)?)[:\s]", re.MULTILINE)
_VERIFICATION_FRONTMATTER = re.compile(r"^status:\s*passed", re.MULTILINE)
_VERIFICATION_BODY = re.compile(r"^\*\*Status:\*\*\s*passed", re.MULTILINE)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalize_status(cell: str) -> str:
    """Normalize a Traceability Status cell to COMPLETE / DEFERRED / PENDING.

    The cell may carry trailing prose (e.g. ``Complete (verified — 47-VERIFICATION passed)``),
    so match by substring. Check DEFERRED before COMPLETE so a "deferred" row never reads as
    complete.
    """
    lowered = cell.lower()
    if "deferred" in lowered:
        return "DEFERRED"
    if "complete" in lowered or "done" in lowered:
        return "COMPLETE"
    return "PENDING"


def _traceability_section(text: str) -> str:
    """Slice just the ``## Traceability`` section (WR-01).

    The Traceability table is the authoritative req-id -> (Status, Phase) mapping. Scanning
    the whole file lets a req-id-shaped table row elsewhere (an Out-of-Scope table, a Future
    Requirements block, a prose example) silently last-wins-overwrite the real row. Slicing
    to the section makes the parser trust only the authoritative table.
    """
    m = re.search(r"^## Traceability\b.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""


def _requirements_list_section(text: str) -> str:
    """Everything before the ``## Traceability`` heading — the requirement-checkbox list (WR-01).

    Checkboxes (``- [x] **REQ-ID**``) only ever live in the requirements list above the
    Traceability table. Scoping checkbox parsing here keeps a stray/duplicate checkbox-shaped
    line inside or after the table from desyncing the authoritative checkbox mapping.
    """
    m = re.search(r"^## Traceability\b", text, re.MULTILINE)
    return text[: m.start()] if m else text


def _parse_requirement_checkboxes(text: str) -> dict[str, bool]:
    """Per-requirement checkbox state: {req_id: is_checked}, scoped to the requirements list."""
    section = _requirements_list_section(text)
    return {rid: (state == "x") for state, rid in _REQ_CHECKBOX.findall(section)}


def _parse_traceability(text: str) -> dict[str, tuple[str, str | None]]:
    """Traceability table: {req_id: (normalized_status, phase_number_or_None)}.

    Scoped to the ``## Traceability`` section so a req-id-shaped row elsewhere in the file
    cannot silently overwrite the authoritative row; a duplicate row *within* the table fails
    loudly rather than last-wins-overwriting (WR-01).

    A phase cell with no ``Phase N`` match (e.g. ``Future (deferred — v7.x)``) yields
    phase=None; such rows are deferred/unmapped and never participate in phase-pass.
    """
    section = _traceability_section(text)
    out: dict[str, tuple[str, str | None]] = {}
    for rid, phase_cell, status_cell in _TABLE_ROW.findall(section):
        if rid in out:
            raise ValueError(f"duplicate Traceability row for {rid} — the table must have exactly one authoritative row per requirement")
        match = _PHASE_IN_CELL.search(phase_cell)
        phase = match.group(1) if match else None
        out[rid] = (_normalize_status(status_cell), phase)
    return out


def _parse_roadmap_phases() -> dict[str, bool]:
    """Every ``- [x] **Phase NN**`` line in ROADMAP.md: {phase_number: is_checked}.

    Includes archived phases nested inside ``<details>`` blocks (they parse as plain
    markdown) and decimal inserts like 57.1 — harmless, since only active phases are looked
    up here.
    """
    return {phase: (state == "x") for state, phase in _ROADMAP_PHASE.findall(_read(_ROADMAP))}


def _verification_passed(phase: str) -> bool:
    """True iff an active-phase ``*VERIFICATION*.md`` reports ``status: passed``.

    Filename-robust glob (``{NN}-VERIFICATION.md`` vs bare ``VERIFICATION.md``). A missing
    file OR a non-passed status means not-passed. Only consulted for the active milestone.
    """
    if not _PHASES.is_dir():
        return False
    for phase_dir in _PHASES.glob(f"{phase}-*"):
        if not phase_dir.is_dir():
            continue
        for vfile in phase_dir.glob("*VERIFICATION*.md"):
            text = _read(vfile)
            if _VERIFICATION_FRONTMATTER.search(text) or _VERIFICATION_BODY.search(text):
                return True
    return False


def _active_phase_passed(phase: str, roadmap: dict[str, bool]) -> bool:
    """D-01: passed iff ROADMAP ``[x]`` AND VERIFICATION ``status: passed``."""
    return roadmap.get(phase, False) and _verification_passed(phase)


# --- Offender collectors (shared by the assertions + the D-05 regression) ---------------


def _passed_phase_completeness_offenders() -> list[str]:
    """D-01/D-02: a passed phase => every mapped req checkbox [x] AND table Status COMPLETE."""
    text = _read(_REQUIREMENTS)
    checkboxes = _parse_requirement_checkboxes(text)
    table = _parse_traceability(text)
    roadmap = _parse_roadmap_phases()
    offenders: list[str] = []
    for rid, (status, phase) in table.items():
        if phase is None or not _active_phase_passed(phase, roadmap):
            continue
        if not checkboxes.get(rid, False):
            offenders.append(f"Phase {phase} passed but {rid} checkbox [ ] unmarked")
        if status != "COMPLETE":
            offenders.append(f"Phase {phase} passed but {rid} table Status is '{status}', not Complete")
    return offenders


def _marked_requirement_offenders_from(
    checkboxes: dict[str, bool],
    table: dict[str, tuple[str, str | None]],
    roadmap: dict[str, bool],
) -> list[str]:
    """D-02 core, iterating the UNION of checkbox and table req-ids (WR-02).

    Iterating ``table.items()`` alone let a requirement ticked ``[x]`` in the checkbox list but
    MISSING from the Traceability table escape the "marked without a passed phase" drift class
    entirely (it is in neither loop). Walking ``set(checkboxes) | set(table)`` closes that
    false negative: a checkbox-only ``[x]`` with no Traceability row is now itself an offender.
    """
    offenders: list[str] = []
    for rid in sorted(set(checkboxes) | set(table)):
        if rid not in table:
            if checkboxes.get(rid, False):
                offenders.append(f"{rid} checkbox [x] but has no Traceability row — add its row or untick the checkbox")
            continue
        status, phase = table[rid]
        marked = checkboxes.get(rid, False) or status == "COMPLETE"
        if not marked:
            continue
        if phase is None:
            offenders.append(f"{rid} marked Complete but is mapped to no passed phase")
        elif not _active_phase_passed(phase, roadmap):
            offenders.append(f"{rid} marked Complete but Phase {phase} not passed")
    return offenders


def _marked_requirement_offenders() -> list[str]:
    """D-02: a req marked Complete (checkbox [x] or table COMPLETE) => its mapped phase passed."""
    text = _read(_REQUIREMENTS)
    return _marked_requirement_offenders_from(_parse_requirement_checkboxes(text), _parse_traceability(text), _parse_roadmap_phases())


def _checkbox_table_offenders(text: str, label: str) -> list[str]:
    """D-03: per-req checkbox [x]<->table COMPLETE and [ ]<->non-COMPLETE must agree.

    Only the intersection of req_ids present in BOTH encodings is checked, so range rows
    (v5.0 ``CLOUDIMG-01..03``) and checkbox-less deferred rows never false-fail.
    """
    checkboxes = _parse_requirement_checkboxes(text)
    table = _parse_traceability(text)
    offenders: list[str] = []
    for rid, (status, _phase) in table.items():
        if rid not in checkboxes:
            continue
        checked = checkboxes[rid]
        if checked and status != "COMPLETE":
            offenders.append(f"{label}: table Status '{status}' != checkbox [x] for {rid}")
        if not checked and status == "COMPLETE":
            offenders.append(f"{label}: table Status 'Complete' != checkbox [ ] for {rid}")
    return offenders


# --- Assertions (one per drift class) ---------------------------------------------------


@pytest.mark.skipif(_NO_ACTIVE_MILESTONE, reason=_NO_ACTIVE_MILESTONE_REASON)
def test_active_passed_phases_have_all_requirements_marked() -> None:
    """D-01/D-02: every passed active phase has all its mapped requirements marked Complete."""
    offenders = _passed_phase_completeness_offenders()
    assert not offenders, (
        f"passed phases have unmarked requirements — mark the requirement checkbox [x] and set its Traceability Status to Complete: {offenders}"
    )


@pytest.mark.skipif(_NO_ACTIVE_MILESTONE, reason=_NO_ACTIVE_MILESTONE_REASON)
def test_active_marked_requirements_have_passed_phases() -> None:
    """D-02: no active requirement is marked Complete unless its mapped phase actually passed."""
    offenders = _marked_requirement_offenders()
    assert not offenders, (
        "requirements are marked Complete without a passed phase — either the phase's ROADMAP "
        f"checkbox / VERIFICATION is missing, or the requirement was marked prematurely: {offenders}"
    )


@pytest.mark.skipif(_NO_ACTIVE_MILESTONE, reason=_NO_ACTIVE_MILESTONE_REASON)
def test_active_checkbox_and_table_status_agree() -> None:
    """D-03: each active requirement's checkbox and its Traceability Status column agree."""
    offenders = _checkbox_table_offenders(_read(_REQUIREMENTS), "REQUIREMENTS.md")
    assert not offenders, (
        f"a requirement's checkbox disagrees with its Traceability table Status — bring the two encodings into agreement: {offenders}"
    )


def test_archived_milestones_internally_consistent() -> None:
    """D-04: archived milestones are checkbox<->table<->Complete/Deferred consistent only.

    Never gated on VERIFICATION files (they are absent for v7.0 and several v4.0 phases).
    """
    offenders: list[str] = []
    for req_file in sorted(_MILESTONES.glob("*-REQUIREMENTS.md")):
        offenders.extend(_checkbox_table_offenders(_read(req_file), req_file.name))
    assert not offenders, f"an archived milestone has an internally-inconsistent requirement (checkbox vs Traceability Status): {offenders}"


def test_traceability_parser_is_section_scoped() -> None:
    """WR-01: a req-id-shaped row outside ``## Traceability`` never overwrites the real row."""
    text = "## Out of Scope\n\n| CI-01 | Phase 99 | Deferred |\n\n## Traceability\n\n| Requirement | Phase | Status |\n|---|---|---|\n| CI-01 | Phase 63 | Complete |\n"
    table = _parse_traceability(text)
    assert table["CI-01"] == ("COMPLETE", "63")


def test_traceability_parser_flags_duplicate_rows() -> None:
    """WR-01: a duplicate row within the table fails loudly instead of last-wins-overwrite."""
    text = "## Traceability\n\n| CI-01 | Phase 63 | Complete |\n| CI-01 | Phase 64 | Pending |\n"
    with pytest.raises(ValueError, match="duplicate Traceability row for CI-01"):
        _parse_traceability(text)


def test_requirement_checkboxes_are_section_scoped() -> None:
    """WR-01: a stray checkbox after the Traceability heading does not desync the mapping."""
    text = "## Requirements\n\n- [ ] **CI-01**: real requirement\n\n## Traceability\n\n- [x] **CI-01**: stray duplicate that must be ignored\n"
    checkboxes = _parse_requirement_checkboxes(text)
    assert checkboxes["CI-01"] is False


def test_marked_requirement_without_traceability_row_is_flagged() -> None:
    """WR-02: a requirement ticked [x] but absent from the Traceability table is drift."""
    offenders = _marked_requirement_offenders_from({"ZZ-01": True}, {}, {})
    assert any("ZZ-01 checkbox [x] but has no Traceability row" in o for o in offenders), offenders


def test_unmarked_requirement_without_traceability_row_is_not_flagged() -> None:
    """WR-02: an un-ticked [ ] requirement with no table row is in-flight, not drift."""
    assert _marked_requirement_offenders_from({"ZZ-01": False}, {}, {}) == []


@pytest.mark.skipif(_NO_ACTIVE_MILESTONE, reason=_NO_ACTIVE_MILESTONE_REASON)
def test_inflight_phase_with_unmarked_requirements_passes() -> None:
    """D-05 regression: the current in-flight active milestone state is NOT drift.

    Phase 66 is ``[ ]`` with DOCS-01/CLEAN-01/CLEAN-02 unmarked + Pending; that must PASS.
    This asserts the three active drift checks are all green on the real repo, so the guard
    stays green throughout Phase 66's own development.
    """
    offenders = (
        _passed_phase_completeness_offenders() + _marked_requirement_offenders() + _checkbox_table_offenders(_read(_REQUIREMENTS), "REQUIREMENTS.md")
    )
    assert not offenders, (
        f"the current in-flight active milestone was flagged as drift — an in-flight phase with unmarked requirements must PASS (D-05): {offenders}"
    )
