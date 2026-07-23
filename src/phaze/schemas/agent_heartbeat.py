"""Pydantic schema for POST /api/internal/agent/heartbeat (phase-25 D-17, D-19)."""

from pydantic import BaseModel, ConfigDict, Field

from phaze.schemas.wire_bounds import INT32_MAX


# queue_depth lands in Agent.last_status['lanes'][lane] JSONB -- no scalar column for wire_bounds
# rule 1/3 to bind against directly (see UNMAPPED_BODY_FIELDS in
# tests/shared/schemas/test_wire_bounds_contract.py). But it is not unbounded in practice:
# _LANE_MERGE_SQL (routers/agent_heartbeat.py) computes `SUM((v ->> 'queue_depth')::bigint)` over
# every stored lane, so the field's REAL effective domain is the int8 that SQL casts to (wire_bounds
# rule 3: "an integer field is bounded by its domain when it has one, otherwise by its column" --
# here the "column" is the bigint cast target). A Pydantic int is arbitrary-precision, so without a
# bound a value past INT64_MAX survives validation, gets json.dumped into the JSONB, and blows up
# the `::bigint` cast with NumericValueOutOfRange -- an unhandled 500 with no DB exception handler
# on the route (phaze-s4r0).
#
# The cap below is NOT simply INT64_MAX: the SQL sums the field across every lane in
# `last_status['lanes']`, so capping each lane at INT64_MAX would let the SUM itself overflow int8
# once more than one lane is populated. There are 4 lanes today (phaze.services.enqueue_router.LANES)
# with no realistic path to more than a handful ever existing, so a per-lane cap many orders of
# magnitude below INT64_MAX / any plausible lane count keeps the cross-lane SUM safely inside int8
# while still being far larger than any real queue depth could ever reach.
QUEUE_DEPTH_MAX = 1_000_000_000_000  # 10**12 per lane; even summed over 1000 lanes (10**15) that is
# still ~4 orders of magnitude under INT64_MAX (~9.22 * 10**18), so SUM(...) can never overflow int8.


class HeartbeatRequest(BaseModel):
    """Heartbeat payload. The original three fields are required per CONTEXT.md D-17.

    Persisted to `agents.last_status` JSONB by the handler (per-lane when `lane` is set --
    see :mod:`phaze.routers.agent_heartbeat`).
    """

    model_config = ConfigDict(extra="forbid")

    agent_version: str
    worker_pid: int = Field(ge=1, le=INT32_MAX)
    """Positive pid (wire_bounds rule 3 fallback: no tighter real-world domain known, and it never
    reaches a numeric cast -- unlike queue_depth this is defense-in-depth, not a required fix)."""
    queue_depth: int = Field(ge=0, le=QUEUE_DEPTH_MAX)
    lane: str | None = None
    """Which lane worker sent this beat (phaze-30fo): analyze|fingerprint|meta|io.

    OPTIONAL and defaulting to None on purpose. Every lane now heartbeats, but an agent
    running an older image (or in all-mode, where there is no lane split) posts without
    this field, and a required field would 422 every one of those beats -- turning a
    liveness fix into a liveness outage during a rolling deploy. `None` means
    "unlaned beat", which the handler stores exactly the way it always did.
    """
