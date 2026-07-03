---
phase: 66
slug: docs-drift-gate-dead-code-sweep
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-03
---

# Phase 66 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

Phase 66 is presentation/tooling only — no backend or runtime behavior change. The
attack surface is: a hermetic CI docs-drift guard (parses trusted in-repo markdown), a
flag-gated `/saq` UI link, and a new dev-only `vulture` dependency plus a manual-verify
dead-code sweep (a no-op this phase). All 10 plan-time threats verified CLOSED.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| repo files → guard parser | The traceability guard reads only trusted in-repo `.planning/` markdown; no external/untrusted input crosses. | Trusted repo markdown |
| PR contents → CI quality job | A PR author controls the docs the guard parses, but the guard only asserts internal consistency — it cannot be coerced into executing attacker input. | Untrusted PR-authored markdown (parsed, never executed) |
| operator browser → Agents page | Operator clicks the new footer link; opens `/saq` in a new tab. | UI navigation only |
| shell page → /saq embedded sub-app | `/saq` is a separate mounted SAQ sub-app; the new link adds discoverability, not access. | Same internal-realm auth as the mount |
| PyPI → dev venv | A new external package (`vulture`) is resolved and installed into the dev environment. | Dev-only package bytes |
| vulture candidate list → source deletions | An automated tool proposes deletions; a human gates which are actually removed. | Deletion diff (human-reviewed) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-66-01 | Tampering | traceability guard parser over `.planning/` markdown | accept | Trusted repo content; regex parse over `read_text` cannot execute code; guard is dependency-free. Verified: `test_requirements_traceability.py:42-43` imports only `pathlib`+`re` (zero `phaze.*`/markdown-lib), reads only in-repo files via `read_text`. | closed |
| T-66-02 | Denial of Service | guard runs in the always-run quality job | accept | Pure filesystem parse of small markdown, sub-second, no network/DB. Verified: no `socket`/`http`/`urllib`/DB symbols in the guard. | closed |
| T-66-03 | Repudiation | drift silently merged | mitigate | `aggregate-results` requires the quality job's success; a drift failure blocks merge and names the offender. Verified: `code-quality.yml:54-55` `just docs-drift` step in the always-run quality job with NO `if:` gate; failure messages name phase+requirement (`test_requirements_traceability.py:147`). | closed |
| T-66-04 | Tampering | `code-quality.yml` wiring drifts out (step removed) | accept | Low risk; optional structural wiring-guard is a deferred follow-up. Verified: step present at `code-quality.yml:55`. | closed |
| T-66-05 | Tampering | new `target="_blank"` anchor to `/saq` | mitigate | `rel="noopener"` prevents reverse-tabnabbing. Verified: `agents.html:27` `<a href="/saq" target="_blank" rel="noopener">`; asserted by `test_admin_agents.py:337-339`. | closed |
| T-66-06 | Information Disclosure | surfacing `/saq` more broadly | accept | `/saq` already mounted + URL-reachable (Phase 33); the link only adds discoverability, gated by `enable_saq_ui`; access control (reverse-proxy internal-realm auth) unchanged. Verified: no access-control code touched. | closed |
| T-66-07 | UX (not security) | dead 404 link when `/saq` unmounted | mitigate | `{% if enable_saq_ui %}` gate mirrors the mount condition so the link never renders when the sub-app is absent. Verified: anchor inside the conditional (`agents.html:25-29`); flag injected from the same settings source that gates the mount (`admin_agents.py:111`); absence asserted at `test_admin_agents.py:357`. | closed |
| T-66-SC | Tampering | `vulture` install (supply-chain) | mitigate | Blocking human-verify legitimacy checkpoint (Task 1) verified canonical source github.com/jendrikseipp/vulture + publish date before `uv sync`; dev-only; `exclude-newer=7 days` cooldown excludes fresh typosquats. Verified: `pyproject.toml:228` `"vulture>=2.16"` inside `[dependency-groups] dev` — NOT in runtime `[project].dependencies`; legitimacy checkpoint executed + human-approved this session. | closed |
| T-66-08 | Tampering | deleting live code as "dead" | mitigate | D-12 guardrail (grep dynamic refs + green suite before removal), enumerated DO-NOT-DELETE list, blocking human-verify deletion checkpoint, hand-audited whitelist. Verified: `git diff 818c151..HEAD -- src/phaze` shows only `M` (zero `D`); DO-NOT-DELETE trio present (`build_dashboard_context`, `get_stage_progress`, `get_queue_activity`); `vulture_whitelist.py` hand-audited with per-symbol justification; deletion-review checkpoint executed + human-approved as a no-op. | closed |
| T-66-09 | Denial of Service | `vulture` wired as a blocking gate producing false failures | accept (avoided) | `vulture` is explicitly NOT a blocking CI/pre-commit gate — a non-blocking `just vulture` recipe only. Verified: no `vulture` in `.pre-commit-config.yaml` or `.github/workflows/`; `justfile:107` recipe is plain/non-blocking. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-66-01 | T-66-01 | Guard parses only trusted in-repo `.planning/` markdown via a dependency-free regex/`read_text` parse; no code execution path from parsed content. | Robert (plan-time disposition) | 2026-07-03 |
| AR-66-02 | T-66-02 | Guard is a bounded sub-second filesystem parse of a few small markdown files — no network, no DB, no unbounded input. | Robert (plan-time disposition) | 2026-07-03 |
| AR-66-03 | T-66-04 | Structural CI-wiring guard (asserting the `docs-drift` step stays present) is a low-risk deferred follow-up; the step is present now. | Robert (plan-time disposition) | 2026-07-03 |
| AR-66-04 | T-66-06 | `/saq` was already mounted and URL-reachable since Phase 33; the new link adds discoverability only, gated by `enable_saq_ui`, with unchanged reverse-proxy internal-realm auth — no new exposure. | Robert (plan-time disposition) | 2026-07-03 |
| AR-66-05 | T-66-09 | `vulture` false-positives make a blocking gate too noisy (D-12/D-13); it ships as a non-blocking `just vulture` recipe only, so it cannot produce spurious CI failures. | Robert (plan-time disposition) | 2026-07-03 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-03 | 10 | 10 | 0 | gsd-security-auditor (opus) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-03
