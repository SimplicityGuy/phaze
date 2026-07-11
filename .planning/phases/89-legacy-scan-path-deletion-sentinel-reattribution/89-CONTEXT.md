# Phase 89: Legacy Scan-Path Deletion & Sentinel Reattribution - Context

**Gathered:** 2026-07-11
**Status:** Ready for planning

<domain>
## Phase Boundary

Retire the `legacy-application-server` FK sentinel. Three moves, in order:

1. **LEGACY-01** — Delete the orphaned legacy scan path: `POST /api/v1/scan` (routers/scan.py), and the ingestion trio `run_scan` → `discover_and_hash_files` → `bulk_upsert_files` (services/ingestion.py). This removes the only two `FileState`-writing upsert sites that survive outside the surviving `scan_directory` path, shrinking the Phase 90 migration surface. The `agent_id = owning fileserver` FK model is preserved.
2. **LEGACY-02** — A data-migration reattributes ALL historical `legacy-application-server`-owned `files` and `scan_batches` to a designated real `kind='fileserver'` agent, with a backfill-verification check.
3. **LEGACY-03** — After reattribution, drop the `agent_id` model default and delete the sentinel `Agent` row. The `ondelete=RESTRICT` FK is satisfiable only because reattribution ran first (ordering enforced within the migration).

**Out of scope:** the destructive `files.state` / `FileState` enum removal (that is Phase 90, gated on shadow-compare green + drained cloud-push lanes). This phase is the data-model twin grouped near that migration work but does NOT touch `files.state`.
</domain>

<decisions>
## Implementation Decisions

### Reattribution Target Selection (LEGACY-02)
- **D-01:** The migration **auto-detects** the target agent = the single non-revoked `kind='fileserver'` agent (`SELECT id FROM agents WHERE revoked_at IS NULL AND kind='fileserver'`). Exactly **1** row → reattribute to that `id`. **0** rows → **abort** the migration (no valid owner exists; the sentinel cannot be safely deleted). **>1** rows → **abort** with a clear operator message, *unless* an explicit override is supplied.
- **D-02:** Ambiguity/override escape hatch: `alembic upgrade head -x reattribute_to=<agent_id>`. When provided, the migration validates the id exists, is `kind='fileserver'`, and is not revoked, then uses it. In current prod there is exactly one real fileserver (nox), so the auto path resolves with no operator input.
- **D-03:** Reattribution keys on `agent_id` (FK → `agents.id`, a string PK, operator-chosen). It writes the target agent's **`id`**, not its display `name`. Scope = **all** legacy-owned `files` AND `scan_batches`, including the `status='live'` sentinel scan_batch created by migration 012 (it simply becomes a historical batch owned by the target).

### Router Disposition (LEGACY-01)
- **D-04:** Delete `src/phaze/routers/scan.py` **wholesale** — both `POST /api/v1/scan` (trigger, the legacy path) and `GET /api/v1/scan/{batch_id}` (batch-status reader). Unregister its `include_router()` from the app (`main.py`). Delete `tests/discovery/routers/test_scan.py`.
- **D-05:** Contingent gate: research/planning must first confirm the GET status endpoint has **no live consumer** (grep shows no template/JS reference; sanity-check no homelab/monitoring poller). If a batch-status API is ever needed for the surviving `scan_directory` batches, it gets built fresh on the pipeline surface — not preserved here.

### Model Default Removal + Test Fixtures (LEGACY-03)
- **D-06:** Drop the Python `default="legacy-application-server"` from **both** `src/phaze/models/file.py` and `src/phaze/models/scan_batch.py`. `agent_id` becomes a **required** construction argument — matching the surviving writer `scan_directory`, which already supplies a real `agent_id` from `ctx["agent_identity"]`.
- **D-07:** `agent_id` has **NO DB-level `server_default`** (migration 012 added the columns nullable and backfilled; the only default is the Python model default). Therefore LEGACY-03's "drop the default" is a **pure model-code change** — the Alembic migration needs **no `ALTER COLUMN … DROP DEFAULT`** DDL. The migration only reattributes + deletes the sentinel row.
- **D-08:** Update `tests/conftest.py` to seed a real fileserver agent (e.g. `Agent(id='test-fileserver', name='test-fileserver', kind='fileserver', scan_roots=[])`, non-revoked) instead of the sentinel. Repoint the ~10 integration tests' `_LEGACY_AGENT_ID` constant to the new fileserver id. Cleanest end state — no vestigial default, no seeded sentinel.

### Migration Verification + Downgrade (LEGACY-02 / LEGACY-03)
- **D-09:** Single-transaction migration. Order within the txn: (1) `UPDATE files` + `UPDATE scan_batches SET agent_id=<target> WHERE agent_id='legacy-application-server'`; (2) **assert** `COUNT(*)` of remaining `legacy-application-server`-owned rows across both tables `= 0`, else `RAISE` (rolls back the whole txn — the sentinel `DELETE` is never attempted); (3) `DELETE FROM agents WHERE id='legacy-application-server'`.
- **D-10:** `downgrade()` raises `NotImplementedError` with a documented reason: which rows were originally legacy-owned is **unrecoverable** once merged into the target agent, so the reattribution and sentinel row cannot be faithfully reconstructed. This is a deliberately irreversible data migration.

### Claude's Discretion
- Migration revision number (assigned at plan time; prod is at Alembic 031 per project memory, unreleased 032+ pending — the new migration slots after the latest on the branch).
- Batching/lock strategy for the bulk `UPDATE` (prod corpus is ~11,428 files) — planner/researcher decides; not a user-facing choice.
- Exact `NotImplementedError` message wording and the abort-message text for the 0 / >1 fileserver cases.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & Roadmap
- `.planning/REQUIREMENTS.md` §LEGACY-01..03 — the three locked requirements this phase delivers.
- `.planning/ROADMAP.md` "Phase 89" — goal, depends-on (Phase 82), and success criteria (ordering enforced, backfill-verification, RESTRICT-FK).
- `.planning/ROADMAP.md` "Phase 90" — the downstream destructive migration this phase de-risks by removing two `FileState` writers early. Do NOT touch `files.state` here.

### Code to delete / modify (grounded during discussion)
- `src/phaze/routers/scan.py` — delete wholesale (POST + GET). Unregister in `src/phaze/main.py`.
- `src/phaze/services/ingestion.py` — delete `discover_and_hash_files` (L47), `bulk_upsert_files` (L94, sole caller is `run_scan`), `run_scan` (L128); remove the now-unused `LEGACY_AGENT_ID` import.
- `src/phaze/models/file.py` L88-95 — drop `default=` on `agent_id`.
- `src/phaze/models/scan_batch.py` L29-34 — drop `default=` on `agent_id`.
- `src/phaze/models/agent.py` L14 — `LEGACY_AGENT_ID` constant (retire once no non-test references remain).
- `alembic/versions/012_add_agents_table_and_backfill.py` — the migration that created the sentinel + agent_id columns (nullable, backfilled). Reference for the new migration's downgrade/reversal reasoning.
- `tests/conftest.py` L198-215 — the sentinel-seeding fixture to repoint.

### Surviving path (must remain untouched)
- `src/phaze/tasks/scan.py` `scan_directory` (L169) + `src/phaze/routers/pipeline_scans.py` — the current scan path via AgentTaskRouter; persists with a real `agent_id`. Independent of the deleted legacy trio.
- `src/phaze/services/enqueue_router.py` `select_active_agent(session, kind="fileserver")` — existing picker (non-revoked, ordered by name) that informs the auto-detect query.

No external ADRs/design specs govern this phase — decisions fully captured above.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `select_active_agent(session, kind="fileserver")` (enqueue_router.py) — models the "non-revoked fileserver" query the migration's auto-detect reuses (the migration writes raw SQL, but the predicate is identical: `revoked_at IS NULL AND kind='fileserver'`).
- `Agent.kind` CHECK constraint (`kind IN ('fileserver','compute')`, agent.py L40) + `revoked_at` — the two columns that define a valid reattribution target.

### Established Patterns
- The legacy trio is a **closed severable unit**: `bulk_upsert_files` has exactly one caller (`run_scan`, ingestion.py:181), and `POST /api/v1/scan` is the only entry to `run_scan`. Deleting all three plus the router leaves no dangling references in source (only tests reference them).
- `scan_directory` (surviving) sources `agent_id` from `ctx["agent_identity"].agent_id` — it never used the Python model default, so removing the default cannot regress the live path.
- Tests use `Base.metadata.create_all` (not migrations), so the sentinel exists in tests ONLY via the conftest seed — which is why D-08's fixture change is the mechanism, not a migration.

### Integration Points
- The migration writes `files.agent_id` / `scan_batches.agent_id` and deletes one `agents` row under `ondelete=RESTRICT` — correctness hinges on the reattribute-before-delete ordering (D-09) inside one transaction.
- Project memory: prod is at Alembic **031** (032+ unreleased); the sentinel + agent_id columns exist in prod from migration 012. The real fileserver in prod is **nox**.

</code_context>

<specifics>
## Specific Ideas

- Abort-message contract for the ambiguous case should literally tell the operator the escape hatch: `pass -x reattribute_to=<id>`.
- Target agent in current prod is the **nox** fileserver; the auto-detect (sole fileserver) is expected to resolve to it with zero operator input.

</specifics>

<deferred>
## Deferred Ideas

- **Rollout/release sequencing** (which vX.Y.Z tag ships this, homelab redeploy timing) — operational, handled at ship time, not a planning decision here.
- Any future scan-batch status API for the surviving `scan_directory` batches — build fresh on the pipeline surface if a need arises (D-05); explicitly not preserved in this deletion.

### Reviewed Todos (not folded)
- `analysis-completed-at-backfill.md` ("analyzed ⇒ analysis_completed_at — 1001 production rows will fail the shadow gate") — keyword collision only; belongs to the shadow-compare / analyzed-invariant work (Phase 80/82 lineage), unrelated to sentinel retirement.
- `wr-01-review-builder-limit-before-filter.md` ("Tag/CUE bulk builders apply .limit() before the qualifying-change filter") — keyword collision only; a proposals/tag-builder concern, out of scope for legacy scan-path deletion.

</deferred>

---

*Phase: 89-legacy-scan-path-deletion-sentinel-reattribution*
*Context gathered: 2026-07-11*
