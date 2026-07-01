---
phase: 60
slug: review-apply
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-01
---

# Phase 60 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
>
> Register authored at plan time (all 4 PLAN.md files carry a `<threat_model>` block).
> Verified against shipped code by gsd-security-auditor (2026-07-01): **9/9 CLOSED, threats_open: 0**.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| client → bulk-approve / tag-bulk endpoints | request body/query is UNTRUSTED — must not drive which rows are approved/written | forged/stale selection (id-list) |
| client → edit PATCH | operator-supplied `proposed` value crosses into a persisted Text field later used to build a physical move path | filename / path string (path-traversal vector) |
| client → `/s/{stage}` | `stage` is whitelisted against `STAGE_PARTIALS`; must never reach a template path | stage key (template-path-injection vector) |
| DB → response HTML | filenames/paths/tags/cue text cross into rendered diff rows, toasts, and the cue `<pre>` | untrusted file/tag/cue strings (XSS vector, incl. Alpine JS-attribute context) |
| client → resolve/undo/generate | keeper `canonical_id`, undo `file_states` blob, cue generate cross to existing routes | dedupe/cue apply parameters |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-60-01 | Tampering / EoP | D-02 bulk-approve + D-03 tag-bulk | mitigate | Endpoints re-QUERY the fixed predicate at submit (`confidence>=0.9` / no-blank+≥1-change); read NO client `proposal_ids`/id-list; SQLAlchemy-parameterized `where`, NULL-confidence excluded. `proposals.py:340-353` (zero Form params → `approve_pending_above_confidence(threshold=0.9)`), `proposal_queries.py:204-207`, `tags.py:405-426`. Legacy id-list isolated to untouched `bulk_action` (`proposals.py:407`). | closed |
| T-60-02 | Tampering | D-05 edit PATCH `proposed` value | mitigate | `_validate_proposed_value` (`proposals.py:311-336`) rejects empty/whitespace, control/NUL (`ord<0x20 or ==0x7F`), any `..` segment, `/` on filename facet; path facet `strip("/")`+collapse `//`. ORM-bound persist via `update_proposal_fields` (`proposal_queries.py:214-242`) — no string interpolation. Blocks path-traversal into the later physical move. | closed |
| T-60-03 | Repudiation | REVIEW-05 audit integrity + dedupe reversibility | mitigate | Integration tests assert exactly ONE `TagWriteLog` per write, one dedupe resolution per resolve, and reversibility (`tests/integration/test_review_audit.py`). Tag undo reuses `execute_tag_write(before_tags, source="undo")` (`tags.py:475`); dedupe UNDO round-trips the `file_states` blob — append-only, coherent trail, no new apply logic. | closed |
| T-60-XSS | Tampering / Info-disclosure | all response partials incl. `_diff_row` edit island + cue `<pre>` | mitigate | Jinja autoescape on every DB-sourced cell; NEVER `\| safe` on user data (grep across all 9 templates → matches are comments only). **JS-context hardened:** the Alpine edit island uses `\|tojson` (NOT `\|e`) with a single-quoted attr delimiter — `_diff_row.html:32` `x-data='{ editing:false, val:{{ after\|tojson }} }'` and `:64` DISCARD; the prior `val:'{{ ..\|e }}'` HTML-only escaping (insufficient for JS-string context) is removed. `test_diff_row_edit_island_is_js_context_safe` asserts `val:'` absent and `'` present. | closed |
| T-60-R6 | Tampering | inline-edit swap scope | mitigate | SAVE EDIT `hx-target` = own row id + `hx-swap="outerHTML"`, `hx-include` scoped to own row; Alpine LOCAL `x-data` island (`_diff_row.html:32,63`). No `hx-swap-oob` on the row; counts-only poll never re-renders it (R-2). | closed |
| T-60-DOS | Denial of Service | `_render_stage` read helpers | mitigate | All four read helpers wrap reads in `async with session.begin_nested()` and `return []` on exception (`review.py:61/104/173/220`, returns at `:76/136/199/269`); `shell.py:142` `oob_counts=False`; no router try/except. | closed |
| T-60-CUE | Tampering | cue in-memory preview render | mitigate | `get_cue_review_cards` builds the preview via `generate_cue_content` ONLY (`review.py:228`); `write_cue_file` absent from `review.py`. Render never mutates disk; the write happens only on explicit APPROVE → `POST /cue/{id}/generate`. | closed |
| T-57-01 | Tampering | `shell.py` `STAGE_PARTIALS` | mitigate | Every value a STATIC string literal (`shell.py:71-119`, no f-string/format/concat); `stage` matched against dict keys; unknown stage → 404 (`shell.py:254`). Never spliced into a template path. | closed |
| T-60-SC | Tampering | package installs | accept | Phase 60 installs nothing — `git diff phase-59..HEAD` shows no `pyproject.toml`/`uv.lock` change. No supply-chain surface. | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-60-01 | T-60-SC | Phase 60 adds no new dependencies (no `pyproject.toml`/`uv.lock` change); no supply-chain surface to mitigate. | Robert Wlodarczyk | 2026-07-01 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-07-01 | 9 | 9 | 0 | gsd-security-auditor (opus, ASVS L1, block_on: high) |

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-07-01
