# Phase 48: Compute-agent type - Research

**Researched:** 2026-06-25
**Domain:** Distributed-agent registry extension (SQLAlchemy model + Alembic migration + SAQ per-agent queue + FastAPI/Jinja2/HTMX admin UI + import-boundary test)
**Confidence:** HIGH — this phase extends a fully-traced, already-shipped subsystem (v4.0 Distributed Agents, phases 24-29 + 46). Every touch-point is a named existing file.

## Summary

Phase 48 adds a `kind="compute"` variant on top of the **existing** file-server Agent machinery. The crucial finding from tracing the code: **the compute agent's job-pull and result-PUT paths require ZERO new mechanics.** A compute agent runs the *same* `phaze.tasks.agent_worker` entrypoint, drains the *same* `phaze-agent-<id>` SAQ PostgresQueue (via `AgentTaskRouter` / `build_pipeline_queue`), and PUTs results to the *same* `PUT /api/internal/agent/analysis/{file_id}` endpoint via the same `PhazeAgentClient`. The only thing that distinguishes a compute agent is **(a) a `kind` marker on the `agents` row, (b) empty `scan_roots`, and (c) no media volume mount** (the mount is a Phase 51 compose concern).

So Phase 48 reduces to four small, surgical changes: a `kind` column (model + Alembic migration 024), a CLI flag (`agents add --kind compute` that relaxes the scan-roots requirement), a config relaxation (`AgentSettings` currently *requires* non-empty `scan_roots` for `PHAZE_ROLE=agent` — a compute agent has none), and an admin-table Kind badge (the `agents_table.html` partial already renders liveness + queue depth; `kind` rides along free on the loaded `Agent` row). The import-boundary requirement (success criterion 3, ORM half) is **already enforced** by `tests/test_task_split.py` because the compute agent *is* `agent_worker`; Phase 48 should reaffirm/extend that test rather than invent a new pattern.

**Primary recommendation:** Add a `kind` String column to `Agent` (default `'fileserver'`, CHECK in `('fileserver','compute')`), migration 024 (additive, backfill existing → `'fileserver'`), a `--kind` CLI flag that makes `--scan-roots` optional-and-empty when compute, an `AgentSettings.kind` field that relaxes the empty-scan-roots validation gate, and a `_kind_badge.html` partial in the admin table. Do **not** fork the worker, the queue router, or the result-PUT endpoint.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CLOUDAGENT-01 | Operator registers a compute agent: empty scan roots, no media, explicit `kind="compute"` marker | `Agent` model + `kind` column (migration 024); CLI `agents add --kind compute` relaxing the `--scan-roots` requirement; `AgentSettings.kind` relaxing the empty-scan-roots config gate (`config.py:512`) |
| CLOUDAGENT-02 | Compute agent drains per-agent SAQ queue + PUTs analysis results over HTTP exactly like a file-server agent; no media/ORM access (only SAQ Postgres broker + cache Redis + HTTP API) | **No new mechanics needed** — same `agent_worker` (`agent_worker.py`), same `phaze-agent-<id>` queue (`AgentTaskRouter._queue_for` / `build_pipeline_queue`), same `PUT /api/internal/agent/analysis/{file_id}` (`agent_analysis.py`), same `PhazeAgentClient`. ORM import boundary already enforced by `tests/test_task_split.py` |
| CLOUDAGENT-03 | Admin page distinguishes compute agents (kind badge + liveness + queue depth) | `admin/partials/agents_table.html` already renders the status pill (`agent_liveness.classify`) + queue depth (`last_status.queue_depth`); add a `_kind_badge.html` partial + Kind column. `kind` is free on the `Agent` rows loaded by `admin_agents._load_agents` |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| `kind` capability marker | Database / Storage (`agents.kind` column) | API (CLI write, admin read) | The marker is durable agent identity — it belongs on the `agents` row alongside `scan_roots`, not in config alone |
| Compute-agent registration | CLI (`phaze agents add`) | Database | Registration is an operator action that mints a token + inserts a row — exactly the existing `agents add` path, extended with `--kind` |
| Worker self-knowledge ("am I compute?") | Agent process config (`AgentSettings`) | API (`/whoami` could echo `kind`) | The worker process must accept empty scan roots when compute — that gate lives in `AgentSettings` validation |
| Job pull (drain per-agent queue) | Agent worker (SAQ PostgresQueue broker) | — | Identical to file-server agent — no tier change |
| Result PUT | Agent worker → API (`/api/internal/agent/analysis`) | — | Identical to file-server agent — no tier change |
| Kind badge + liveness + queue depth display | Frontend Server (FastAPI + Jinja2) | — | `admin_agents` router + `agents_table.html` already own agent presentation |
| ORM / media import-boundary enforcement | Test harness (`tests/test_task_split.py`) | — | The compute agent runs `agent_worker`, already covered by the subprocess import-boundary test |

## Standard Stack

No new libraries. This phase uses only what the project already ships. Verified against the current `agents` subsystem code.

### Core (already in the project)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| SQLAlchemy | >=2.0.48 | `Agent` model + `kind` `Mapped[str]` column | [VERIFIED: codebase grep — `src/phaze/models/agent.py`] |
| Alembic | >=1.18.4 | Migration 024 (additive `kind` column) | [VERIFIED: codebase — `alembic/versions/`, head is `023`] |
| SAQ | >=0.26.3 (`saq[redis]>=0.26.4` per Phase 33) | Per-agent PostgresQueue drain (unchanged) | [VERIFIED: codebase — `agent_worker.py`, `queue_factory.py`] |
| FastAPI + Jinja2 + HTMX 2.x + Alpine.js + Tailwind | per CLAUDE.md | Admin agents page Kind badge | [VERIFIED: codebase — `admin_agents.py`, `agents_table.html`] |
| argparse (stdlib) | 3.14 | `phaze agents add --kind` CLI flag | [VERIFIED: codebase — `src/phaze/cli/__init__.py`] |

**Installation:** none. No dependency changes.

## Package Legitimacy Audit

Not applicable — this phase installs **no external packages**. All changes use libraries already pinned in `pyproject.toml` and already present in `uv.lock`. (Per protocol: when a phase adds no packages, the audit is a no-op; slopcheck not invoked.)

## Architecture Patterns

### System Architecture Diagram

```
                          CONTROL PLANE (app server, has Postgres + Redis + ORM)
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │  phaze agents add --kind compute --id oci-a1 --name "OCI A1"                   │
  │        │  (CLI, src/phaze/cli) → INSERT agents row (kind='compute',            │
  │        │                          scan_roots=[]) + mint bearer token           │
  │        ▼                                                                       │
  │  agents table  ◄──── migration 024 adds `kind` column (default 'fileserver')   │
  │        │                                                                       │
  │        │  admin_agents._load_agents (SELECT *) → classify() liveness           │
  │        ▼                                                                       │
  │  GET /admin/agents → agents_table.html: [Kind badge][Status pill][Queue depth] │
  │        ▲                                                                       │
  │        │ POST /api/internal/agent/heartbeat (last_seen_at + queue_depth)       │
  │        │ PUT  /api/internal/agent/analysis/{file_id}  (result upsert)          │
  │        │                                                                       │
  │   SAQ PostgresQueue broker  ◄── enqueue (Phase 49 routes ≥90min jobs here) ──┐ │
  └────────┼──────────────────────────────────────────────────────────────────┼─┘
           │ (HTTP only — bearer token)          (Postgres broker DSN)         │
  ─────────┼────────────────────────────────────────────────────────────────  │
           ▼  COMPUTE AGENT (OCI A1, arm64 image — Phase 47)                    │
  ┌──────────────────────────────────────────────────────────────────────────┐│
  │  phaze.tasks.agent_worker  (PHAZE_ROLE=agent, PHAZE_AGENT_KIND=compute)    ││
  │   • startup: whoami probe, models check, essentia process pool, heartbeat  ││
  │   • drains phaze-agent-oci-a1 queue  ─────────────────────────────────────┘│
  │   • runs analyze_file (essentia) — NO media mount, NO ORM, NO phaze.database│
  │   • PUTs result via PhazeAgentClient ─────────────────────────────────────►│
  │  IMPORT BOUNDARY (test_task_split): no phaze.database / .session /          │
  │                                     sqlalchemy.ext.asyncio                   │
  └──────────────────────────────────────────────────────────────────────────┘
```

(In Phase 48 the compute agent has no files to analyze yet — duration routing is Phase 49, file push is Phase 50. Phase 48 proves the *type* registers, is visible, and drains/PUTs through the identical path.)

### Recommended file touch-set (smallest viable change)
```
src/phaze/models/agent.py                       # + kind column + CHECK constraint
alembic/versions/024_add_agents_kind.py         # NEW: additive column, backfill, CHECK
src/phaze/cli/__init__.py                        # + --kind flag, relax --scan-roots for compute
src/phaze/config.py                              # + AgentSettings.kind, relax empty-scan-roots gate
src/phaze/templates/admin/partials/_kind_badge.html        # NEW: kind badge partial
src/phaze/templates/admin/partials/agents_table.html       # + Kind column rendering _kind_badge
tests/test_task_split.py                         # reaffirm/extend ORM import boundary for compute
tests/test_cli/...                               # compute registration (empty scan roots) test
tests/test_models/...                            # kind column default + CHECK constraint test
tests/test_migrations/...                        # migration 024 up/down test
tests/test_routers/test_admin_agents... (or new) # kind badge renders for compute agents
```

### Pattern 1: Additive `kind` column with CHECK constraint
**What:** Mirror the existing `agents.id_charset` CheckConstraint pattern already on the model.
**When to use:** Adding the capability marker.
**Example:**
```python
# Source: existing pattern at src/phaze/models/agent.py:28-38 [VERIFIED: codebase]
kind: Mapped[str] = mapped_column(
    String(16), nullable=False, server_default=text("'fileserver'")
)
__table_args__ = (
    CheckConstraint("id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="id_charset"),
    CheckConstraint("kind IN ('fileserver', 'compute')", name="kind_enum"),
)
```
Migration 024 follows the additive pattern (server_default backfills existing rows to `'fileserver'`; migration head is `023`, down_revision `'022'` → new revision `'024'`, down_revision `'023'`). [VERIFIED: codebase — `alembic/versions/023_add_scheduling_ledger_job_policy.py`]

### Pattern 2: CLI flag that conditionally relaxes scan-roots
**What:** `agents add --kind {fileserver,compute}` (default `fileserver`). When `compute`, `--scan-roots` becomes optional and defaults to `[]`; `validate_scan_roots([])` must pass for compute. The existing `add_agent()` signature gains `kind` and sets it on the `Agent(...)` constructor.
**Current blocker:** `add.add_argument("--scan-roots", required=True)` and `validate_scan_roots` rejects empty entries — both must become conditional on kind. [VERIFIED: codebase — `src/phaze/cli/__init__.py:62-67,108-114`]

### Pattern 3: Worker config self-knowledge
**What:** `AgentSettings` (`config.py:359`) currently raises `ValueError` if `scan_roots` is empty for any agent (`config.py:512-513`). A compute agent process has empty scan roots. Add `kind: str = Field("fileserver", validation_alias=AliasChoices("PHAZE_AGENT_KIND", "kind"))` and gate the scan-roots requirement on `kind != "compute"`. [VERIFIED: codebase — `src/phaze/config.py:395-403,508-513`]
**Decision to make (Claude's discretion):** whether the worker derives `kind` purely from the `PHAZE_AGENT_KIND` env var, or also has `/whoami` echo `kind` (the `AgentIdentity` response schema is "loose" and can add fields non-breakingly — `schemas/agent_identity.py`). Recommendation: **env var is sufficient for Phase 48**; echoing `kind` on `/whoami` is a nice cross-check but optional. The worker does not branch behavior on kind in Phase 48 — it only needs to accept empty scan roots.

### Pattern 4: Kind badge in the admin table (mirror `_status_pill.html`)
**What:** New `_kind_badge.html` partial mirroring the geometry of `_status_pill.html` (`text-xs font-semibold px-2 py-0.5 rounded-full`). Render a "COMPUTE" badge (e.g. indigo/violet to read as "cloud capacity") vs. a "FILE SERVER" badge (neutral). Add a Kind column (or inline next to agent name) in `agents_table.html`. The `Agent` row already carries `kind` once the column exists — `_load_agents` does `SELECT Agent` with no projection, so no router change. [VERIFIED: codebase — `admin_agents._load_agents:67`, `agents_table.html:45-62`, `_status_pill.html`]
**Liveness + queue depth already work for compute agents unchanged:** `agent_liveness.classify` is kind-agnostic; `last_status.queue_depth` is populated by the heartbeat the compute agent already sends (`tasks/heartbeat.py`).

### Anti-Patterns to Avoid
- **Forking `agent_worker` into a `compute_worker`.** The drain + PUT path is identical; a fork doubles maintenance and risks import-boundary drift. CLOUDAGENT-02 explicitly says "exactly like a file-server agent."
- **Forking the queue router or a separate "cloud queue" in Phase 48.** Per-agent `phaze-agent-<id>` queues already isolate work. A named cloud queue is a Phase 49/51 concern (`CLOUDDEPLOY-02` lists "cloud queue name" as a Phase 51 config knob); do not pre-build it here.
- **Storing `kind` only in config, not the DB.** The admin page (CLOUDAGENT-03) reads from the `agents` table, not from any agent's config — the marker MUST be a column.
- **Making `kind` nullable or free-text.** Use `NOT NULL DEFAULT 'fileserver'` + CHECK so existing rows backfill cleanly and bad values are rejected at the DB.
- **Reducing the worker's registered SAQ function set as a *requirement*.** Empty scan roots already mean `scan_directory`/`scan_live_set`/`extract_file_metadata` are never enqueued to a compute agent. Trimming the function list is optional hardening (see Open Questions), not load-bearing for Phase 48.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-agent job queue for the compute agent | A new cloud queue / dispatcher | Existing `AgentTaskRouter` + `build_pipeline_queue` (`phaze-agent-<id>`) | Already battle-tested; carries the `apply_project_job_defaults` + deterministic-key hooks the dashboard counters depend on |
| Result upload from compute agent | New endpoint | Existing `PUT /api/internal/agent/analysis/{file_id}` (`agent_analysis.py`) + `PhazeAgentClient` | Idempotent upsert (`pg_insert.on_conflict_do_update`) already handles re-drives; windows-replace already wired (Phase 31) |
| Liveness classification for compute agents | Compute-specific status logic | `agent_liveness.classify` / `sort_key` (kind-agnostic) | 5-state pill already proven; compute agents heartbeat identically |
| Queue-depth display | New telemetry | `agents.last_status.queue_depth` (heartbeat-populated) | `agents_table.html:53-57` already renders it |
| ORM/Postgres-free enforcement | New AST/import-graph framework | Extend `tests/test_task_split.py` (subprocess import check) | The compute agent IS `agent_worker`; the test already runs against it |

**Key insight:** Phase 48 is a *typing/labeling* phase, not a *mechanism* phase. The mechanisms (queue, PUT, heartbeat, liveness, import boundary) all shipped in v4.0. Resist the urge to build cloud-specific infrastructure — that's phases 49-51.

## Runtime State Inventory

This phase ADDS a capability rather than renaming, but it introduces a new schema column and a new config var, so the rename/migration lens still applies to "what runtime state must be seeded/backfilled."

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | `agents` table gains a `kind` column. Existing rows (incl. `legacy-application-server` + any real file-server agents) must backfill to `'fileserver'`. | Data migration: `server_default='fileserver'` in migration 024 backfills automatically; verify with an up/down migration test |
| Live service config | A compute agent process needs a new `PHAZE_AGENT_KIND=compute` env var (no media mount). The cloud-agent compose that sets it is **Phase 51** (`docker-compose.agent.yml` currently mounts `${SCAN_PATH:?...}:/data/music:ro` on the worker — a compute agent must NOT). Phase 48 only needs `AgentSettings` to *accept* the var. | Code: add `AgentSettings.kind`; compose change deferred to Phase 51 |
| OS-registered state | None — no Task Scheduler / launchd / pm2 registrations involved. | None — verified by absence of any such integration in the repo |
| Secrets/env vars | `PHAZE_AGENT_KIND` is a non-secret env var (not a token). The compute agent still uses the existing `PHAZE_AGENT_TOKEN` bearer minted by `agents add`. No new secret. | None — `kind` is not sensitive |
| Build artifacts / installed packages | None — no package rename, no egg-info churn. The compute agent runs the Phase 47 arm64 GHCR image (`ghcr.io/simplicityguy/phaze:<version>-arm64`), already built. | None |

**Canonical question — after every file is updated, what runtime state still has stale info?** Only the `agents` table rows, handled by the migration's `server_default` backfill. Nothing else.

## Common Pitfalls

### Pitfall 1: `AgentSettings` validation rejects empty scan roots
**What goes wrong:** A compute agent boots with `PHAZE_AGENT_SCAN_ROOTS` unset/empty and `AgentSettings.__init__` raises `ValueError("AgentSettings.scan_roots is required when PHAZE_ROLE=agent ...")` (`config.py:512-513`) — the worker container crash-loops before it can drain a single job.
**Why it happens:** v4.0 assumed every agent owns scan roots (it owns FileRecords). Compute agents break that assumption.
**How to avoid:** Add `AgentSettings.kind` and gate the scan-roots requirement on `self.kind != "compute"`. Add a `test_config` case for `PHAZE_ROLE=agent` + `PHAZE_AGENT_KIND=compute` + empty scan roots → no error.
**Warning signs:** Worker exits non-zero at startup with the scan-roots ValueError.

### Pitfall 2: CLI `--scan-roots required=True` blocks compute registration
**What goes wrong:** `phaze agents add --kind compute --id oci-a1 --name "OCI A1"` fails argparse (`--scan-roots` is required) — operator can't register the agent at all (CLOUDAGENT-01 fails).
**Why it happens:** The flag is unconditionally `required=True` (`cli/__init__.py:109-114`); `validate_scan_roots` also rejects empties (`:62-67`).
**How to avoid:** Make `--scan-roots` optional with default `""`/`[]` when `--kind compute`; only enforce the absolute-path / non-empty rule for `fileserver`. Set `kind` on the `Agent(...)` insert.
**Warning signs:** argparse error or `ValueError: every scan root must be an absolute path` on compute registration.

### Pitfall 3: Migration ordering / head collision
**What goes wrong:** New migration uses the wrong `down_revision` and Alembic reports multiple heads, or `uv run alembic upgrade head` fails.
**Why it happens:** The current head is `023` (`down_revision='022'`). A migration that says `down_revision='022'` forks the tree.
**How to avoid:** `revision='024'`, `down_revision='023'`. Confirm with `uv run alembic heads` before/after. [VERIFIED: codebase — head is `023`]
**Warning signs:** `alembic` "Multiple head revisions are present" error.

### Pitfall 4: Misreading the import-boundary requirement (success criterion 3)
**What goes wrong:** Building a brand-new "media access" import test from scratch and missing that the existing `tests/test_task_split.py` already enforces the ORM half against `agent_worker` (the exact module the compute agent runs).
**Why it happens:** The phase brief says "import-boundary test passes" as if it's net-new.
**How to avoid:** Reaffirm/extend `test_task_split.py`. The ORM boundary (`phaze.database`, `phaze.tasks.session`, `sqlalchemy.ext.asyncio` banned) is already asserted via subprocess. For the "no media" half, note it is **runtime-enforced** (empty scan roots + no mount), not import-enforced — `analyze_file` legitimately reads the audio file path it's handed. The honest framing: the compute agent cannot reach the *ORM/app DB*; it analyzes a local scratch file path (Phase 50 push). Add a compute-specific assertion only if it buys clarity, e.g. asserting the compute registration path never imports media-walking scan code — but do not over-engineer.
**Warning signs:** A new test that duplicates `test_task_split` or that tries (incorrectly) to ban `essentia`/file-reading from the worker.

### Pitfall 5: Kind badge column added to the wrong template layer
**What goes wrong:** Editing only `agents_table.html` (the HTMX poll partial) but not the full-page `agents.html`, or vice-versa, so the badge flickers/absent on first load vs. poll.
**Why it happens:** The page renders the partial for HX requests and the full page otherwise (`admin_agents.page`), but both ultimately render the same `agents_table.html` partial via `{% include %}` — verify the include chain so the badge appears in both the first full-page load and every 5s poll.
**How to avoid:** Put the badge in the shared `agents_table.html` / a new `_kind_badge.html` include (the same place `_status_pill.html` is included at `:51`). One edit site covers both render paths. [VERIFIED: codebase — `admin_agents.py:94`, both branches render `agents_table.html`]

## Code Examples

### Worker drain + PUT path (UNCHANGED — proves CLOUDAGENT-02 needs no new code)
```python
# Source: src/phaze/tasks/agent_worker.py:212-247 [VERIFIED: codebase]
# Compute agent runs THIS module unchanged. Queue name from PHAZE_AGENT_QUEUE.
queue = build_pipeline_queue(_queue_name, get_settings().queue_url,
                             cache_redis_url=get_settings().redis_url, min_size=1, max_size=4)
settings = {
    "queue": queue,
    "after_process": increment_completed,
    "functions": [process_file, extract_file_metadata, fingerprint_file,
                  scan_live_set, scan_directory, execute_approved_batch],
    "concurrency": get_settings().worker_max_jobs,
    "startup": startup, "shutdown": shutdown,
}
```

### Existing import-boundary test to extend (CLOUDAGENT-02 / criterion 3)
```python
# Source: tests/test_task_split.py:33-68 [VERIFIED: codebase]
# Subprocess-isolated: imports phaze.tasks.agent_worker, asserts the ORM/async-engine
# modules never entered sys.modules. The compute agent IS agent_worker → already covered.
forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")
present = [m for m in forbidden if m in sys.modules]
# ... asserts `present == []` AND that saq.queue.postgres (the broker) IS present.
```

### CLI registration call site to extend (CLOUDAGENT-01)
```python
# Source: src/phaze/cli/__init__.py:75-89 [VERIFIED: codebase]
async def add_agent(session, agent_id, name, scan_roots):       # + kind param
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    agent = Agent(id=agent_id, name=name, token_hash=hash_token(token), scan_roots=scan_roots)  # + kind=kind
    session.add(agent); await session.commit(); return token
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Agent == file server that owns FileRecords + scan roots | Agent gains a `kind` discriminator; compute agents own no files, no scan roots | Phase 48 (this phase) | The "every agent has scan roots" invariant (config + CLI) must be relaxed for `kind='compute'` |
| Heartbeat as SAQ CronJob (could starve) | Heartbeat as asyncio background task (`_heartbeat_loop`) | Phase 46 | Compute agent liveness inherits the fix for free — no work needed |
| Redis-broker SAQ queues | Postgres-broker SAQ (`PostgresQueue` via `build_pipeline_queue`), Redis = cache only | Phase 36 | Compute agent reaches only the Postgres broker DSN + cache Redis + HTTP API (matches CLOUDAGENT-02's allowed surface exactly) |

**Deprecated/outdated:** none relevant to this phase.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | A compute agent runs the *same* `agent_worker` entrypoint/image (Phase 47 arm64) with no media mount, rather than a new entrypoint | Summary / Pattern 3 | LOW — if a separate entrypoint is desired, it's a larger change; but CLOUDAGENT-02 ("exactly like a file-server agent") strongly implies reuse |
| A2 | `kind` is best modeled as a `String(16)` + CHECK rather than a Postgres native ENUM | Pattern 1 | LOW — String+CHECK matches the existing `id_charset` pattern and avoids ENUM migration friction; a native ENUM would also work |
| A3 | Indigo/violet badge colour for "COMPUTE" (vs neutral for file server) reads as "cloud capacity" | Pattern 4 | LOW — pure cosmetic; planner/UI gate can adjust the Tailwind palette |
| A4 | The "no media access" half of criterion 3 is runtime-enforced (empty scan roots + no mount), not import-enforced (essentia must read the scratch file path it's given) | Pitfall 4 | MEDIUM — if the verifier insists on an *import* test banning file reads, that's infeasible; the planner should frame criterion 3 as "ORM/app-DB import boundary (test) + media isolation (no mount/empty roots, Phase 51 compose)" |
| A5 | `PHAZE_AGENT_KIND` env var is sufficient for the worker to accept empty scan roots; `/whoami` need not echo `kind` in Phase 48 | Pattern 3 | LOW — echoing `kind` is an easy optional add (response schema is loose) |

## Open Questions

1. **Should the compute worker register a reduced SAQ function set (only `process_file`/analyze, dropping `scan_directory`, `scan_live_set`, `extract_file_metadata`)?**
   - What we know: Empty scan roots mean media-walking jobs are never *enqueued* to a compute agent, so the full list is harmless. Trimming would be defense-in-depth.
   - What's unclear: Whether the planner wants the extra hardening now or defers it.
   - Recommendation: **Keep the full function list in Phase 48** (minimal change, CLOUDAGENT-02 is about the drain/PUT *path*, not the function registry). Revisit as optional hardening if duration routing (Phase 49) wants a stricter compute worker.

2. **Does `/whoami` (and `AgentIdentity`) need to echo `kind`?**
   - What we know: The response schema is intentionally loose (`schemas/agent_identity.py`) and can add fields non-breakingly. The worker's anti-misconfig probe already compares token-derived `agent_id` to `PHAZE_AGENT_QUEUE`.
   - What's unclear: Whether an analogous `kind` cross-check (token-row `kind` vs `PHAZE_AGENT_KIND`) is wanted.
   - Recommendation: Optional. If cheap, echo `kind` and add a soft mismatch warning; not required for the three success criteria.

3. **Badge placement — new column vs inline next to the agent name?**
   - Recommendation: New "Kind" column (leftmost or right after "Agent") keeps the table scannable; defer exact placement to the UI safety gate (`ui_phase: true`, `ui_safety_gate: true` are set in config).

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| uv | all dev/test/migration commands | ✓ (project constraint) | — | — |
| PostgreSQL 16+ | migration 024, `agents` table, SAQ broker | ✓ (compose) | 16+ | — |
| Alembic | migration 024 | ✓ (dep) | >=1.18.4 | — |
| arm64 essentia GHCR image | the compute agent at runtime (not Phase-48 code) | ✓ (Phase 47 shipped) | `<version>-arm64` | — |

No external dependency is missing for Phase 48 code work. (Actual OCI A1 provisioning + Tailscale are Phase 51 deploy concerns, out of scope here.)

## Validation Architecture

`workflow.nyquist_validation: true` → this section is REQUIRED.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (project standard) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) — per CLAUDE.md |
| Quick run command | `uv run pytest tests/test_cli tests/test_models tests/test_config -x -q` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (85% min, per CLAUDE.md) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CLOUDAGENT-01 | `agents add --kind compute` inserts row with `kind='compute'`, empty scan_roots, no error | unit | `uv run pytest tests/test_cli/test_agents_add.py -k compute -x` | ❌ Wave 0 (extend existing CLI tests) |
| CLOUDAGENT-01 | `Agent.kind` defaults to `'fileserver'`; CHECK rejects bad values | unit | `uv run pytest tests/test_models/ -k kind -x` | ❌ Wave 0 |
| CLOUDAGENT-01 | migration 024 up backfills existing rows → `'fileserver'`; down drops column | unit | `uv run pytest tests/test_migrations/ -k 024 -x` | ❌ Wave 0 |
| CLOUDAGENT-01 | `AgentSettings(kind='compute', scan_roots=[])` validates (no ValueError) | unit | `uv run pytest tests/test_config -k compute -x` | ❌ Wave 0 (extend `test_config`) |
| CLOUDAGENT-02 | `agent_worker` import graph excludes `phaze.database`/`.session`/`sqlalchemy.ext.asyncio`; broker present | unit (subprocess) | `uv run pytest tests/test_task_split.py -x` | ✅ exists — reaffirm/extend |
| CLOUDAGENT-02 | compute agent uses the same `phaze-agent-<id>` queue + `PUT /analysis/{file_id}` path | unit | (covered by existing `AgentTaskRouter` + `agent_analysis` tests; no new mechanic) | ✅ exists |
| CLOUDAGENT-03 | `agents_table.html` renders a Kind badge for `kind='compute'` rows alongside status pill + queue depth | unit (template render) | `uv run pytest tests/test_routers -k "admin_agents and kind" -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_cli tests/test_models tests/test_config tests/test_task_split.py -x -q`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing` (assert ≥85%)
- **Phase gate:** full suite green + `uv run ruff check .` + `uv run mypy .` + `pre-commit run --all-files` before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_models/` — `Agent.kind` default + CHECK constraint test (covers CLOUDAGENT-01)
- [ ] `tests/test_migrations/` — migration 024 up/down + backfill test (covers CLOUDAGENT-01)
- [ ] `tests/test_cli/test_agents_add.py` — `--kind compute` with empty scan roots (covers CLOUDAGENT-01); also assert `--kind fileserver` still requires scan roots
- [ ] `tests/test_config/` — `AgentSettings` compute path accepts empty scan roots; fileserver still rejects (covers CLOUDAGENT-01)
- [ ] `tests/test_routers/` (or `test_deployment`) — admin agents table renders kind badge for a compute row (covers CLOUDAGENT-03)
- [ ] Extend `tests/test_task_split.py` assertion/comment to explicitly cover the compute-agent invariant (covers CLOUDAGENT-02)
- Framework install: none — pytest + pytest-asyncio already present.

## Security Domain

`security_enforcement` is not set in `.planning/config.json` (absent = enabled), so this section is included. The security surface of Phase 48 is small and mostly inherited.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes (inherited) | Compute agent uses the existing per-agent bearer token (`hash_token`, sha256, `get_authenticated_agent`). No new auth surface. Token minted by `agents add`, printed once, only hash stored — unchanged. |
| V3 Session Management | no | Stateless bearer; no sessions. |
| V4 Access Control | yes | Compute agent reaches ONLY the SAQ Postgres broker + cache Redis + HTTP API (CLOUDAGENT-02). The ORM import boundary (`test_task_split`) enforces no app-DB access. A least-privilege Postgres broker role is a Phase 51 (`CLOUDDEPLOY-03`) concern. |
| V5 Input Validation | yes | `kind` constrained by DB CHECK + CLI choices `{fileserver,compute}` + `AgentSettings` field. `agent_id` charset CHECK unchanged. Reject unknown `kind` values at every layer (CLI, config, DB). |
| V6 Cryptography | no | No new crypto; reuses existing token hashing. |

### Known Threat Patterns for this stack
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Compute agent gains DB/media access beyond its mandate | Elevation of Privilege | Import boundary test (`test_task_split`) bans ORM imports; no media mount + empty scan roots (Phase 51 compose); HTTP-only result PUT |
| Bad/spoofed `kind` value bypasses badge/routing logic | Tampering | DB CHECK `kind IN ('fileserver','compute')` + CLI `choices=` + config field — defense in depth at three layers |
| Bearer token leakage during registration | Information Disclosure | Existing posture unchanged: token `print()`-only, never logged (`cli/__init__.py` D-13); only sha256 hash persisted |

## Sources

### Primary (HIGH confidence — codebase, this session)
- `src/phaze/models/agent.py` — `Agent` model, `id_charset` CHECK pattern, no existing `kind`
- `src/phaze/cli/__init__.py` — `agents add`, `--scan-roots required=True`, `validate_scan_roots`, `add_agent`
- `src/phaze/config.py:359-527` — `AgentSettings`, `scan_roots` required gate (`:512-513`), `PHAZE_AGENT_*` aliases
- `src/phaze/services/agent_task_router.py` — per-agent `phaze-agent-<id>` queue, `build_pipeline_queue`
- `src/phaze/tasks/agent_worker.py` — worker entrypoint, function registry, startup/heartbeat
- `src/phaze/tasks/heartbeat.py` — `_heartbeat_loop`, `queue_depth` via `Queue.info()`
- `src/phaze/services/agent_liveness.py` — `classify`/`sort_key` (kind-agnostic 5-state)
- `src/phaze/routers/admin_agents.py` — `_load_agents` (`SELECT Agent`), page + `/_table` partial
- `src/phaze/templates/admin/partials/agents_table.html` + `_status_pill.html` — table render, queue depth, status pill
- `src/phaze/routers/agent_analysis.py` — `PUT /api/internal/agent/analysis/{file_id}` idempotent upsert
- `src/phaze/routers/agent_identity.py` + `schemas/agent_identity.py` — `/whoami`, loose response schema
- `tests/test_task_split.py` — subprocess import-boundary test (ORM/async-engine banned, broker required)
- `alembic/versions/012,014,023` — agents table creation, `last_status`, current migration head (`023`)
- `docker-compose.agent.yml` — current agent compose (media mount on worker — Phase 51 will branch for compute)
- `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md` — CLOUDAGENT-01..03, v5.0 phase boundaries

### Secondary / Tertiary
- None — no web research needed; this phase is fully grounded in the existing codebase.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new packages; every touch-point is an existing, read file
- Architecture: HIGH — drain/PUT/liveness/queue-depth mechanics already shipped; phase is additive labeling
- Pitfalls: HIGH — config + CLI scan-roots gates verified by line number; migration head verified
- Validation: HIGH — existing import-boundary test + clear unit-test seams identified

**Research date:** 2026-06-25
**Valid until:** 2026-07-25 (stable — internal subsystem, no fast-moving external deps)
