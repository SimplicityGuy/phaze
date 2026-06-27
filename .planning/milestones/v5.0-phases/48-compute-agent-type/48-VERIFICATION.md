---
phase: 48-compute-agent-type
verified: 2026-06-26T00:00:00Z
status: human_needed
score: 3/3
overrides_applied: 0
re_verification:
  previous_status: passed  # frontmatter mismatch in prior file — body correctly said human_needed
  previous_score: 3/3
  gaps_closed: []
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "Register a compute agent (`phaze agents add --kind compute --id oci-a1 --name 'OCI A1'`), start its container using the Phase 47 arm64 image with no media mount, and open /admin/agents in a browser while the agent is running and has work to drain."
    expected: "The Agents admin page shows the compute agent row with an indigo COMPUTE kind badge (aria-label='Kind: compute'), a green liveness pill, and a non-zero queue depth number."
    why_human: "End-to-end verification requires a real registered agent draining a real SAQ queue against a live deployment. Template-render tests cover the rendering contract against a seeded DB. The live capacity visibility leg (kind badge + green liveness + queue depth in a running agent) is the only non-automatable check; routing work to a compute agent lands in Phase 49 and the deploy in Phase 51, so this cannot be exercised in Phase 48 isolation."
---

# Phase 48: Compute-Agent Type — Verification Report

**Phase Goal:** Register a media-less `kind="compute"` agent that drains its queue + PUTs results, surfaced on the Agents page.
**Verified:** 2026-06-26
**Status:** human_needed — all automated checks pass (3/3 requirements); one live-deployment observation required for CLOUDAGENT-03.
**Re-verification:** Yes — previous VERIFICATION.md existed with frontmatter `status: passed` but a body that correctly identified a human verification item; this version corrects the frontmatter to `human_needed` per the verification methodology.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | An operator can register a compute agent with empty scan roots | VERIFIED | `src/phaze/cli/__init__.py:117-130` (--kind choices), `config.py:658` (gate relaxed), `cli/__init__.py:91` (Agent(kind=kind)) |
| 2 | A compute agent drains its per-agent SAQ queue and PUTs results with no ORM/app-DB access | VERIFIED | `tests/test_task_split.py:33-106` (import boundary subprocess test, CLOUDAGENT-02 docstring, forbidden set asserted absent, saq.queue.postgres asserted present) |
| 3 | The Agents admin page distinguishes compute agents via a kind badge alongside liveness + queue depth | VERIFIED (automated) / HUMAN NEEDED (live) | `_kind_badge.html` + `agents_table.html:37,52` (Kind column between Agent and Status); 4 router-render tests green |

**Score:** 3/3 automated requirements verified; 1 human check pending

---

## Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence (file:line) |
|-------------|------------|-------------|--------|----------------------|
| CLOUDAGENT-01 | 48-01, 48-02 | Operator can register a compute agent (kind='compute', empty scan_roots) | MET | Model: `src/phaze/models/agent.py:28,39-42`. Migration: `alembic/versions/024_add_agents_kind.py:36-37,44-45`. CLI: `src/phaze/cli/__init__.py:117-130,154-158`. Config: `src/phaze/config.py:484-488,658`. |
| CLOUDAGENT-02 | 48-03 | Compute agent drains per-agent SAQ queue and PUTs results; no app ORM/DB access | MET | `tests/test_task_split.py:36-50` (CLOUDAGENT-02 docstring), `:83-95` (forbidden set + broker assertion). Live spot-check: 7 passed. |
| CLOUDAGENT-03 | 48-03 | Agents admin page distinguishes compute agents (kind badge + liveness + queue depth) | MET (automated) / HUMAN NEEDED (live) | `src/phaze/templates/admin/partials/_kind_badge.html:11-15`. `agents_table.html:37,52`. Liveness: `agents_table.html:53` (pre-existing `_status_pill.html`). Queue depth: `agents_table.html:55-60` (pre-existing). Render tests: `tests/test_routers/test_admin_agents.py:179-239`. |

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/models/agent.py` | kind String(16) NOT NULL server_default='fileserver', ck_agents_kind_enum CHECK | VERIFIED | Lines 28 (column) and 39-42 (CHECK in __table_args__) |
| `alembic/versions/024_add_agents_kind.py` | revision="024", down_revision="023", single head | VERIFIED | Lines 36-37; `uv run alembic heads` → `024 (head)` |
| `tests/test_models/test_agent.py` | kind default/type/CHECK assertions | VERIFIED | 3 tests: test_kind_defaults_fileserver, test_kind_column_not_null_string16, test_kind_charset_constraint_declared; pass live |
| `tests/test_migrations/test_024.py` | 024 round-trip: up/down, backfill, CHECK-reject | VERIFIED (structure) | Mirrors test_023.py; DB-backed — confirmed green in SUMMARY (15 passed) |
| `src/phaze/cli/__init__.py` | --kind choices, conditional scan-roots, Agent(kind=kind) | VERIFIED | Lines 117-130 (argparse), 154-158 (fileserver gate), 91 (Agent constructor) |
| `src/phaze/config.py` | AgentSettings.kind Literal + PHAZE_AGENT_KIND alias + relaxed gate | VERIFIED | Lines 484-488 (field), 658 (gate condition `kind != "compute"`) |
| `tests/test_cli/test_agents_add.py` | compute registration + fileserver-still-requires-roots | VERIFIED | 5 new tests; test_main_fileserver_without_scan_roots_fails passes live; DB-backed tests green in SUMMARY |
| `tests/test_config/test_agent_settings_kind.py` | compute accepts empty roots; fileserver rejects; env alias | VERIFIED | 6 tests, all pass live |
| `src/phaze/templates/admin/partials/_kind_badge.html` | COMPUTE/FILE SERVER branches, locked geometry, aria-labels | VERIFIED | Lines 11-15; `text-xs font-semibold px-2 py-0.5 rounded-full`, aria-label="Kind: compute"/"Kind: file server" |
| `src/phaze/templates/admin/partials/agents_table.html` | Kind th + td include between Agent and Status | VERIFIED | Line 37 (th), line 52 (td include); both full-page and poll render path use this partial |
| `tests/test_routers/test_admin_agents.py` | badge render assertions for both kinds, both render paths | VERIFIED (structure) | 4 kind-specific tests; DB-backed — confirmed green in SUMMARY (21 passed) |
| `tests/test_task_split.py` | CLOUDAGENT-02 reaffirmation, forbidden set, broker assertion | VERIFIED | Lines 36-50 (compute-agent docstring), 83-95 (forbidden + broker); 7 tests pass live |

---

## Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `cli/__init__.py add_agent` | `Agent(kind=kind)` | constructor kwarg | VERIFIED | `cli/__init__.py:91` — `Agent(id=..., kind=kind, ...)` |
| `config.py AgentSettings._enforce_required_agent_fields` | scan_roots gate | `self.kind != "compute"` | VERIFIED | `config.py:658` — gate skips scan_roots check when kind=="compute" |
| `alembic/versions/024_add_agents_kind.py` | migration 023 | `down_revision = "023"` | VERIFIED | Line 37; `alembic heads` confirms single head `024` |
| `agents_table.html` | `_kind_badge.html` | `{% include %}` | VERIFIED | `agents_table.html:52` — `{% include "admin/partials/_kind_badge.html" %}` |
| `_kind_badge.html` | `agent.kind` | `{% if agent.kind == 'compute' %}` | VERIFIED | `_kind_badge.html:11` — branches on loop variable `agent.kind` |

---

## ORM-Free / No-Media Boundary (CLOUDAGENT-02 / DIST-04)

The compute agent runs `phaze.tasks.agent_worker` — the same module as a file-server agent, on the same `phaze-agent-<id>` queue, via the same `PUT /api/internal/agent/analysis/{file_id}` endpoint. No compute-specific worker code was introduced.

**Import boundary (verifiable):** `tests/test_task_split.py:33-106` — subprocess import of `phaze.tasks.agent_worker` asserts:
- ABSENT from sys.modules: `phaze.database`, `phaze.tasks.session`, `sqlalchemy.ext.asyncio`
- PRESENT in sys.modules: `saq.queue.postgres` (psycopg3 broker)

**Live spot-check:** `uv run pytest tests/test_task_split.py -x -q` → 7 passed in 1.46s (no test DB required).

**Media isolation (runtime only):** The "no media filesystem" guarantee is NOT import-enforced. The worker legitimately reads audio paths it is handed. Media isolation is a runtime guarantee: empty `scan_roots` + no volume mount on the cloud host — a Phase 51 Docker Compose concern. This is documented design (48-RESEARCH §Pitfall 4, 48-03-PLAN.md Task 2 action), not a gap.

---

## Data-Flow Trace (Level 4 — kind badge)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `_kind_badge.html` | `agent.kind` | `select(Agent)` in `admin_agents.py._load_agents` (pre-existing, unchanged) | Yes — reads from agents table which has kind column since migration 024 | FLOWING |

The router (`src/phaze/routers/admin_agents.py`) was not modified — `_load_agents` already does a bare `select(Agent)` which now includes the `kind` column. The kind badge reads `agent.kind` from the loaded row. No stub or static data path.

---

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Agent.kind column: String(16) NOT NULL, ck_agents_kind_enum | `uv run pytest tests/test_models/test_agent.py -k kind -x -q` | 3 passed | PASS |
| AgentSettings.kind: all gate behaviors (6 tests) | `uv run pytest tests/test_config/test_agent_settings_kind.py -x -q` | 6 passed | PASS |
| Fileserver without scan-roots still fails (no DB needed) | `uv run pytest tests/test_cli/test_agents_add.py::test_main_fileserver_without_scan_roots_fails -x -q` | 1 passed | PASS |
| Import boundary + CLOUDAGENT-02 invariant (7 tests) | `uv run pytest tests/test_task_split.py -x -q` | 7 passed | PASS |
| Single alembic head | `uv run alembic heads` | `024 (head)` | PASS |
| Migration 024 round-trip + backfill + CHECK-reject | Requires ephemeral Postgres (`just test-db`) | N/A here | SKIP — confirmed green in SUMMARY (15 passed, live round-trip) |
| CLI compute registration + admin render tests | Require ephemeral Postgres | N/A here | SKIP — confirmed green in SUMMARY (2071 passed, 0 failed) |

---

## Anti-Patterns Found

No TBD, FIXME, or XXX markers found in any file modified by this phase.

The `{% else %}` fallback in `_kind_badge.html` is intentional defensive design (T-48-02 mitigation): any out-of-enum `kind` renders the neutral FILE SERVER badge rather than blanking the cell. It is not a stub — it is the security fallback.

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | None | — | — |

---

## Human Verification Required

### 1. Live compute agent on Agents admin page

**Test:** Register a compute agent (`phaze agents add --kind compute --id oci-a1 --name "OCI A1"`), start its container using the Phase 47 arm64 image with no media volume mount, enqueue one analysis job to its queue, and open `/admin/agents` in a browser.

**Expected:** The Agents admin page shows the compute agent row with:
- An indigo `COMPUTE` kind badge (pill with `aria-label="Kind: compute"`)
- A green liveness pill (agent reported a heartbeat within the liveness window)
- A non-zero queue depth number

**Why human:** End-to-end requires a real registered agent draining a real SAQ queue against a live deployment. Template-render tests (`test_kind_badge_compute_renders`, `test_kind_badge_in_poll_partial`) confirm the HTML rendering contract against a seeded DB on both the full-page and 5s poll render paths. The live capacity visibility leg — kind badge + green liveness + queue depth in a browser while the agent is running — is the only remaining check and is documented as manual-only in 48-VALIDATION.md §Manual-Only.

Note: routing work to a compute agent lands in Phase 49; the Phase 51 Docker Compose deploy is the expected environment for this check.

---

## Deferred Items

DEF-48-01 (stale `test_012_upgrade.py` column inventory) was resolved before merge — `"kind"` added to the expected set at `tests/test_migrations/test_012_upgrade.py:55`; full integration suite green at 2071 passed, 0 failed per SUMMARY.

No items deferred to later phases.

---

## Gaps Summary

No gaps. All three requirements are met at the automated-test level:
- CLOUDAGENT-01: Agent model, migration 024, CLI `--kind` flag, `AgentSettings.kind` — fully implemented and tested.
- CLOUDAGENT-02: ORM import boundary reaffirmed and passing live.
- CLOUDAGENT-03: `_kind_badge.html` partial + Kind column in `agents_table.html`, covering both render paths — fully implemented; router-render tests green.

The single human verification item (live compute agent on admin page) is correctly classified as manual-only in 48-VALIDATION.md and is not a defect.

---

_Verified: 2026-06-26_
_Verifier: Claude (gsd-verifier)_
