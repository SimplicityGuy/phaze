---
phase: 57
slug: shell-dag-rail
status: verified
threats_open: 0
asvs_level: 2
created: 2026-06-29
---

# Phase 57 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

Phase 57 (shell-dag-rail) is a presentation-only milestone: a three-column app shell
(`GET /` + `GET /s/{stage}`), an HTMX DAG rail, a ⌘K skeleton modal, lifted theme/
`$store.pipeline` machinery, and conditional redirects on legacy GET handlers. The threat
register below was authored at plan time (`register_authored_at_plan_time: true`); each
mitigation has been verified to exist in the implemented code (grep-confirmed, not accepted
on documentation or intent).

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| CDN → browser | htmx/Alpine bytes fetched from unpkg/jsDelivr cross into the app execution context | JavaScript (SRI-gated) |
| browser → shell route | `{stage}` path parameter crosses from an untrusted URL into stage resolution | URL path segment |
| browser address bar / bookmark → legacy route | a plain GET crosses into a redirect handler that emits a `Location` header | HTTP `Location` header |
| in-page filter (HX) → legacy route | same path, distinguished only by the `HX-Request` header | filter query params |
| /pipeline/stats poll → DOM | server-computed OOB count seeds interpolated into Alpine `x-init`/`x-text` | numeric ints |
| browser → ⌘K modal | client-only Alpine state; no server input in Phase 57 | none |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-57-SRI | Tampering | htmx/Alpine CDN `<script integrity=>` | mitigate | Pinned recomputed SHA-384 SRI hashes — `base.html:34` htmx@2.0.10 `sha384-H5Srcfyg…`, `base.html:40` alpinejs@3.15.12 `sha384-pb6hrQvo…`; `tests/test_base_html_sri.py` enforces full-semver pin + https-only scheme + live-CDN SHA-384 recompute | closed |
| T-57-SC | Tampering | npm/pip/cargo installs | accept | No package installs this phase — Tailwind vendored static (`static/vendor/tailwindcss-browser-4.3.2.min.js`); htmx/Alpine are SRI-gated CDN scripts. All four plan SUMMARY `## Threat Flags` = None; no new dependency | closed |
| T-57-01 | Tampering | `GET /s/{stage}` stage resolution | mitigate | `shell.py:51-64` static `STAGE_PARTIALS` dict; `shell.py:113-114` `if stage not in STAGE_PARTIALS: raise HTTPException(404)`; `shell.py:86` `stage_partial=STAGE_PARTIALS[stage]` — partial name is a static literal, `stage` never interpolated into a template path (ASVS V5) | closed |
| T-57-02 | Tampering | `/pipeline`→`/` RedirectResponse Location | accept | `pipeline.py:565-566` `RedirectResponse(url="/", status_code=302)` — target is the static internal constant `/`, never user input; no open-redirect surface | closed |
| T-57-03 | Injection (XSS) | new shell templates | mitigate | No `\| safe` / autoescape-off in any `shell/**` template (grep: zero matches; only a comment confirming `stage` is autoescaped); Jinja2 autoescape on by default; lifted `$store.pipeline` seeds are server-computed ints | closed |
| T-57-04 | Injection (XSS) | rail count / status-dot `x-text` + OOB `x-init` seeds | mitigate | `rail.html:45-120` + `header.html:44-48` bind `x-text` to numeric `$store.pipeline.*` keys only (discovered/metadataDone/fingerprintDone/analyzeActive/tracklistDone/proposalsDone/agentOnline); no user-influenced string interpolation, no new store keys, no `\| safe` | closed |
| T-57-05 | Tampering | ⌘K skeleton modal | accept | `cmdk_modal.html` — client-only Alpine affordance, empty skeleton body, no `fetch`/`hx-get`/`x-text`/`action=`/`@submit` (grep: zero matches), no server input (D-04) | closed |
| T-57-06 | Spoofing/DoS | status strip refresh | accept | `header.html:40` binds to the EXISTING `/pipeline/stats` 5s OOB poll; no `setInterval`/`hx-trigger every`/new poll loop in any `shell/**` template (grep: zero matches), no new endpoint (D-05) | closed |
| T-57-RD | Tampering | legacy-route RedirectResponse Location | mitigate | All 7 redirect targets are static internal string literals — `proposals.py:129` `/s/propose`, `tracklists.py:90` `/s/tracklist`, `tags.py:151` `/s/tagwrite`, `cue.py:187` `/s/cue`, `duplicates.py:90` `/s/dedupe`, `preview.py:46` `/s/move`, `search.py:40` `/?palette=1` — never derived from query param/user input; `test_redirect_resolution.py` asserts exact canonical targets | closed |
| T-57-FH | Tampering | conditional HX-Request branch | mitigate | Each redirect fires under `if request.headers.get("HX-Request") != "true"` (FIRST handler statement) with the existing `== "true"` filter branch left intact (verified in all 6 filterable routers); `tests/test_redirect_resolution.py::test_hx_filter_not_redirected` enforces a `HX-Request: true` GET is NOT 302/307 | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-57-SC | T-57-SC | No package-manager installs this phase: Tailwind is a vendored audited static build, htmx/Alpine are SRI-gated CDN scripts, no new runtime dependency. No registry supply-chain surface introduced. | gsd-security-auditor | 2026-06-29 |
| AR-57-02 | T-57-02 | `/pipeline`→`/` redirect target is the hardcoded constant `/` (`pipeline.py:566`); no request input flows into the Location header. No open-redirect surface. | gsd-security-auditor | 2026-06-29 |
| AR-57-05 | T-57-05 | The ⌘K command palette is a client-only Alpine skeleton with an empty body — no fetch, no command execution, no user input reaches the server in Phase 57 (D-04). Functional palette is deferred to Phase 61. | gsd-security-auditor | 2026-06-29 |
| AR-57-06 | T-57-06 | The header agent-status strip rides the single existing `/pipeline/stats` 5s OOB poll; no new poll loop, timer, or endpoint is introduced, so there is no new request-amplification surface (D-05). | gsd-security-auditor | 2026-06-29 |

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-29 | 10 | 10 | 0 | gsd-security-auditor |

### Verification Notes

- **T-57-SRI hash deviation (benign, intent preserved):** the plan-time register cited the
  RESEARCH-supplied Alpine SHA-384 (`sha384-LUONAH…`). The implementation correctly ships
  the live-CDN-recomputed value (`sha384-pb6hrQvo…`) per the Plan-01 Rule-1 auto-fix — the
  RESEARCH hash was stale and would have silently blocked Alpine. The mitigation *intent*
  (pinned recomputed SHA-384 + enforcing live-CDN-recompute test) holds; `test_base_html_sri.py`
  is the authority and is green. Threat CLOSED.
- **Unregistered flags:** none. All four plan SUMMARY files (`57-01`…`57-04`) report
  `## Threat Flags: None` — no new network endpoint, auth path, file-access pattern, or
  trust-boundary surface appeared during implementation that lacks a register mapping.
- **Code review cross-check:** `57-REVIEW.md` independently cleared the four flagged risk
  surfaces (template-path injection, HX-conditional redirect, open-redirect/SSRF, htmx/Alpine
  wiring) with 0 critical findings. The 4 WARNING / 3 INFO findings are maintainability/
  robustness/a11y items, not unmitigated security threats in this register.

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-29
