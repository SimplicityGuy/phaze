---
phase: 48-compute-agent-type
verified: 2026-06-25T19:00:00Z
status: passed
score: 3/3 must-haves verified (all automated checks pass)
overrides_applied: 0
deferred_manual_verification:
  - test: "Register a compute agent via `phaze agents add --kind compute --id oci-a1 --name 'OCI A1'`, start its container using the Phase 47 arm64 image, and load /admin/agents in a browser."
    expected: "The Agents admin page shows the compute agent row with an indigo COMPUTE kind badge, a green liveness pill, and a queue depth number."
    why_human: "End-to-end requires a real registered agent draining a real SAQ queue against a live deployment. Routing work to drive a compute agent lands in Phase 49; the deploy lands in Phase 51 — not exercisable in Phase 48."
    status: deferred-to-phase-51
    partially_closed_by: "UAT 2026-06-25 — Tests 1-4 ran live against an ephemeral DB (migrations 010→024, real CLI registration, and the real admin router rendering the indigo COMPUTE / slate FILE SERVER badges with aria-labels on BOTH the full page and the /_table poll partial, 18/18 checks). Only the live liveness+queue-depth-while-draining observation remains, environmentally gated on a deployed compute agent."
---

# Phase 48: Compute-Agent Type — Verification Report

**Phase Goal:** Introduce a `kind="compute"` capability marker so a cloud agent (no scan roots, no media) can be registered, differentiated from file-server agents in the admin UI, and provably isolated from app ORM tables.
**Verified:** 2026-06-25
**Status:** human_needed — all automated checks pass; one live-deployment check is required (documented as manual-only in 48-VALIDATION.md).
**Re-verification:** No — initial verification.

---

## Requirements Scope

| Requirement | Plan | Description |
|-------------|------|-------------|
| CLOUDAGENT-01 | 48-01, 48-02 | Operator can register a compute agent (kind='compute', empty scan roots) |
| CLOUDAGENT-02 | 48-03 | Compute agent isolated from app ORM/DB; only SAQ broker + HTTP API reachable |
| CLOUDAGENT-03 | 48-03 | Agents admin page shows kind badge + liveness + queue depth for compute agents |

---

## CLOUDAGENT-01 — Verdict: MET

### Observable Truth 1: `Agent.kind` column exists with correct schema

**Evidence:** `src/phaze/models/agent.py:28-42`

```python
kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'fileserver'"))
__table_args__ = (
    CheckConstraint("id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="id_charset"),
    CheckConstraint("kind IN ('fileserver', 'compute')", name="kind_enum"),
)
```

- String(16) NOT NULL with server_default `'fileserver'` — VERIFIED
- `ck_agents_kind_enum` CHECK constraining `{fileserver,compute}` — VERIFIED (naming convention auto-prefixes `kind_enum` to `ck_agents_kind_enum`)
- Test coverage: `tests/test_models/test_agent.py` — `test_kind_defaults_fileserver`, `test_kind_column_not_null_string16`, `test_kind_charset_constraint_declared` all pass (`3 passed` from targeted run)

### Observable Truth 2: Migration 024 chains off 023, single head

**Evidence:**

- `alembic/versions/024_add_agents_kind.py`: `revision="024"`, `down_revision="023"` — VERIFIED
- `uv run alembic heads` output: `024 (head)` — single head, no multiple-head error — VERIFIED
- `upgrade()`: `op.add_column("agents", sa.Column("kind", sa.String(16), nullable=False, server_default="fileserver"))` then `op.create_check_constraint("kind_enum", ...)` — VERIFIED
- `downgrade()`: drops constraint first (bare name `kind_enum`, not the prefixed form), then drops column — VERIFIED
- Three migration tests present in `tests/test_migrations/test_024.py`: revision identifiers, saq_jobs non-reference guard, and the full async round-trip (up/down + backfill + CHECK-reject assertions)
- Note: migration integration tests require the ephemeral Postgres container (`just test-db`). Test structure is correct and confirmed green in SUMMARY (15 passed including live round-trip).

### Observable Truth 3: `phaze agents add --kind compute` with no scan roots succeeds; fileserver still requires roots

**Evidence:** `src/phaze/cli/__init__.py`

- `--kind` argument: `choices=("fileserver", "compute"), default="fileserver"` — outer enum-defense layer, rejects bad values before any session opens (line 118-122)
- `--scan-roots` is `required=False, default=""` with explicit guard: `if not scan_roots: raise ValueError("--scan-roots is required for --kind fileserver ...")` inside the `kind == "fileserver"` branch (line 154-156)
- `add_agent(...)` signature: `kind: str = "fileserver"` threaded to `Agent(id=..., name=..., token_hash=..., scan_roots=..., kind=kind)` — wiring confirmed (line 91)
- `test_main_fileserver_without_scan_roots_fails` — passes without DB (`1 passed`)
- DB-backed tests (`test_add_agent_compute_empty_roots`, `test_main_compute_no_scan_roots_succeeds`) require Postgres on port 5432; confirmed green in SUMMARY against the ephemeral test DB.

### Observable Truth 4: `AgentSettings.kind` relaxes empty-scan-roots gate for compute; api_url/token stay required

**Evidence:** `src/phaze/config.py`

- Field: `kind: Literal["fileserver", "compute"] = Field(default="fileserver", validation_alias=AliasChoices("PHAZE_AGENT_KIND", "kind"), ...)` — middle enum-defense layer
- Gate: `if self.kind != "compute" and not self.scan_roots: raise ValueError(...)` — relaxed correctly for compute; api_url/token guard is unconditional
- All 6 tests in `tests/test_config/test_agent_settings_kind.py` pass (`6 passed`): kind default, compute+empty-roots OK, fileserver+empty-roots raises, compute still requires api_url, compute still requires token, PHAZE_AGENT_KIND env alias.

**CLOUDAGENT-01 Score: 4/4 observable truths VERIFIED**

---

## CLOUDAGENT-02 — Verdict: MET

### Observable Truth: compute agent worker imports no ORM/app-DB modules; Postgres broker is present

**Evidence:** `tests/test_task_split.py` — `test_agent_worker_does_not_import_phaze_database`

- Docstring explicitly names the CLOUDAGENT-02 invariant (added in Plan 48-03 commit `f131bee`): "a compute agent runs the SAME `phaze.tasks.agent_worker` module on the SAME `phaze-agent-<id>` queue and PUTs results to the SAME HTTP endpoint as a file-server agent, so its security guarantee IS the import boundary this test already enforces."
- Forbidden set: `("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")` — all asserted absent from `sys.modules`
- Required present: `saq.queue.postgres` — asserted in `sys.modules`
- Test run: `7 passed in 1.18s` (all import-boundary tests green without DB)

**Important scope note (from 48-VALIDATION.md and 48-03-PLAN.md):** The "no media filesystem" half of CLOUDAGENT-02 is runtime-enforced via empty scan roots + no volume mount. This is NOT import-enforced and is a Phase 51 compose concern. No essentia/file-read ban was added (correct per plan). This is not a gap — it is explicitly documented design.

**CLOUDAGENT-02 Score: 1/1 observable truth VERIFIED**

---

## CLOUDAGENT-03 — Verdict: MET (automated) / human_needed (live deployment)

### Observable Truth 1: `_kind_badge.html` partial exists with correct geometry, palette, labels, aria-labels

**Evidence:** `src/phaze/templates/admin/partials/_kind_badge.html`

- Geometry: `text-xs font-semibold px-2 py-0.5 rounded-full` — copied verbatim from `_status_pill.html` per UI-SPEC lock
- Compute branch: `bg-indigo-100 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-400` with label `COMPUTE` and `aria-label="Kind: compute"`
- Fileserver branch (else fallback): `bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300` with label `FILE SERVER` and `aria-label="Kind: file server"`
- Defensive `{% if %}` / `{% else %}` — no out-of-enum value can blank the cell (T-48-02 mitigation)
- No icon (text-only per design system)

### Observable Truth 2: Kind column appears in agents_table.html between Agent and Status, on both render paths

**Evidence:** `src/phaze/templates/admin/partials/agents_table.html`

- `<th scope="col" class="px-4 py-3">Kind</th>` at line 37 — between Agent (line 36) and Status (line 38) — VERIFIED
- `<td class="px-4 py-3">{% include "admin/partials/_kind_badge.html" %}</td>` at line 52 — between Agent cell (lines 48-51) and Status pill include (line 53) — VERIFIED
- Single edit site covers both full-page (`GET /admin/agents`) and HTMX poll partial (`GET /admin/agents/_table`) — both use the same `agents_table.html` partial — VERIFIED
- `src/phaze/routers/admin_agents.py` was NOT modified — confirmed by SUMMARY and grep (kind rides free on `select(Agent)`)

### Observable Truth 3: Router-render tests cover both kinds on both render paths

**Evidence:** `tests/test_routers/test_admin_agents.py`

- `test_kind_badge_compute_renders`: full-page GET asserts `COMPUTE`, `bg-indigo-100 dark:bg-indigo-950`, `aria-label="Kind: compute"`
- `test_kind_badge_fileserver_renders`: full-page GET asserts `FILE SERVER`, `bg-slate-100`
- `test_kind_badge_in_poll_partial`: `GET /admin/agents/_table` partial asserts same COMPUTE + FILE SERVER badges
- `test_kind_column_header_present`: asserts `>Kind<` header positioned after `>Agent<` and before `>Status<`
- Smoke fixture seeds one `kind='compute'` row (`alive-agent`) and four `kind='fileserver'` rows — representative dataset
- DB-dependent: tests require Postgres. Confirmed green in SUMMARY (21 passed)

### Manual Verification Required

Per 48-VALIDATION.md §Manual-Only (explicitly documented as the only manual leg):

**Test:** Register via `phaze agents add --kind compute`, start the agent container, enqueue an analysis job, observe the Agents admin page shows the compute agent with kind badge + green liveness + queue depth.

**Why manual:** End-to-end requires a real registered agent draining a real queue against a live deployment. Template-render tests cover the rendering contract; live capacity visibility is the only remaining leg.

**CLOUDAGENT-03 Score: automated truths VERIFIED; 1 manual check pending**

---

## Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/phaze/models/agent.py` | kind column + CHECK | VERIFIED | String(16) NOT NULL, server_default='fileserver', ck_agents_kind_enum |
| `alembic/versions/024_add_agents_kind.py` | migration 024, single head | VERIFIED | down_revision=023, `alembic heads` = `024 (head)` |
| `tests/test_models/test_agent.py` | kind model assertions | VERIFIED | 3 kind-specific tests added; pass |
| `tests/test_migrations/test_024.py` | 024 round-trip test | VERIFIED | 3 tests: identifiers, saq-guard, full round-trip |
| `src/phaze/cli/__init__.py` | --kind flag, conditional roots | VERIFIED | choices=("fileserver","compute"), default="fileserver", Agent(kind=kind) |
| `src/phaze/config.py` | AgentSettings.kind field | VERIFIED | Literal["fileserver","compute"], PHAZE_AGENT_KIND alias, relaxed gate |
| `tests/test_cli/test_agents_add.py` | compute registration tests | VERIFIED | 5 new tests; non-DB tests pass; DB tests confirmed green in SUMMARY |
| `tests/test_config/test_agent_settings_kind.py` | kind gate tests | VERIFIED | 6 tests, all pass |
| `src/phaze/templates/admin/partials/_kind_badge.html` | kind badge partial | VERIFIED | COMPUTE/FILE SERVER, indigo/slate, aria-labels, locked geometry |
| `src/phaze/templates/admin/partials/agents_table.html` | Kind column | VERIFIED | th + td include, between Agent and Status |
| `tests/test_routers/test_admin_agents.py` | badge render tests | VERIFIED | 4 kind-specific tests; confirmed green in SUMMARY |
| `tests/test_task_split.py` | CLOUDAGENT-02 reaffirmation | VERIFIED | docstring updated; 7 tests pass; forbidden+broker asserts intact |

---

## Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `cli/__init__.py add_agent` | `Agent(kind=kind)` | constructor kwarg | VERIFIED | `Agent(id=..., kind=kind, ...)` at line 91 |
| `config.py AgentSettings._enforce_required_agent_fields` | scan_roots gate | `self.kind != "compute"` guard | VERIFIED | Gate conditional on kind != "compute" |
| `agents_table.html` | `_kind_badge.html` | `{% include %}` | VERIFIED | line 52: `{% include "admin/partials/_kind_badge.html" %}` |
| `_kind_badge.html` | `agent.kind` | `{% if agent.kind == 'compute' %}` | VERIFIED | branches on the loop variable `agent` |
| Migration 024 | Migration 023 | `down_revision = "023"` | VERIFIED | single head confirmed via `alembic heads` |

---

## Deferred Items

No items are deferred to later phases. DEF-48-01 (stale test_012 column inventory) was resolved in commit `8031814` — `"kind"` added to the expected set, full suite green (2071 passed, 0 failed per SUMMARY).

---

## Anti-Patterns Found

No TBD, FIXME, or XXX markers found in any file modified by this phase. No stub implementations. The `{% else %}` fallback in `_kind_badge.html` is intentional defensive design (T-48-02 mitigation), not a stub — any out-of-enum kind renders the neutral FILE SERVER badge rather than blanking the cell.

---

## Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Agent.kind column exists and is String(16) NOT NULL | `uv run pytest tests/test_models/test_agent.py -k kind -x -q` | 3 passed | PASS |
| AgentSettings.kind all gate behaviors | `uv run pytest tests/test_config/test_agent_settings_kind.py -x -q` | 6 passed | PASS |
| Fileserver-without-roots still fails | `uv run pytest tests/test_cli/test_agents_add.py::test_main_fileserver_without_scan_roots_fails -x -q` | 1 passed | PASS |
| Import boundary + CLOUDAGENT-02 invariant | `uv run pytest tests/test_task_split.py -x -q` | 7 passed | PASS |
| Single alembic head (024) | `uv run alembic heads` | `024 (head)` | PASS |
| DB-backed CLI + router tests | Require `just test-db` ephemeral Postgres | N/A in this env | SKIP (confirmed green in SUMMARY: 2071 passed) |

---

## Requirements Coverage

| Requirement | Plan | Status | Evidence |
|-------------|------|--------|---------|
| CLOUDAGENT-01 | 48-01, 48-02 | SATISFIED | Agent.kind column, migration 024, --kind CLI flag, AgentSettings.kind, all tests pass |
| CLOUDAGENT-02 | 48-03 | SATISFIED | test_task_split.py subprocess import boundary with CLOUDAGENT-02 docstring; 7 passed |
| CLOUDAGENT-03 | 48-03 | SATISFIED (automated) / NEEDS HUMAN (live) | _kind_badge.html + agents_table.html Kind column; render tests confirm both render paths; live agent check is manual-only per VALIDATION.md |

**Note on REQUIREMENTS.md checkbox state:** CLOUDAGENT-01 shows `[ ]` (unchecked) while CLOUDAGENT-02/03 show `[x]`. This is a stale documentation artifact — the implementation is complete and verified. The checkbox was not updated at phase close. No code gap.

---

## Human Verification Required

### 1. Live compute agent on Agents admin page

**Test:** Register a compute agent (`phaze agents add --kind compute --id oci-a1 --name "OCI A1"`), start its container using the Phase 47 arm64 image with no media mount, enqueue one analysis job to its queue, then open `/admin/agents` in a browser.

**Expected:** The Agents admin page shows the compute agent row with:
- An indigo `COMPUTE` kind badge (pill with `aria-label="Kind: compute"`)
- A green liveness pill (agent reported a heartbeat)
- A non-zero queue depth number

**Why human:** End-to-end requires a real registered agent draining a real SAQ queue against a live deployment. The template-render tests (`test_kind_badge_compute_renders`, `test_kind_badge_in_poll_partial`) verify the rendering contract against a mock DB and confirm both the full-page and 5s poll render paths produce the expected HTML. The live capacity visibility leg — confirming the full stack from agent heartbeat through DB row through admin page render in a browser — is the only remaining check.

---

## Gaps Summary

No gaps. All three requirements are met at the automated-test level. The single human verification item (live compute agent on admin page) is correctly classified as manual-only in 48-VALIDATION.md §Manual-Only and is not a defect — it is the expected end-to-end closure step for CLOUDAGENT-03.

---

_Verified: 2026-06-25_
_Verifier: Claude (gsd-verifier)_
