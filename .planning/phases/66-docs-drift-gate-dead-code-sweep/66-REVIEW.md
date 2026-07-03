---
phase: 66-docs-drift-gate-dead-code-sweep
reviewed: 2026-07-03T00:00:00Z
depth: standard
files_reviewed: 9
files_reviewed_list:
  - .github/workflows/code-quality.yml
  - justfile
  - pyproject.toml
  - src/phaze/routers/admin_agents.py
  - src/phaze/templates/admin/agents.html
  - tests/agents/routers/test_admin_agents.py
  - tests/shared/core/test_dead_template_guard.py
  - tests/shared/core/test_requirements_traceability.py
  - vulture_whitelist.py
findings:
  critical: 0
  warning: 3
  info: 4
  total: 7
status: issues_found
---

# Phase 66: Code Review Report

**Reviewed:** 2026-07-03
**Depth:** standard
**Files Reviewed:** 9
**Status:** issues_found

## Summary

Phase 66 adds three cleanup deliverables: a hermetic REQUIREMENTS/ROADMAP docs-drift
traceability guard (DOCS-01), a discreet flag-gated `/saq` footer link on the Agents page
(CLEAN-01), and a non-blocking vulture dead-code sweep with a hand-audited whitelist (CLEAN-02).

The three headline security/correctness concerns the brief called out all check out:

- **Reverse-tabnabbing guard is correct.** `agents.html:27` emits
  `<a href="/saq" target="_blank" rel="noopener">`. `rel="noopener"` is present and severs
  `window.opener`. (`/saq` is same-origin so `noreferrer` is unnecessary; no finding.)
- **Flag-gating is correct and symmetric.** The link is gated on `enable_saq_ui`
  (`admin_agents.py:111` → `agents.html:25`), the SAME setting that gates the `/saq` mount
  (`main.py:160`). Both read the lru-cached `get_settings()`, so they cannot diverge within a
  process — the link can never dangle as a dead 404. Verified by
  `test_saq_link_absent_when_enable_saq_ui_false`. Jinja2Templates uses the default (non-strict)
  `Undefined`, so the `{% if enable_saq_ui %}` block is also safe on the partial/poll path where
  the key is not passed.
- **The docs-drift guard is genuinely hermetic.** Zero `phaze.*` imports; it ran green in 0.13s
  with no Postgres/Redis, confirming it survives the `get_settings` lru_cache / `saq_jobs` stub
  cross-test poison and will pass in the `code-quality` job that provisions no services.
- **No injection surface in the CI/justfile wiring.** The new `docs-drift`/`vulture` recipes and
  the `code-quality.yml` step invoke static commands with no interpolated untrusted input.

I ran the new guards on the real repo: `test_requirements_traceability.py` (6),
`test_dead_template_guard.py` (2 incl. the new entry-literal check), and `just vulture` all pass
clean. The `test_admin_agents.py` failures observed locally are infrastructure-only (the shared
`session` fixture cannot reach a test Postgres in this sandbox) — not code defects.

No BLOCKERs were provable. The findings below are robustness gaps in the drift guard (which
weaken the very drift classes it advertises) and maintainability concerns.

## Warnings

### WR-01: Traceability parser is whole-file, not section-scoped — a stray req-id row silently overwrites the real mapping

**File:** `tests/shared/core/test_requirements_traceability.py:86-97` (`_parse_traceability`), `:81-83` (`_parse_requirement_checkboxes`)
**Issue:** Both parsers scan the entire `REQUIREMENTS.md` text, not just the `## Traceability`
section. `_parse_traceability` builds a `dict` keyed by `req_id`, so if a `req-id`-shaped row
(`| CI-01 | ... | ... |`) appears in any *other* markdown table in the file, it silently
overwrites the authoritative Traceability row (last-match wins). The same last-wins hazard
applies to a duplicate `- [x] **REQ-ID**` checkbox line. Today the file has exactly one such
table so the guard is correct, but the guard's whole purpose is to be the trustworthy source of
truth for drift — a parser that can be quietly desynced by an unrelated table edit undermines
that guarantee without any test catching it.
**Fix:** Scope both parsers to the `## Traceability` section (and the requirements-list section
for checkboxes). E.g. slice the text at the `## Traceability` heading before applying
`_TABLE_ROW`, and assert no `req_id` is seen twice:
```python
def _traceability_section(text: str) -> str:
    m = re.search(r"^## Traceability\b.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""
# ...and in _parse_traceability, raise/append an offender if `rid in out` (duplicate row).
```

### WR-02: A requirement marked `[x]` but absent from the Traceability table escapes all three drift checks

**File:** `tests/shared/core/test_requirements_traceability.py:153-168` (`_marked_requirement_offenders`)
**Issue:** `_marked_requirement_offenders` iterates `table.items()` only. A requirement whose
checkbox is `[x]` (or that reads Complete somewhere) but which is **missing from the Traceability
table** is never in `table`, so it is never evaluated — a false negative for exactly the drift
class the gate advertises ("a requirement marked without a passed phase"). A maintainer who ticks
a requirement checkbox but forgets to add/keep its Traceability row gets a green gate.
`_passed_phase_completeness_offenders` and `_checkbox_table_offenders` share the same
table-driven blind spot in the opposite direction (checkbox-only requirements are invisible).
**Fix:** Iterate the union of `checkboxes` and `table` keys and flag any `req_id` marked in one
encoding but absent from the other:
```python
for rid in set(checkboxes) | set(table):
    if rid not in table and checkboxes.get(rid):
        offenders.append(f"{rid} checkbox [x] but has no Traceability row")
```

### WR-03: `_HTML_LITERAL` captures any quoted `*.html` string in a router — a non-template href/redirect literal will spuriously fail CI

**File:** `tests/shared/core/test_dead_template_guard.py:50` (`_HTML_LITERAL`), `:98-115` (`test_entry_literals_resolve_to_templates`)
**Issue:** The new D-14 check requires every captured `.html` literal to resolve to an on-disk
template. But `_HTML_LITERAL` matches *any* quoted `"...html"` in router source — including a
future `RedirectResponse("/foo.html")`, an `href="/x.html"` literal, or a docstring example.
Such a literal would be added to the entry set and then fail `test_entry_literals_resolve_to_templates`
even though it is legitimately not a template, breaking the always-run `code-quality` gate for an
unrelated router change. The docstring documents a `_NON_TEMPLATE_HTML` escape hatch but it does
not yet exist, so the first offender is a red CI with no seam ready.
**Fix:** Either restrict capture to render-call contexts (literals passed to
`TemplateResponse`/`_render_partial`/`name=`), or pre-create the documented
`_NON_TEMPLATE_HTML: frozenset[str] = frozenset()` and subtract it in
`test_entry_literals_resolve_to_templates` so the seam is in place before it is needed.

## Info

### IN-01: vulture whitelist redundantly lists ~90 `@router`-decorated handlers already covered by `--ignore-decorators`, weakening the sweep for those symbols

**File:** `vulture_whitelist.py:80-188` (and the `justfile:108` recipe `--ignore-decorators "@router.*,..."`)
**Issue:** The recipe already ignores `@router.*`/`@app.*`/validator/fixture decorators, yet the
whitelist re-lists dozens of route handlers (e.g. `table_partial`, `dashboard`, every
`trigger_*` handler). The header calls this deliberate "self-sufficiency," but the side effect is
that if one of these handlers ever *loses* its decorator and becomes genuinely dead, the explicit
whitelist entry keeps vulture silent — the tool's value for those symbols is suppressed twice.
Since `just vulture` is non-blocking this is low-impact, but the belt-and-suspenders duplication
raises maintenance cost and dilutes the "hand-audited" signal.
**Fix:** Prefer one mechanism. Drop the decorator-covered handler entries and rely on
`--ignore-decorators`, keeping the whitelist for the genuine dynamic/framework false-positives
(ORM columns, transient `_status`/`_agent_name` attrs, string-annotation imports) that
`--ignore-decorators` cannot reach.

### IN-02: Inconsistent file-read encoding between the two structural guards

**File:** `tests/shared/core/test_requirements_traceability.py:62-63` vs `tests/shared/core/test_dead_template_guard.py:68-69,74`
**Issue:** The traceability guard reads with explicit `read_text(encoding="utf-8")`; the
dead-template guard reads router/template sources with bare `read_text()` (platform-default
encoding). Non-ASCII content in a template (already present elsewhere in the app, e.g. the
`↗` glyph in `agents.html`) would decode differently on a non-UTF-8 CI locale.
**Fix:** Use `read_text(encoding="utf-8")` in `_entry_templates` and `_referenced_from` for
parity and locale-independence.

### IN-03: `table_partial` context omits `enable_saq_ui` while `page` includes it — latent asymmetry

**File:** `src/phaze/routers/admin_agents.py:116-140`
**Issue:** `page()` passes `enable_saq_ui` into the context (used by the shell) but
`table_partial()` does not. Correct today because `agents_table.html` does not reference the flag,
but the asymmetry is a trap: if the `/saq` link (or any flag-gated element) is ever moved into the
polled partial, it would silently render as falsy on every poll.
**Fix:** No change required now; consider a shared context-builder helper so both handlers pass an
identical base context, documenting that the link deliberately lives only in the shell.

### IN-04: `_parse_roadmap_phases` last-wins dict is ambiguous if a phase number appears both active and inside an archived `<details>` block

**File:** `tests/shared/core/test_requirements_traceability.py:100-107`
**Issue:** ROADMAP.md keeps archived phases inside `<details>` blocks that parse as plain
markdown. `_parse_roadmap_phases` builds `{phase: is_checked}` across the whole file, so if a
phase number were ever duplicated (active line + archived line), the later occurrence wins
non-deterministically w.r.t. document order. Active REQUIREMENTS only reference 63-66 today, so
this is currently harmless.
**Fix:** Scope roadmap phase parsing to the active (non-`<details>`) portion, or flag duplicate
phase numbers as an offender to fail loudly rather than silently pick one.

---

_Reviewed: 2026-07-03T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
