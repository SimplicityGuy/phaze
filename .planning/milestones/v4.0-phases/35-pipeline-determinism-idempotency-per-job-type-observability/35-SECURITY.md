---
phase: 35
slug: pipeline-determinism-idempotency-per-job-type-observability
status: secured
threats_open: 0
threats_total: 12
threats_mitigated: 9
threats_accepted: 3
asvs_level: 1
created: 2026-06-12
---

# SECURITY.md — Phase 35

**Phase:** 35 — Pipeline Determinism, Idempotency & Per-Job-Type Observability
**Audited:** 2026-06-12
**Disposition:** SECURED — all 12 registered threats verified present (9 mitigate, 3 accept)
**Threats open:** 0

This phase introduces no net-new HTTP trigger surface, no new dependencies, and no
user-controlled string rendered into a template. All mutating verbs reuse pre-existing
Phase-34 endpoints; all new code paths are read-only counters/COUNT queries plus a
deterministic-key enqueue hook and a partial-unique-index idempotent upsert.

## Threat Verification — mitigate

| Threat ID | Category | Evidence (file:line / pattern) |
|-----------|----------|--------------------------------|
| T-35-01 | Tampering | `tasks/_shared/deterministic_key.py:99-103` — `job.key = f"{job.function}:{builder(...)}"` set UNCONDITIONALLY for any `_KEY_BUILDERS` fn (overrides caller key). Drift-guard `tests/test_deterministic_key.py:183-191` (`test_every_routable_task_is_keyed_or_exempt`) fails loud on any routable task lacking a builder or `_UNKEYED_TASKS` exemption. CLOSED |
| T-35-03 | Tampering/Elevation | D-06 auto-enqueue removed at `services/ingestion.py:181-183` and `routers/agent_files.py:123-131` (`enqueued=0`); NO new endpoint added. Sole metadata producer is the pre-existing Phase-34 `routers/pipeline.py:457 POST /api/v1/extract-metadata` / `:490 POST /pipeline/extract-metadata`. CLOSED |
| T-35-04 | Tampering (SQLi) | `alembic/versions/019_...py:49-57` `_DEDUPE_PENDING_SQL` — static `row_number()` window, zero parameters interpolated; `op.execute(sa.text(...))` at `:69`. No injection surface. CLOSED |
| T-35-05 | Tampering | `services/proposal.py:347-361` `on_conflict_do_update(index_elements=["file_id"], index_where=(RenameProposal.status == "pending"), set_={...})` — partial index covers ONLY pending rows; APPROVED/EXECUTED/REJECTED/FAILED never a conflict target. Backed by `models/proposal.py:59` partial unique index + migration 019. File-state regression also blocked via `_TERMINAL_FILE_STATES` guard (`proposal.py:370`). CLOSED |
| T-35-06 | DoS/Availability | `alembic/versions/019_...py:60-78` — `upgrade()` runs op-1 dedupe DELETE (`:69`) BEFORE op-2 `create_index(... unique=True, postgresql_where="status='pending'")` (`:72`). Ordering is load-bearing and explicit. CLOSED |
| T-35-07 | DoS | `services/pipeline.py:141-158` `_safe_count` wraps every COUNT(DISTINCT) in try/except → returns 0 + `session.rollback()` on failure (never raises); `get_stage_progress:161` routes every node source through it. Output tables carry file_id/tracklist_id indexes. CLOSED |
| T-35-09 | DoS | `routers/pipeline.py:71-87` `_read_pipeline_counters` try/except → `{}` on any Redis/app.state failure; `services/pipeline_counters.py:78-97 read_counters` two pipelined MGETs over fixed keys. Poll degrades to DB-truth, never 500s. CLOSED |
| T-35-10 | Tampering/EoP | `templates/pipeline/partials/dag_canvas.html` hx-post targets resolve to pre-existing routes only: `/pipeline/extract-metadata` (`:218`), `/pipeline/analyze` (`:233`), `/pipeline/fingerprint` (`:248`), `/pipeline/proposals` (`:280`); Execute node is an `<a href="/proposals/">` navigation (`:309`). NO net-new trigger endpoint. CLOSED |
| T-35-11 | Tampering/XSS | `dag_canvas.html:161-170` + `stats_bar.html:46-68` — all `x-init` store writes interpolate server-computed ints (`_build_dag_context` returns `dict[str,int]`, `routers/pipeline.py:128-147`); SVG `d` strings derive from server-side `NODE_LAYOUT` map (`dag_canvas.html:31-41`), not user input. No `|safe` on dynamic data; Jinja autoescape on. Confirmed by 35-REVIEW. CLOSED |

## Threat Verification — accept (premise sanity-checked)

| Threat ID | Category | Premise check |
|-----------|----------|---------------|
| T-35-02 | DoS (key cardinality) | `services/pipeline_counters.py:33-42 PIPELINE_FUNCTIONS` = exactly 8 fixed names; key builders `_enqueued_key`/`_completed_key` (`:45-52`) embed only the function name — no user-controlled component. Cardinality bounded; premise HOLDS. CLOSED |
| T-35-08 | Information Disclosure | Per-stage/per-node counts are non-sensitive aggregate integers on the internal admin dashboard (reverse-proxy realm, Phase-33 precedent). Premise HOLDS. CLOSED |
| T-35-SC | Tampering (supply chain) | `git diff main...HEAD -- pyproject.toml` returns EMPTY — no dependency added/changed. Tailwind/HTMX/Alpine remain vendored/self-hosted. Premise HOLDS. CLOSED |

## Unregistered Flags

None. All five 35-0x-SUMMARY.md `## Threat Flags` sections report "None" and map to
register dispositions T-35-07/08/09/10/11. No new attack surface appeared during
implementation.

## Observations (not registered threats)

**WR-02 (deferred path traversal) — CORRECTLY DEFERRED, not an open Phase-35 threat.**
`services/proposal.py:329-333 store_proposals` persists the LLM `proposed_path` with `..`
segments intact (only `strip("/")` + `//` collapse). This is a *storage*-time concern.
The ACTIVE move/execution stage neutralizes it:
- `tasks/execution.py:166-167` (the wired `execute_approved_batch` agent task, registered
  in `tasks/agent_worker.py:204`) routes both `original_path` and `proposed_path` through
  `_resolve_and_check_containment` (`tasks/execution.py:72-87`), which calls
  `Path(candidate).resolve()` (collapsing `..` and symlinks) then asserts
  `relative_to(root)` for each scan_root — raising `ValueError` on any escape.

The only `proposed_path` consumer WITHOUT a `..` containment guard was the legacy
`services/execution.py::execute_single_file`, which had NO caller (the live path is
`execute_approved_batch`). **Resolved 2026-06-12 (commit 8f101db):** the entire dead
`services/execution.py` module and its test were removed, eliminating the unguarded
consumer outright; `docs/architecture.md` was corrected to point at the live
`tasks/execution.py::execute_approved_batch` path. No unguarded `proposed_path` consumer
remains.

Conclusion: storing `..` is safe because the sole live execution path resolves under a
root with a `..`/symlink guard. WR-02 is correctly deferred to the (already-guarded) move
stage; it is NOT an open threat for this phase's storage scope.

## Accepted Risks Log

- T-35-02 — Redis counter key cardinality bounded to 8 fixed function names; accepted.
- T-35-08 — Per-stage aggregate counts exposed on internal admin dashboard; accepted.
- T-35-SC — No package installs / CDN changes this phase; accepted (verified empty deps diff).
