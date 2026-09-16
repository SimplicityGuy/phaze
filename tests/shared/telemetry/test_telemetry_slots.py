"""The bounded worker-slot identity: its validation, its pool, and its BOUND.

phaze-21nnf. Two concurrent analysis children exporting cumulative counters under one
``service.instance.id`` overwrite each other at the collector -- measured against
``otel/opentelemetry-collector-contrib`` 0.140.0, the exposed series DECREASED and
``increase()`` over-counted by 84.4%
(``docs/telemetry/concurrent-identity.md``).

**Most of this file is about the BOUND rather than about the fix**, and that is deliberate.
Separating concurrent producers is easy -- a pid would do it. Separating them without
minting a series block per analyzed file is the hard half and the only reason a slot index
was chosen over the per-pod id ADR-0017 rejected, so the tests that fail loudest here are
the ones guarding the ceiling: the drift pin against ``worker_process_pool_size``, and the
refusal of an out-of-range slot.
"""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest

from phaze.config_base import BaseSettings
from phaze.telemetry import bootstrap, slots


@pytest.fixture(autouse=True)
def _clean_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (slots.SLOT_ENV, slots.SLOT_MAX_ENV, bootstrap.INSTANCE_ENV):
        monkeypatch.delenv(name, raising=False)
    slots._reset_for_tests()


def test_the_default_bound_matches_the_worker_pool_default() -> None:
    """THE DRIFT PIN, and the most valuable assertion in this file.

    ``DEFAULT_SLOT_MAX`` is a cardinality ceiling and ``worker_process_pool_size`` is the
    concurrency that ceiling has to cover. They are two numbers in two modules, so nothing
    but this test stops them parting company -- and the failure they would produce is the
    silent kind: raise the pool to 8 and children 5-8 quietly fall back to the shared
    identity, reinstating exactly the merge this module exists to prevent, with a green
    suite and correct-looking dashboards.

    A comment saying "keep these in sync" is what this replaces. The bound is not a style
    preference; it is the number a shared Prometheus is sized against.
    """
    assert BaseSettings.model_fields["worker_process_pool_size"].default == slots.DEFAULT_SLOT_MAX


def test_an_absent_slot_resolves_to_none_and_says_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Every role but the analysis child has no slot, and that is not a warning.

    The api, the controller and the watcher run one process each; a line in their boot logs
    about a variable they should never carry would be noise that trains an operator to
    ignore the log. Only a MALFORMED slot warns.
    """
    with caplog.at_level(logging.WARNING, logger="phaze.telemetry.slots"):
        assert slots.resolve_slot({}) is None
        assert slots.resolve_slot({slots.SLOT_ENV: "   "}) is None
    assert caplog.records == []


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("not-a-number", "non-numeric"),
        ("-1", "negative"),
        ("4", "at the exclusive bound"),
        ("11428", "a file count, i.e. the cardinality bomb this refuses"),
    ],
)
def test_a_slot_outside_the_bound_is_refused_and_logged(value: str, reason: str, caplog: pytest.LogCaptureFixture) -> None:
    """FAILS CLOSED, because the two failure directions are not symmetric.

    Refusing the slot degrades this process to the shared identity -- bad, bounded, and
    exactly the behaviour that shipped before the fix. ACCEPTING whatever integer arrived
    would multiply the analysis role's 2,290-series block by however many values were ever
    seen, in a Prometheus phaze does not own, invisibly until it had already been scraped.
    Only one of those is recoverable.

    ``"4"`` is the interesting case: the bound is EXCLUSIVE, so a default pool of 4 hands
    out 0-3 and a ``4`` is evidence something upstream is out of step.
    """
    with caplog.at_level(logging.WARNING, logger="phaze.telemetry.slots"):
        assert slots.resolve_slot({slots.SLOT_ENV: value}) is None, reason
    assert caplog.records, f"a refused slot ({reason}) must say so: a silent fallback is invisible"


def test_a_raised_bound_admits_the_slots_it_covers() -> None:
    """An operator who raises the concurrency can raise the bound with it -- and pays for it.

    This is the knob that makes the scheme deployable at a concurrency above the default
    rather than a hardcoded 4. Its cost is stated where the bound is documented: each extra
    slot is another 2,290-series block at the catalogue ceiling.
    """
    env = {slots.SLOT_ENV: "6", slots.SLOT_MAX_ENV: "8"}
    assert slots.resolve_slot(env) == 6
    assert slots.slot_max(env) == 8


@pytest.mark.parametrize("value", ["nonsense", "0", "-3"])
def test_a_nonsense_bound_falls_back_to_the_default(value: str) -> None:
    """A malformed ceiling must not become NO ceiling, nor a ceiling of zero.

    ``0`` is the sharp one: read literally it admits no slot at all, which would silently
    disable the scheme everywhere. Falling back to the default keeps the fix on and keeps
    the bound real.
    """
    assert slots.slot_max({slots.SLOT_MAX_ENV: value}) == slots.DEFAULT_SLOT_MAX


def test_the_pool_hands_out_distinct_slots_and_takes_them_back() -> None:
    """The whole mechanism in one test: distinct while held, REUSED once released.

    Reuse is what bounds the cardinality. A pool that never reissued a slot would be a
    per-child identity by a slower route, which is the design ADR-0017 rejected.
    """
    pool = slots.SlotPool(3)
    first, second, third = pool.acquire(), pool.acquire(), pool.acquire()
    assert {first, second, third} == {0, 1, 2}

    pool.release(second)
    assert pool.acquire() == second, "a released slot must be the next one out, not a fresh index"


def test_an_exhausted_pool_degrades_instead_of_raising(caplog: pytest.LogCaptureFixture) -> None:
    """Instrumentation may never be the thing that fails the work it observes.

    More concurrent children than slots is a misconfiguration (the pool is sized from the
    semaphore that bounds them), and its correct outcome is a warning plus the pre-fix
    shared identity -- not a raised exception in the middle of spawning an analysis, and not
    a block waiting for a slot that only frees when another multi-hour file finishes.
    """
    pool = slots.SlotPool(1)
    assert pool.acquire() == 0
    with caplog.at_level(logging.WARNING, logger="phaze.telemetry.slots"):
        assert pool.acquire() is None
    assert any("exhausted" in record.getMessage() for record in caplog.records)


def test_releasing_none_or_twice_is_harmless() -> None:
    """The release runs in a ``finally`` reached by success, failure, stall and cancellation.

    ``None`` arrives there whenever the pool was exhausted, and a double release is the
    shape a future refactor of that teardown would produce. Neither may raise over the top
    of an analysis result, and a double release must not put a duplicate in the free list --
    which would hand the SAME slot to two live children, i.e. reinstate the merge.
    """
    pool = slots.SlotPool(2)
    slot = pool.acquire()
    pool.release(None)
    pool.release(slot)
    pool.release(slot)
    pool.release(99)

    assert {pool.acquire(), pool.acquire()} == {0, 1}
    assert pool.acquire() is None, "a double release duplicated a slot in the free list"


def test_assign_sends_the_bound_the_slot_was_drawn_from(caplog: pytest.LogCaptureFixture) -> None:
    """THE REGRESSION TEST FOR A RAISED POOL, and it fails without the stamped bound.

    The parent sizes its pool from ``worker_process_pool_size``; the CHILD enforces the
    bound, reading it from its own environment where it defaults to ``DEFAULT_SLOT_MAX``.
    Those are the same number only at the default. At a pool of 6 with nothing stamped, the
    parent issues slots 4 and 5 and the child refuses both -- measured,
    ``telemetry_slot_out_of_range slot=5 bound=4`` -- so a third of the fleet silently falls
    back to the shared identity that this module exists to eliminate, with a green suite and
    correct-looking dashboards.

    So this drives a SIX-slot pool through the real ``assign`` and then asks the CHILD-side
    resolver about the environment the parent actually produced. Asserting on
    ``environ[SLOT_MAX_ENV]`` alone would not do: what has to hold is that ``resolve_slot``
    accepts the slot, which is the function whose refusal caused the defect.
    """
    pool = slots.SlotPool(6)
    issued = []
    for _ in range(6):
        environ, slot = slots.assign({}, pool=pool)
        issued.append((environ, slot))

    assert [slot for _, slot in issued] == [0, 1, 2, 3, 4, 5]
    with caplog.at_level(logging.WARNING, logger="phaze.telemetry.slots"):
        resolved = [slots.resolve_slot(environ) for environ, _ in issued]

    assert resolved == [0, 1, 2, 3, 4, 5], "the child refused a slot its own parent issued"
    assert not caplog.records, f"a parent-issued slot was logged as out of range: {[r.getMessage() for r in caplog.records]}"
    assert issued[5][0][slots.SLOT_MAX_ENV] == "6"


def test_assign_sends_a_bound_that_cannot_be_inflated_by_the_parents_own_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stamped bound is the POOL's, not whatever the parent happened to inherit.

    ``child_environment`` copies ``os.environ``, so a stale or operator-set
    ``PHAZE_TELEMETRY_SLOT_MAX`` rides along into the child. The pool's own size is the only
    value that is correct by construction -- the parent can never issue a slot at or above
    it -- so it must win. A larger inherited bound would admit a slot no pool can issue; a
    smaller one would refuse slots that are perfectly valid.
    """
    monkeypatch.setenv(slots.SLOT_MAX_ENV, "2")
    pool = slots.SlotPool(6)
    for _ in range(5):
        pool.acquire()
    environ, slot = slots.assign({slots.SLOT_MAX_ENV: "2"}, pool=pool)

    assert slot == 5
    assert environ[slots.SLOT_MAX_ENV] == "6"
    assert slots.resolve_slot(environ) == 5


def test_assign_stamps_the_slot_into_the_childs_environment() -> None:
    """The parent's half of the process boundary: the slot travels in the child's env.

    The mapping is returned as handed (it is already a copy, from
    ``context.child_environment``) so the caller has one object to pass to
    ``create_subprocess_exec``, and the slot comes back separately because the CALLER owns
    the release -- it must outlive the spawn and end only when the child has exited.
    """
    pool = slots.SlotPool(2)
    environ: dict[str, str] = {"PATH": "/usr/bin"}
    returned, slot = slots.assign(environ, pool=pool)

    assert returned is environ
    assert slot == 0
    assert environ[slots.SLOT_ENV] == "0"
    assert environ["PATH"] == "/usr/bin"


def test_assign_leaves_the_environment_alone_when_no_slot_is_free() -> None:
    """An exhausted pool must not stamp a slot it did not allocate.

    A stale or invented value here would be worse than no value: the child would resolve a
    confident identity that another live child is already exporting under.
    """
    pool = slots.SlotPool(1)
    pool.acquire()
    environ, slot = slots.assign({}, pool=pool)

    assert slot is None
    assert slots.SLOT_ENV not in environ
    assert slots.SLOT_MAX_ENV not in environ, "a bound was sent for a slot that was never issued"


def test_the_default_pool_is_sized_from_the_concurrency_knob() -> None:
    """``set_default_pool_size`` is how the host lane makes the bound follow the semaphore."""
    slots.set_default_pool_size(7)
    assert slots.default_pool().size == 7


def test_a_resize_is_declined_while_slots_are_held(caplog: pytest.LogCaptureFixture) -> None:
    """Shrinking under live children would hand out a DUPLICATE, so it does not happen.

    Rebuilding the pool forgets which slots are out, and the next acquire would reissue
    slot 0 to a second live child -- the exact collision the module exists to prevent. In
    production the resize happens once, at worker startup, before any child exists; this is
    the guard for every other ordering.
    """
    slots.set_default_pool_size(4)
    held = slots.default_pool().acquire()
    with caplog.at_level(logging.WARNING, logger="phaze.telemetry.slots"):
        slots.set_default_pool_size(2)

    assert slots.default_pool().size == 4, "the pool was rebuilt while a slot was out"
    assert any("resize_declined" in record.getMessage() for record in caplog.records)
    slots.default_pool().release(held)


def test_the_default_pool_is_a_singleton() -> None:
    """Two calls must return the SAME pool, and the reason is not tidiness.

    A second pool would have its own free list starting at 0, so two live children could
    both be told they hold slot 0 -- the collision the whole module exists to prevent,
    reintroduced by an accessor rather than by a scheme. This is the ``_pool is not None``
    path of the lazy accessor, which nothing else exercises.
    """
    first = slots.default_pool()
    held = first.acquire()
    second = slots.default_pool()

    assert second is first
    assert second.acquire() != held, "a second pool handed out a slot the first one already holds"


# phaze-w15ju: the burst lane injects its slot from outside the process


def test_assign_passes_an_inherited_slot_through_and_allocates_nothing() -> None:
    """THE BURST LANE'S WHOLE MECHANISM, and the branch its correctness rests on.

    A burst pod runs ``run_analysis_subprocess`` exactly like the host lane, so it builds a
    process-local pool -- of ONE competitor, because the pod shares memory with nobody -- and would
    acquire index 0. That would overwrite the slot the controller allocated against the other
    in-flight pods, and every burst pod would export under ``phaze-analysis-0`` again with a
    correct-looking Job manifest. ADR-0017 section 8d named that trap before this bead existed.

    So an inherited, VALID slot wins: nothing is taken from the pool, the environment is untouched,
    and the returned slot is None -- which also means the caller's ``finally`` releases nothing it
    never held.
    """
    pool = slots.SlotPool(4)
    environ, slot = slots.assign({slots.SLOT_ENV: "2", slots.SLOT_MAX_ENV: "4"}, pool=pool)

    assert slot is None
    assert environ[slots.SLOT_ENV] == "2"
    assert environ[slots.SLOT_MAX_ENV] == "4"
    assert pool.acquire() == 0, "the pass-through consumed a slot from the pool"
    assert bootstrap._instance_id("phaze-analysis", environ) == "phaze-analysis-2"


def test_an_inherited_slot_the_child_would_refuse_is_not_honoured(caplog: pytest.LogCaptureFixture) -> None:
    """An out-of-range or malformed inherited slot falls through to a REAL allocation.

    Deferring to a value ``resolve_slot`` rejects would hand the process the shared identity while a
    perfectly good slot sat free in its pool -- the worst of both answers. The bound is what decides:
    slot 9 against a bound of 4 is not a slot, it is a lie about concurrency.
    """
    for inherited in ({slots.SLOT_ENV: "9", slots.SLOT_MAX_ENV: "4"}, {slots.SLOT_ENV: "not-a-slot"}):
        pool = slots.SlotPool(4)
        with caplog.at_level(logging.WARNING, logger="phaze.telemetry.slots"):
            environ, slot = slots.assign(dict(inherited), pool=pool)
        assert slot == 0, f"{inherited} should not have been honoured"
        assert environ[slots.SLOT_ENV] == "0"
        assert environ[slots.SLOT_MAX_ENV] == "4"


def test_a_process_that_allocates_its_own_slots_disowns_an_inherited_one(caplog: pytest.LogCaptureFixture) -> None:
    """``disown_inherited_slot`` is what keeps the pass-through from breaking the HOST lane.

    Once an inherited slot is honoured, a ``PHAZE_TELEMETRY_SLOT`` set by hand in the agent worker's
    environment would ride ``child_environment``'s ``os.environ`` copy into EVERY child, collapsing
    all ``worker_process_pool_size`` of them onto one identity -- the merge the pool exists to
    prevent, with a plausible-looking environment and a green suite.

    Ownership, not precedence: the seat that bounds the concurrency disowns what it was handed, and
    logs the discard so the operator's value does not vanish silently. The BOUND is deliberately
    left in place -- it is a documented override for the ceiling, not a claim about which slot this
    process holds.
    """
    environ = {slots.SLOT_ENV: "3", slots.SLOT_MAX_ENV: "6", "PATH": "/usr/bin"}
    with caplog.at_level(logging.WARNING, logger="phaze.telemetry.slots"):
        assert slots.disown_inherited_slot(environ) == 3

    assert slots.SLOT_ENV not in environ
    assert environ[slots.SLOT_MAX_ENV] == "6"
    assert environ["PATH"] == "/usr/bin"
    assert any("telemetry_slot_inherited_discarded" in record.getMessage() for record in caplog.records)

    # With the inherited value gone, assign is back to allocating from the pool it owns.
    pool = slots.SlotPool(4)
    _, slot = slots.assign(dict(environ), pool=pool)
    assert slot == 0


def test_disowning_nothing_is_a_no_op() -> None:
    """The normal case: no inherited slot, nothing removed, nothing logged."""
    assert slots.disown_inherited_slot({}) is None
    assert slots.disown_inherited_slot({slots.SLOT_ENV: "   "}) is None


def test_the_agent_worker_startup_disowns_before_it_spawns_anything() -> None:
    """The host lane's protection is WIRED, not merely available (phaze-w15ju).

    ``disown_inherited_slot`` closes a regression that only exists because ``assign`` now honours an
    inherited slot, and a helper nobody calls closes nothing. Read at the source level because the
    alternative is booting a real agent worker (broker, heartbeat, queue readiness) to observe one
    call, and because what has to hold is an ORDERING: the disown must sit beside
    ``set_default_pool_size`` in ``startup``, before any child can be spawned.
    """
    # Read the file rather than import the module: ``phaze.tasks.agent_worker`` builds
    # ``AgentSettings`` at import time and raises without the agent env, which is a fixture cost this
    # assertion does not need.
    agent_worker = pathlib.Path(slots.__file__).parents[1] / "tasks" / "agent_worker.py"
    source = agent_worker.read_text(encoding="utf-8")
    tree = ast.parse(source)
    startup = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "startup")
    called = [node.func.attr for node in ast.walk(startup) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "set_default_pool_size" in called
    assert "disown_inherited_slot" in called
    assert called.index("disown_inherited_slot") > called.index("set_default_pool_size"), (
        "the pool must be sized from the real concurrency knob before the inherited slot is discarded"
    )
