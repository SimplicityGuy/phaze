"""The bounded worker-slot identity that keeps CONCURRENT producers apart.

**THE DEFECT THIS EXISTS TO FIX, measured rather than argued.** Up to
``worker_process_pool_size`` analysis children run at once (default 4), each its own OS
process running ``configure_telemetry("analysis")``, and before this module every one of
them exported under an IDENTICAL resource identity. A collector's Prometheus exporter
keeps ONE series per identity and takes the last write, so two children at different
running totals overwrite each other's cumulative counters.

Measured 2026-09-15 against ``otel/opentelemetry-collector-contrib`` 0.140.0 with the
repo's own ``deploy/telemetry/otel-collector.example.yaml``: two processes flushing
totals 10/20/30 and 100/200/300 under one identity produced this exposition sequence at
``:8889/metrics`` --

    10 -> 10 -> 10 -> 100 -> 100 -> 20 -> 20 -> 200 -> 200 -> 30 -> 300 -> 300

-- where ``100 -> 20`` and ``200 -> 30`` are counter DECREASES. Prometheus reads each as
a counter reset and counts the post-reset value as increment, so ``increase()`` over that
window reports **590** where the producers delivered **320**: an **84.4% over-count** on
every ``phaze_analysis_*`` counter panel and all three alert rules. The final exposed total
was 300 against a true 330, i.e. one producer's last export simply gone.

**The error GROWS with the number of interleaved exports** -- the same arm with twice as
many flushes per producer reported 2,090 against 650, **+221.5%** -- which is what makes
this a production defect rather than a curiosity: a multi-hour analysis exports every 15 s.
The full record, including the two rejected alternatives and what the figures do NOT
establish, is ``docs/telemetry/concurrent-identity.md``.

**THE FIX IS A BOUNDED SLOT INDEX, and the bound is the whole point.** The obvious repair
-- a per-process instance id -- is the one thing
``bootstrap._resource_attributes`` already forbids: the analysis role's catalogue block is
2,290 series, so a fresh identity per analyzed file is 2,290 x 11,428 = **26,170,120**
series in a Prometheus phaze does not own. A slot index is instead REUSED by the next
child, so the identity set is bounded by the CONCURRENCY rather than by the corpus:
2,290 x 4 = 9,160 series for the analysis role, and the catalogue ceiling moves 8,587 ->
15,457. **That figure does not grow with the archive**, which is the property that makes
this affordable and the per-pod id unaffordable.

**A slot is NOT a host, and the two labels stack deliberately.** ``PHAZE_TELEMETRY_INSTANCE``
remains the per-host override, whose value is the operator's to pick and is bounded by the
number of hosts; the slot is appended to it, so ``host-prod`` with four children is
``host-prod-0 .. host-prod-3``.

**THE BOUND IS SENT WITH THE SLOT, not documented alongside it.** The parent's pool size
is stamped into the child's environment by :func:`assign`, so a raised
``worker_process_pool_size`` needs no second variable and no operator action. Telling an
operator to keep two numbers in step was the drift this module's own design note says
:func:`set_default_pool_size` exists to prevent, and it cost slots 4 and 5 of a six-slot
pool -- issued by the parent, refused by the child, shared identity for a third of the
fleet.

**WHY VALIDATION FAILS CLOSED HERE.** An out-of-range or non-numeric slot is REFUSED and
the process falls back to the unsuffixed identity -- degraded to the shared-identity merge
this module exists to fix, which is bad, but bounded. Accepting whatever integer arrived
would be the per-file cardinality bomb wearing a slot's name, and it would be invisible
until it had already been scraped into somebody else's storage. Of the two failure
directions only one is recoverable.

**Dashboards and alert rules need no change, and that was checked rather than hoped.**
Every expression in ``dashboards/*.json`` and ``alerts/phaze-alerts.yml`` already wraps its
selector in ``sum(...)`` or ``sum by (<label>)(...)`` -- ``sum by (tier)
(rate(phaze_analysis_windows_total{...}[$__rate_interval]))``. Summing AFTER ``rate`` is
exactly the canonical Prometheus treatment of one series per producer, and it is why the
slot label is invisible at the panel.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping

log = logging.getLogger(__name__)

#: Names THIS process's worker slot. Set by whoever bounds the concurrency -- the agent
#: worker for the host lane, via :func:`assign` around the child spawn -- and read back by
#: ``bootstrap._instance_id``. An operator does not set this by hand; it would be a lie
#: about concurrency the process cannot keep.
SLOT_ENV = "PHAZE_TELEMETRY_SLOT"

#: The exclusive upper bound on a slot index, and therefore the cardinality multiplier.
#: **The host lane sets this FOR the child** -- :func:`assign` stamps the issuing pool's own
#: size alongside the slot, so the bound the child enforces is the one the slot was drawn
#: from and the two cannot drift. An operator does not need to set it to raise
#: ``worker_process_pool_size``; it remains an explicit override, and the input for a
#: producer nothing allocates for (the burst lane, ``phaze-w15ju``).
SLOT_MAX_ENV = "PHAZE_TELEMETRY_SLOT_MAX"

#: Matches ``config_base.Settings.worker_process_pool_size``'s default, which is what
#: actually bounds concurrent analysis children. Pinned against it by
#: ``tests/shared/telemetry/test_telemetry_slots.py::test_the_default_bound_matches_the_worker_pool_default``
#: rather than by this comment -- a second source of truth for a cardinality bound is how
#: the bound stops being one.
DEFAULT_SLOT_MAX = 4


def slot_max(environ: Mapping[str, str] | None = None) -> int:
    """The exclusive upper bound on a slot index. Never raises; nonsense falls back."""
    raw = (os.environ if environ is None else environ).get(SLOT_MAX_ENV, "").strip()
    if not raw:
        return DEFAULT_SLOT_MAX
    try:
        value = int(raw)
    except ValueError:
        log.warning("telemetry_slot_max_not_an_integer value=%r falling_back=%d", raw, DEFAULT_SLOT_MAX)
        return DEFAULT_SLOT_MAX
    if value < 1:
        log.warning("telemetry_slot_max_not_positive value=%d falling_back=%d", value, DEFAULT_SLOT_MAX)
        return DEFAULT_SLOT_MAX
    return value


def resolve_slot(environ: Mapping[str, str] | None = None) -> int | None:
    """This process's validated slot index, or None when it has none.

    None is the "carry on with the shared identity" answer and is returned for an absent,
    empty, non-numeric, negative or out-of-range value. Only the last three log: an ABSENT
    slot is the normal state of every role that is not the analysis child, and warning
    about it would put a line in the api's boot log for a variable it should never have.
    """
    env = os.environ if environ is None else environ
    raw = env.get(SLOT_ENV, "").strip()
    if not raw:
        return None
    try:
        slot = int(raw)
    except ValueError:
        log.warning("telemetry_slot_not_an_integer value=%r using_shared_identity", raw)
        return None
    bound = slot_max(env)
    if slot < 0 or slot >= bound:
        # Fails CLOSED -- see the module docstring. An unbounded slot would multiply every
        # series this role emits by however many values ever arrived.
        log.warning("telemetry_slot_out_of_range slot=%d bound=%d using_shared_identity", slot, bound)
        return None
    return slot


class SlotPool:
    """A process-local free list of slot indices, sized by the concurrency it mirrors.

    **Process-local is the RIGHT granularity for the host lane and cannot serve the burst
    lane**, and that asymmetry is a property of the deployment rather than a shortcut here.
    Concurrent analysis children on a host are bounded by ONE
    ``asyncio.Semaphore(worker_process_pool_size)`` in ONE agent-worker process
    (``tasks/agent_worker.py``), so a free list in that process sees every competitor it
    needs to. A burst-lane child runs in a one-shot Kueue pod that is Postgres-less and
    shares no memory with its peers, so nothing in the pod can allocate against them --
    that slot has to be injected by the controller at submit time. Tracked separately as
    bead ``phaze-w15ju``; see ``docs/design/0017-telemetry-export-topology.md`` section 8d.

    Exhaustion returns None rather than raising or blocking. Instrumentation may never be
    the thing that fails or stalls the work it observes (bootstrap's acceptance-7 contract),
    and a child with no slot still analyzes its file correctly -- it merely shares an
    identity, which is the pre-existing behaviour.
    """

    def __init__(self, size: int) -> None:
        self._lock = threading.Lock()
        self._size = max(1, size)
        self._free: list[int] = list(range(self._size))

    @property
    def size(self) -> int:
        return self._size

    def acquire(self) -> int | None:
        """Take the lowest free slot, or None when every slot is held."""
        with self._lock:
            if not self._free:
                log.warning("telemetry_slot_pool_exhausted size=%d using_shared_identity", self._size)
                return None
            return self._free.pop(0)

    def release(self, slot: int | None) -> None:
        """Hand a slot back. Tolerates None and a double release, on purpose.

        This runs in the teardown path of an analysis child, which is reached by success,
        failure, stall and cancellation alike. A bookkeeping error there must not raise
        over the top of an analysis result, and a slot leaked by one is a cardinality
        non-event -- the pool simply runs smaller until the process restarts.
        """
        if slot is None:
            return
        with self._lock:
            if 0 <= slot < self._size and slot not in self._free:
                self._free.append(slot)
                self._free.sort()


_pool_lock = threading.Lock()
_pool: SlotPool | None = None


def default_pool() -> SlotPool:
    """The process-wide pool, built lazily from :func:`slot_max` on first use."""
    global _pool  # process-wide singleton, like the providers in bootstrap
    with _pool_lock:
        if _pool is None:
            _pool = SlotPool(slot_max())
        return _pool


def set_default_pool_size(size: int) -> None:
    """Size the pool from the knob that ACTUALLY bounds concurrency. Called at worker startup.

    The host lane's ceiling is ``worker_process_pool_size``, and this exists so the
    cardinality bound is read from that number rather than from a second copy of it in the
    environment. That is only half the job: the size set here is what :func:`assign` then
    stamps into each child's environment, because the child enforces the bound and would
    otherwise fall back to :data:`DEFAULT_SLOT_MAX` and refuse the very slots this pool
    hands out. Sizing down while slots are held would hand out a duplicate on the next
    acquire, so a resize replaces the pool only when nothing is out.
    """
    global _pool  # process-wide singleton, like the providers in bootstrap
    with _pool_lock:
        if _pool is not None and len(_pool._free) != _pool.size:
            log.warning("telemetry_slot_pool_resize_declined held=%d size=%d", _pool.size - len(_pool._free), _pool.size)
            return
        _pool = SlotPool(size)


def assign(environ: dict[str, str], *, pool: SlotPool | None = None) -> tuple[dict[str, str], int | None]:
    """Stamp a freshly acquired slot AND THE BOUND IT WAS DRAWN FROM into ``environ``.

    Returns the SAME mapping it was handed (already a copy, from
    ``context.child_environment``) so the caller has one object to pass to
    ``create_subprocess_exec``. The slot comes back separately because the caller owns the
    release, and it must release it only once the child has EXITED -- a slot released at
    spawn time would be handed to the next child while this one is still exporting under it,
    which is the merge this whole module exists to prevent.

    **THE BOUND TRAVELS WITH THE SLOT, and shipping only the slot was a defect.** The parent
    sizes its pool from ``worker_process_pool_size``; the child validates what it receives in
    :func:`resolve_slot` against :func:`slot_max`, which reads the CHILD's environment and
    defaults to :data:`DEFAULT_SLOT_MAX`. Those are two different numbers the moment an
    operator raises the pool: measured at ``worker_process_pool_size=6`` with
    ``PHAZE_TELEMETRY_SLOT_MAX`` unset, the parent issued slots 4 and 5, the child logged
    ``telemetry_slot_out_of_range slot=5 bound=4`` for each, and two of six children fell
    back to the shared identity -- silently reinstating the overwrite this module exists to
    fix, for a third of the fleet, with a green suite.

    Stamping ``pool.size`` closes it at the source rather than in documentation. The parent
    can never issue a slot at or above its own pool size, so the bound sent here is by
    construction the SMALLEST one that admits every slot the parent can hand out: it cannot
    be too small for a real slot, and it cannot be inflated by a stale value inherited from
    the parent's own environment. ``PHAZE_TELEMETRY_SLOT_MAX`` survives as an explicit
    operator override and as the input for a producer nothing allocates for -- the burst
    lane, ``phaze-w15ju``.
    """
    target = pool or default_pool()
    chosen = target.acquire()
    if chosen is not None:
        environ[SLOT_ENV] = str(chosen)
        environ[SLOT_MAX_ENV] = str(target.size)
    return environ, chosen


def _reset_for_tests() -> None:
    """Forget the process-wide pool. Tests only."""
    global _pool  # test seam
    with _pool_lock:
        _pool = None
