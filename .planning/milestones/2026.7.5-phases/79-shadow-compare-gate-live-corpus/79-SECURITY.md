---
phase: 79
slug: shadow-compare-gate-live-corpus
status: verified
threats_open: 0
threats_closed: 6
asvs_level: 1
created: 2026-07-08
---

# Security Audit — Phase 79: Shadow-Compare Gate + Live Corpus

**Audited:** 2026-07-08
**Auditor:** gsd-security-auditor
**Status:** SECURED — all threats closed
**Threats Closed:** 6/6
**ASVS Level:** default

Verification method: each declared mitigation was grep-confirmed present in the cited
implementation file, not accepted on documentation or intent. Implementation files were
treated as read-only; only this SECURITY.md was authored.

## Threat Verification

| Threat ID | Category | Disposition | Status | Evidence |
|-----------|----------|-------------|--------|----------|
| T-79-01 | Tampering | mitigate | CLOSED | `src/phaze/services/shadow_compare.py` — imports `done_clause`/`failed_clause` from `phaze.services.stage_status` (line 54); gap-table predicates use correlated `exists(select(Model.id).where(Model.file_id == FileRecord.id, ...))` (lines 77, 82, 87, 97) with bound params (`CloudJob.status == "awaiting"`, `RenameProposal.status == status`); no `text()` import (import line 47) and no `text()`/`stage_status_case` in code (only docstring references); no LEFT-JOIN-null / `not_in(subquery)` anti-pattern. |
| T-79-02 | Information Disclosure | mitigate | CLOSED | `shadow_compare.py` — `InvariantResult.sample` holds only `str(fid)` file_id UUIDs (line 220); `Report.render` emits `r.sample` only (line 193), capped at `sample_cap` via `.limit(sample_cap)` (line 219); no `original_path`/`original_filename` referenced anywhere in render/CLI (only in the docstring stating the exclusion). |
| T-79-03 | Repudiation (false assurance) | mitigate | CLOSED | `tests/integration/test_shadow_compare.py` — `test_divergent_hard_invariant_flags` parametrized over `HARD_INVARIANTS` gives every HARD invariant a non-vacuous RED cell (lines 176-186); `test_allowlist_soft_divergence_counted_but_not_gated` is the counted-but-green soft cell (lines 221-232); `test_core_registry_shape_locks_coverage_and_allowlist` proves full FileState coverage + DISCOVERED absence + exact soft-allowlist (lines 238-247). |
| T-79-04 | Information Disclosure | mitigate | CLOSED | `src/phaze/cli/shadow_compare.py` — `_safe_target(url)` renders host/db only (line 95); `_parse_dsn_or_exit` swallows `make_url` errors with `from None` so the raw DSN never hits stderr (lines 59-63); the password-masking `URL` object (not the raw string) is threaded to `create_async_engine(url)` (line 110); `main()` prints `_safe_target(url)`, never the raw `--database-url` (line 131). Strengthened by review fix WR-01 (commit `ba489b62`). |
| T-79-05 | Tampering | mitigate | CLOSED | `cli/shadow_compare.py` — `--sample-cap` uses `type=_non_negative_int` which rejects negatives at parse time before any DB opens (lines 41-49, 76); `--database-url` is parsed to a `URL` and handed to `create_async_engine`, never string-concatenated into SQL; all queries stay ORM-only (inherited from the Plan-01 core). Strengthened by review fix WR-02 (commit `ba489b62`). |
| T-79-SC | Tampering (package installs) | accept | CLOSED | Accepted risk logged below. Both Plan-01 and Plan-02 SUMMARY `tech-stack.added: []` — zero new dependencies introduced this phase. Nothing to install, nothing to verify. |

## Accepted Risks Log

### T-79-SC — Package installs (Tampering)

**Disposition:** accept
**Rationale:** Phase 79 introduces zero new third-party dependencies. Both plan summaries
declare `tech-stack.added: []`; the implementation imports only already-vendored project
modules and the existing SQLAlchemy stack. There is no new supply-chain surface (no
`pyproject.toml`/`uv.lock` dependency additions), so the RESEARCH Package Legitimacy Audit is
N/A and this threat is accepted as no-op with no residual exposure.

## Additional Hardening Verified (code-review resolutions, commit `ba489b62`)

Beyond the declared register, the following review findings were confirmed fixed in the
current code (not part of the threat register but security-relevant):

- **CR-01 (data-loss footgun):** `tests/integration/test_shadow_compare.py` carries a
  module-level guard (lines 69-75) that `pytest.skip`s at module load unless the target DB
  name ends in `_test`, preventing the committed `TRUNCATE agents CASCADE` from wiping a
  non-test (dev) database.
- **IN-01 (non-deterministic sample):** the sample query adds `.order_by(FileRecord.id)`
  before `.limit(...)` (`shadow_compare.py` line 217) for reproducible triage output.

## Unregistered Flags

None. Neither `79-01-SUMMARY.md` nor `79-02-SUMMARY.md` contains a `## Threat Flags`
section, and no new attack surface was detected during implementation beyond the registered
threats.

## Trust Boundaries (from PLAN threat models)

| Boundary | Description |
|----------|-------------|
| CI / test corpus → DB (`:5433`) | Fixture-seeded rows drive read-only SELECT anti-joins; no untrusted external input |
| Report → CI logs | Divergence output surfaced in CI logs — file_id UUIDs only (T-79-02) |
| Operator CLI → target DB (restore DSN) | `--database-url` may carry a password; never echoed in full (T-79-04) |
| CLI output → stdout / CI logs | Report + connection context printed; at most host/db surfaced |
