"""Static + runtime guard against control-plane default-queue producers (Phase 30 Plan 05).

The v4.0.6 incident: every control-plane (API/UI) enqueue grabbed the unnamed
``app.state.queue`` SAQ Queue, which had *no consumer* — 11,428 ``process_file``
jobs rotted in ``saq:job:default:*``. Plans 01-04 removed every misrouted site and
routed each enqueue through :func:`phaze.services.enqueue_router.resolve_queue_for_task`
(named ``controller`` queue or a per-agent ``phaze-agent-<id>`` queue).

This module locks the fix in so the bug class cannot silently recur:

- A **static scan** walks every ``.py`` under ``src/phaze/routers`` and
  ``src/phaze/services`` and fails (with the offending ``file:line``) if any code
  reintroduces an ``*.state.queue`` attribute access (the removed default attr) or
  constructs an *unnamed* ``Queue.from_url(...)``. The scan parses each file with
  :mod:`ast` rather than matching raw text, so prose mentions inside docstrings /
  comments (e.g. the ``Queue.from_url(...)`` reference in ``agent_task_router``'s
  module docstring) never produce a false positive.
- A **runtime scan** asserts the routing chokepoint itself stays honest: every
  ``CONTROLLER_TASKS`` name routes to the controller queue (agent_id ``None``),
  every ``AGENT_TASKS`` name routes to a ``phaze-agent-<id>`` queue when an active
  agent exists, and any unknown task name raises ``ValueError`` (fail loud, never
  silently default).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from phaze.services.enqueue_router import (
    AGENT_TASKS,
    CONTROLLER_TASKS,
    resolve_queue_for_task,
)
from tests._queue_fakes import seed_active_agent, stub_app_state


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# The two control-plane source trees that must never produce onto the default queue.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCANNED_DIRS = (
    _REPO_ROOT / "src" / "phaze" / "routers",
    _REPO_ROOT / "src" / "phaze" / "services",
)


# ---------------------------------------------------------------------------
# Static guard
# ---------------------------------------------------------------------------


class _ProducerVisitor(ast.NodeVisitor):
    """Collect default-queue producers in a single module's AST.

    Records two offence classes, each as ``(lineno, detail)``:

    - ``default_refs``: any ``<expr>.state.queue`` attribute access — the exact
      removed default attribute (``controller_queue`` / ``task_router`` / ``redis``
      have different ``attr`` names and are intentionally ignored). The visitor
      also catches the **two-step** form where ``app.state`` is first bound to a
      local named ``state`` and the offending attribute is then read as
      ``state.queue`` (``node.value`` is a ``Name`` rather than an ``Attribute``).
    - ``unnamed_queues``: a ``Queue.from_url(...)`` call with no ``name=`` keyword.
    """

    def __init__(self) -> None:
        self.default_refs: list[tuple[int, str]] = []
        self.unnamed_queues: list[tuple[int, str]] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Match `*.state.queue` (the removed default attr), never `*.state.controller_queue`.
        if node.attr == "queue":
            val = node.value
            if isinstance(val, ast.Attribute) and val.attr == "state":
                # Direct form: `*.state.queue`.
                self.default_refs.append((node.lineno, "*.state.queue attribute access"))
            elif isinstance(val, ast.Name) and val.id == "state":
                # Indirect/two-step form: `state = *.app.state` then `state.queue`.
                self.default_refs.append((node.lineno, "state.queue attribute access (possible indirect)"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "from_url"
            and isinstance(func.value, ast.Name)
            and func.value.id == "Queue"
            and not any(kw.arg == "name" for kw in node.keywords)
        ):
            self.unnamed_queues.append((node.lineno, "Queue.from_url(...) without name="))
        self.generic_visit(node)


def _scan_source_files() -> list[str]:
    """Parse every ``.py`` under the scanned dirs; return human-readable offences."""
    offences: list[str] = []
    for directory in _SCANNED_DIRS:
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            visitor = _ProducerVisitor()
            visitor.visit(tree)
            rel = path.relative_to(_REPO_ROOT)
            for lineno, detail in (*visitor.default_refs, *visitor.unnamed_queues):
                offences.append(f"{rel}:{lineno}: {detail}")
    return offences


def test_no_default_queue_producers_in_routers_or_services() -> None:
    """No router/service reintroduces ``*.state.queue`` or an unnamed ``Queue.from_url``.

    The named ``controller`` queue lives in the lifespan (``src/phaze/main.py``, out
    of scan scope) and per-agent queues are built *named* inside
    ``AgentTaskRouter._queue_for`` — both pass. Any regression that grabs the default
    queue or builds an unnamed one fails here with its exact ``file:line``.
    """
    offences = _scan_source_files()
    assert offences == [], "default-queue producers reintroduced:\n" + "\n".join(offences)


def test_static_guard_would_catch_a_reintroduced_producer() -> None:
    """Meta-test: the AST visitor flags both offence classes on a crafted sample.

    Proves the guard above is not vacuously green — if a future edit reintroduced
    either pattern, the visitor records it (so :func:`_scan_source_files` would fail).
    """
    sample = "def boom(request):\n    q = request.app.state.queue\n    return Queue.from_url(url)\n"
    visitor = _ProducerVisitor()
    visitor.visit(ast.parse(sample))

    assert [lineno for lineno, _ in visitor.default_refs] == [2]
    assert [lineno for lineno, _ in visitor.unnamed_queues] == [3]


def test_static_guard_catches_two_step_state_queue_access() -> None:
    """Meta-test: the visitor also flags the two-step ``state = app.state; state.queue`` form.

    The single-expression form (``request.app.state.queue``) is an ``Attribute``
    whose value is itself an ``Attribute`` (``...state``). Binding ``app.state`` to
    a local first produces a ``Name`` node at the ``.queue`` access site, which the
    direct check misses. This proves that gap is now closed.
    """
    sample = "def boom(request):\n    state = request.app.state\n    return state.queue\n"
    visitor = _ProducerVisitor()
    visitor.visit(ast.parse(sample))

    # The offending `state.queue` read is on line 3 of the sample.
    assert [lineno for lineno, _ in visitor.default_refs] == [3]


def test_static_guard_allows_named_queue_construction() -> None:
    """A *named* ``Queue.from_url(..., name=...)`` is allowed (per-agent queues)."""
    sample = 'q = Queue.from_url(url, name="phaze-agent-nox")\n'
    visitor = _ProducerVisitor()
    visitor.visit(ast.parse(sample))

    assert visitor.unnamed_queues == []
    assert visitor.default_refs == []


# ---------------------------------------------------------------------------
# Runtime guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_controller_task_routes_to_controller_queue() -> None:
    """Every CONTROLLER_TASKS name resolves to the controller queue with agent_id None."""
    app_state = stub_app_state()

    for task_name in sorted(CONTROLLER_TASKS):
        routed = await resolve_queue_for_task(task_name, app_state, None)
        assert routed.queue is app_state.controller_queue, task_name
        assert routed.agent_id is None, task_name


@pytest.mark.asyncio
async def test_every_agent_task_routes_to_per_agent_queue(session: AsyncSession) -> None:
    """Every AGENT_TASKS name resolves to the active agent's phaze-agent-<id> queue."""
    agent = await seed_active_agent(session)
    app_state = stub_app_state()

    for task_name in sorted(AGENT_TASKS):
        routed = await resolve_queue_for_task(task_name, app_state, session)
        assert routed.agent_id == agent.id, task_name
        assert routed.queue.name == f"phaze-agent-{agent.id}", task_name


@pytest.mark.asyncio
async def test_unknown_task_raises_value_error() -> None:
    """An unknown task name fails loud — it must never return the default queue."""
    app_state = stub_app_state()

    with pytest.raises(ValueError, match="unroutable task"):
        await resolve_queue_for_task("definitely_not_a_task", app_state, None)


# ---------------------------------------------------------------------------
# Phase 55 (KROUTE-04): the k8s enqueue site is routed; the k8s backfill is bounded
#
# Two properties lock the no-over-enqueue invariant for the Phase-55 k8s seam:
# 1. The k8s post-staging enqueue (``submit_cloud_job`` in ``report_uploaded``) is a known
#    CONTROLLER_TASK routed through ``resolve_queue_for_task`` -- the static scan above already
#    fails on any raw ``*.state.queue`` enqueue introduced in ``routers/`` (which includes
#    ``agent_s3.py``), so a raw/default-queue regression there is caught.
# 2. The "Backfill to K8s" candidate query is the bounded ledger-scoped filter
#    (ANALYSIS_FAILED ∧ duration ∧ EXISTS scheduling_ledger), NOT a bare ``state ==
#    ANALYSIS_FAILED`` whole-backlog sweep -- the v4.0.6 / v5.0 over-enqueue incident class.
# ---------------------------------------------------------------------------


_SERVICES_PIPELINE = _REPO_ROOT / "src" / "phaze" / "services" / "pipeline.py"


def _backfill_candidates_stmt_source() -> str:
    """Return the source segment of ``_backfill_candidates_stmt`` in services/pipeline.py.

    Parses the module with :mod:`ast` (so a future rename/move is caught by the lookup failing)
    and returns the exact function source for the membership assertions below.
    """
    source = _SERVICES_PIPELINE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SERVICES_PIPELINE))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_backfill_candidates_stmt":
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("_backfill_candidates_stmt not found in src/phaze/services/pipeline.py")


def test_submit_cloud_job_is_a_routed_controller_task() -> None:
    """KROUTE-04: ``submit_cloud_job`` (the k8s post-staging enqueue) is a known CONTROLLER_TASK.

    ``report_uploaded`` enqueues ``submit_cloud_job`` through ``resolve_queue_for_task``; the static
    scan fails on any raw ``*.state.queue`` enqueue in ``routers/``. This pins the positive side:
    ``submit_cloud_job`` is routable, so it can never silently fall through to the default queue.
    """
    assert "submit_cloud_job" in CONTROLLER_TASKS


@pytest.mark.asyncio
async def test_submit_cloud_job_routes_to_controller_queue() -> None:
    """KROUTE-04: ``submit_cloud_job`` resolves to the controller queue with agent_id None."""
    app_state = stub_app_state()

    routed = await resolve_queue_for_task("submit_cloud_job", app_state, None)
    assert routed.queue is app_state.controller_queue
    assert routed.agent_id is None


def test_k8s_backfill_query_is_ledger_scoped_not_whole_backlog() -> None:
    """KROUTE-04 / L4: the backfill candidate query is the bounded ledger-scoped filter.

    A static guard over the real ``_backfill_candidates_stmt`` source: it must filter on
    ``ANALYSIS_FAILED`` AND the duration threshold AND an ``EXISTS`` predicate against
    ``SchedulingLedger`` -- i.e. it is NOT a bare ``state == ANALYSIS_FAILED`` whole-backlog
    sweep. A future edit that drops the ledger ``EXISTS`` predicate (re-opening the v4.0.6 / v5.0
    over-enqueue class) fails here.
    """
    src = _backfill_candidates_stmt_source()

    assert "FileState.ANALYSIS_FAILED" in src  # the failure filter
    assert "FileMetadata.duration" in src  # the duration threshold bound
    # the ledger-scoped EXISTS predicate -- the property that makes this NOT a whole-backlog sweep
    assert "exists(" in src
    assert "SchedulingLedger" in src
