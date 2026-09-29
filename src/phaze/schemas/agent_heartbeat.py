"""Pydantic schema for POST /api/internal/agent/heartbeat (phase-25 D-17, D-19)."""

from pydantic import BaseModel, ConfigDict, Field, JsonValue

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

# The cap below is NOT simply INT64_MAX: the SQL sums the field across every lane in
# `last_status['lanes']`, so capping each lane at INT64_MAX would let the SUM itself overflow int8
# once more than one lane is populated. There are 4 lanes today (phaze.services.enqueue_router.LANES)
# with no realistic path to more than a handful ever existing, so a per-lane cap many orders of
# magnitude below INT64_MAX / any plausible lane count keeps the cross-lane SUM safely inside int8
# while still being far larger than any real queue depth could ever reach.
QUEUE_DEPTH_MAX = 1_000_000_000_000  # 10**12 per lane; even summed over 1000 lanes (10**15) that is
# still ~4 orders of magnitude under INT64_MAX (~9.22 * 10**18), so SUM(...) can never overflow int8.


#: Server-side bound on ``EffectiveConfigLastReload.error``. A reload error is itself unbounded (it
#: lists every offending key by name), so the AGENT truncates to this before building a beat
#: (``phaze.tasks.heartbeat._build_effective_config``) -- an over-long error must cost the operator
#: the tail of a message, never the heartbeat (phaze-mvq8z.19).
LAST_RELOAD_ERROR_MAX_LENGTH = 500

#: Bound on the free-text labels below (a layer, a reload source or outcome) -- today's longest is 9.
EFFECTIVE_CONFIG_LABEL_MAX_LENGTH = 64

# phaze-mvq8z.20 -- VERSION TOLERANCE of the ``effective_config`` sub-document (the implementer's
# mechanism for that bead's finding 1). The reloadable key set, the layers, the reload sources and
# outcomes all move between versions, and a rolling deploy puts a new agent in front of an old
# control plane and vice versa. This snapshot is DISPLAY-ONLY -- the server stores it as sent and
# ``routers/admin_agents._effective_config_view`` renders any mapping -- so its models ignore
# fields they do not know (``extra="ignore"``), ``values`` is a plain JSON mapping rather than
# ``RuntimeConfig`` (whose every field is required and whose extras are forbidden, so ANY key
# added or removed 422'd heartbeats between mismatched versions), and the enum-like labels are
# bounded strings rather than Literals. ``HeartbeatRequest``'s own core fields keep
# ``extra="forbid"``. A control plane that predates this field entirely still 422s the whole beat;
# that half is absorbed on the AGENT, which re-sends the core beat without the snapshot
# (``phaze.tasks.heartbeat.send_heartbeat``) -- so the snapshot can never cost liveness.


class EffectiveConfigLastReload(BaseModel):
    """One reload attempt, as the agent's OWN :class:`~phaze.runtime_config.RuntimeConfigStore` last
    resolved it -- mirrors :class:`phaze.runtime_config.ReloadResult` (a dataclass, not a pydantic
    model, so it cannot ride a strict wire schema unmodified) with the ``changes``/``applier_errors``
    maps dropped: a value that changed on THIS agent is already visible in ``values`` below, and
    appliers are a control-plane-only concept today (phaze-mvq8z.7/.8) with none registered on the
    agent role, so there is nothing yet to report from that map.
    """

    model_config = ConfigDict(extra="ignore")

    source: str = Field(max_length=EFFECTIVE_CONFIG_LABEL_MAX_LENGTH)
    """A :data:`phaze.runtime_config.ReloadSource` -- a plain string on the wire (version tolerance, above)."""
    outcome: str = Field(max_length=EFFECTIVE_CONFIG_LABEL_MAX_LENGTH)
    """A :data:`phaze.runtime_config.ReloadOutcome` -- a plain string on the wire (version tolerance, above)."""
    error: str | None = Field(default=None, max_length=LAST_RELOAD_ERROR_MAX_LENGTH)
    at: float
    """``time.time()`` when this reload attempt completed (``ReloadResult.at``)."""


class EffectiveConfig(BaseModel):
    """The calling agent's own resolved reloadable-config snapshot (ADR-0019 (runtime config hot-reload) §15): what is
    ACTUALLY in force on THIS agent process, not merely what the control plane last intended.

    Shaped to match ``build_runtime_config_pane_context`` (``routers/admin_runtime_config.py``), the
    SAME reloadable-key table the control plane's own admin pane renders, so the per-agent activity
    panel (``routers/admin_agents.py``) can reuse that rendering rather than inventing a second shape.
    """

    model_config = ConfigDict(extra="ignore")

    values: dict[str, JsonValue]
    """Every reloadable key's current value, as the agent's own ``RuntimeConfig`` dumped it. A plain
    mapping, NOT ``RuntimeConfig``, so a key this server does not know -- or one it knows that this
    agent does not send -- is accepted (version tolerance, above). The values were already validated
    by that ``RuntimeConfig`` on the agent before they could be in force there; this server only
    stores and displays them."""
    sources: dict[str, str]
    """Per reloadable key: which :data:`phaze.runtime_config.Layer` resolved its current value
    (``override``/``file``/``env``/``default``) -- a plain string on the wire (version tolerance, above)."""
    restart_only_keys: list[str] = Field(default_factory=list)
    """Names only, NEVER values (several restart-only keys carry credentials --
    :data:`phaze.runtime_config.RESTART_ONLY_KEYS`'s own module docstring states this rule; this
    field exists purely so an operator can see WHICH settings on this agent cannot be changed live)."""
    last_reload: EffectiveConfigLastReload | None = None
    """The agent's own last reload attempt (startup, sighup, file, or poll) -- ``None`` only in the
    window before the agent's first reload completes, which today never survives past startup."""


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
    """Which lane worker sent this beat (phaze-30fo): analyze|meta|io.

    OPTIONAL and defaulting to None on purpose. Every lane now heartbeats, but an agent
    running an older image (or in all-mode, where there is no lane split) posts without
    this field, and a required field would 422 every one of those beats -- turning a
    liveness fix into a liveness outage during a rolling deploy. `None` means
    "unlaned beat", which the handler stores exactly the way it always did.
    """
    effective_config: EffectiveConfig | None = None
    """The agent's resolved reloadable-config snapshot (phaze-mvq8z.9, ADR-0019 (runtime config hot-reload) §15).

    OPTIONAL and defaulting to None for the SAME rolling-deploy reason as ``lane`` immediately
    above: an agent running an image built before this bead posts without this field, and a
    required field would 422 every one of those beats. ``routers/agent_heartbeat.py`` excludes a
    ``None`` value from storage (mirroring its existing ``exclude={"lane"}`` treatment) so an
    old-shape beat's persisted `last_status` stays byte-identical to before this bead, with no
    stray `effective_config: null` key. The other direction -- a NEW agent against a control plane
    that predates this field -- is handled on the agent (phaze-mvq8z.20; see the version-tolerance
    note above :class:`EffectiveConfigLastReload`).
    """
