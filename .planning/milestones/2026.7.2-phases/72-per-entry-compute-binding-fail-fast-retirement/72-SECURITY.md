---
phase: 72
slug: per-entry-compute-binding-fail-fast-retirement
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-05
---

# Phase 72 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator → `backends.toml` → `ControlSettings` | operator-authored registry config crosses into the process; a compute entry's `agent_ref` becomes a DB lookup key and boot-time validation input | non-secret config (`kind`/`id`/`rank`/`cap`/`agent_ref`/`scratch_dir`) — no `SecretStr` on compute entries |
| operator → `backends.toml` `agent_ref` → `Agent.id` DB lookup | operator-authored `agent_ref` becomes a parameterized DB key selecting the dispatch-eligible compute agent | agent id slug (constrained FK target) |
| control plane → `agents` table (liveness) | the `revoked_at IS NULL AND last_seen_at IS NOT NULL` liveness filter decides whether a bound agent is dispatch-eligible | agent liveness state |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-72-01-01 | Tampering | golden characterization asserting a wrong baseline | mitigate | `tests/analyze/services/test_compute_binding_golden.py` pins CURRENT behavior (`resolved_non_local_kind`, `active_compute_scratch_dir`, `/pushed` path, `is_available` T/F); authored against unchanged prod, green in Wave 1 | closed |
| T-72-01-02 | Information disclosure | test fixtures echoing secrets | accept | `config_backends.py` `ComputeBackend` carries only `kind/id/rank/cap/agent_ref/scratch_dir` — no `SecretStr`; fixtures construct no credential material | closed |
| T-72-01-SC | Tampering (supply-chain) | npm/pip/cargo installs | mitigate | Zero new dependencies; `git diff main…HEAD -- pyproject.toml uv.lock` empty; no install task | closed |
| T-72-02-01 | Elevation of privilege / wrong-target dispatch | `active_compute_scratch_dir` returning a wrong `scratch_dir` for N compute | accept | Transitional first-entry reduction (`config.py`); the ≤1 return is byte-identical (D-07, golden asserts `/srv/scratch`); `agent_push.py` untouched; per-agent widening deferred to Phase 73 (MCOMP-03) | closed |
| T-72-02-02 | Denial of service | dropping the fail-fast masks a genuine mis-config | mitigate | Replaced by (a) the Plan-04 boot-time duplicate-`agent_ref` guard (`config.py:445-451`) and (b) D-05 runtime degrade-to-hold (`backends.py:274-278`) | closed |
| T-72-02-03 | Tampering | non-preserving edit slips past review | mitigate | Plan-01 golden re-run and green; only the two intentionally-flipped raise tests differ | closed |
| T-72-02-SC | Tampering (supply-chain) | npm/pip/cargo installs | mitigate | Zero new dependencies | closed |
| T-72-03-01 | Spoofing / wrong-target dispatch | `agent_ref` resolving to an unintended agent | mitigate | `select_agent_by_id` matches `Agent.id == agent_id` ONLY (no name fallback, D-01); parameterized SQLAlchemy `where` — no SQL injection; verified by name-collision test | closed |
| T-72-03-02 | Denial of service | mistyped / not-yet-registered `agent_ref` | mitigate | D-05 degrade-to-hold: absent/unregistered/revoked/wrong-kind → `NoActiveAgentError` caught → `is_available` False, never raises (`backends.py:274-278`) | closed |
| T-72-03-03 | Tampering | non-preserving rewire on the real single-compute deploy | mitigate | Golden `is_available` matching-ref cell binds `agent_ref==id`, asserts byte-identical single-compute behavior | closed |
| T-72-03-SC | Tampering (supply-chain) | npm/pip/cargo installs | mitigate | Zero new dependencies | closed |
| T-72-04-01 | Spoofing / silent double-bind | two compute backends binding the same `agent_ref` after the ≤1 fail-fast retirement | mitigate | D-04 boot-time id-tagged duplicate-`agent_ref` guard (`_validate_registry` `Counter`) raises `ValueError` naming the value + colliding backend ids; verified by test asserting message contents | closed |
| T-72-04-02 | Denial of service | a boot-time DB existence check wedging startup when an agent has not checked in | mitigate | D-05: guard is STATIC (`Counter` over config values, no DB session); unregistered `agent_ref` boots cleanly (test with no DB fixture) | closed |
| T-72-04-03 | Tampering | validator masking the per-variant "requires an `agent_ref`" message | mitigate | The duplicate block filters `agent_ref is not None`, so `_require_dispatch_fields`'s per-variant message still surfaces; container guard fires only on genuine duplicates | closed |
| T-72-04-SC | Tampering (supply-chain) | npm/pip/cargo installs | mitigate | Zero new dependencies | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-72-01 | T-72-01-02 | Compute registry entries carry no `SecretStr` field (`config_backends.py` `ComputeBackend`: `kind/id/rank/cap/agent_ref/scratch_dir` only); no credential material is constructed or logged by the golden/registry fixtures | gsd-security-auditor (opus) | 2026-07-05 |
| AR-72-02 | T-72-02-01 | Transitional first-entry `active_compute_scratch_dir` reduction; the ≤1-compute return is byte-identical (D-07) and the real target deploy is `local + N-Kueue + ≤1-compute`. Per-agent scratch resolution is Phase 73 (MCOMP-03); `agent_push.py` stays byte-identical in Phase 72 | gsd-security-auditor (opus) | 2026-07-05 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-05 | 15 | 15 | 0 | gsd-security-auditor (opus), verify-mitigations mode |

**Non-threat note (tracked for Phase 73):** Code review WR-01 — `_probe_availability` (`backends.py:593-603`) shares one `AsyncSession` across an `asyncio.gather`, a comment-asserted invariant now stale for N≥2 compute. Assessed as a **correctness/availability** item, **not a security threat**: it affects only the read-only BEUI lane-snapshot poll, which is fully degrade-safe (`get_backend_lane_snapshot` wraps the fan-out in `try/except` → `[]`), derives no write or dispatch decision, and exposes no spoofing/EoP/info-disclosure surface. Already dispositioned to Phase 73 (MCOMP-02..06).

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-05
