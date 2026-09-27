# ADR-0019 (runtime config hot-reload): layered sources, triggers, reloadable set, LAN-posture admin API

- **Status:** accepted
- **Date:** 2026-09-27
- **Bead:** `phaze-mvq8z.1` (epic `phaze-mvq8z`)
- **Supersedes / superseded by:** supersedes the planning placeholder `phaze-y6af8` (filed
  2026-09-27), closed in favour of the `phaze-mvq8z` molecule this ADR belongs to. Nothing
  supersedes this ADR.

______________________________________________________________________

## 1. Context

Every agent/control-plane setting is read once at process start — pydantic settings from env /
`*_FILE`, `backends.toml` via `PHAZE_BACKENDS_CONFIG_FILE` — so tuning anything today means
recreating containers, and a container recreation drops in-flight jobs. Tuning a live deployment
(phaze-demo, 2026-09-27) took two analyze-lane recreations — `PHAZE_LANE_ANALYZE_CONCURRENCY` and
`WORKER_MAX_JOBS`, then `worker_process_pool_size`, which separately gates `analysis_child`
subprocesses via an `asyncio.Semaphore` — and each restart killed and re-queued up to 16
multi-hour analyses. homelab additionally force-recreates `phaze-api`/`phaze-worker` on every
`backends.toml` change.

**Goal:** operators change safe-to-change knobs on a running worker/control-plane process and they
take effect for NEW work, without a restart and without dropping in-flight jobs. A key that cannot
be changed live is reported as restart-only rather than silently ignored.

This ADR records the architecture and the operator decisions behind it. It does not itself
implement anything — the child beads listed in §16 do that.

## 2. Decision, in one paragraph

Introduce a **reloadable-key subset** of runtime configuration, resolved through a strict
**layer precedence** (§3), rebuilt as an immutable, validated snapshot on any of three
**triggers** (§4) and swapped in atomically with registered appliers (§6). Everything outside
the reloadable set keeps today's static, restart-only path. The **admin config API matches the
existing unauthenticated private-LAN operator posture** (§10), which is a new authorization-
boundary decision, compensated for by audit logging on every reload attempt.

## 3. Layer precedence

For a reloadable key only, highest wins:

1. **DB override** (written by the admin API/UI, §10)
2. **Watched `runtime.toml`** (directory-mounted, §4, §12)
3. **Env / `*_FILE`** (today's path, unchanged)
4. **Code defaults** (`derive_sizing()`, `src/phaze/services/analysis_sizing.py:396`, for sizing
   defaults specifically)

A **restart-only key** (DB/queue/Redis URLs, auth tokens, agent id/lane, TLS/CA material, scan
roots, and anything else not on the reloadable list in §5) never participates in this ladder. A
change to one of these arriving via the watched file or the admin API is **rejected** and reported
as "requires restart" — it is never silently accepted and never silently ignored.

## 4. Reload triggers

**Operator decision (2026-09-27, AskUserQuestion, planning session for `phaze-y6af8`; durable
record: epic `phaze-mvq8z` + this ADR).** Question as put: *"Which reload trigger(s) do you
want?"* Answer as given (selected option labels, verbatim): *"Watched config file"*, *"SIGHUP"*,
*"Admin API + UI"* — all three, not a single winner.

- **SIGHUP** — a phaze-owned handler (`loop.add_signal_handler`, matching the
  `src/phaze/agent_watcher/__main__.py` idiom) in every process that hosts reloadable config:
  `api`, control worker, and agent-lane workers.
- **Watched directory** — `watchdog` (already a dependency; see §13 for why it, not
  `watchfiles`) observes the runtime-config directory, debounces, and compares a content hash
  before triggering a reload. The directory is watched rather than the file itself for a mount-
  semantics reason, not a style preference — see §12.
- **Admin API + UI** — writes land in the DB override table (§3, §10) directly; no file or
  signal round-trip is needed for that layer.

All three converge on the same reload pipeline (§6): a trigger only says *when* to rebuild, never
what the rebuilt snapshot contains.

## 5. Reloadable vs. restart-only keys

**Reloadable** (subject to §3's ladder, §6's validate-and-swap): analyze/lane concurrency and
`worker_max_jobs` (via the SAQ Worker subclass, §8), the analysis process-pool size and the
resizable analysis semaphore, TF/OMP thread-count env for *newly started* analysis children,
`analysis_stall_timeout_sec`, log level, `cloud_route_threshold_sec`, and `backends.toml` (subject
to the removal policy in §9).

**Restart-only** (rejected if offered through a reloadable-layer source, reported as such): DB,
queue and Redis connection URLs; auth tokens and credentials; agent id and lane assignment;
TLS/CA material; scan roots. This list is enumerated by the runtime-config core bead
(`phaze-mvq8z.4`), which is the authoritative source for the exact key set — this ADR states the
*policy* (why a key sits on one side or the other), not a copy of the list that would drift from
it.

A key already growing on TCP/process/subprocess-pool concurrency is reloadable in the sense that
the *pool size* is reloadable; growth spawns additional workers or slots, and shrink happens by
attrition (letting the current occupant finish rather than killing it) — this is the shape used
uniformly across §7's semaphore/pool appliers and §8's Worker subclass, and is what "without
dropping in-flight jobs" means operationally.

## 6. Reload mechanism: validate, then atomic swap, then apply

A reload — from any of §4's triggers — builds a **fresh immutable snapshot off the event loop**,
validates it (pydantic field validation, plus `derive_sizing` coherence: an override that would
oversubscribe the host's cores is rejected, not clamped), and only then **atomically swaps the
reference** that live code reads. On validation failure, the process **keeps the last-good
snapshot** and reports a structured error — a bad reload never leaves the process on a partially
applied config, and never crashes it.

A successful swap runs every registered **applier** with `(old, new)`, so each subsystem decides
for itself what "resizable" means for its own resource (§7, §8, §9).

**Audit and telemetry**, on every attempt, successful or not: the trigger source
(`sighup|file|api|poll`), every changed key with its old and new value, and the result. Metrics:
`phaze_config_last_reload_successful` and `phaze_config_last_reload_timestamp_seconds` (names to
be verified against this repo's existing telemetry conventions — see
`docs/design/0017-telemetry-export-topology.md` — before the metrics-emitting bead ships them).
This audit trail is also the compensating control referenced in §10.

## 7. Appliers: semaphore, pool, thread env, timeouts, log level

Covered by `phaze-mvq8z.7` (semaphore/pool/thread-env/stall-timeout) and `phaze-mvq8z.8`
(`backends.toml`, `cloud_route_threshold_sec`, log level). The pattern is uniform: each applier
receives `(old, new)` from the swap in §6 and does the minimum needed to bring the live resource
in line — grow the analysis semaphore and telemetry slot pool immediately; apply new TF/OMP
thread-count env only to analysis children started *after* the swap (an already-running child
keeps its inherited env); re-apply `configure_logging` (already idempotent); adopt
`analysis_stall_timeout_sec` for the *next* job's liveness window, not a job already in flight;
adopt `cloud_route_threshold_sec` for the next routing decision.

## 8. The SAQ Worker subclass (lane concurrency / `worker_max_jobs`)

SAQ 0.26.4 fixes `Worker` concurrency at start: `Worker.start()` spawns `concurrency`
self-perpetuating `_process()` loops once (`saq/worker.py:200-201`, `:440-453`) and never re-reads
that number afterward. Growing concurrency upstream has no supported path other than restarting
the worker; shrinking is not supported at all.

**Operator decision (2026-09-27, AskUserQuestion, planning session for `phaze-y6af8`; durable
record: epic `phaze-mvq8z` + this ADR).** Question as put: *"Lane concurrency / worker_max_jobs:
SAQ 0.26.4 fixes Worker concurrency at start ... How should phaze handle live changes?"* Answer as
given (selected option label, verbatim): *"Worker subclass (Recommended)"*.

**Decision:** a phaze-owned `Worker` subclass tracks a *target* concurrency that can change live.
Growing spawns additional `_process()` loops to reach the new target immediately; shrinking stops
relaunching loops once their current job finishes, converging on the lower target by attrition —
never a forced kill of an in-flight job.

**Upstream-drift risk, stated explicitly (required by this bead's acceptance):** `_process()` and
the loop-management internals the subclass hooks into are SAQ's private implementation, not a
documented, versioned public API. A future SAQ release can change or remove that internal shape
without treating it as a breaking change from SAQ's own point of view, and the subclass would then
fail silently or loudly depending on exactly what changed. Mitigation is process, not code: SAQ
stays pinned to an exact version tested against this subclass (mirroring the litellm exact-minor
pin in `CLAUDE.md`'s pins table, for the same reason — an unpinned dependency this load-bearing is
a supply-chain-shaped risk even without a security angle), and a SAQ version bump is treated as a
compatibility review of this subclass specifically, not a routine dependency update.

## 9. `backends.toml` reload and the backend-removal policy

**Operator decision (2026-09-27, AskUserQuestion, planning session for `phaze-y6af8`; durable
record: epic `phaze-mvq8z` + this ADR).** Question as put: *"backends.toml reload that REMOVES a
backend while cloud_job rows still reference it: what should happen?"* Answer as given (selected
option label, verbatim): *"Reject while in-flight (Recommended)"*.

**Decision:** a `backends.toml` reload fully re-validates the new file and swaps the backend
registry atomically (§6), but a reload that would **remove** a backend still referenced by an
in-flight `cloud_job` row is **rejected outright** — the whole reload fails closed, not just the
removal of that one backend, so an operator sees one clear error rather than a partially-applied
registry. The backend becomes removable once no `cloud_job` row references it.

## 10. Admin config API authorization posture — a new authorization-boundary decision

No operator authentication exists anywhere in phaze's admin surfaces today
(`src/phaze/routers/admin_agents.py:41-43`'s "Auth posture: NO `get_authenticated_agent`
dependency — operator pages are open on the private LAN"). The precedent for a runtime-mutable
piece of DB-backed config is `route_control` / `pipeline_stage_control`: a degrade-safe fresh
read, a thin POST endpoint, a header pill — no auth layer of its own, because the whole admin
surface sits inside one trust boundary (the private LAN) rather than each page inventing its own.

**Operator decision (2026-09-27, AskUserQuestion, planning session for `phaze-y6af8`; durable
record: epic `phaze-mvq8z` + this ADR).** Question as put: *"Admin auth: every operator page today
... is deliberately unauthenticated on the private LAN ... What should it do?"* Answer as given
(selected option label, verbatim): *"Match LAN posture (Recommended)"*.

**Decision:** the reloadable-config admin API and UI (DB-override writes, §3's top layer) carry
**no authentication of their own**, matching every other operator-facing admin surface in this
repo. This is stated here as a **new authorization-boundary decision**, distinct from
`docs/design/0008-changes-review-approval-boundary.md`: that ADR governs who may authorize a
*catalog* mutation — a filename, destination, or tag change to a discovered media file — and says
nothing about administrative reconfiguration of the running system itself. The two boundaries
protect different things (catalog content vs. process configuration) and this decision does not
narrow, extend, or reinterpret ADR-0008 in either direction.

**Compensating control:** every reload attempt is audit-logged regardless of trigger (§6) —
source, actor where the trigger is the admin API, every changed key's old and new value, and the
result. An unauthenticated write surface on a trusted LAN is the accepted posture everywhere else
in this codebase; the audit trail is what makes a config change **attributable after the fact**
even though it is not **gated before the fact** — the same trade this repo already makes for every
other admin page, extended to config rather than invented for it.

## 11. Two settings singletons — a pre-existing divergence this ADR's layering must not widen

A control process today runs **two** settings singletons side by side: the import-time
`phaze.config.settings` module attribute (~15 importers) and the `lru_cache`d `get_settings()`
(~40 importers). A reload that updates one and not the other would make "the current config"
depend on which importer a given piece of code happens to use — silently, and per-process. This
ADR's atomic-swap design (§6) assumes a **single** point of truth to swap; `phaze-mvq8z.2`
collapses the two singletons onto `get_settings()` ahead of the reload core landing, and is a
precondition for §6, not an independent cleanup.

## 12. Why the watch is on a directory, not a file

A single-file Docker bind mount pins the inode at mount time: an atomic-rename edit on the host
(the usual safe way to update a config file) never reaches the container, because the mount still
points at the old inode. A Kubernetes `subPath` ConfigMap mount has the same failure shape — it
never updates after the pod starts, `subPath` being exactly the case kubelet's ConfigMap-refresh
mechanism does not cover. Watching the **directory** the file lives in, debouncing rapid
successive writes, and comparing a content hash before triggering a reload sidesteps both mount
shapes; k8s burst pods are explicitly out of scope for this mechanism (§14).

## 13. Planner's decision: reuse `watchdog`, not `watchfiles`

Reusing `watchdog` for the directory watch in §4/§12 is the **planner's** decision, not the
operator's — it is already a vendored, proven dependency in this codebase, and introducing
`watchfiles` alongside it would add a second file-watching dependency with no capability gain
identified during planning. Whether a *polling* observer is additionally needed (as a fallback for
a filesystem `watchdog`'s native backend cannot watch reliably) is left to the container
smoke-test bead, `phaze-mvq8z.3`, which verifies SIGHUP delivery and directory-watch events inside
the real `api`/worker/agent containers rather than deciding it in the abstract here.

## 14. Propagation to remote agents and out-of-scope surfaces

**Operator decision (2026-09-27, AskUserQuestion, planning session for `phaze-y6af8`; durable
record: epic `phaze-mvq8z` + this ADR).** Question as put: *"Propagation latency to remote agents
for admin-API changes: is ~30s (heartbeat-cadence poll) acceptable?"* Answer as given (selected
option label, verbatim): *"~30s is fine (Recommended)"*.

**Decision:** control-plane processes (`api`, control worker) that share the same Postgres learn
of a DB-override reload via **Postgres `NOTIFY`**, with a fallback poll for the case a `NOTIFY` is
missed. There is no existing control-plane → agent push channel — the heartbeat response is a
bare `204` with no body, and `HeartbeatRequest` is `extra="forbid"` — so a **remote agent instead
polls a new agent-authenticated `GET /api/internal/agent/config`** on the existing heartbeat
cadence, accepting the same ~30 s worst-case propagation latency the operator accepted above,
rather than building a second, lower-latency channel for this one use.

**Explicitly out of scope:** Kubernetes burst pods. Each burst pod is one Job per file and already
picks up `ConfigMap` changes on its **next** Job — there is no long-lived process inside it for
"live" reload to mean anything, so this mechanism does not extend there.

## 15. Effective-config visibility

The resolved config (value, source layer, and whether a key is restart-required) rides a schema-
versioned field on the existing heartbeat into `agents.last_status`, surfacing in the per-agent
activity panel; the control plane's own effective config renders on the admin page. This makes
§3's layering inspectable rather than something an operator has to reconstruct from separate env,
file, and DB reads. Covered by `phaze-mvq8z.9`.

## 16. Consequences

- Every reloadable key gets a validated, atomic, audited path to change without a restart; every
  other key keeps today's static, restart-only behavior and is reported as such rather than
  silently accepting or silently dropping an attempted live change.
- The admin config API is deliberately unauthenticated, matching this codebase's existing LAN
  posture, with audit logging as the compensating control (§10) — no new authentication surface
  is introduced anywhere in this molecule.
- The SAQ Worker subclass (§8) is a standing maintenance obligation: every SAQ version bump
  requires a compatibility check against it, not just the usual dependency-update pass.
- `backends.toml` reloads fail closed on an in-flight-referencing removal (§9), never partially
  applying a registry change.
- Child beads: `phaze-mvq8z.2` (singleton collapse, precondition for §11), `phaze-mvq8z.4`
  (reloadable-key core: snapshot, layering, validate-and-swap, appliers, telemetry), `phaze-mvq8z.5`
  (triggers: SIGHUP + directory watch), `phaze-mvq8z.6` (DB override table + admin API/UI +
  Postgres `NOTIFY`), `phaze-mvq8z.7` (semaphore/pool/thread-env/stall-timeout appliers),
  `phaze-mvq8z.8` (backends.toml/route-threshold/log-level appliers), `phaze-mvq8z.9` (agent config
  channel + effective-config reporting), `phaze-mvq8z.10` (SAQ Worker subclass), `phaze-mvq8z.3`
  (container-level trigger verification), `phaze-mvq8z.11` (end-to-end retune demo), `phaze-mvq8z.12`
  (operator guide).
