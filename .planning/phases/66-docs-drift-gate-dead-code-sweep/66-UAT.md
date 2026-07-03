---
status: complete
phase: 66-docs-drift-gate-dead-code-sweep
source: [66-01-SUMMARY.md, 66-02-SUMMARY.md, 66-03-SUMMARY.md]
started: 2026-07-03T00:00:00Z
updated: 2026-07-03T00:00:00Z
---

## Current Test

[testing complete]

## Tests

### 1. Docs-drift gate passes clean AND catches real drift (DOCS-01)
expected: `just docs-drift` passes on the current (clean) repo state; when a passed phase's ROADMAP checkbox is unmarked (real drift), the gate goes RED with a precise message naming the offending phase + requirement(s); returns green once fixed.
result: pass
evidence: Clean run → 5 passed. Injected drift (unmarked Phase 65's `[x]`→`[ ]`) → RED with `AssertionError: VER-01..04 marked Complete but Phase 65 not passed` (D-02 drift class, names phase+requirements). Restored via git → 5 passed again. Driven by orchestrator; tree left clean.

### 2. Dead-template guard entry-literal resolution (CLEAN-02, D-14)
expected: The dead-template guard fails loudly if a router references a `*.html` entry-root literal that has no on-disk template; the existing orphan-template check is untouched.
result: pass
evidence: `uv run pytest tests/shared/core/test_dead_template_guard.py` → 2 passed (`test_no_orphan_templates` + new `test_entry_literals_resolve_to_templates`).

### 3. Docs-drift runs on doc-only PRs (DOCS-01, D-06/D-07)
expected: The `just docs-drift` guard is wired into the always-run Code Quality job with NO `if:` skip gate, so it fires on doc-only PRs without re-enabling the CI-04-skipped heavy jobs.
result: pass
evidence: `.github/workflows/code-quality.yml:54-55` — `- name: 🧭 Docs-drift traceability gate` / `run: just docs-drift`, in the single `runs-on: ubuntu-latest` Code Quality job with no `if:` condition on the step.

### 4. Discreet flag-gated /saq shell link (CLEAN-01)
expected: The Agents page shows a discreet muted footer link to `/saq` when `enable_saq_ui` is true, opening a new tab with `target=_blank rel=noopener`; the link is absent when the flag is false (never a dead 404) and never leaks into the polled `/_table` partial.
result: pass
evidence: Rendered anchor `<a href="/saq" target="_blank" rel="noopener" class="hover:underline">SAQ monitor ↗</a>` inside `<p class="mt-6 text-xs text-gray-400 dark:text-gray-500">` (muted/small/footer = discreet), gated on `{% if enable_saq_ui %}` (agents.html:25-29). All 3 render tests PASSED live against the ephemeral DB: present-when-true, absent-when-false, absent-from-poll-partial.

### 5. Vulture dead-code tooling, dev-only + non-blocking (CLEAN-02)
expected: `vulture>=2.16` is a dev-only dependency; `just vulture` runs against the hand-audited whitelist and exits 0 (clean); the sweep found no vestigial dead code and the DO-NOT-DELETE trio is preserved.
result: pass
evidence: `uv run vulture --version` → 2.16; `vulture>=2.16` at pyproject.toml:228 inside `[dependency-groups]` (dev — NOT runtime `[project].dependencies`); `just vulture` (min-confidence 80 + whitelist + --ignore-decorators) exits 0 with zero candidates; no `src/phaze` deletions this phase.

## Summary

total: 5
passed: 5
issues: 0
pending: 0
skipped: 0
blocked: 0

## Gaps

[none]
