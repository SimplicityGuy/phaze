---
phase: 44
slug: analyze-observability-ui-straggler-failed-count-sampled-badg
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-18
---

# Phase 44 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| Admin browser → FastAPI (HTMX) | Pipeline dashboard polls + the `POST /pipeline/files/{file_id}/deepen` re-trigger | Path-param `file_id` (typed `uuid.UUID`); no request body |
| control-plane producer → SAQ broker (Postgres `saq_jobs`) | `enqueue_process_file` serializes a complete `ProcessFilePayload` onto a per-agent queue | Job key `process_file:<file_id>` + JSON payload (server-built; `fine_cap`/`coarse_cap` ints) |
| SAQ broker → agent worker | `process_file` deserializes `ProcessFilePayload` (`extra="forbid"`) and runs essentia compute | Validated payload only |
| FastAPI → Postgres (`saq_jobs` / `files`) | Hot 5s `/pipeline/stats` poll reads straggler + analysis-failed counts | Static `text()` SQL, ORM-bound params; no operator free-text |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-44-01 | Tampering | ProcessFilePayload cap fields | mitigate | Typed `int \| None` under `extra="forbid"`; pydantic rejects malformed/extra at `process_file` deserialization. `agent_tasks.py:31,42-43`; consumed `functions.py:141` | closed |
| T-44-02 | DoS | cap=0 analyze-ALL reaches analyze_file | accept | Intended deepen lever; bounded by inner pebble timeout (`analysis_inner_timeout_sec` default 6600, `lt=7200`) + outer SAQ `timeout=7200`. `config.py:460-466`; `analysis_enqueue.py:94`; `functions.py:157-167` | closed |
| T-44-03 | Spoofing | producer routing to consumer-less queue | mitigate | Plan-01 only extends the single `enqueue_process_file` funnel + bulk caller; routing is the caller's job (Plan 03). Deterministic key preserved. `analysis_enqueue.py:32-40,88`; single funnel (no second key path) | closed |
| T-44-04 | DoS | hot 5s poll 500ing on DB hiccup | mitigate | `session.begin_nested()` SAVEPOINT, log `straggler_degraded`/`analysis_failed`, return 0, never raise. `pipeline.py:828-833` (straggler); `get_analysis_failed_count` via `_safe_count` `pipeline.py:612-616` | closed |
| T-44-05 | Tampering/Injection | static SQL against saq_jobs | mitigate | `_STRAGGLER_ACTIVE_SQL` static `text()`, only literals (`'active'`, `'process_file'`); `threshold_sec` is a Python int compared post-deserialize, never interpolated. `pipeline.py:779,815-816,838` | closed |
| T-44-06 | DoS | deserializing every saq_jobs row | accept | Query restricts to `status = 'active' AND split_part(key,':',1) = 'process_file'` — bounded small active set. `pipeline.py:779,812-813` | closed |
| T-44-07 | Spoofing/Misrouting | re-enqueue hitting consumer-less default queue (Phase-30) | mitigate | `enqueue_router.resolve_queue_for_task` raises (never falls through to default); `NoActiveAgentError` caught → fragment returned WITHOUT enqueuing. `enqueue_router.py:120-151,78`; `pipeline.py:491-495` | closed |
| T-44-08 | Tampering | file_id-only payload dead-lettering under extra="forbid" (v4.0.8) | mitigate | Deepen funnels through `enqueue_process_file` which builds the COMPLETE `ProcessFilePayload`. `analysis_enqueue.py:66-77`; `pipeline.py:502` | closed |
| T-44-09 | DoS | repeated deepen clicks flooding queue | mitigate | Deterministic `process_file:<file_id>` key dedups an in-flight repeat to a no-op (SAQ incomplete-set). `analysis_enqueue.py:32-40,88` | closed |
| T-44-10 | Tampering/Injection | path-param file_id | mitigate | Typed `file_id: uuid.UUID` (422 on malformed); `scalar_one_or_none()` → not-found fragment, never raw SQL/500. `pipeline.py:457,484-485,487,507` | closed |
| T-44-11 | DoS | dashboard 500 on degraded read | mitigate | Counts come from Plan-02 degrade-safe services (zero on error); router adds NO try/except. `pipeline.py:358-362,398-402`; card `straggler_failed_card.html:7-9` | closed |
| T-44-12 | Info Disclosure/Error | badge erroring on NULL coverage (pre-43 rows) | mitigate | Badge gates `analysis is not none and analysis.sampled`; NULL/false renders nothing. `sampled_badge.html:12`; `analysis_timeline.html:6`; fetch `proposals.py:282-283` | closed |
| T-44-13 | XSS | coverage counts / file_id in badge + hx-post URL | mitigate | Ints + typed uuid via Jinja autoescape; `hx-post` uses path-param uuid only, no operator free-text. `sampled_badge.html:14`; `straggler_failed_card.html:23,28`; `analysis_timeline.html:10` | closed |
| T-44-14 | CSRF | deepen hx-post cross-site | accept | Single-user admin tool behind internal reverse-proxy realm; consistent with existing `/pipeline/*` POSTs (Phase 37 no-app-auth decision). | closed |
| T-44-SC | Tampering | dependency installs | mitigate | No new packages in any Plan 01-04 commit (no `pyproject.toml` / `uv.lock` change since the Phase-43 base `9976cb5`). | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-44-02 | T-44-02 | Unbounded (cap=0) deepen analysis is the intended operator lever. Re-verified bound holds: inner pebble timeout `analysis_inner_timeout_sec` default 6600 with `lt=7200` enforced (config.py:460-466) sits below the outer SAQ `timeout=7200` (analysis_enqueue.py:94), so a runaway is SIGKILLed deterministically. No new unbounded path introduced. | Phase 44 plan author | 2026-06-18 |
| AR-44-06 | T-44-06 | Deserializing active `process_file` job blobs is bounded — the SQL restricts to `status='active' AND process_file` prefix (pipeline.py:779), a small concurrent-analysis set, not the full backlog. | Phase 44 plan author | 2026-06-18 |
| AR-44-14 | T-44-14 | No CSRF token on the deepen hx-post. Accepted: single-user admin tool on a private network behind a reverse-proxy internal realm; consistent with every existing `/pipeline/*` POST and the Phase 37 no-app-auth decision. | Phase 44 plan author | 2026-06-18 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-18 | 14 | 14 | 0 | gsd-security-auditor |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-18
