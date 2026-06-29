---
phase: 57
slug: shell-dag-rail
status: approved
nyquist_compliant: true
wave_0_complete: false
created: 2026-06-29
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
| SHELL-01 | `GET /` → 200, renders shell, Analyze rail node pre-selected (`aria-current="page"`), no redirect | integration (ASGI client) | `uv run pytest tests/test_shell_routes.py::test_root_renders_shell_analyze_default` | ❌ W0 | ⬜ pending |
| SHELL-02 | `GET /s/<stage>` with `HX-Request: true` → bare fragment (no `<html>`); counts bound to `$store.pipeline` | integration + render assert | `uv run pytest tests/test_shell_routes.py::test_stage_fragment_is_bare` | ❌ W0 | ⬜ pending |
| SHELL-02 | Rail markup carries `hx-get=/s/<stage>` `hx-target=#stage-workspace` `hx-push-url=true` for every node | render assert | `uv run pytest tests/test_shell_routes.py::test_rail_nodes_wired` | ❌ W0 | ⬜ pending |
| SHELL-03 | Legacy `<nav>` tab-bar absent from shell; header has ⌘K button + Agents link + status dots | render assert | `uv run pytest tests/test_shell_routes.py::test_tabbar_removed_header_present` | ❌ W0 | ⬜ pending |
| SHELL-04 | Theme `<head>` script + `Alpine.store('theme')` + Jura/wave brand present in shell; `$store.pipeline` NOT redefined | render assert | `uv run pytest tests/test_shell_routes.py::test_theme_and_store_preserved` | ❌ W0 | ⬜ pending |
| SHELL-05 | All 8 legacy canonical (trailing-slash) routes → ≤1-hop redirect → 200 with matching rail node | integration parametrized | `uv run pytest tests/test_redirect_resolution.py` | ❌ W0 | ⬜ pending |
| SHELL-05 | In-page filter on a legacy route (`HX-Request: true`) still returns its filter partial (NOT a redirect) | integration | `uv run pytest tests/test_redirect_resolution.py::test_hx_filter_not_redirected` | ❌ W0 | ⬜ pending |
| cross-cut | SRI hashes match served CDN bytes for bumped htmx/Alpine; full-semver pins | static + integration | `uv run pytest tests/test_base_html_sri.py` | ✅ (update hashes) | ⬜ pending |
| cross-cut | No orphaned Jinja2 templates | static AST | `uv run pytest tests/test_dead_template_guard.py` | ❌ W0 (seed green) | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_shell_routes.py` — SHELL-01..04 (root render, bare fragment, rail wiring, tab-bar removal, theme/store preserved)
- [ ] `tests/test_redirect_resolution.py` — SHELL-05 8-route ≤1-hop + HX-filter-not-redirected (uses `_route_introspection.iter_effective_routes`)
- [ ] `tests/test_dead_template_guard.py` — orphan-template AST guard via `jinja2.meta.find_referenced_templates` (seed green)
- [ ] Update inline SRI hashes in `base.html` (existing `tests/test_base_html_sri.py` then validates)
- [ ] Shared ASGI-app fixture for the new route tests (reuse existing `app`/`client` fixture in `tests/conftest.py` if present)

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

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < ~5s (quick run)
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
