---
phase: 55
slug: routing-state-ledger-integration-the-live-seam
status: secured
threats_open: 0
threats_closed: 28
asvs_level: 1
created: 2026-06-28
---

<!-- gsd:security-audit -->
# SECURITY.md — Phase 55: Routing / State / Ledger Integration (the Live Seam)

**Audited:** 2026-06-28
**Auditor:** gsd-security-auditor
**ASVS Level:** default
**Threats Closed:** 28/28
**Status:** SECURED (no open threats; phase may ship)

Verification method: each `mitigate` threat confirmed by locating the declared mitigation
pattern in the cited implementation file. Each `accept` threat confirmed against the documented
rationale (logged below). Implementation files were NOT modified.

---

## Threat Verification — `mitigate` (21 closed)

| Threat ID | Category | Evidence (file:line) |
|-----------|----------|----------------------|
| T-55-CFG-01 | Tampering | `src/phaze/config.py:406` — `cloud_target: Literal["local","a1","k8s"]` field; invalid members rejected at construction. Zero `cloud_burst_enabled` refs in `src/phaze/` or `tests/`. |
| T-55-CFG-02 | DoS | `config.py:600/621/642` three fail-fast validators raise `ValueError` on misconfig; loud rename in `.env.example:171-183`, `docs/cloud-burst.md:18-25`, `docs/configuration.md:128`, `docs/deployment.md:50-51`. |
| T-55-CFG-03 | Tampering | `config.py:601 _enforce_s3_config_when_k8s`, `:622 _enforce_compute_scratch_dir_when_a1`, `:643 _enforce_kube_config_when_k8s` — three separate target-keyed validators, NOT collapsed to a single `!= "local"` gate. |
| T-55-PHASE-01 | Tampering | `models/cloud_job.py:49 class CloudPhase(StrEnum)` + CheckConstraint `cloud_phase_enum` (`:98-100`); writers seed only enum `.value` (`submit_cloud_job.py:89,98`; `reconcile_cloud_jobs.py:134,254,270`). |
| T-55-PHASE-02 | Tampering | `alembic/versions/027_add_cloud_job_cloud_phase.py:45-54` — additive/reversible, `op` calls reference only `cloud_job`; no `saq_jobs` operation (line 18 is a CRITICAL warning comment, not a reference). |
| T-55-SEAM-01 | DoS | `cloud_staging.py:71 _stage_file_to_s3` no-commit core (public `stage_file_to_s3:68` keeps its commit); `release_awaiting_cloud.py:182` k8s branch calls the core under `pg_advisory_xact_lock` (`:135`) with a SINGLE post-loop `session.commit()` (`:190`). |
| T-55-SEAM-02 | DoS | `agent_s3.py:118` rowcount-guarded `PUSHING→PUSHED` flip frees the window slot; CR-01 fix `agent_s3.py:181` exits the terminal upload-failure path `PUSHING→ANALYSIS_FAILED` (b169382); WR-01 fix clears `cloud_phase` on terminal failure (`reconcile_cloud_jobs.py:167`). |
| T-55-SEAM-03 | Tampering | `agent_s3.py:128 resolve_queue_for_task("submit_cloud_job", ...)` then `routed.queue.enqueue` (`:129`) — no raw/default-queue enqueue. AST guard `tests/test_no_default_queue_producers.py:235-251`. |
| T-55-SEAM-04 | Spoofing | `agent_s3.py:64 get_authenticated_agent` Depends on `report_uploaded`; `file_id` on path, agent from token (AUTH-01); adding `request: Request` did not weaken auth. |
| T-55-SEAM-05 | Tampering | `agent_s3.py:118-123` rowcount-guarded idempotent flip; `rowcount == 0 → return` with no re-enqueue (replay-safe). |
| T-55-SEAM-06 | DoS | `release_awaiting_cloud.py:142-144` GATE-1 (`select_active_agent kind="compute"`) gated under `cloud_target == "a1"` — SKIPPED on k8s; GATE-2 fileserver (`:163`) kept for both. |
| T-55-BF-01 | DoS | `services/pipeline.py:977 exists(select(SchedulingLedger.key)...)` ledger-scoped predicate on `_backfill_candidates_stmt`; AST guard `test_no_default_queue_producers.py:255-269` forbids a bare `ANALYSIS_FAILED` sweep. |
| T-55-BF-02 | Tampering | `routers/pipeline.py:744` k8s branch `return`s BEFORE the `insert_ledger_if_absent` block (`:762`) — k8s seeds ZERO `process_file:<id>` ledger rows (no `recover_orphaned_work` local replay). |
| T-55-BF-03 | Tampering | backfill routes via `_route_discovered_by_duration` (`pipeline.py:725`) → stage_cloud_window k8s branch → `enqueue_router`; AST guard covers the enqueue site. |
| T-55-BF-04 | Tampering | `services/pipeline.py:977` ORM `exists()` + `cast(FileRecord.id, String)` bound params; no f-string SQL in the candidate query. |
| T-55-CARD-01 | DoS | `services/pipeline.py:844 get_cloud_phase_counts` wraps each of the four phase counts in `_safe_count` (`:274`, degrades to 0 on DB error, rolls back) — the 5s poll never 500s. |
| T-55-CARD-02 | XSS / info-disclosure | `admission_state_card.html` interpolates only `{{ *_count }}` ints in autoescaped Jinja; NO `role="alert"`, NO `amber`. |
| T-55-CARD-03 | Tampering | `dashboard.html:41` mounts the card OUTSIDE `#pipeline-stats` (`:45`) / `#pipeline-stages` (`:51`); `hx-swap-oob` replaces only its own stable `#admission-state-card` id. |
| T-55-DOC-01 | DoS | Loud rename callouts present; `grep` for `PHAZE_CLOUD_BURST_ENABLED`/`cloud_burst` in `.env.example`, `docker-compose*.yml`, `docs/` returns ZERO. |
| T-55-DOC-02 | Info-disclosure | Docs reference the `_FILE` secret-pointer convention only (`.env.example` 17 `_FILE` vars; `configuration.md:107`); no literal secret values. |
| T-55-DOC-03 | Tampering | `PHAZE_CLOUD_TARGET` count == 0 in `docker-compose.agent.yml` and `docker-compose.cloud-agent.yml`; == 2 in the control-plane `docker-compose.yml` (control-plane-only invariant holds). |

## Threat Verification — `accept` (7 closed; accepted-risks log)

| Threat ID | Category | Accepted Rationale (verified) |
|-----------|----------|-------------------------------|
| T-55-CFG-SC | Tampering (supply chain) | Zero packages installed — no dependency-manifest change vs `main` (`pyproject.toml`/`uv.lock` untouched). |
| T-55-PHASE-03 | Repudiation | `cloud_phase` kept ORTHOGONAL to the `inadmissible` fault flag — `reconcile_cloud_jobs.py:235-274` writes the two independently; auditable distinction preserved. Accepted: progression and fault are separate columns by design. |
| T-55-PHASE-SC | Tampering (supply chain) | Zero packages installed (no manifest change). |
| T-55-SEAM-SC | Tampering (supply chain) | Zero packages installed (no manifest change). |
| T-55-BF-SC | Tampering (supply chain) | Zero packages installed (no manifest change). |
| T-55-CARD-SC | Tampering (supply chain) | Zero packages installed; hand-rolled Jinja, no shadcn/registry. |
| T-55-DOC-SC | Tampering (supply chain) | Zero packages installed (docs-only). |

---

## Unregistered Flags

None requiring action.

- `55-01-SUMMARY ## Threat Flags` — `doc-drift` (docs still referenced `cloud_burst_enabled`):
  maps to T-55-CFG-02 / T-55-DOC-01 and was resolved by Plan 06 (zero `cloud_burst` references
  remain in `docs/` / operator config). Informational, closed.
- `55-06-SUMMARY` — explicitly "None"; control-plane-only invariant asserted (T-55-DOC-03).
- Plans 02/03/04/05 summaries declared no new threat surface beyond their registers.

## Carried Robustness Note (not a registered threat)

- **WR-02 (deferred, from 55-REVIEW.md):** the k8s `stage_cloud_window` loop can orphan S3
  multiparts on mid-loop failure. Backstopped by the S3 lifecycle TTL (`s3_lifecycle_ttl_days`,
  default 2). Documented as a pre-existing robustness tradeoff, not a Phase 55 threat-register
  item. No security disposition required; logged here for traceability.
