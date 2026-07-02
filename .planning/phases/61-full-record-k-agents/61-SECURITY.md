---
phase: 61
slug: full-record-k-agents
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-01
---

# Phase 61 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
>
> Register authored at plan time (all 5 PLAN.md files carry a `<threat_model>` block).
> Verified against shipped code by gsd-security-auditor (2026-07-01): **9/9 CLOSED, threats_open: 0**.
>
> Phase 61 is a v7.0 IA/template rewrite over existing routers/services. NO backend
> behavior change: reads are read-only; the one sanctioned additive query is
> `distinct_artists()`. A prior code review (61-REVIEW.md) confirmed the security surfaces
> clean; findings fixed in commit 7a9f9cc.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| jsdelivr CDN → browser | `@alpinejs/focus` third-party script enters the shell's execution context | pinned JS bundle (supply-chain vector) |
| client → `GET /record/{file_id}` | an arbitrary/guessed file id crosses into a DB read | file id (BAC / template-path vector) |
| DB → rendered HTML | filenames, paths, tags, artist names, lane cells flow DB→record/palette/agents body | untrusted DB strings (XSS vector, incl. Alpine JS-attr context) |
| client query → `search()` / `distinct_artists()` | free-text ⌘K query crosses into a DB read | query string (SQLi vector) |
| DB (CloudJob) read → lane card | in-flight counts + state cross into HTML; a DB hiccup must not paint DEAD | count/state (false-alarm DoS vector) |
| empty-state Scan form → `POST /pipeline/scans` | agent_id + scan_root cross to a scan enqueue | path (traversal / info-disclosure vector) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-61-02 | Tampering | `@alpinejs/focus` CDN script | mitigate | SRI SHA-384 pin + full-semver URL + first-party publisher on BOTH templates: `shell/shell.html:41` and `base.html:41` — `@alpinejs/focus@3.15.12` `integrity="sha384-ysJc…"` `crossorigin="anonymous"`. The SRI gate is EXTENDED to guard both: `tests/test_base_html_sri.py:50` `_ALL_TEMPLATES = (_BASE_HTML, _SHELL_HTML)`, parametrized across the presence, full-semver-pin (`:77`), and network hash-match (`:116`) tests. | closed |
| T-61-03 | Elevation of Privilege | `GET /record/{file_id}` | mitigate | Typed `uuid.UUID` path param (FastAPI-validated) `record.py:44`; missing file → friendly 404 fragment `record.py:54-61` (never a template-path build). EVERY read strictly `file_id`-scoped: windows `:64`, analysis `:69`, proposals `:75`, exec-log join `:96`, tag-log `:100` — mirrors proposals.py T-31-06-02. Template name is a static literal. | closed |
| T-61-01 | Tampering (XSS) | record / palette / lane-agent / scan_roots cells | mitigate | Jinja2 autoescape on every DB cell; NEVER `\| safe` and no bare `\|e` in any JS-attribute context (grep across all Phase-61 templates → matches are comments only). Palette artist names `\| urlencode`d into `hx-get`/`href` (`palette_results.html:79-80`); reused `_diff_row.html` keeps `\|tojson` in its Alpine `x-data`. The two `agents_table.html` `x-data` islands (`:83`, `:96`) carry only a server ISO timestamp / localStorage read — no attacker-controllable DB string, not the Phase-60 apostrophe-filename class. | closed |
| T-61-05 | Information Disclosure | 404 path | mitigate | `record.py:54-61` returns `record/record_not_found.html` with `status_code=404`; the fragment is static copy ("That file no longer exists.") — no stack trace, no filesystem detail, no JSON error. | closed |
| T-61-06 | Tampering (SQLi) | `distinct_artists()` ILIKE + `search()` | mitigate | SQLAlchemy bound parameters throughout — `search_queries.py:181-186` builds the LIKE *pattern value* `like = f"%{query}%"` then `.ilike(like)` (pattern is a bound param, never interpolated into SQL text); `search()` `.ilike(f"%{artist}%")` `:76/107/134` identical. Read-only `SELECT DISTINCT … LIMIT` (`:185`). No `.execute(text(...))`, no f-string SQL. | closed |
| T-61-08 | Denial of Service (false-alarm) | `classify_compute_lanes` on DB error | mitigate | `agent_liveness.py:161-186`: read-only `CloudJob` aggregation wrapped in `try/except SQLAlchemyError` → logs, guarded `session.rollback()`, returns `("IDLE", 0)` `:180`. `DEAD` is not a member of `ComputeLaneState` (`:134`) — a DB hiccup can never paint the lane DEAD/red (KDEPLOY-04). | closed |
| T-61-04 | Information Disclosure | first-run scan surface | mitigate | `empty_state.html`: NO directory-browse endpoint, NO free-text path `<input>` — only hidden `agent_id` + `scan_root` from `agent.scan_roots` (`:65-66`) POSTed to the EXISTING `POST /pipeline/scans`. That route re-validates server-side: `..`-as-path-component reject `pipeline_scans.py:339`, scan_root ∈ `agent.scan_roots` membership `:366`, prefix/descendant check `:377`, revoked-agent reject `:351` (T-27-03). | closed |

---

## Accepted Risks

| Threat ID | Category | Rationale | Verified Holds |
|-----------|----------|-----------|----------------|
| T-61-SC | Tampering (supply chain) | No npm/pip/cargo install occurs this phase. The only external asset added is the CDN `<script>` for `@alpinejs/focus`, guarded by the SRI analog gate (T-61-02). No Package Legitimacy table required. | Confirmed — the sole new external asset is the SRI-pinned `@alpinejs/focus@3.15.12` in `shell.html`/`base.html`; no dependency manifest changed by this phase. |
| T-61-07 | Denial of Service | Per-keystroke `distinct_artists` scan runs on UNINDEXED `Text` artist columns. Bounded by debounce (≥150-250ms, client) + `len(q) >= 2` gate + `LIMIT`; single-user tool; a trigram index is a schema change deferred out of presentation scope (RESEARCH A2). | Confirmed — `search.py:64` gates `if q and len(q) >= 2` before calling `distinct_artists`; `search_queries.py:185` applies `.limit(limit)` (default 20); debounce is owned client-side in the ⌘K palette. Rationale holds. |

---

## Unregistered Flags

None. `61-02-SUMMARY.md ## Threat Flags` explicitly records "None" — the only new surface
(`GET /record/{file_id}`) is already in the plan register as T-61-03. Plans 61-01/03/04/05
carry no `## Threat Flags` section (no new attack surface detected during implementation).

---

## Audit Trail

- 2026-07-01 — gsd-security-auditor: verified 9/9 threats CLOSED against shipped code
  (7 mitigate + 2 accept). Implementation files unmodified. Register authored at plan time.
- Prior: gsd-code-reviewer (61-REVIEW.md) confirmed SQLi / template-path-BAC / XSS / write-path
  surfaces clean; correctness findings (CR-01, WR-01..05) fixed in commit 7a9f9cc.
