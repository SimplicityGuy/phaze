---
phase: 81
slug: per-stage-failure-persistence-retry-paths
status: secured
threats_open: 0
threats_total: 24
threats_closed: 24
asvs_level: 1
block_on: high
created: 2026-07-09
audited: 2026-07-09
register_authored_at_plan_time: true
remediation_commits: [1d6af9f7, feaebc48]
---

# Phase 81 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

**Verdict: SECURED.** 24 plan-time threats, 24 closed — 16 `mitigate` verified against the
implementation, 8 `accept` rationales confirmed. Nothing at or above `block_on: high` is open.

The register was authored at plan time (every one of the six PLAN.md files carries a
`<threat_model>` block), so the audit **verified the declared mitigations** rather than
reconstructing a register retroactively.

Two threats — `T-81-03-04` and `T-81-05-03` — were initially scored CLOSED by the auditor because
their written *mitigation plan* mentioned only the "oversized" limb. Scored against the threat as
*titled* ("PG-invalid free text (NUL/surrogates) **or** oversized error") they were **OPEN**. Both
were remediated in `1d6af9f7` before this file was written. See the audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| agent → control-plane API | `POST /metadata/{file_id}/failed` and `POST /analysis/{file_id}/failed` are agent-authenticated terminal-failure acks. | agent-supplied `reason` (Literal) + `error` (bounded free text) |
| agent free text → Postgres → operator UI | `error_message` persists agent free text, later rendered on the pipeline dashboard. | untrusted free text |
| operator → control-plane API | `POST /pipeline/metadata-failed/retry` is an operator bulk enqueue over the current failed-metadata set. | no body; server-derived file set |
| enqueue → SAQ queue | The retry endpoint routes jobs to a per-agent lane queue. | job payloads |
| alembic → live corpus | Migration `033` runs `ACCESS EXCLUSIVE` DDL plus a data-mutating UPDATE on deploy. | production `analysis` rows |

Realm note: the `pipeline` router carries no auth dependency — neither does the pre-existing
`retry_analysis_failed` donor nor the router as a whole. This is the established single-user,
private-network operator realm, not a new exposure introduced by this phase.

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation (verified) | Status |
|-----------|----------|-----------|-------------|------------------------|--------|
| T-81-01-01 | Tampering | `enums/stage.py` DB-free invariant | mitigate | Subprocess import guard asserts `sqlalchemy`/`phaze.models`/`phaze.database` never enter `sys.modules`. Module imports only `enum`/`typing`. `tests/shared/test_stage_resolver.py` — 27 passed. | closed |
| T-81-01-02 | Info Disclosure | derived-status semantics | accept | Refactor moves per-stage constants into module dicts; touches no row data, no PII. | closed |
| T-81-02-01 | DoS | `create_check_constraint` on pre-existing mixed row | mitigate | Cleanup UPDATE at `033:70` precedes `create_check_constraint` at `033:73`; the migration test reads `upgrade()`'s source and asserts the ordering. | closed |
| T-81-02-02 | Tampering | data loss on cleanup UPDATE | mitigate | `033:63` clears only `failed_at` where `analysis_completed_at IS NOT NULL`. Test asserts the SQL never contains `SET analysis_completed_at` — no done marker can be lost. | closed |
| T-81-02-03 | Repudiation | ORM ↔ migration schema drift | mitigate | CHECK mirrored at `models/analysis.py:56`. **Caveat:** the declared "empty autogenerate diff" gate is vacuous (alembic emits no CHECK diffs). Closed on the substantive gates that did land: ORM name+predicate equality and a live `pg_constraint` lookup. | closed |
| T-81-03-01 | Spoofing | forged agent/file identity via body | mitigate | `Depends(get_authenticated_agent)`; upsert `file_id` and ledger key both read the PATH param. `MetadataFailurePayload` has no id field. | closed |
| T-81-03-02 | Tampering | unknown/extra body fields | mitigate | `ConfigDict(extra="forbid")` at `schemas/agent_metadata.py:49` → unknown field 422s. | closed |
| T-81-03-03 | DoS | version-skew bodyless POST → ledger never clears | mitigate | `body: MetadataFailurePayload \| None = None`, no `Body()` wrapper → bodyless POST 200s, writes the marker, clears the ledger. | closed |
| T-81-03-04 | Tampering | PG-invalid (NUL/surrogates) **or** oversized free text | mitigate | **Both limbs now hold.** Oversized: `max_length=2000` at the wire + `[:2000]` before persist. PG-invalid: `sanitize_pg_text` strips NUL before persist (`1d6af9f7`); lone surrogates are rejected at the wire as `string_unicode`. | closed |
| T-81-04-01 | Tampering | synthetic `engine='_task'` sentinel poisons per-engine joins | mitigate | `report_fingerprint_failed`'s only DB effect is `clear_ledger_entry`; test asserts `COUNT(*) WHERE engine='_task'` is 0 and row count is invariant across the ack. | closed |
| T-81-04-02 | DoS | poison-file infinite fingerprint auto-retry | accept | Encoded, not implied: `FAILURE_IS_TERMINAL[FINGERPRINT]=False`, `ELIGIBLE_AFTER_FAILURE[FINGERPRINT]=True`. Auto-retry is intentional and unbounded by design. See Accepted Risks. | closed |
| T-81-05-01 | Spoofing | forged agent/file identity via body | mitigate | `Depends(get_authenticated_agent)`; marker `file_id`, the `FileRecord` UPDATE predicate, and the ledger key all read the PATH param. `AnalysisFailurePayload` carries only `reason`/`error`. | closed |
| T-81-05-02 | Tampering | mixed row (both `completed_at` + `failed_at`) | mitigate | Writer sets `analysis_completed_at=None` in both the INSERT and the `on_conflict_do_update` `set_`; `put_analysis` clears `failed_at` unconditionally on success; migration 033's CHECK is the DB backstop. | closed |
| T-81-05-03 | Tampering | PG-invalid **or** oversized free text | mitigate | **Both limbs now hold** — same remediation as T-81-03-04 (`1d6af9f7`). | closed |
| T-81-06-01 | DoS | consumer-less default-queue fallthrough (once stranded 11,428 jobs) | mitigate | `resolve_queue_for_task` called once inside `try`; `NoActiveAgentError` caught and the handler returns immediately. The except branch reaches no enqueue and no state mutation. | closed |
| T-81-06-02 | DoS | unbounded fan-out / duplicate in-flight jobs | mitigate | `before_enqueue` sets `job.key = "extract_file_metadata:<file_id>"` unconditionally, overriding any caller key → live in-flight jobs dedup to a no-op. | closed |
| T-81-06-03 | Tampering | dead-lettered jobs from a `file_id`-only enqueue | mitigate | `_enqueue_extraction_jobs` builds the complete four-field `ExtractMetadataPayload`; no file_id-only path exists. | closed |
| T-81-06-04 | Elevation of Privilege | failure row deleted/cleared prematurely | mitigate | `retry_metadata_failed` contains no `state` assignment, no `delete(FileMetadata)`, no `failed_at` write (D-11). Only `put_metadata`'s clear-on-success wipes the marker. | closed |
| T-81-01-SC · T-81-02-SC · T-81-03-SC · T-81-04-SC · T-81-05-SC · T-81-06-SC | Tampering | npm/pip/cargo installs | accept | `git diff f84a4db0..HEAD -- pyproject.toml uv.lock` is **empty**. Every import added across the phase is `sqlalchemy`, already a dependency. Independently re-verified by the orchestrator. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-81-01 | T-81-04-02 | Fingerprint failure is non-terminal by design (ELIG-04) so a poison file auto-retries without bound. A `MAX_FINGERPRINT_ATTEMPTS` cap is a deferred idea; no Phase-81 requirement asks for it. **This leaves a real unbounded-retry surface.** | operator | 2026-07-09 |
| AR-81-02 | T-81-01-02 | Derived-status refactor is semantics-preserving (D-04); no PII crosses it; the Phase 79 shadow gate proves no derived status changed. | operator | 2026-07-09 |
| AR-81-03 | T-81-0{1..6}-SC | Zero new dependencies this phase — verified by an empty `pyproject.toml`/`uv.lock` diff against the phase base `f84a4db0`. | operator | 2026-07-09 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit 2026-07-09

| Metric | Count |
|--------|-------|
| Threats found | 24 |
| Closed | 24 |
| Open | 0 |
| Remediated during audit | 2 |

**Method.** `gsd-security-auditor` (read-only, verify-mitigations mode, ASVS L1, `block_on: high`)
checked each of the 16 `mitigate` threats against the implementation and confirmed the 8 `accept`
rationales. The orchestrator independently re-verified the supply-chain accepts, the DB-free import
invariant (T-81-01-01), and both findings below before acting on them. Three plan SUMMARY files
self-reported "no threat flags"; a summary's self-assertion was not accepted as evidence.

**Remediated during this audit (`1d6af9f7`):** `T-81-03-04` / `T-81-05-03`. Both threats are titled
"PG-invalid free text (NUL/surrogates) **or** oversized error", but only the oversized limb had
landed. Reproduced end-to-end: NUL clears pydantic validation (only lone surrogates are rejected at
the wire, as `string_unicode`), and Postgres then rejects the write —
`asyncpg.exceptions.CharacterNotInRepertoireError: invalid byte sequence for encoding "UTF8": 0x00`.
Because the marker upsert and the scheduling-ledger clear share one transaction, the abort rolls
**both** back: the ledger row survives, recovery re-enqueues the file, and it fails identically on
the same exception text forever — the unbounded-recovery-loop outcome `T-81-03-03` exists to prevent,
reached through a different door. Reachable by an authenticated agent, or organically from any
exception message carrying a NUL byte (the same class as the v4.0.5 production incident). Fixed by
extracting the already-tested `_sanitize_pg_text` into a stdlib-only `services/pg_text.py` and
applying it before truncation at both persist sites. Two end-to-end tests assert the **ledger
actually clears**, not merely that a string was stripped; both fail with the real driver error when
the sanitizer is reverted.

**Remediated during this audit (`feaebc48`):** WR-03, carried over from `81-REVIEW.md`. `Status` is a
`StrEnum`, and `eligible()` / `domain_completed()` compared with `is`. A raw-string status map — which
is exactly what a SQL round-trip yields, since `stage_status_case` emits `Status.X.value` — made
`eligible({ANALYZE: "failed"}, ANALYZE)` return `True`, reporting a terminally-failed analyze as
eligible. That is the 44.5K over-enqueue class `ELIG-03` guards. Not reachable today (no production
caller outside `enums/stage.py`), but Phase 80's reader cutover consumes these predicates directly.
Fixed by coercing through `Status(...)` and comparing by value; an unrecognised status now raises
rather than silently reading as `NOT_STARTED`.

**Process gaps recorded, not blocking:**
1. `81-03`, `81-04`, and `81-06` SUMMARY files omit the `## Threat Flags` section entirely. `81-06`
   introduced a new HTTP endpoint — precisely the surface that section exists to catch. Its posture
   was verified independently (pre-existing operator realm, no new exposure class).
2. `T-81-02-03`'s declared "empty autogenerate diff" gate is vacuous — alembic's `compare_metadata`
   emits no CHECK-constraint diffs. The threat is closed on the substantive gates that did land, but
   the register's stated control should not be relied on as written.
3. WR-03 was present in `81-REVIEW.md` but omitted from `deferred-items.md` when the review outcome
   was recorded — an orchestrator bookkeeping error, corrected here and in that file.

**Verification after remediation:** mypy clean (204 source files); ruff check + format clean; all
nine test buckets green (3,123 tests — shared 997, analyze 517, agents 441, review 421, identify 230,
discovery 204, integration 155, fingerprint 83, metadata 75); `just docs-drift` green. Both fixes
proven non-vacuous by reverting each and observing the new tests fail.

---

## Still Open (tracked elsewhere, not security-blocking)

These are correctness/robustness items from `81-REVIEW.md`, recorded in `deferred-items.md`. Neither
is a threat-register entry.

- **WR-02** — the `domain_completed` twins diverge on `in_flight ∧ failed` rows, and FAIL-03's retry
  now routinely produces that cell. A Phase-80 prerequisite.
- **WR-01** — `report_metadata_failed`'s upsert can mark a row that already carries real tags as
  failed.
