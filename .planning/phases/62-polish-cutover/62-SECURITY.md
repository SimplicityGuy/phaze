---
phase: 62
slug: polish-cutover
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-02
---

# Phase 62 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Phase 62 is a **presentation-only** cutover (a11y attributes + narrow-width rail + dead-code deletion + docs). The attack surface strictly **shrinks** — no new endpoint, input, auth path, data flow, or dependency was introduced (verifier confirmed zero diff under `src/phaze/{services,tasks,models}`; no new runtime dep). Register authored at plan time across all 4 plans; all mitigations verified via the guard tests, code review (0 blockers), and phase verification (5/5) that ran this session.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| browser → server-rendered HTML | Net-new markup is static only: ARIA attributes (`aria-label`), decorative `aria-hidden` inline SVG glyphs, Tailwind class strings, static `title` tooltips. | None (no new input/parameter) |
| browser → legacy routes | Untrusted GETs to `/proposals`, `/tracklists`, `/tags`, `/cue`, `/duplicates`, `/preview`, `/pipeline`, `/search` still cross here; they keep 302-redirecting into the shell (SHELL-05) and content routers keep serving their live HX fragment for HX requests. | HTTP GET (unchanged) |
| template include graph | Deleting a still-referenced partial would 500 a live page at render time. | Jinja render-time include resolution |
| repo docs → readers | Static markdown; no code/endpoint/input introduced. | None |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-62-01-XSS | Tampering / Info-disclosure | shell.html / cmdk_modal.html edits near Alpine JS contexts | mitigate | Only a static `aria-label` string added + an element deletion — no new dynamic value emitted; no `\|e` in a JS-attribute context. Code review confirmed no unsafe interpolation. | closed |
| T-62-01-SRI | Tampering | shell.html `<script integrity=...>` pins | mitigate | Edits far from script tags; `test_base_html_sri.py` green (guards base.html + shell.html). | closed |
| T-62-01-NA | (n/a) | new endpoints/inputs/auth | accept | None introduced — presentation-only a11y attributes. No new attack surface. | closed |
| T-62-02-SUP | Tampering (supply chain) | inline SVG paths copied from heroicons v2 | mitigate | Static path geometry copied verbatim from a known MIT source, inlined (no package install, no fetch); no executable content in SVG `d` attributes. | closed |
| T-62-02-SRI | Tampering | rail.html near shell script pins | accept | rail.html contains no `<script>`/`integrity` tags; no SRI drift possible. | closed |
| T-62-02-NA | (n/a) | new endpoints/inputs/auth | accept | None introduced — pure CSS breakpoint + decorative icons. | closed |
| T-62-03-NA | (n/a) | new endpoints/inputs/auth/data | accept | None introduced — pure documentation. Only risk is doc drift, mitigated by `tests/test_docs_ia_current.py` (now incl. a negative anti-drift assertion). | closed |
| T-62-04-DEL | Denial of Service (broken render) | template deletions | mitigate | Only guard-proven-orphaned templates deleted; KEEP-list protected live shell/record fragments; `test_dead_template_guard.py` green with `_ALLOWLIST` drained to `frozenset()`; full suite (2565) green. | closed |
| T-62-04-RDR | Spoofing / broken links | legacy route redirects | mitigate | Redirects RETAINED (only dead render tails removed); `test_shell_routes.py` / redirect tests assert every legacy route still 302s into the shell (bookmarks preserved). Code review confirmed. | closed |
| T-62-04-REG | Tampering (functional regression) | live HX pagination/filter/sort branches | mitigate | The 5 content routers' live HX fragment renders (proposal_content/tag_list/cue_list/tracklist_list/group_list) are KEPT; only pipeline.py's dead dashboard render path removed. Code review (0 blockers) + verifier confirmed the live branches intact. | closed |
| T-62-04-SRI | Tampering | base.html script pins | mitigate | Tab-bar nav strip far from `<script integrity=...>` lines; `test_base_html_sri.py` green. | closed |
| T-62-04-NA | (n/a) | new endpoints/inputs/auth | accept | None introduced — deletion of dead presentation code; surface strictly shrinks. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

No accepted risks. All `accept`-disposition entries are "no new attack surface" assessments for a presentation-only phase (no residual risk introduced), not risk acceptances of an exploitable condition.

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-02 | 12 | 12 | 0 | /gsd:secure-phase (short-circuit: register authored at plan time, threats_open=0, mitigations verified via green guards + code review 0-blockers + verification 5/5) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-02
