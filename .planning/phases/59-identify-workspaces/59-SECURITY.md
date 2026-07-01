---
phase: 59
slug: identify-workspaces
status: verified
threats_open: 0
asvs_level: 1
created: 2026-06-30
---

# Phase 59 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| client → `/s/{stage}` | `stage` path segment is whitelisted against `STAGE_PARTIALS`; unknown stage 404s and never reaches a template path | untrusted path segment |
| client → bulk trigger endpoints | SEARCH/SCRAPE/MATCH ALL reuse existing `/pipeline/*-tracklists` endpoints; existing auth/routing posture preserved, no new sink | POST trigger (enqueue) |
| DB → service helper → template cell | rows read from `fingerprint_results` / `tracklists` / `tracklist_tracks` flow to plain dicts then to autoescaped template cells | filename/path/artist/event (user-derived) |
| hot render/poll → helper | helpers run inside the request render path; an unhandled exception would 500 the page/poll | n/a (availability) |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-57-01 | Tampering (template-path injection) | `shell.py` STAGE_PARTIALS['trackid'] & ['tracklist'] | mitigate | Both values are STATIC string literals (`shell.py:82`, `:87`) — no f-string/format/concat; `stage` is matched against dict keys and never spliced into a template path; unknown stage 404s (`shell.py:187-188`) | closed |
| T-59-XSS | Info-disclosure/Tampering | `trackid_workspace.html` + `tracklist_workspace.html` cells | mitigate | All DB-sourced `text` rendered via `_file_table.html:44` `{{ cell.text }}` autoescape (also `title="{{ cell.title }}"`); zero `| safe` in either fragment; color/status are caller-controlled class strings (in-template Jinja dicts), not user data | closed |
| T-59-DOS | Denial of Service | `get_trackid_stage_files` / `get_tracklist_set_rows` + `_render_stage` branches | mitigate | Both helpers wrap their SELECT in `async with session.begin_nested()` (SAVEPOINT) and `except Exception: logger.warning(...); return []` (`pipeline.py:893-944`, `:988-1018`) — degrade to empty, never raise into render/poll; `oob_counts` stays False | closed |
| T-59-INJ | Tampering | ORM read queries | mitigate | Pure ORM (`select`/`outerjoin`/`exists`/`func`/`group_by`/`.desc().nulls_last()`) — no `text()`, no raw SQL string-building, no interpolated operator input (`pipeline.py:893-1018`) | closed |
| T-59-SCOPE | Elevation/Tampering | new helper write surface | mitigate | Both helper bodies are read-only by construction — grep confirms no `enqueue`/`session.commit`/`session.add`/`session.flush`/`insert`/`update`/`delete`/DDL (`pipeline.py:869-1034`); preserves the no-backend-change boundary | closed |
| T-59-OVERENQ | Denial of Service | SEARCH/SCRAPE/MATCH ALL triggers | mitigate | R-4 guard on each of the three triggers — `hx-confirm` + `:disabled="$store.pipeline.{search|scrape|match}Busy > 0"` (`tracklist_workspace.html:49-53`, `:69-74`, `:90-95`); endpoints reused verbatim (deterministic-keyed/idempotent), no new sink | closed |
| T-59-SC | Tampering | package installs | accept | Phase 59 installs nothing — `git diff` over the phase commit range shows no changes to `pyproject.toml` / `uv.lock`; no supply-chain surface introduced | closed |

*Status: open · closed*
*Disposition: mitigate (implementation required) · accept (documented risk) · transfer (third-party)*

---

## Accepted Risks Log

| Risk ID | Threat Ref | Rationale | Accepted By | Date |
|---------|------------|-----------|-------------|------|
| AR-59-SC | T-59-SC | Phase 59 is UI-only (two read-only HTMX workspaces + two read-only service helpers) and installs no packages; verified no `pyproject.toml`/`uv.lock` diff across the phase commit range (a04c635…78aee66). No new supply-chain surface to audit. | gsd-security-auditor | 2026-06-30 |

*Accepted risks do not resurface in future audit runs.*

---

## Security Audit Trail

| Audit Date | Threats Total | Closed | Open | Run By |
|------------|---------------|--------|------|--------|
| 2026-06-30 | 7 | 7 | 0 | gsd-security-auditor |

**Audit notes:** All seven declared mitigations verified against implemented code (not documentation). Corroborated by `59-REVIEW.md` (0 critical, 0 blockers). Executor `## Threat Surface` sections in all three SUMMARY.md files report no new attack surface beyond the plan threat models; `tech-stack.added: []` in every summary and the empty dependency diff confirm no unregistered supply-chain flags. Code-review WR-01 (per-set coverage version scoping) was applied post-review (commit `d7cafee`) — a correctness fix, not a security threat; the shipped `get_tracklist_set_rows` scopes counts to `latest_version_id` (`pipeline.py:990-1012`). No unregistered flags.

---

## Sign-Off

- [x] All threats have a disposition (mitigate / accept / transfer)
- [x] Accepted risks documented in Accepted Risks Log
- [x] `threats_open: 0` confirmed
- [x] `status: verified` set in frontmatter

**Approval:** verified 2026-06-30
