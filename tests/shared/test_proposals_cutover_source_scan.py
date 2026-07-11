"""AST source-scan guard (SIDECAR-03 / D-01): the proposal->``FileRecord.state`` cutover across
``services/proposal.py``, ``services/proposal_queries.py``, and ``routers/agent_proposals.py`` cannot
silently regress.

Standing anti-drift insurance behind SIDECAR-03: the behavioral tests (Plans 01/02) catch a wrong value
at the sites they exercise; THIS guard catches a ``FileRecord.state`` write/read (or a ``FileState.<member>``
reference, or a ``getattr(_, "state")``) reintroduced at a NEW site those tests never touch.

All three target files end Phase 86 at **clean absence** -- none of ``proposal.py``, ``proposal_queries.py``,
or ``agent_proposals.py`` may reference ``FileRecord.state`` / ``FileState.<member>`` at all. Plan 01
deleted the ``store_proposals`` file-load-and-guard block and the ``proposal_queries`` APPROVED/REJECTED
``file.state`` limbs (D-01 sites 1-3); Plan 02 removed the agent apply-PATCH ``file_record.state`` write
and the idempotent-replay read (D-01 sites 4-5). ``proposals.status`` is now the sole review-decision
authority. Clean absence means **no allow-list is needed** -- the invariant is simply "zero occurrences,"
which cannot false-positive on a legitimate writer.

Invariants enforced (RESEARCH forms #1-#6):

* ``#1`` ``select(FileRecord.id).where(FileRecord.state.in_([...]))`` -- attribute ``.state`` off
  ``FileRecord`` (or a FileRecord-bound local) in ANY position.
* ``#2`` ``.where(FileRecord.state == FileState.X)`` -- the ``ast.Compare`` form (both the ``.state``
  read AND the ``FileState.<member>`` occurrence are flagged).
* ``#3`` ``update(FileRecord).where(...).values(state=FileState.X)`` -- the **removed write** (flagged by
  the ``FileState.<member>`` occurrence scan; the keyword ``state=`` param name is NOT an attribute, so
  the ``.state``-read scan correctly ignores it -- only the ``FileState`` RHS is the violation).
* ``#4`` ``.state`` read/write off a FileRecord-shaped base, matched BASE-KIND-AGNOSTICALLY (WR-01): a bare
  ``ast.Name`` bound to a ``FileRecord`` -- either via a ``FileRecord``-textual RHS OR the two-step ORM
  row-fetch idiom ``file_record = result.scalar_one_or_none()`` the deleted ``store_proposals`` used -- OR ANY
  ``ast.Attribute`` chain such as ``proposal.file.state`` (the exact chained-attribute cascade Plan 01 deleted
  from ``update_proposal_status``, whose base ``proposal.file`` is an ``ast.Attribute``, not a ``ast.Name``).
  Keying stays on ``.attr == "state"`` only, so ``.status`` / ``.file_state`` / ``.id`` never fire.
* ``#5`` ``getattr(file, "state")`` -- the reflective read.
* ``#6`` ``FileRecord.state`` passed **positionally** into ``.where(a, b, <read>)`` -- the Phase-83
  blind spot. The scan walks BOTH ``Call.args`` and ``Call.keywords`` of the ``.where``-family funcs,
  so a positional read is not missed.

Why this is an ``ast.walk`` scan and NEVER a line-oriented ``grep`` (Pitfall 1 / project memory
``feedback_mutation_test_guard_tests`` -- Phase 83 shipped two *toothless* guards):

1. All three modules import and USE ``FileRecord`` (``FileRecord.id`` binds, ``select(FileRecord)``);
   a ``grep`` for ``FileRecord`` would drown in legitimate ``.id`` reads. The AST scan keys ONLY on the
   ``.state`` attribute.
2. The apply-PATCH echoes ``body.file_state`` and reads ``proposal.status`` -- ``.attr`` is ``"status"``
   / a ``file_state`` param, NOT the ``"state"`` attribute. A sloppy substring grep (``state``) would
   false-positive on every one. The AST scan keys on the EXACT attribute name, so those never fire.
3. Both real modules describe the RETIRED ``FileRecord.state`` cascade in their docstrings/comments
   (``agent_proposals.py`` mentions ``FileRecord.state`` twice in prose); an AST attribute scan does not
   see docstring text, so a line grep would false-positive here -- precisely Pitfall 1.

The negative tests below (``test_guard_flags_*`` / ``test_guard_ignores_*``) are the mutation directions
encoded permanently and hermetically -- they mutate crafted source STRINGS, never the real files, so this
test is DB-free and leaves no source dirty. The executor ALSO exercised the mutation discipline against
the real files (inject form #N -> guard RED -> restore); see 86-03-SUMMARY.md.
"""

from __future__ import annotations

import ast
from pathlib import Path


# This file is ``tests/shared/test_proposals_cutover_source_scan.py`` -- the SAME depth as the template
# ``test_reenqueue_reconcile_source_scan.py`` -- so parents[2] is the repo root. (PATTERNS.md's
# ``parents[1]`` is an ERROR; it assumed a deeper file. The existence asserts below fail loudly on a
# wrong root instead of silently scanning nothing.)
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "phaze"
_PROPOSAL = _SRC_ROOT / "services" / "proposal.py"
_PROPOSAL_QUERIES = _SRC_ROOT / "services" / "proposal_queries.py"
_AGENT_PROPOSALS = _SRC_ROOT / "routers" / "agent_proposals.py"

# Fail loud on a wrong repo root: a mis-resolved path would scan nothing and be silently, uselessly GREEN
# (the toothless-guard anti-goal, T-86-06 / feedback_mutation_test_guard_tests).
assert _PROPOSAL.exists(), _PROPOSAL
assert _PROPOSAL_QUERIES.exists(), _PROPOSAL_QUERIES
assert _AGENT_PROPOSALS.exists(), _AGENT_PROPOSALS

# SQLAlchemy read-clause entry points. A ``FileRecord.state`` / ``FileState.<member>`` inside ANY
# argument of one of these is a read of ``FileRecord.state`` (the exact thing the cutover removed).
_WHERE_FUNCS = frozenset({"where", "filter", "filter_by", "having"})

# SQLAlchemy Result row-fetch idioms. A local assigned from one of these (``file_record =
# result.scalar_one_or_none()``) holds a ``FileRecord`` ORM row even though its assignment RHS never
# textually mentions ``FileRecord`` -- the exact two-step idiom the deleted ``store_proposals`` code used
# (WR-01, compounding factor 1). ``_orm_row_bound_names`` treats such a local as FileRecord-bound so a
# ``file_record.state`` write/read off it is caught by the base-kind-agnostic scanners below.
_ROW_FETCH_METHODS = frozenset({"scalar_one_or_none", "scalar_one", "scalar", "first", "one_or_none", "one"})


def _filerecord_bound_names(tree: ast.AST) -> set[str]:
    """Names that resolve to a ``FileRecord`` (the model class + any local assigned from a FileRecord expr).

    Always includes ``"FileRecord"`` itself. Additionally collects any ``name`` bound by an assignment
    whose RHS references ``FileRecord`` (e.g. ``file = (await ...select(FileRecord)...).scalar_one()``),
    so a ``file.state`` read/write off that local (form #4) is caught. Deliberately conservative-broad: it
    can over-include a Select-bound local (``stmt = select(FileRecord.id)``), but that only makes a spurious
    ``stmt.state`` read *more* likely to be flagged -- strictly stronger, and never a false-positive on a
    legitimate ``.status`` / ``.id`` read (this scan keys ONLY on ``.attr == "state"``).
    """
    names = {"FileRecord"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(sub, ast.Name) and sub.id == "FileRecord" for sub in ast.walk(node.value)):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _orm_row_bound_names(tree: ast.AST) -> set[str]:
    """Names bound from a SQLAlchemy Result row-fetch idiom (``x = result.scalar_one_or_none()`` etc.).

    Closes WR-01 compounding factor 1: the deleted ``store_proposals`` code obtained its ``FileRecord``
    row via the two-step idiom ``result = await session.execute(select(FileRecord)...)`` then
    ``file_record = result.scalar_one_or_none()``. ``file_record``'s assignment RHS references ``result``,
    NOT ``FileRecord`` textually, so ``_filerecord_bound_names`` never binds it and a re-added
    ``file_record.state = "moved"`` write off it evades the bare-Name scanners. Any local assigned from a
    call whose method is one of ``_ROW_FETCH_METHODS`` (``.scalar_one_or_none()`` / ``.scalar_one()`` /
    ``.scalar()`` / ``.first()`` / ``.one_or_none()`` / ``.one()``) is therefore treated as a FileRecord-bound
    row. Deliberately conservative-broad (it also binds locals fetched from a non-FileRecord select), but
    that only makes a spurious ``x.state`` read/write *more* likely to be flagged -- strictly stronger, and
    never a false-positive on ``.status`` / ``.id`` because every scanner keys ONLY on ``.attr == "state"``.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in _ROW_FETCH_METHODS for sub in ast.walk(node.value)
        ):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _state_reads(tree: ast.AST) -> list[ast.Attribute]:
    """Every ``.state`` attribute READ off a FileRecord-shaped base (forms #1, #2, #4, #6).

    Matches ``ast.Attribute`` nodes whose ``.attr`` is ``"state"`` and whose context is ``Load`` (a READ --
    never the ``Store`` target of ``f.state = ...``). Base-kind-agnostic per WR-01: the ``.state`` is flagged
    when its base is EITHER (a) a bare ``ast.Name`` bound to a ``FileRecord`` (via ``_filerecord_bound_names``
    OR an ORM row-fetch idiom, ``_orm_row_bound_names``), OR (b) ANY ``ast.Attribute`` chain -- e.g.
    ``proposal.file.state``, the exact chained-attribute shape Plan 01 deleted from ``update_proposal_status``,
    whose base is ``proposal.file`` (an ``ast.Attribute``, NOT a ``ast.Name``) and so evaded the old
    bare-Name-only scan. Keying stays strictly on ``.attr == "state"`` -- ``.status`` / ``.file_state`` /
    ``.id`` are never flagged, and the three clean source files have zero ``.state`` attribute nodes so this
    broadening stays false-positive-free. Walks the WHOLE tree, so a read nested anywhere -- inside
    ``.in_(...)``, an ``ast.Compare``, or a POSITIONAL ``.where`` arg (the Phase-83 blind spot) -- is reached,
    because ``ast.walk`` descends into both ``Call.args`` and ``Call.keywords``.
    """
    bound = _filerecord_bound_names(tree) | _orm_row_bound_names(tree)
    hits: list[ast.Attribute] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "state" and isinstance(node.ctx, ast.Load)):
            continue
        base = node.value
        if isinstance(base, ast.Attribute) or (isinstance(base, ast.Name) and base.id in bound):
            hits.append(node)
    return hits


def _state_writes(tree: ast.AST) -> list[ast.Attribute]:
    """Every ``.state`` attribute WRITE off a FileRecord-shaped base (form #4's Store side).

    Plan 01/02 removed exactly these: the apply-PATCH ``file_record.state = FileState.MOVED`` mirror write,
    the ``update_proposal_status`` chained ``proposal.file.state = FileState.APPROVED.value`` cascade, and the
    two-step-ORM-idiom ``file_record = result.scalar_one_or_none(); file_record.state = "moved"`` write in
    ``store_proposals``. Base-kind-agnostic per WR-01: a ``Store``-context ``.state`` attribute is flagged when
    its base is EITHER (a) a bare ``ast.Name`` bound to a ``FileRecord`` (``_filerecord_bound_names`` OR the
    ORM row-fetch idiom ``_orm_row_bound_names`` -- so the two-step idiom whose RHS references ``result``, not
    ``FileRecord`` textually, is no longer invisible), OR (b) ANY ``ast.Attribute`` chain (``proposal.file.state``,
    whose base ``proposal.file`` is an ``ast.Attribute``). Keying stays strictly on ``.attr == "state"``; the
    three clean source files have zero ``.state`` attribute nodes, so this stays false-positive-free.
    """
    bound = _filerecord_bound_names(tree) | _orm_row_bound_names(tree)
    hits: list[ast.Attribute] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "state" and isinstance(node.ctx, ast.Store)):
            continue
        base = node.value
        if isinstance(base, ast.Attribute) or (isinstance(base, ast.Name) and base.id in bound):
            hits.append(node)
    return hits


def _filestate_occurrences(tree: ast.AST) -> list[ast.Attribute]:
    """Every ``FileState.<member>`` attribute access (form #2's RHS + form #3, the removed write).

    A docstring/comment mention of a state name is NOT an ``ast.Attribute`` and is therefore invisible
    here -- the whole point of an AST scan over a line scan (Pitfall 1).
    """
    return [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "FileState"]


def _getattr_state_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``getattr(_, "state")`` call (form #5, the reflective read).

    Keys on a ``getattr`` call whose SECOND positional arg is the constant ``"state"``. ``getattr(job,
    "status", None)`` has a ``"status"`` constant, so it is never flagged.
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
    keyword ``.filter_by(state=FileState.X)`` form -- the two Phase-83 blind spots. This is a
    SUPPLEMENTARY structural check: the aggregate guard already catches these reads via the whole-tree
    scans, but this walker exists to make the args+keywords coverage explicit and directly testable
    (see ``test_where_walker_*``).
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
    """All distinct ``FileRecord.state`` read/write / ``FileState.<member>`` / ``getattr(_,"state")`` occurrences.

    The union of the whole-tree scans, deduplicated by node identity (form #2 is found by both the
    ``.state``-read scan AND the ``FileState`` scan on distinct nodes; that is two occurrences, correct).
    Clean absence == an empty list.
    """
    tree = ast.parse(source)
    nodes: list[ast.AST] = [
        *_state_reads(tree),
        *_state_writes(tree),
        *_filestate_occurrences(tree),
        *_getattr_state_calls(tree),
    ]
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
# The real-source guards (the invariant this test exists to protect)
# ---------------------------------------------------------------------------


def test_proposal_py_has_zero_state_writes() -> None:
    """``services/proposal.py``: ZERO ``FileRecord.state`` / ``FileState`` / ``getattr(_,"state")`` occurrences.

    Plan 01 deleted the ``store_proposals`` ``_TERMINAL_FILE_STATES`` frozenset and the file-load-and-guard
    block (D-01 site 1); ``proposals.status`` is the sole authority. Clean absence -- no allow-list.
    """
    violations = _violations(_PROPOSAL.read_text(encoding="utf-8"))
    assert violations == [], (
        f"services/proposal.py reintroduced a FileRecord.state write/read / FileState reference "
        f"({len(violations)} found) at lines {_lines(violations)}. Plan 01 removed the store_proposals "
        "file.state cascade (D-01 site 1); proposals.status is the sole review-decision authority. "
        "No FileRecord.state read/write, no FileState.<member>, and no getattr(_, 'state') is permitted."
    )


def test_proposal_queries_has_zero_state_writes() -> None:
    """``services/proposal_queries.py``: ZERO ``FileRecord.state`` / ``FileState`` / ``getattr(_,"state")`` occurrences.

    Plan 01 removed the ``update_proposal_status`` ``.file.state`` cascade and the ``bulk_update_status``
    ``update(FileRecord).values(state=...)`` write (D-01 sites 2, 3). Clean absence -- no allow-list.
    """
    violations = _violations(_PROPOSAL_QUERIES.read_text(encoding="utf-8"))
    assert violations == [], (
        f"services/proposal_queries.py reintroduced a FileRecord.state write/read / FileState reference "
        f"({len(violations)} found) at lines {_lines(violations)}. Plan 01 removed the APPROVED/REJECTED "
        "file.state limbs and the update(FileRecord).values(state=...) write (D-01 sites 2, 3). "
        "No FileRecord.state read/write, no FileState.<member>, and no getattr(_, 'state') is permitted."
    )


def test_agent_proposals_has_zero_state_writes() -> None:
    """``routers/agent_proposals.py``: ZERO ``FileRecord.state`` / ``FileState`` / ``getattr(_,"state")`` occurrences.

    Plan 02 removed the apply-PATCH ``file_record.state = FileState.MOVED`` write and the idempotent-replay
    read (D-01 sites 4, 5); the response ``file_state`` now echoes ``body.file_state``. The two remaining
    ``FileRecord.state`` mentions are docstring PROSE -- invisible to an AST attribute scan. Clean absence.
    """
    violations = _violations(_AGENT_PROPOSALS.read_text(encoding="utf-8"))
    assert violations == [], (
        f"routers/agent_proposals.py reintroduced a FileRecord.state write/read / FileState reference "
        f"({len(violations)} found) at lines {_lines(violations)}. Plan 02 removed the apply-PATCH "
        "file_record.state write and the replay read (D-01 sites 4, 5); the response echoes "
        "body.file_state. No FileRecord.state read/write, no FileState.<member>, no getattr(_, 'state')."
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
    """MUTATION #2 (RED): ``.where(FileRecord.state == FileState.MOVED)`` -- BOTH halves are flagged.

    The ``FileRecord.state`` READ and the ``FileState.MOVED`` RHS are two distinct violations (the
    ``.state``-read scan + the ``FileState`` occurrence scan), so the ``ast.Compare`` form yields 2.
    """
    source = "q = select(FileRecord).where(FileRecord.state == FileState.MOVED)\n"
    violations = _violations(source)
    assert len(violations) == 2
    attrs = {getattr(n, "attr", None) for n in violations}
    assert attrs == {"state", "MOVED"}


def test_guard_flags_removed_values_write() -> None:
    """MUTATION #3 (RED): the removed ``update(FileRecord).where(...).values(state=FileState.X)`` write is caught.

    The ``state=`` keyword is a PARAMETER NAME, not an ``ast.Attribute``, so the ``.state``-read scan
    correctly ignores it; the ``FileState.MOVED`` RHS is the single flagged violation. (proposal_queries.py
    retired exactly this write in ``bulk_update_status``.)
    """
    source = "stmt = update(FileRecord).where(FileRecord.id == fid).values(state=FileState.MOVED)\n"
    violations = _violations(source)
    assert len(violations) == 1
    assert isinstance(violations[0], ast.Attribute)
    assert violations[0].attr == "MOVED"


def test_guard_flags_instance_state_write() -> None:
    """MUTATION #4 (RED): ``file_record.state = FileState.MOVED`` off a FileRecord-bound local is caught.

    This is the exact apply-PATCH mirror write Plan 02 removed. ``file_record`` is bound from an expression
    referencing ``FileRecord`` (``session.get(FileRecord, fid)``), so ``_filerecord_bound_names`` recognises
    it; both the Store-context ``.state`` write AND the ``FileState.MOVED`` RHS are flagged (2 violations).
    """
    source = "file_record = session.get(FileRecord, fid)\nfile_record.state = FileState.MOVED\n"
    violations = _violations(source)
    assert len(violations) == 2
    attrs = {getattr(n, "attr", None) for n in violations}
    assert attrs == {"state", "MOVED"}


def test_guard_flags_instance_state_read() -> None:
    """MUTATION #4b (RED): ``held = file.state`` off a FileRecord-bound local is caught (the replay read)."""
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
    source = "stmt = select(FileRecord.id).where(\n    FileRecord.sha256_hash == h,\n    FileRecord.state != FileState.MOVED,\n)\n"
    violations = _violations(source)
    # FileRecord.state (read) + FileState.MOVED (occurrence) = 2.
    assert len(violations) == 2
    assert {getattr(n, "attr", None) for n in violations} == {"state", "MOVED"}


def test_guard_flags_chained_attr_string_write() -> None:
    """MUTATION #4c (RED / WR-01): ``proposal.file.state = "approved"`` -- the chained-attribute cascade.

    Re-encodes the EXACT literal shape Plan 01 deleted from ``update_proposal_status``
    (``proposal.file.state = FileState.APPROVED.value``, reintroduced here in its string-value form). The
    assignment target is ``Attribute(value=Attribute(value=Name('proposal'), attr='file'), attr='state')`` --
    its base is an ``ast.Attribute`` (``proposal.file``), NOT a bare ``ast.Name``, so the OLD bare-Name-only
    scanner returned ``[]`` (the WR-01 blind spot). There is NO ``FileState`` node here, so coverage rests
    ENTIRELY on the base-kind-agnostic ``.state``-write scan added in Task 1. Mutation-verified RED->GREEN
    (revert the broadening -> ``_violations`` returns ``[]`` -> this asserts fail); see 86-05-SUMMARY.md.
    """
    source = 'proposal.file.state = "approved"\n'
    violations = _violations(source)
    assert violations != []
    writes = [n for n in violations if isinstance(n, ast.Attribute) and n.attr == "state" and isinstance(n.ctx, ast.Store)]
    assert len(writes) == 1
    assert isinstance(writes[0].value, ast.Attribute)  # the chained ``proposal.file`` base


def test_guard_flags_two_step_orm_idiom_write() -> None:
    """MUTATION #4d (RED / WR-01 factor 1): the two-step ORM idiom ``store_proposals`` itself used.

    Re-encodes ``file_record = result.scalar_one_or_none()`` then ``file_record.state = "moved"`` -- the exact
    idiom the deleted ``store_proposals`` code used. ``file_record``'s binding RHS is ``result.scalar_one_or_none()``,
    which does NOT textually contain ``FileRecord``, so the OLD ``_filerecord_bound_names`` never bound it and
    the write was invisible. This proves the ``_orm_row_bound_names`` binding added in Task 1 has teeth: the
    bare-Name base ``file_record`` is recognised as a FileRecord row via the row-fetch idiom. Mutation-verified
    RED->GREEN (revert the broadening -> ``_violations`` returns ``[]``); see 86-05-SUMMARY.md.
    """
    source = 'file_record = result.scalar_one_or_none()\nfile_record.state = "moved"\n'
    violations = _violations(source)
    assert violations != []
    writes = [n for n in violations if isinstance(n, ast.Attribute) and n.attr == "state" and isinstance(n.ctx, ast.Store)]
    assert len(writes) == 1
    assert isinstance(writes[0].value, ast.Name)  # bare ``file_record`` bound via the ORM row-fetch idiom


# ---------------------------------------------------------------------------
# Explicit Call.args + Call.keywords coverage (the Phase-83 blind-spot closure)
# ---------------------------------------------------------------------------


def test_where_walker_catches_positional() -> None:
    """The ``.where``-family walker inspects POSITIONAL ``Call.args`` (not just chained/keyword forms)."""
    tree = ast.parse("stmt = select(FileRecord.id).where(FileRecord.sha256 == h, FileRecord.state != FileState.MOVED)\n")
    hits = _where_family_arg_violations(tree)
    # The positional FileRecord.state read AND the FileState.MOVED occurrence are both reached.
    assert {h.attr for h in hits} == {"state", "MOVED"}


def test_where_walker_catches_keyword() -> None:
    """The ``.where``-family walker inspects ``Call.keywords`` (e.g. ``.filter_by(state=FileState.X)``)."""
    tree = ast.parse("q = session.query(FileRecord).filter_by(state=FileState.MOVED)\n")
    hits = _where_family_arg_violations(tree)
    assert any(h.attr == "MOVED" for h in hits)


# ---------------------------------------------------------------------------
# GREEN false-positive checks (the guard must NOT over-fire)
# ---------------------------------------------------------------------------


def test_guard_ignores_proposal_status_read() -> None:
    """GREEN false-positive check: a legitimate ``proposal.status`` read (``.attr == "status"``) is NOT flagged.

    The cutover moved authority to ``proposals.status``; the modules read ``proposal.status`` and
    ``getattr(proposal, "status", None)`` throughout. ``"status"`` is not ``"state"``, so neither the
    ``.state``-read scan nor the ``getattr("state")`` scan fires. This is the exact substring trap a sloppy
    grep(``state``) would fall into.
    """
    source = (
        "if proposal.status == ProposalStatus.EXECUTED.value:\n"
        "    counter = getattr(proposal, 'status', None) or {}\n"
        "    stmt = select(RenameProposal).where(RenameProposal.status.in_([s.value for s in APPLIED]))\n"
    )
    assert _violations(source) == []


def test_guard_ignores_body_file_state_echo() -> None:
    """GREEN false-positive check: the apply-PATCH ``body.file_state`` echo is NOT flagged.

    Plan 02's contract-preserving change echoes ``body.file_state`` into the response. ``.attr`` is
    ``"file_state"`` (and the base is ``body``, not a FileRecord-bound name), so the ``.state``-read scan
    -- which keys on the EXACT attribute ``"state"`` off a FileRecord-bound name -- stays silent.
    """
    source = "response_file_state = body.file_state\nfile_record.current_path = body.current_path\n"
    assert _violations(source) == []


def test_guard_ignores_filerecord_id_read() -> None:
    """GREEN false-positive check: ``FileRecord.id`` reads (the row-scope idiom) are NOT flagged.

    All three modules bind and read ``FileRecord.id`` heavily (``select(FileRecord.id).where(FileRecord.id
    == ...)``); ``.attr == "id"`` is never ``"state"``, so the scan stays silent.
    """
    source = "stmt = select(FileRecord.id).where(FileRecord.id == func.any(bindparam('ids')))\n"
    assert _violations(source) == []


def test_guard_ignores_state_mention_in_docstring() -> None:
    """GREEN false-positive check: a prose ``FileRecord.state`` / ``FileState`` mention is NOT an occurrence.

    ``agent_proposals.py`` describes the RETIRED ``FileRecord.state`` cascade in its module docstring; an
    AST attribute scan does not see docstring text (a line grep would false-positive here -- Pitfall 1).
    """
    source = '"""The proposal->FileRecord.state cascade (mirror into FileState.MOVED) was removed in Phase 86."""\n'
    assert _violations(source) == []
