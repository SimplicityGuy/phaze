# Phase 62: Polish & cutover - Context

**Gathered:** 2026-07-01
**Updated:** 2026-07-01 (post-research reconciliation — see 62-RESEARCH.md)
**Status:** Ready for planning

<domain>
## Phase Boundary

The **final v7.0 phase** — dependency-strict last in the 57→62 chain, and CUT-02 is
necessarily the last work item within it (removal only after every surface supersedes).
Presentation-only: **no backend behavior change** (REQUIREMENTS.md line 82 still holds).
Delivers CUT-01..04:

1. **CUT-01 — Baseline accessibility to WCAG 2.1 AA.** Complete/audit the a11y baseline
   Phase 57 seeded (skip link `shell.html:153`, `aria-current` rail sync + focus-to-heading
   `shell.html:205-268`, focus-visible rings on rail/header). Add the ARIA the new surfaces
   still lack — the ⌘K palette as combobox/listbox, the record slide-in as `role="dialog"
   aria-modal`, DAG rail ARIA at parity — and prove it with pytest structural guards.
2. **CUT-02 — Full dead-code cutover.** Legacy routes ALREADY redirect into the shell (Phase 57
   SHELL-05), so this is pure **deletion** of the unreachable wrappers + their dead render tails +
   the now-dead legacy HX filter branches (D-03b) + the orphaned partial cascade. Drain the
   dead-template guard `_ALLOWLIST` to empty; guard stays green. "No orphaned dead code."
3. **CUT-03 — Docs/README refresh** for the new DAG-centric IA (README + architecture.md +
   project-structure.md; no screenshots).
4. **CUT-04 — Narrow-width rail collapse** to an icon-only strip via a CSS breakpoint.

**Carrying forward (locked — do not re-litigate):**
- The a11y **baseline already exists** (Phase 57): skip link, `aria-current`, focus-to-heading,
  focus rings, `nav`/`aside` landmarks in `rail.html`. CUT-01 completes + audits it, it does not
  start from zero.
- The **empty 350px right `<aside>`** (`shell.html:169`, `aria-label="Detail pane"`) is dead
  since Phase 61's slide-in superseded the preview tier — remove it here (deferred from Phase 61).
- The **dead-template AST guard** (`tests/test_dead_template_guard.py`) is the objective arbiter
  for CUT-02. Its `_ALLOWLIST` currently holds 7 entries queued explicitly for this phase; CUT-02
  drains it to empty. Do NOT relax the closure logic to force green.
- Stack unchanged: FastAPI + Jinja2 + HTMX + Tailwind + Alpine, server-rendered, no SPA build,
  no new runtime dep. C3 aesthetic + all Phase 57 shell contracts (`#stage-workspace` swap,
  fragment-only responses, single `/pipeline/stats` poll + `oob_counts` gate, `$store.pipeline`
  consumed-not-redefined, `/s/<stage>` scheme, theme/brand).

**Explicitly NOT this phase:** any new capability, backend/routing/logic change, touch-input /
tablet support (SHELL-06, deferred), per-stage configurable confidence thresholds (REVIEW-06,
deferred), full first-class light theme (RECORD-05, deferred), a phone UI (never).
</domain>

<decisions>
## Implementation Decisions

### Accessibility (CUT-01)
- **D-01: Target WCAG 2.1 AA; enforce with pytest structural guards — no new runtime dep, no browser audit.** Prove a11y with rendered-HTML assertion tests in the repo's established guard-test style (cf. `test_dead_template_guard.py`, `test_base_html_sri.py`, the `x-data` quote guard). Assertions cover, at minimum: skip link present and first focusable; DAG rail exposes proper landmarks + `aria-current`; the ⌘K palette carries combobox/listbox semantics (`role="combobox"`/`listbox"`/`option"`, `aria-activedescendant`, `aria-expanded`); the record slide-in is `role="dialog" aria-modal="true"` with an accessible name and the `x-trap` focus-trap; interactive controls have visible focus states. **Rejected:** axe-core/pa11y browser audit (adds a Node/browser CI dependency + flake surface, disproportionate for a single-user tool); manual-only (nothing prevents regressions).
- **D-01a (post-research): CUT-01 is audit-and-close-gaps, NOT an ARIA rebuild.** Research (62-RESEARCH.md) confirmed Phase 61 already built the hard ARIA — the ⌘K palette already has `role=combobox`/`listbox`/`option` + `aria-activedescendant`/`aria-expanded`, and the record slide-in already has `role=dialog aria-modal` + `x-trap`. The one confirmed real gap is that the **⌘K combobox input lacks an accessible name → add `aria-label`**. The rest of CUT-01 is verifying the existing baseline (skip link, rail landmarks/`aria-current`, visible focus) and locking it with the pytest guards. Do NOT rework rail keyboard-nav / add roving-tabindex / broaden ARIA to new surfaces — that exceeds CUT-01 (rejected as scope creep).
- **D-02: "Parity with or better than today" is the floor, WCAG 2.1 AA is the ceiling target.** Where a specific AA success criterion is impractical for a server-rendered admin tool, the planner may note it — but the four named surfaces (rail keyboard nav, ⌘K, focus states, DAG ARIA + skip link) are non-negotiable per CUT-01.

### Dead-code cutover (CUT-02)
- **D-03 (REVISED post-research): CUT-02 is pure dead-code DELETION, not routing conversion.** Research (62-RESEARCH.md) overturned the discuss-time assumption: every legacy top-level non-HX GET **already 302-redirects into the shell** (Phase 57 SHELL-05 — `proposals.py:130`, `tracklists.py:89`, `tags.py:167`, `cue.py:186`, `duplicates.py:89`, `preview.py:45`, `pipeline.py:598`, `search.py:39`). The `return TemplateResponse(...list.html)` tail in each handler is **unreachable dead code**. So CUT-02 = delete the dead wrappers + their unreachable render tails + orphaned partials + drain the guard `_ALLOWLIST` to empty. No redirect needs to be *added* — they already exist. The `/search` handler is the confirmed reference shape (non-HX → redirect, HX branch retained).
- **D-03a: Exact deletion worklist (research-verified, simulated GREEN against the real guard).** Delete these **8 wrapper templates** + their unreachable router `TemplateResponse` tails: `proposals/list.html`, `tags/list.html`, `duplicates/list.html`, `cue/list.html`, `tracklists/list.html`, `search/page.html`, `preview/tree.html`, `pipeline/dashboard.html`. Plus the **6-partial orphan cascade** they alone referenced: `_partials/cross_fs_fingerprint_notice.html`, `pipeline/partials/dag_canvas.html`, `preview/partials/tree_node.html`, `tags/partials/pagination.html`, `tracklists/partials/filter_tabs.html`, `tracklists/partials/stats_header.html`. Then drain `_ALLOWLIST` (its current 7 entries: `search/page.html` + 5 `search/partials/*` + `tracklists/partials/toast.html`) to empty. Research simulated the jinja2 closure over the post-deletion tree → **0 orphans**. **KEEP all other `partials/`** — they are the shell's live fragments.
- **D-03b (this-update decision): ALSO strip the now-dead legacy HX filter/pagination branches** in the legacy routers, not just the non-HX render tails. This is the more aggressive cut (cleaner router source) and it **changes which partials fall out of reach** — so the planner MUST re-run the dead-template guard's reach simulation after removing the HX branches to recompute the exact orphaned-partial set (it may extend beyond the 6-partial cascade in D-03a, which was computed for the minimal cut). Definition of done is unchanged: `_ALLOWLIST` empty + `test_no_orphan_templates` green + closure logic untouched. **Rejected:** minimal cut that leaves the legacy HX branches as thin dead paths.
- **D-04: `/audit/` and `/admin/agents` are KEPT, not superseded.** The rail links to both as real pages (`rail.html:147-150` — below-the-line plain links, NOT `/s/` stages), and the Agents page is Phase 61's RECORD-03 deliverable. `execution/audit_log.html` and the admin/agents templates stay. Everything the audit view and Agents page transitively reference stays reachable — the purge targets only the tab-era page wrappers the shell workspaces replaced.
- **D-04a (this-update decision): `base.html` is KEPT but its legacy tab-bar nav block is removed — strip to logo + theme toggle only.** The KEPT audit/agents pages `{% extends base.html %}`, so `base.html` survives; but its legacy top tab-bar nav block (`base.html:161-251`, research-located) is dead (SHELL-03 removed the tab bar from the shell). Remove that nav block and keep **only the wave-logo home link + the theme toggle** in the header, so the standalone KEPT pages can navigate back to the shell and are not dead-ends. **Rejected:** adding a separate "back to app" affordance (the logo home link already serves that; no extra control needed).
- **D-05 (REVISED post-research): Supersession mapping confirmed; verify-then-delete, but deletion is already safe.** Because the redirects already exist (D-03), deleting a wrapper cannot break a bookmark — the route keeps 302-ing into the shell. Verification is now a confirmation step, not a blocker: research verified the mapping `/proposals`→propose, `/tracklists`→tracklist, `/tags`→tagwrite, `/cue`→cue, `/duplicates`→dedupe, `/preview`→move, `/pipeline`→analyze(default), `/search`→⌘K palette. If a shell workspace is found missing a feature the legacy page had, surface it as a supersession gap — do NOT silently drop the capability, and do NOT add new capability to close it (that would be scope creep for a later phase).

### Docs & README (CUT-03)
- **D-06: Refresh README + `docs/architecture.md` + `docs/project-structure.md`; no screenshots.** Rewrite the UI/IA-describing sections to reflect the DAG-centric shell (three-column layout, rail-as-nav, `/s/<stage>` stages, ⌘K palette, header status strip, per-file record slide-in, Agents page). Skip screenshots/GIFs (they rot fast for a single-user tool). **Rejected:** README-only (leaves `docs/` stale); full sweep incl. `quick-start.md` + screenshots (disproportionate maintenance). If `quick-start.md` contains now-wrong UI navigation steps, correct those inline — but a full walkthrough rewrite is out of scope.

### Narrow-width rail (CUT-04)
- **D-07: Auto-collapse the 280px rail to an icon-only strip via a CSS breakpoint — pure CSS, no persistence, no JS toggle.** Below ~`lg` (~1024px) the rail (`rail.html:25`, `w-[280px]`) collapses to an icon strip; labels/counts hide, icons + `aria-label`/tooltip remain. Matches CUT-04's literal "rail collapses to icons" for the single-user desktop tool. The record slide-in and ⌘K palette already render as overlays, so they are unaffected — verify they remain usable at the narrow width. **Rejected:** manual toggle + persisted state (needs JS + localStorage, more than CUT-04 asks); off-canvas hamburger drawer (mobile-nav pattern, heavier, wrong for a desktop-primary tool).
- **D-08: Add real per-stage icons (inline SVG, no new dep).** The rail today uses colored status dots + text labels, NOT icons — an icon-only collapse REQUIRES adding a per-stage icon set. Use **inline SVG** (consistent with the existing inline wave-logo and the no-build/no-new-dep ethos), one glyph per rail node, exposed with `aria-hidden` + the node's existing accessible label so the collapsed rail stays screen-reader-navigable. No icon font, no icon library dependency.

### Claude's Discretion
- Exact CSS breakpoint value and the collapsed-rail width (D-07).
- The specific inline-SVG glyph chosen per stage (D-08) — match the prototype/design language where one exists.
- Precise redirect status codes + whether legacy HX branches are also removed or left as thin redirects (D-03) — planner picks the cleanest cut that keeps the guard green and bookmarks working (SHELL-05).
- Which exact pytest assertions/roles constitute the CUT-01 guard set (D-01), so long as the four named surfaces are covered and WCAG 2.1 AA is the target.
- Whether `docs/quick-start.md` needs inline nav corrections (D-06).
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### This phase's contracts
- `.planning/phases/62-polish-cutover/62-RESEARCH.md` — **READ FIRST.** Verified CUT-02 deletion worklist (8 wrappers + 6-partial cascade, simulated green against the real guard), the file:line redirect inventory proving routes already 302 (overturns the discuss-time conversion assumption), the CUT-01 audit map (only gap = ⌘K combobox `aria-label`), and the CUT-04 `max-lg:` / self-hosted-Tailwind build findings.
- `.planning/phases/62-polish-cutover/62-VALIDATION.md` — Nyquist sampling contract + per-requirement automated-verification map (which guard test proves each CUT-NN).
- `.planning/ROADMAP.md` § "Phase 62: Polish & cutover" (line 48-49) — Goal + 4 Success Criteria (keyboard rail + palette with visible focus and skip link; dead-template guard green; docs/README describe new IA; rail collapses to icons at narrow widths). **Notes block** lists the exact wrapper-deletion set incl. `pipeline/dashboard.html` + the `base.html` nav block, and "keep all partials/".
- `.planning/REQUIREMENTS.md` § "Polish & cutover (CUT)" — CUT-01..04 (lines 67-70; mapping table 118-121). Line 82 = the milestone "logic unchanged" rule (still binding — this phase is presentation-only). Also note deferred SHELL-06 (touch/tablet), RECORD-05 (full light theme) — NOT this phase.

### Design & IA (authoritative, inherited from v7.0)
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` — the IA the docs (CUT-03) must describe; the shell/rail/⌘K/record structure the a11y (CUT-01) and narrow-width (CUT-04) work hardens.
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` — canonical visual reference; source for the rail node order and any per-stage iconography (D-08).

### The objective CUT-02 arbiter + a11y baseline
- `tests/test_dead_template_guard.py` — the dead-template AST guard; its `_ALLOWLIST` (7 entries: `search/page.html` + 5 `search/partials/*`, `tracklists/partials/toast.html`) is the explicit CUT-02 removal worklist. Drain to empty; keep green without relaxing the closure.
- `tests/test_base_html_sri.py` — the guard-test style CUT-01's pytest a11y assertions should mirror.

### Cutover surfaces (verified at discuss time)
- `src/phaze/templates/shell/shell.html` — `:153` skip link (a11y baseline), `:169` the dead empty `<aside>` to remove, `:205-268` history-restore + focus-to-heading + `aria-current` sync (extend for CUT-01), the single `/pipeline/stats` poll.
- `src/phaze/templates/shell/partials/rail.html` — the DAG rail (`:25` `w-[280px]`, `nav`/`aside` landmarks, `aria-current`, focus-visible rings, colored dots + labels). Target of CUT-04 (D-07 collapse + D-08 icons) and CUT-01 (DAG ARIA parity). `:147-150` audit/agents plain links (KEPT — D-04).
- `src/phaze/templates/shell/partials/cmdk_modal.html` — the ⌘K palette; add combobox/listbox ARIA (D-01).
- `src/phaze/templates/shell/partials/record_host.html` + `src/phaze/templates/record/record_body.html` — the record slide-in; add `role="dialog" aria-modal` + accessible name (D-01).
- **Legacy full-page routes to supersede+delete (D-03/D-05):** `src/phaze/routers/proposals.py:167` (`proposals/list.html`), `tracklists.py:160` (`tracklists/list.html`), `tags.py:226` (`tags/list.html`), `cue.py:236` (`cue/list.html`), `duplicates.py:113` (`duplicates/list.html`), `preview.py:57` (`preview/tree.html`), and their now-orphaned partials. `src/phaze/routers/search.py` — the reference redirect pattern (non-HX GET → shell redirect, HX branch retained).
- **KEEP (D-04):** `src/phaze/routers/execution.py:375` (`execution/audit_log.html` — `/audit/` is a rail link), `src/phaze/routers/admin_agents.py` + `admin/*` templates (Agents page, RECORD-03).

### Docs to update (CUT-03)
- `README.md`, `docs/architecture.md`, `docs/project-structure.md` — refresh IA/UI sections for the DAG-centric shell.

### Predecessor context (do not re-litigate)
- `.planning/phases/61-full-record-k-agents/61-CONTEXT.md` — the slide-in/⌘K/Agents/empty-state this phase hardens; its `<deferred>` explicitly hands a11y depth, `<aside>` removal, and narrow-width to Phase 62.
- `.planning/phases/57-shell-dag-rail/57-CONTEXT.md` + `57-UI-SPEC.md` — the shell contracts + the seeded a11y baseline and dead-template guard CUT-01/CUT-02 build on.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **a11y baseline (Phase 57)**: skip link, `aria-current` rail sync, focus-to-heading on swap/restore, focus-visible rings, `nav`/`aside` landmarks — CUT-01 completes rather than creates.
- **Guard-test pattern**: `test_dead_template_guard.py` / `test_base_html_sri.py` / the `x-data` quote guard establish the "rendered-HTML pytest assertion" style CUT-01's a11y guards adopt.
- **`/search` redirect pattern** (`search.py`): non-HX GET → `RedirectResponse` into the shell, HX branch retained — the exact template CUT-02 applies to the other 6 legacy routes.
- **Dead-template `_ALLOWLIST`**: the pre-computed 7-entry CUT-02 removal worklist.

### Established Patterns
- **Fragment-only responses + single `/pipeline/stats` poll + `oob_counts` gate** (Phase 57): unchanged; the narrow-width collapse and a11y work must not add a loop or break the swap contract.
- **Inline SVG, no icon dependency** (existing wave logo): the model for D-08's per-stage rail icons.
- **Server-rendered, no build step**: CUT-04 is a Tailwind CSS-breakpoint concern, not a JS one (D-07).

### Integration Points
- CUT-01: `shell.html` (skip link/focus handlers), `rail.html` (DAG ARIA), `cmdk_modal.html` (combobox/listbox), `record_host.html`/`record_body.html` (dialog) + new `tests/test_a11y_*.py`.
- CUT-02: the 6 legacy routers (redirect conversion), the deleted page/partial templates, `_ALLOWLIST` emptied, guard green.
- CUT-03: `README.md`, `docs/architecture.md`, `docs/project-structure.md`.
- CUT-04: `rail.html` (breakpoint classes + inline-SVG icons); verify `shell.html` slide-in/palette overlays at the narrow width.
</code_context>

<specifics>
## Specific Ideas

- **The dead-template guard is the definition of done for CUT-02** — allowlist empty + green, closure logic untouched. Not a subjective "looks clean."
- **a11y is parity-or-better AND WCAG 2.1 AA-targeted**, proven by pytest guards in the repo's own style — no browser-audit dependency.
- **The rail must stay screen-reader-navigable when collapsed to icons** (D-08 `aria-label`s) — an icon-only strip must not become a label-less nav.
- **Verify supersession before deleting** (D-05): the cutover must not silently drop a capability the shell workspace hasn't yet absorbed.
- **Conservative additive posture holds to the end**: presentation-only, no new runtime dep, no backend/logic change — v7.0 ships without touching analysis/identify/proposal/execution behavior.
</specifics>

<deferred>
## Deferred Ideas

- **Touch-input / tablet support** for the three-column shell (SHELL-06) — deferred to v7.x; CUT-04 does desktop narrow-width only, no phone UI ever.
- **Full first-class C3 light theme** (RECORD-05) — dark stays primary for v7.0.
- **Per-stage configurable confidence thresholds + override UI** (REVIEW-06) — v7.0 shipped a fixed threshold.
- **axe-core/pa11y browser a11y audit in CI** — rejected for CUT-01 (dependency/flake cost); could return if the pytest structural guards prove insufficient.
- **Screenshots/GIFs of the new shell in docs** — rejected for CUT-03 (maintenance rot); revisit if onboarding needs grow.
- **Manual rail-collapse toggle with persisted state** — rejected for CUT-04 (auto CSS breakpoint chosen); a future enhancement if the operator wants manual control.

None arose as scope creep — these are conscious later-phase boundaries or rejected alternatives.
</deferred>

---

*Phase: 62-polish-cutover*
*Context gathered: 2026-07-01*
