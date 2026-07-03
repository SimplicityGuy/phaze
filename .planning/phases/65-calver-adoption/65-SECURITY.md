---
phase: 65
slug: calver-adoption
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-03
---

# Phase 65 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

Register origin: **authored at plan time** (both 65-01-PLAN.md and 65-02-PLAN.md
carry `<threat_model>` blocks). Verification confirmed each mitigation exists in the
implementation; this was cross-checked by the independent phase verifier (65-VERIFICATION.md,
4/4 PASS) and the code review (65-REVIEW.md, which hardened T-65-01's guard test).

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| git tag push → GitHub Actions `on: push: tags` gate | The `tags` glob is the sole gate deciding whether a tag ref runs CI/publish at all. Whoever can push a tag triggers (or fails to trigger) the release pipeline. | Release tag ref (`2026.7.0`) — integrity-critical |
| CI build → GHCR image publish | The published `:latest` / `:2026.7.0` / `:2026.7` image set is the integrity artifact consumers pin via `PHAZE_IMAGE_TAG`. | Container images — supply-chain integrity |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-65-01 | Denial of Service (availability) / Tampering (integrity) | `.github/workflows/ci.yml` `on: push: tags` glob | mitigate | `ci.yml:13` uses the docs-cited CalVer-only glob `["[0-9]+.[0-9]+.[0-9]+"]` (quoted). It matches `2026.7.0`/`2026.12.10` and rejects `main`/`v2026.7.0`/`2026.7`/pre-release suffixes — so it neither fails to publish a CalVer release (availability) nor over-matches an unintended ref (integrity). Guarded by `tests/agents/deployment/test_agent_compose.py::test_ci_workflow_triggers_on_version_tags`, which asserts the exact CalVer glob is present, that legacy `v*.*.*` is absent, **and** (hardened in code review, commit `d1e3a54`) that no entry carries a leading `v` or `*` wildcard — closing a substring-match evasion. `actionlint` (pre-commit) validates the workflow syntax. | closed |
| T-65-02 | Repudiation (auditability) | annotated-tag-PUSH-triggers-publish invariant (D-04) | mitigate | `docs/deployment.md:354` states the invariant in prose: GHCR publish fires on the **push** of an annotated tag (`git tag -a … && git push origin …`); creating the tag locally publishes nothing, and the delete-recreate recovery recipe (`git push --delete origin <tag>` → re-tag → push) is preserved under CalVer. `test_ci_detect_changes_forces_code_changed_on_tags` (kept unchanged, green) proves the tag-ref force branch still fires; the retargeted trigger test proves the tag gate still exists. `docker-publish.yml`'s metadata-action is left functionally unchanged (D-05/D-06 — only a stale comment de-`v`-ed), preserving the deterministic `:latest`+`:<version>`+`:<major>.<minor>` publish set and the auditable "annotated tag push → publish" path. | closed |
| T-65-SC | Tampering (supply chain) | npm/pip/cargo installs | accept | No package installs in this phase. `uv lock` only re-synced the existing `phaze` version entry (`git diff` on `uv.lock` = version line only; `pyproject.toml` diff = `version = "7.0.0"` → `"2026.7.0"` only). No new/changed dependency, no `[ASSUMED]`/`[SUS]`/`[SLOP]` package to gate (RESEARCH §Package Legitimacy Audit: not applicable). | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-65-01 | T-65-SC | No packages are installed in this phase; `uv lock` only re-syncs the existing `phaze` version entry with no dependency addition or upgrade. There is no install-time supply-chain surface to mitigate. | Robert (operator) | 2026-07-03 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-03 | 3 | 3 | 0 | gsd-secure-phase (short-circuit: plan-time register, threats_open 0; mitigations re-verified inline against live artifacts) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-03
