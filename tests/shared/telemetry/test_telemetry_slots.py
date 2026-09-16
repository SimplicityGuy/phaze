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

import logging

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
