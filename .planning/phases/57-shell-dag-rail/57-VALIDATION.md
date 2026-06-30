---
phase: 57
slug: shell-dag-rail
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-29
updated: 2026-06-30
---

# Phase 57 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `57-RESEARCH.md` § Validation Architecture.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode = "auto"`) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths = ["tests"]`, `integration` marker) |
| **Quick run command** | `uv run pytest tests/test_shell_routes.py tests/test_redirect_resolution.py tests/test_dead_template_guard.py tests/test_base_html_sri.py -x` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~5s quick · full suite ~minutes (1750+ tests) |

---

## Sampling Rate

- **After every task commit:** Run the quick run command above (the 4 phase-critical test files).
- **After every plan wave:** Run `uv run pytest` (full suite — must stay green; ~1750+ tests today).
- **Before `/gsd:verify-work`:** Full suite green + `uv run ruff check . && uv run mypy .`.
- **Max feedback latency:** ~5 seconds (quick run).

---

## Per-Task Verification Map

| Req ID | Behavior | Test Type | Automated Command | File Exists | Status |
|--------|----------|-----------|-------------------|-------------|--------|
| SHELL-01 | `GET /` → 200, renders shell, Analyze rail node pre-selected (`aria-current="page"`), no redirect | integration (ASGI client) | `uv run pytest tests/test_shell_routes.py::test_root_renders_shell_analyze_default` | ✅ | ✅ green † |
| SHELL-02 | `GET /s/<stage>` with `HX-Request: true` → bare fragment (no `<html>`); counts bound to `$store.pipeline` | integration + render assert | `uv run pytest tests/test_shell_routes.py::test_stage_fragment_is_bare` | ✅ | ✅ green † |
| SHELL-02 | Rail markup carries `hx-get=/s/<stage>` `hx-target=#stage-workspace` `hx-push-url=true` for every node | render assert | `uv run pytest tests/test_shell_routes.py::test_rail_nodes_wired` | ✅ | ✅ green † |
| SHELL-03 | Legacy `<nav>` tab-bar absent from shell; header has ⌘K button + Agents link + status dots | render assert | `uv run pytest tests/test_shell_routes.py::test_tabbar_removed_header_present` | ✅ | ✅ green † |
| SHELL-04 | Theme `<head>` script + `Alpine.store('theme')` + Jura/wave brand present in shell; `$store.pipeline` NOT redefined | render assert | `uv run pytest tests/test_shell_routes.py::test_theme_and_store_preserved` | ✅ | ✅ green † |
| SHELL-05 | All 8 legacy canonical (trailing-slash) routes → ≤1-hop redirect → 200 with matching rail node | integration parametrized | `uv run pytest tests/test_redirect_resolution.py::test_legacy_route_redirects_one_hop` | ✅ | ✅ green † |
| SHELL-05 | In-page filter on a legacy route (`HX-Request: true`) still returns its filter partial (NOT a redirect) | integration | `uv run pytest tests/test_redirect_resolution.py::test_hx_filter_not_redirected` | ✅ | ✅ green † |
| cross-cut | SRI hashes match served CDN bytes for bumped htmx/Alpine; full-semver pins | static + integration | `uv run pytest tests/test_base_html_sri.py` | ✅ | ✅ green |
| cross-cut | No orphaned Jinja2 templates | static AST | `uv run pytest tests/test_dead_template_guard.py` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

† Green per the executors' full-suite run against an ephemeral Postgres+Redis during execution (2517 passed, 0 failed, 97.24% coverage). These ASGI-client tests require a live DB; in a DB-less audit sandbox they raise `OSError` at connection time (environmental, not assertion failures). The two static tests (`test_base_html_sri.py`, `test_dead_template_guard.py`) were re-confirmed green in the audit sandbox.

---

## Wave 0 Requirements

- [x] `tests/test_shell_routes.py` — SHELL-01..04 (root render, bare fragment, rail wiring, tab-bar removal, theme/store preserved)
- [x] `tests/test_redirect_resolution.py` — SHELL-05 8-route ≤1-hop + HX-filter-not-redirected (uses `_route_introspection.iter_effective_routes`)
- [x] `tests/test_dead_template_guard.py` — orphan-template AST guard via `jinja2.meta.find_referenced_templates` (seed green)
- [x] Update inline SRI hashes in `base.html` (existing `tests/test_base_html_sri.py` then validates)
- [x] Shared ASGI-app fixture for the new route tests (reuse existing `app`/`client` fixture in `tests/conftest.py`)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| No-FOUC on hard reload across dark/light | SHELL-04 | Pre-paint flash is browser-render-timing — not assertable in ASGI client | Hard-reload `/` in dark and light; confirm no white flash before paint |
| C3 "evolution not reskin" visual fidelity | SHELL-03/04 | Subjective aesthetic comparison | Compare shell against `aesthetic-C3-evolved.html` + `prototype.html` |
| Back/forward re-binds Alpine in restored workspace | SHELL-02 | Browser history + `htmx:historyRestore` is browser-level | Navigate rail, press Back/Forward; confirm Alpine bindings live in restored `#stage-workspace` |
| ⌘K keybinding opens skeleton modal | SHELL-03 (D-04) | Keybinding + modal open is browser-interaction | Press ⌘K; confirm skeleton palette modal opens/closes |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < ~5s (quick run)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** validated 2026-06-30

---

## Validation Audit 2026-06-30

Post-execution audit (State A — existing contract audited against built artifacts).

| Metric | Count |
|--------|-------|
| Requirements in contract | 9 |
| Gaps found | 0 |
| Resolved | 0 |
| Escalated | 0 |

Every requirement row maps to an existing, behavior-targeting test; all 9 are COVERED. Two static tests re-confirmed green in the DB-less audit sandbox; the seven ASGI-client tests were verified green by the executors against an ephemeral Postgres+Redis during execution. No `gsd-nyquist-auditor` spawn was required (no MISSING/PARTIAL gaps). Phase 57 is **Nyquist-compliant**.
