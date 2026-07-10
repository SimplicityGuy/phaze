"""AST source-scan guard (READ-03 / SC-1): the phase's core "no ``FileRecord.state`` read" cutover in
``tasks/reenqueue.py`` and ``tasks/reconcile_cloud_jobs.py`` cannot silently regress.

Standing insurance behind READ-03: the behavioral recovery/reconcile tests catch a wrong predicate at
the sites they exercise; THIS guard catches a ``FileRecord.state`` read (or a ``FileState.<member>``
reference, or a ``getattr(_, "state")``) reintroduced at a NEW site those tests never touch.

Both target files end Phase 80 at **clean absence** -- unlike the Phase-84 dedup scanner (which allows
EXACTLY ONE surviving ``FileState.DUPLICATE_RESOLVED`` dual-writer), neither ``reenqueue.py`` nor
``reconcile_cloud_jobs.py`` may reference ``FileRecord.state`` / ``FileState.<member>`` at all. Recovery
derives "done" from the Phase-78/81 ``done_clause`` / ``domain_completed_clause`` builders; reconcile's
at-cap spill re-stamps the ``cloud_job`` sidecar via ``hold_awaiting_cloud`` (D-04) and writes NO
``FileRecord.state``. Clean absence means **no allow-list is needed** -- the invariant is simply "zero
occurrences," which is strictly stronger than the dedup guard and cannot false-positive on a legitimate
writer.

Invariants enforced (RESEARCH §(b) forms #1-#6):

* ``#1`` ``select(FileRecord.id).where(FileRecord.state.in_([...]))`` -- attribute ``.state`` off
  ``FileRecord`` (or a FileRecord-bound local) in ANY position.
* ``#2`` ``.where(FileRecord.state == FileState.X)`` -- the ``ast.Compare`` form (both the ``.state``
  read AND the ``FileState.<member>`` occurrence are flagged).
* ``#3`` ``update(FileRecord).where(...).values(state=FileState.X)`` -- the **removed write** (flagged by
  the ``FileState.<member>`` occurrence scan; the keyword ``state=`` param name is NOT an attribute, so
  the ``.state``-read scan correctly ignores it -- only the ``FileState`` RHS is the violation).
* ``#4`` ``file.state`` -- a read off an instance bound to a ``FileRecord``.
* ``#5`` ``getattr(file, "state")`` -- the reflective read.
* ``#6`` ``FileRecord.state`` passed **positionally** into ``.where(a, b, <read>)`` -- the Phase-83
  blind spot. The scan walks BOTH ``Call.args`` and ``Call.keywords`` of the ``.where``-family funcs
  (the very blind spot the Phase-84 scanner already closes), so a positional read is not missed.

Why this is an ``ast.walk`` scan and NEVER a line-oriented ``grep`` (Pitfall 1 / project memory
``feedback_mutation_test_guard_tests`` -- Phase 83 shipped two *toothless* guards):

1. Both modules import and heavily USE ``FileRecord`` (``FileRecord.id`` binds, ``select(FileRecord)``);
   a ``grep`` for ``FileRecord`` would drown in legitimate ``.id`` reads. The AST scan keys ONLY on the
   ``.state`` attribute.
2. ``reconcile_cloud_jobs.py`` reads ``cloud_job.status`` and ``getattr(job, "status", ...)`` all over
   -- ``.attr == "status"``, NOT ``"state"``. A sloppy substring grep (``state``) would false-positive
   on every ``status``. The AST scan keys on the EXACT attribute name, so ``status`` is never flagged.
3. The former read sites passed the clause POSITIONALLY (``.where(a, b, c)``), not as a chained
   ``.where(<Compare>)``. A rule keyed only on ``keyword.arg`` or on a chained comparator is BLIND to
   them -- precisely the blind spot that made Phase 83's guards toothless. This scan walks BOTH the
   positional ``Call.args`` list AND the ``Call.keywords`` list.

The negative tests below (``test_guard_flags_*`` / ``test_guard_ignores_*``) are the mutation directions
encoded permanently and hermetically -- they mutate crafted source STRINGS, never the real files, so this
test is DB-free and leaves no source dirty. The executor ALSO exercised the mutation discipline against
the real files (inject form #N -> guard RED -> restore) per RESEARCH §(b); see 80-05-SUMMARY.md.
"""

from __future__ import annotations

import ast
from pathlib import Path


# This file is ``tests/shared/test_reenqueue_reconcile_source_scan.py``; parents[2] is the repo root.
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "phaze"
_REENQUEUE = _SRC_ROOT / "tasks" / "reenqueue.py"
_RECONCILE = _SRC_ROOT / "tasks" / "reconcile_cloud_jobs.py"

# SQLAlchemy read-clause entry points. A ``FileRecord.state`` / ``FileState.<member>`` inside ANY
# argument of one of these is a read of ``FileRecord.state`` (the exact thing the cutover removed).
_WHERE_FUNCS = frozenset({"where", "filter", "filter_by", "having"})


def _filerecord_bound_names(tree: ast.AST) -> set[str]:
    """Names that resolve to a ``FileRecord`` (the model class + any local assigned from a FileRecord expr).

    Always includes ``"FileRecord"`` itself. Additionally collects any ``name`` bound by an assignment
    whose RHS references ``FileRecord`` (e.g. ``file = (await ...select(FileRecord)...).scalar_one()``),
    so a ``file.state`` read off that local (form #4) is caught. Deliberately conservative-broad: it can
    over-include a Select-bound local (``stmt = select(FileRecord.id)``), but that only makes a spurious
    ``stmt.state`` read *more* likely to be flagged -- strictly stronger, and never a false-positive on a
    legitimate ``.status`` / ``.id`` read (this scan keys ONLY on ``.attr == "state"``).
    """
    names = {"FileRecord"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(sub, ast.Name) and sub.id == "FileRecord" for sub in ast.walk(node.value)):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _state_reads(tree: ast.AST) -> list[ast.Attribute]:
    """Every ``<FileRecord-bound>.state`` attribute READ (forms #1, #2, #4, #6).

    Matches ``ast.Attribute`` nodes whose ``.attr`` is ``"state"``, whose context is ``Load`` (a READ --
    never the ``Store`` target of ``f.state = ...``), and whose base ``Name`` resolves to ``FileRecord``.
    Walks the WHOLE tree, so a read nested anywhere -- inside ``.in_(...)``, an ``ast.Compare``, or a
    POSITIONAL ``.where`` arg (the Phase-83 blind spot) -- is reached, because ``ast.walk`` descends into
    both ``Call.args`` and ``Call.keywords``.
    """
    bound = _filerecord_bound_names(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "state"
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id in bound
    ]


def _filestate_occurrences(tree: ast.AST) -> list[ast.Attribute]:
    """Every ``FileState.<member>`` attribute access (form #2's RHS + form #3, the removed write).

    A docstring/comment mention of a state name is NOT an ``ast.Attribute`` and is therefore invisible
    here -- the whole point of an AST scan over a line scan (Pitfall 1).
    """
    return [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "FileState"]


def _getattr_state_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``getattr(_, "state")`` call (form #5, the reflective read).

    Keys on a ``getattr`` call whose SECOND positional arg is the constant ``"state"``. ``getattr(job,
    "status", None)`` (live in reconcile) has a ``"status"`` constant, so it is never flagged.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "state"
    ]


def _where_family_arg_violations(tree: ast.AST) -> list[ast.Attribute]:
    """Every ``FileRecord.state`` read OR ``FileState.<member>`` appearing in an arg of a ``.where``-family call.

    Walks BOTH the positional ``Call.args`` list AND the ``Call.keywords`` list (regardless of
    ``keyword.arg``), so it is blind to NEITHER the positional ``.where(a, b, <read>)`` form NOR the
    keyword ``.filter_by(state=FileState.X)`` form -- the two Phase-83 blind spots the Phase-84 scanner
    already closes. This is a SUPPLEMENTARY structural check: the aggregate guard already catches these
    reads via the whole-tree scans, but this walker exists to make the args+keywords coverage explicit
    and directly testable (see ``test_where_walker_*``).
    """
    bound = _filerecord_bound_names(tree)
    hits: list[ast.Attribute] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _WHERE_FUNCS):
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:  # positional args AND keyword-arg values.
            for sub in ast.walk(arg):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and ((sub.attr == "state" and sub.value.id in bound) or sub.value.id == "FileState")
                ):
                    hits.append(sub)
    return hits


def _violations(source: str) -> list[ast.AST]:
    """All distinct ``FileRecord.state`` / ``FileState.<member>`` / ``getattr(_,"state")`` occurrences.

    The union of the three whole-tree scans, deduplicated by node identity (form #2 is found by both the
    ``.state``-read scan AND the ``FileState`` scan on distinct nodes; that is two occurrences, correct).
    Clean absence == an empty list.
    """
    tree = ast.parse(source)
    nodes: list[ast.AST] = [*_state_reads(tree), *_filestate_occurrences(tree), *_getattr_state_calls(tree)]
    seen: set[int] = set()
    uniq: list[ast.AST] = []
    for node in nodes:
        if id(node) not in seen:
            seen.add(id(node))
            uniq.append(node)
    return uniq


def _lines(nodes: list[ast.AST]) -> list[object]:
    """Line numbers of violation nodes (for assertion messages)."""
    return [getattr(n, "lineno", "?") for n in nodes]


# ---------------------------------------------------------------------------
# The real-source guard (the invariant this test exists to protect)
# ---------------------------------------------------------------------------


def test_reenqueue_has_zero_state_reads() -> None:
    """``tasks/reenqueue.py``: ZERO ``FileRecord.state`` / ``FileState`` / ``getattr(_,"state")`` occurrences.

    Recovery derives "done" DIRECTLY from ``done_clause`` / ``domain_completed_clause`` (READ-03 / D-05);
    every former ``FileRecord.state.in_([...])`` read (old lines 187/197/209) is gone, and the
    ``FileState`` import was dropped. Clean absence -- no allow-list.
    """
    violations = _violations(_REENQUEUE.read_text(encoding="utf-8"))
    assert violations == [], (
        f"tasks/reenqueue.py reintroduced a FileRecord.state read / FileState reference "
        f"({len(violations)} found) at lines {_lines(violations)}. The Phase-80 cutover replaced every "
        "state read with the derived done_clause / domain_completed_clause builders (READ-03 / D-05). "
        "No FileRecord.state read, no FileState.<member>, and no getattr(_, 'state') is permitted."
    )


def test_reconcile_cloud_jobs_has_zero_state_reads() -> None:
    """``tasks/reconcile_cloud_jobs.py``: ZERO ``FileRecord.state`` / ``FileState`` / ``getattr(_,"state")`` occurrences.

    The at-cap spill re-stamps the ``cloud_job`` sidecar via ``hold_awaiting_cloud`` (D-04/D-12) and
    writes NO ``FileRecord.state`` -- the removed write (old line 212 ``.values(state=...)``) and the
    ``FileRecord, FileState`` import are gone. ``cloud_job.status`` / ``getattr(job, "status")`` reads
    (``.attr == "status"``) are NOT ``.state`` and are correctly ignored.
    """
    violations = _violations(_RECONCILE.read_text(encoding="utf-8"))
    assert violations == [], (
        f"tasks/reconcile_cloud_jobs.py reintroduced a FileRecord.state read / FileState reference "
        f"({len(violations)} found) at lines {_lines(violations)}. The Phase-80 cutover swapped the "
        "retired FileRecord.state write for the single hold_awaiting_cloud spill writer (D-04/D-12); "
        "reconcile writes NO FileRecord.state at all. No state read, no FileState, no getattr(_, 'state')."
    )


# ---------------------------------------------------------------------------
# Mutation directions, encoded permanently (proof the guard has teeth)
# ---------------------------------------------------------------------------


def test_guard_flags_attribute_in_call_read() -> None:
    """MUTATION #1 (RED): ``select(FileRecord.id).where(FileRecord.state.in_([...]))`` is caught.

    ``FileRecord.state`` is the ``.value`` of an ``.in_`` attribute -- a Load-context read nested inside a
    method call. The whole-tree ``.state``-read scan reaches it.
    """
    source = "stmt = select(FileRecord.id).where(FileRecord.state.in_([s.value for s in DONE]))\n"
    violations = _violations(source)
    assert len(violations) == 1
    assert isinstance(violations[0], ast.Attribute)
    assert violations[0].attr == "state"


def test_guard_flags_compare_read_and_filestate() -> None:
    """MUTATION #2 (RED): ``.where(FileRecord.state == FileState.ANALYZED)`` -- BOTH halves are flagged.

    The ``FileRecord.state`` READ and the ``FileState.ANALYZED`` RHS are two distinct violations (the
    ``.state``-read scan + the ``FileState`` occurrence scan), so the ``ast.Compare`` form yields 2.
    """
    source = "q = select(FileRecord).where(FileRecord.state == FileState.ANALYZED)\n"
    violations = _violations(source)
    assert len(violations) == 2
    attrs = {getattr(n, "attr", None) for n in violations}
    assert attrs == {"state", "ANALYZED"}


def test_guard_flags_removed_values_write() -> None:
    """MUTATION #3 (RED): the removed ``update(FileRecord).where(...).values(state=FileState.X)`` write is caught.

    The ``state=`` keyword is a PARAMETER NAME, not an ``ast.Attribute``, so the ``.state``-read scan
    correctly ignores it; the ``FileState.AWAITING_CLOUD`` RHS is the single flagged violation. (Reconcile
    retired exactly this write at old line 212.)
    """
    source = "stmt = update(FileRecord).where(FileRecord.id == fid).values(state=FileState.AWAITING_CLOUD)\n"
    violations = _violations(source)
    assert len(violations) == 1
    assert isinstance(violations[0], ast.Attribute)
    assert violations[0].attr == "AWAITING_CLOUD"


def test_guard_flags_instance_state_read() -> None:
    """MUTATION #4 (RED): ``file.state`` off a FileRecord-bound local is caught.

    ``file`` is bound from an expression referencing ``FileRecord`` (``session.get(FileRecord, fid)``),
    so ``_filerecord_bound_names`` recognises it and the subsequent ``file.state`` Load is flagged.
    """
    source = "file = session.get(FileRecord, fid)\nheld = file.state\n"
    violations = _violations(source)
    assert len(violations) == 1
    assert isinstance(violations[0], ast.Attribute)
    assert violations[0].attr == "state"


def test_guard_flags_getattr_state() -> None:
    """MUTATION #5 (RED): ``getattr(file, "state")`` -- the reflective read -- is caught."""
    source = "val = getattr(file, 'state')\n"
    violations = _violations(source)
    assert len(violations) == 1
    assert isinstance(violations[0], ast.Call)


def test_guard_flags_positional_where_read() -> None:
    """MUTATION #6 (RED): a read passed POSITIONALLY as ``.where(a, b, FileRecord.state != ...)`` is caught.

    The Phase-83 positional-arg blind spot: ``.where()`` receives the read comparator positionally, NOT as
    a chained ``.where(<Compare>)``. The whole-tree scan catches it (and ``_where_family_arg_violations``
    confirms the args-walk independently -- see ``test_where_walker_catches_positional``).
    """
    source = "stmt = select(FileRecord.id).where(\n    FileRecord.sha256_hash == h,\n    FileRecord.state != FileState.DISCOVERED,\n)\n"
    violations = _violations(source)
    # FileRecord.state (read) + FileState.DISCOVERED (occurrence) = 2.
    assert len(violations) == 2
    assert {getattr(n, "attr", None) for n in violations} == {"state", "DISCOVERED"}


# ---------------------------------------------------------------------------
# Explicit Call.args + Call.keywords coverage (the Phase-83 blind-spot closure)
# ---------------------------------------------------------------------------


def test_where_walker_catches_positional() -> None:
    """The ``.where``-family walker inspects POSITIONAL ``Call.args`` (not just chained/keyword forms)."""
    tree = ast.parse("stmt = select(FileRecord.id).where(FileRecord.sha256 == h, FileRecord.state != FileState.DISCOVERED)\n")
    hits = _where_family_arg_violations(tree)
    # The positional FileRecord.state read AND the FileState.DISCOVERED occurrence are both reached.
    assert {h.attr for h in hits} == {"state", "DISCOVERED"}


def test_where_walker_catches_keyword() -> None:
    """The ``.where``-family walker inspects ``Call.keywords`` (e.g. ``.filter_by(state=FileState.X)``)."""
    tree = ast.parse("q = session.query(FileRecord).filter_by(state=FileState.DUPLICATE_RESOLVED)\n")
    hits = _where_family_arg_violations(tree)
    assert any(h.attr == "DUPLICATE_RESOLVED" for h in hits)


# ---------------------------------------------------------------------------
# GREEN false-positive checks (the guard must NOT over-fire)
# ---------------------------------------------------------------------------


def test_guard_ignores_cloud_job_status_read() -> None:
    """GREEN false-positive check: a legitimate ``cloud_job.status`` read (``.attr == "status"``) is NOT flagged.

    ``reconcile_cloud_jobs.py`` reads ``cloud_job.status`` and ``getattr(job, "status", None)`` throughout;
    ``"status"`` is not ``"state"``, so neither the ``.state``-read scan nor the ``getattr("state")`` scan
    fires. This is the exact substring trap a sloppy grep(``state``) would fall into.
    """
    source = (
        "if cloud_job.status == CloudJobStatus.SUCCEEDED.value:\n"
        "    counter = getattr(job, 'status', None) or {}\n"
        "    stmt = select(CloudJob).where(CloudJob.status.in_([s.value for s in IN_FLIGHT]))\n"
    )
    assert _violations(source) == []


def test_guard_ignores_filerecord_id_read() -> None:
    """GREEN false-positive check: ``FileRecord.id`` reads (the ledger-scope idiom) are NOT flagged.

    Both modules bind and read ``FileRecord.id`` heavily (``select(FileRecord.id).where(FileRecord.id ==
    ...)``); ``.attr == "id"`` is never ``"state"``, so the scan stays silent.
    """
    source = "stmt = select(FileRecord.id).where(FileRecord.id == func.any(bindparam('ids')))\n"
    assert _violations(source) == []


def test_guard_ignores_state_mention_in_docstring() -> None:
    """GREEN false-positive check: a prose ``FileRecord.state`` / ``FileState`` mention is NOT an occurrence.

    Both real modules describe the RETIRED ``FileRecord.state`` reads in their docstrings/comments; an AST
    attribute scan does not see docstring text (a line grep would false-positive here -- Pitfall 1).
    """
    source = '"""Cut over from the retired FileRecord.state == FileState.AWAITING_CLOUD read to the derived layer."""\n'
    assert _violations(source) == []
