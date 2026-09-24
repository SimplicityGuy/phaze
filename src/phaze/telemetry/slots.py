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
``host-prod-0 .. host-prod-3``. A burst pod carries a third label between the two, the LANE
(:data:`LANE_ENV`, phaze-7nl67), because the host pool and the burst allocator both count from 0
and cannot see each other: ``phaze-analysis-burst-0 ..``. That gives each lane its own identity
space, so it ADDS to the bound rather than sharing it -- ``docs/design/0017-telemetry-export-topology.md``
section 8b has the arithmetic.

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
#: ``worker_process_pool_size``; it remains an explicit override. The BURST lane sets it for the
#: child too, one boundary further out -- the controller injects the pair into the Kueue Job env
#: (``phaze-w15ju``), and :func:`assign` passes an inherited slot through untouched.
SLOT_MAX_ENV = "PHAZE_TELEMETRY_SLOT_MAX"

#: Names the LANE whose slot space this process's slot was drawn from, when that is not the host
#: lane (phaze-7nl67). The host lane's pool and the burst lane's controller-allocated slots both
#: count from 0, and neither can see the other's allocations -- the host pool is process-local
#: memory, the burst allocator reads ``cloud_job`` rows the host lane never writes for a local
#: dispatch. So a host child and a burst pod on the same index resolved to the same identity and
#: merged at the collector, whenever ``PHAZE_TELEMETRY_INSTANCE`` was left unset (the default). The
#: lane is inserted between the base and the slot, which gives each lane its OWN identity space:
#: ``phaze-analysis-1`` for a host child, ``phaze-analysis-burst-1`` for a burst pod.
#:
#: **Code-injected, never operator-set**: ``kube_staging.build_job_manifest`` stamps it into every
#: Job's ``env``, and the agent worker -- the host lane's seat -- disowns any value it inherits
#: (:func:`disown_inherited_lane`). Operator decision 2026-09-24, "Code-inject burst lane (Recommended)";
#: the question, the answer and the rounds before it are in bead ``phaze-7nl67``'s comments and
#: ``docs/design/0017-telemetry-export-topology.md`` section 8d. Carrying it in a separate key rather
#: than an injected ``PHAZE_TELEMETRY_INSTANCE`` is the IMPLEMENTER'S decision (dispatcher correction
#: comment on ``phaze-7nl67``, 2026-09-24), made so the operator's host override keeps working on both
#: lanes.
LANE_ENV = "PHAZE_TELEMETRY_LANE"

#: The burst lane's name, and the only value :data:`LANE_ENV` may carry.
BURST_LANE = "burst"

#: Every lane name :func:`resolve_lane` accepts. A CLOSED set on purpose: the lane multiplies every
#: series the analysis role emits, exactly like the slot, so an arbitrary value would be an unbounded
#: identity by another name. The same set is RESERVED against the analysis role's operator base
#: (:func:`escape_reserved_lanes`), which is what makes the two lanes' identities disjoint rather than
#: merely different by default.
LANES: frozenset[str] = frozenset({BURST_LANE})

#: The only role a lane can apply to. Lanes separate CONCURRENT ANALYSIS producers, and the analysis
#: child is the only process a burst pod exports from. Every other role exports under its own
#: ``service.name`` (the Prometheus ``job``), and an identity only merges within one job, so a
#: ``burst`` segment in another role's base can never meet a burst pod's identity.
LANE_ROLE = "analysis"

#: Appended to a reserved lane segment found in an analysis-role operator base. See
#: :func:`escape_reserved_lanes`.
RESERVED_SEGMENT_ESCAPE = "_"

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


def resolve_lane(environ: Mapping[str, str] | None = None) -> str | None:
    """This process's validated lane, or None for the host lane.

    Fails closed like :func:`resolve_slot`: a value outside :data:`LANES` is refused and logged, and
    the process carries on in the host lane's identity space. Absent is the normal state of every
    process but a burst pod's, so it does not log.
    """
    raw = (os.environ if environ is None else environ).get(LANE_ENV, "").strip()
    if not raw:
        return None
    if raw not in LANES:
        log.warning("telemetry_lane_unknown value=%r using_host_lane", raw)
        return None
    return raw


def base_claims_a_lane(base: str) -> bool:
    """Whether an operator-supplied base contains a reserved lane name as a ``-`` segment."""
    return any(segment in LANES for segment in base.split("-"))


def escape_reserved_lanes(base: str) -> str:
    """Rewrite each reserved lane segment of an analysis-role base so it no longer reads as a lane.

    **This is what makes the two identity spaces DISJOINT, not merely different by default.** A host
    identity is ``<base>[-<slot>]`` and a burst identity is ``<base>-burst[-<slot>]``. The only way
    a host identity can equal a burst one is for the host's base itself to carry a ``burst`` segment:
    ``PHAZE_TELEMETRY_INSTANCE=host-burst`` on the agent host would resolve its slot-1 child to the
    identity of a burst pod whose own base is ``host``. After this rewrite a host identity has no
    ``burst`` segment at all, and every burst identity has one, so no pair can collide.

    **It ESCAPES rather than discarding the base, and the difference is a merge.** An earlier version
    fell back to the service name. Two agent hosts that both hit that fallback would then both report
    ``phaze-analysis-<n>`` and merge, which is the defect this whole module exists to prevent, moved
    one level up. ``host-burst`` becomes ``host-burst_``: still the operator's own, host-distinct name,
    and no longer a lane. Two hosts could only collide after the rewrite if the operator had named one
    literally ``host-burst_``, and giving two hosts one name is already the operator's own collision.
    """
    return "-".join(f"{segment}{RESERVED_SEGMENT_ESCAPE}" if segment in LANES else segment for segment in base.split("-"))


class SlotPool:
    """A process-local free list of slot indices, sized by the concurrency it mirrors.

    **Process-local is the RIGHT granularity for the host lane and cannot serve the burst
    lane**, and that asymmetry is a property of the deployment rather than a shortcut here.
    Concurrent analysis children on a host are bounded by ONE
    ``asyncio.Semaphore(worker_process_pool_size)`` in ONE agent-worker process
    (``tasks/agent_worker.py``), so a free list in that process sees every competitor it
    needs to. A burst-lane child runs in a one-shot Kueue pod that is Postgres-less and
    shares no memory with its peers, so nothing in the pod can allocate against them --
    that slot is injected by the controller at submit time instead
    (``phaze.services.burst_telemetry_slots``, bead ``phaze-w15ju``; see
    ``docs/design/0017-telemetry-export-topology.md`` section 8d). A burst pod still builds one
    of these pools, and :func:`assign` is what stops it from overwriting the controller's slot
    with its own local 0.

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


def disown_inherited_slot(environ: dict[str, str] | None = None) -> int | None:
    """Drop a slot this process INHERITED but does not own. Returns the value removed, or None.

    Called by the seat that bounds a lane's concurrency and therefore owns its slots -- the agent
    worker, beside :func:`set_default_pool_size`. It exists because of :func:`assign`'s
    pass-through: once an inherited slot is honoured, a ``PHAZE_TELEMETRY_SLOT`` set by hand in the
    agent worker's environment would be handed to EVERY child, giving all
    ``worker_process_pool_size`` of them one identity -- the exact merge the pool prevents, with a
    green suite and a plausible-looking environment.

    The pass-through is still right: in a burst pod the controller genuinely does own the slot, one
    process boundary further out. The two are reconciled by *ownership*, not by precedence -- a
    process that allocates its own slots disowns any it was handed, and says so in its log, which
    is the whole difference between this and silently ignoring the value.

    ``PHAZE_TELEMETRY_SLOT_MAX`` is deliberately left alone: it is a documented operator override
    for the BOUND, not a claim about which slot this process holds, and
    :func:`set_default_pool_size` has already sized the pool from the real concurrency knob.
    """
    env = os.environ if environ is None else environ
    raw = env.pop(SLOT_ENV, "").strip()
    if not raw:
        return None
    try:
        inherited = int(raw)
    except ValueError:
        log.warning("telemetry_slot_inherited_discarded value=%r reason=not_an_integer", raw)
        return None
    log.warning("telemetry_slot_inherited_discarded slot=%d reason=this_process_allocates_its_own", inherited)
    return inherited


def disown_inherited_lane(environ: dict[str, str] | None = None) -> str | None:
    """Drop a lane this process INHERITED; return the value removed, or None. The host lane's seat calls it.

    The same ownership rule as :func:`disown_inherited_slot`, for the same reason: the agent worker
    allocates host-lane slots, so a ``PHAZE_TELEMETRY_LANE=burst`` left in its environment would ride
    ``child_environment``'s copy into every child and move the whole host pool into the BURST lane's
    identity space -- where host slot 1 and burst slot 1 merge again, which is the defect the lane
    exists to close (phaze-7nl67). Logged rather than silently ignored, so the operator's value does
    not vanish without a trace.
    """
    env = os.environ if environ is None else environ
    raw = env.pop(LANE_ENV, "").strip()
    if not raw:
        return None
    log.warning("telemetry_lane_inherited_discarded lane=%r reason=this_process_is_the_host_lane", raw)
    return raw


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
    the parent's own environment.

    **AN INHERITED SLOT IS PASSED THROUGH, NOT REALLOCATED (phaze-w15ju), and this is what
    makes the burst lane work at all.** When ``environ`` already carries a slot that
    :func:`resolve_slot` accepts, this function consumes nothing from the pool, changes
    nothing in ``environ``, and returns ``None`` as the slot -- so the caller's ``finally``
    releases nothing it never took.

    The rule is *whoever bounds the concurrency owns the slot*, and in a burst pod that seat
    is the CONTROLLER, one process boundary further out: it allocates against the in-flight
    ``cloud_job`` rows at submit time and injects the pair into the Job env
    (``kube_staging.JOB_ENV_CODE_INJECTED_TELEMETRY``). A burst pod runs
    ``run_analysis_subprocess`` exactly like the host lane, so without this branch its
    process-local pool -- a pool of one competitor, because the pod shares memory with
    nobody -- would acquire slot 0 and OVERWRITE the controller's decision in the child's
    environment. Every burst pod would then export under ``phaze-analysis-0`` again: the
    manifest would look correct, the injected key would be present and the defect would be
    untouched. That is precisely the trap ADR-0017 (telemetry export topology) section 8d warned about in writing, and it
    is why ``test_two_concurrent_burst_submissions_reach_the_child_with_distinct_identities``
    drives the real child-environment path rather than asserting on the manifest alone.

    A slot that is present but INVALID (non-numeric, negative, at or above the bound) is not
    honoured: :func:`resolve_slot` rejects it, so this falls through to a real allocation.
    Deferring to a value the child would refuse anyway would hand the process the shared
    identity when a perfectly good slot was available.
    """
    inherited = resolve_slot(environ)
    if inherited is not None:
        log.debug("telemetry_slot_inherited slot=%d not_allocating", inherited)
        return environ, None
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
