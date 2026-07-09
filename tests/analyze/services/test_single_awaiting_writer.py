"""Anti-drift source guard (D-02): ``hold_awaiting_cloud`` is the SINGLE writer of ``cloud_job.status='awaiting'``.

Phase 83's LOCKED **D-02** requires ONE awaiting writer, reused by the hold path and both over-cap spill
paths -- not three hand-written copies. 83-07 consolidated the two inline spill CAS re-stamps into
``services.backends.hold_awaiting_cloud``. This hermetic (no-DB) AST scan makes that invariant
self-enforcing: it goes RED the moment ANY module under ``src/phaze/`` re-introduces an inline awaiting
WRITE (a ``.values(status=CloudJobStatus.AWAITING...)`` / ``.values(status="awaiting")`` on a SQLAlchemy
insert/update statement) outside ``services/backends.py``.

Because the check keys on ``.values(...)`` -- a WRITE -- the drain / count-card / shadow-invariant / D-14
reaper predicates that merely READ awaiting via ``.where(CloudJob.status == AWAITING...)`` (or the reaper's
``delete(...).where(...)``) are correctly NOT flagged: a WHERE clause is not a ``.values(...)`` call.
"""

from __future__ import annotations

import ast
from pathlib import Path


# Repo-root-relative source root (mirror the sibling migration tests' parents[3] idiom): this file is
# tests/analyze/services/test_single_awaiting_writer.py, so parents[3] is the repo root.
_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "phaze"

# The SOLE module allowed to WRITE cloud_job.status='awaiting' (the single go-forward writer, D-01/D-02).
_ALLOWED_WRITERS = {_SRC_ROOT / "services" / "backends.py"}


def _references_awaiting(node: ast.AST) -> bool:
    """True iff an AST subtree references ``CloudJobStatus.AWAITING`` or the literal ``"awaiting"``."""
    for sub in ast.walk(node):
        # CloudJobStatus.AWAITING(.value) -> an Attribute whose attr == "AWAITING".
        if isinstance(sub, ast.Attribute) and sub.attr == "AWAITING":
            return True
        # A bare string literal "awaiting".
        if isinstance(sub, ast.Constant) and sub.value == "awaiting":
            return True
    return False


def _dict_writes_awaiting(node: ast.Dict) -> bool:
    """True iff a dict literal maps the ``"status"`` key to an awaiting value."""
    return any(
        isinstance(key, ast.Constant) and key.value == "status" and _references_awaiting(value)
        for key, value in zip(node.keys, node.values, strict=False)
        if key is not None
    )


def _name_binds_awaiting_status(tree: ast.AST, name: str) -> bool:
    """True iff ``name`` is ever bound to a dict that carries an awaiting ``status`` entry.

    Covers both the dict-literal binding (``vals = {"status": AWAITING}``) and the subscript
    mutation (``vals["status"] = AWAITING``) a drifter could reach for.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            # vals = {"status": <awaiting>}
            if (
                isinstance(node.value, ast.Dict)
                and _dict_writes_awaiting(node.value)
                and any(isinstance(t, ast.Name) and t.id == name for t in targets)
            ):
                return True
            # vals["status"] = <awaiting>
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == name
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "status"
                    and _references_awaiting(node.value)
                ):
                    return True
        # vals: dict[str, Any] = {"status": <awaiting>}
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and isinstance(node.value, ast.Dict)
            and _dict_writes_awaiting(node.value)
        ):
            return True
    return False


def _targets_cloud_job(call: ast.Call) -> bool:
    """True iff a ``.values(...)`` call's statement chain targets the ``CloudJob`` model."""
    return any(isinstance(sub, ast.Name) and sub.id == "CloudJob" for sub in ast.walk(call))


def _values_call_writes_awaiting(call: ast.Call, tree: ast.AST) -> bool:
    """True iff a ``.values(...)`` call writes ``status='awaiting'`` — keyword OR ``**splat`` form."""
    for keyword in call.keywords:
        # (a) the literal keyword form: .values(status=CloudJobStatus.AWAITING.value, ...)
        if keyword.arg == "status" and _references_awaiting(keyword.value):
            return True
        if keyword.arg is not None:
            continue
        # (b/c) the **splat form: .values(**vals). `keyword.arg is None` means `**`, and the
        # shipped writer itself uses this idiom (backends.py `.values(**values)`) -- so a
        # copy-pasted inline spill would too. A keyword-only scan is BLIND to it (found by
        # 83-07 review WR-01; confirmed by mutation test).
        if isinstance(keyword.value, ast.Dict) and _dict_writes_awaiting(keyword.value):
            return True
        if isinstance(keyword.value, ast.Name) and _name_binds_awaiting_status(tree, keyword.value.id):
            return True
        # (c) an UNRESOLVABLE splat (e.g. `.values(**build_row())`). Flag it only when the
        # statement targets CloudJob AND the module knows about AWAITING at all -- otherwise
        # every unrelated `.values(**row)` (services/proposal.py) would false-positive.
        if not isinstance(keyword.value, ast.Dict | ast.Name) and _targets_cloud_job(call) and _references_awaiting(tree):
            return True
    return False


def _file_writes_awaiting(path: Path) -> bool:
    """True iff the module writes ``cloud_job.status='awaiting'`` via a ``.values(...)`` call."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # Match ``<stmt>.values(...)`` calls (covers both pg_insert(CloudJob).values(...) and
        # update(CloudJob).where(...).values(...)); a WHERE/DELETE predicate is not a .values(...) call.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "values"
            and _values_call_writes_awaiting(node, tree)
        ):
            return True
    return False


def test_hold_awaiting_cloud_is_the_only_awaiting_writer() -> None:
    """D-02 anti-drift: exactly ``services/backends.py`` writes ``cloud_job.status='awaiting'`` via ``.values(...)``.

    Reverting 83-07 (re-adding an inline ``update(CloudJob).values(status=CloudJobStatus.AWAITING.value...)``
    to ``routers/agent_s3.py`` or ``routers/agent_push.py``) turns this RED: that router file would join the
    writer set, breaking the ``== _ALLOWED_WRITERS`` equality.
    """
    writers = {path for path in _SRC_ROOT.rglob("*.py") if _file_writes_awaiting(path)}

    assert writers == _ALLOWED_WRITERS, (
        "cloud_job.status='awaiting' must be written ONLY by services/backends.hold_awaiting_cloud (D-02). "
        f"Unexpected inline awaiting writers: {sorted(str(p) for p in writers - _ALLOWED_WRITERS)}; "
        f"missing expected writers: {sorted(str(p) for p in _ALLOWED_WRITERS - writers)}"
    )
