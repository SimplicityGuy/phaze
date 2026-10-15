# Phaze UI Design Reference

**Status:** Compatibility contract for the shipped UI templates as of 2026-10-12
**Scope:** Visual identity and interaction language only; this is not a redesign specification
**Fixture:** [Privacy-safe reference states](ui-reference-fixtures.html)

This reference records the visual and interaction rules already expressed by the production templates. It is a baseline for evaluating shared-component changes: preserve the identity-bearing characteristics, while allowing components to improve hierarchy, accessibility, and responsive behavior. Where production is inconsistent, this document reports the inconsistency rather than selecting a new style.

## Source Of Truth

The served application remains authoritative. This contract was derived from:

- `assets/src/app.css`: palette overrides, dark surfaces, font families, HTMX indicator transition, and Alpine cloak.
- `src/phaze/templates/shell/shell.html`: persistent shell, auto/dark/light behavior, independent scrolling, focus handoff, and theme transition.
- `src/phaze/templates/shell/partials/header.html`: wordmark, circular wave mark, command affordance, the header health pill with its non-modal attention tray, Config link, and theme control.
- `src/phaze/templates/shell/partials/rail.html`: DAG navigation, the expanded rail at `lg`+ and the off-canvas drawer below it, outline icons, active state, review-queue counts, and amber review grouping. There is no icon-only navigation state at any width — see `docs/design/0009-responsive-accessibility-baseline.md`, which retired the 64px icon strip this file previously described.
- `src/phaze/templates/shell/partials/attention_items.html` and `action_feedback.html`: the attention tray body, the persistent action-failure panel, and the change-aware `#incident-announcement` live region logic.
- `src/phaze/templates/shell/partials/record_dock.html` and `record_host.html`: the docked non-modal file inspector at `xl`+ and the modal record host everywhere else.
- `src/phaze/templates/shell/partials/_summary_flow_board.html` and `summary_overview.html`: the Summary flow board and the stage cards beneath it.
- `src/phaze/templates/ui/primitives.html`: the shared page-header, metric-strip, status-badge, alert, filter-toolbar, table, empty/loading/error, confirmation, and detail-drawer primitives every workspace composes from.
- `src/phaze/templates/pipeline/partials/_workspace_scaffold.html`: stage heading, action placement, and content spacing.
- `src/phaze/templates/pipeline/partials/_diff_row.html`: review diff language and compact controls.
- `src/phaze/templates/pipeline/partials/files_table_view.html`, `_status_filter_bar.html`, `_stage_pill.html`, and `_stage_matrix.html`: the Files workbench — glyph stage cells, the single legend, status filter chips, the row-density control, and semantic stage states.
- `src/phaze/templates/pipeline/partials/_changes_list.html` and `_diff_row.html`: Changes Review triage keys, per-row Undo, and the bulk-approval Undo.
- `src/phaze/templates/pipeline/partials/empty_state.html`, `inadmissible_card.html`, and `analysis_failed_card.html`: empty, warning, stalled, and terminal-error treatments.
- `src/phaze/templates/ui/set_marks.html`, `record/partials/_harmonic_wheel.html`, and `proposals/partials/analysis_timeline.html`: the shared set glyph, harmonic journey, and projected analysis timeline.
- `src/phaze/services/set_glyph_colors.py` and `analysis_timeline.py`: the shared Camelot/energy/mood color vocabulary behind those templates.
- `src/phaze/static/favicon-32.svg`: compact favicon form of the brand mark.

The older prototypes under `docs/superpowers/specs/2026-06-28-ui-redesign-assets/` explain the chosen direction, but production templates take precedence when prototype and shipped behavior differ.

## Compatibility Boundary

### Identity-bearing constraints

These characteristics make the interface recognizably Phaze and must remain stable across component work:

1. **Jura plus Inter.** Jura identifies the product and pipeline structure; Inter carries readable operational content. Replacing either family, or using Jura for long-form body copy, changes the identity.
2. **Tracked technical headings.** Product, stage, group, table, and compact action labels use uppercase Jura with generous tracking. The result should remain technical and instrument-like, not editorial or decorative.
3. **Wave/cycle brand marks.** Preserve the blue circular-wave header mark, the waveform empty-state mark, the compact favicon, and the tracked `PHAZE` wordmark as one related family. Do not substitute a generic music-note logo.
4. **Dark technical character.** Dark mode is the primary visual character: near-black canvas, slightly lifted cool panels, quiet cool-gray borders, dense information, and restrained depth. It must not become a generic flat black dashboard or a high-gloss neon interface.
5. **Cyan-blue accent family.** The project overrides Tailwind blue with a contrast-adjusted cyan-blue scale (`#036b85` at 500/600 and `#1abbdb` at 400). It identifies the brand, navigation/current state, links, primary actions, progress, and keyboard focus.
6. **Amber review and operator-attention family.** Amber separates human review/apply work and recoverable attention states from normal pipeline flow. It is not the generic color for every highlighted control.
7. **Persistent console shell.** The compact top header and left pipeline/DAG rail remain the stable frame around a swappable stage workspace. The flow stays visible while work changes.
8. **Thin outline icon language.** Navigation and utility icons are simple inline SVG outlines, normally `24 x 24`, `currentColor`, round caps/joins, and about `1.5` stroke width. Icons support labels rather than replace meaning.
9. **Compact operational density.** Controls and rows are intentionally concise, with strong alignment, tabular/monospace data where useful, and progressive disclosure for detail. Spacious marketing-page rhythm is outside the product character.
10. **Visible blue keyboard focus.** Interactive elements expose an approximately 2 px cyan-blue ring. Focus is an interaction state, not optional decoration.
11. **State is never color alone.** Statuses combine color with a word and, where compact, a glyph or shape. For example, `✓ done`, `● in flight`, `✕ failed`, `⊘ skipped`, `∅ not applicable`, and `— not started` remain distinguishable without hue.
12. **Restrained motion.** Motion communicates state changes: 200 ms color/opacity transitions, loading pulse, the lane-detail pane transition, and HTMX activity. It is functional, short, and never ambient spectacle.
13. **Set marks share one semantic scale.** Camelot position determines hue and projected energy determines lightness through `services/set_glyph_colors.py`; glyphs, legends, and the harmonic wheel do not invent local formulas. Mood river bands, their legend, and track mood dots use the one palette in `services/analysis_timeline.py`.

### Components allowed to evolve

The following are implementations, not brand locks. They may change when a change improves hierarchy, accessibility, maintainability, or responsive behavior without violating the constraints above:

- Exact grid columns, card grouping, breakpoints, and whether dense content becomes a drawer, overlay, stack, or horizontal scroller.
- Exact padding, gap, radius, line-height, and control height within the established compact density.
- Specific gray steps and border opacity when contrast testing requires adjustment.
- Heading size and wrapping behavior where translated, narrow, or zoomed layouts need more room.
- Icon paths and icon library, provided the result stays consistent, outline-based, and meaningfully labeled.
- Table column order, resizing mechanics, sticky regions, pagination, and responsive table alternatives.
- Status component markup and hue values when semantics, text/glyph redundancy, and light/dark contrast are preserved.
- Focus-ring offset, thickness, and color adjustment where needed to remain visible against adjacent surfaces.
- Motion duration and suppression, especially to honor `prefers-reduced-motion` or avoid distracting repeated animation.
- Component consolidation. Duplicate styles may become shared primitives without forcing all contexts into one hierarchy.

These permissions are not approval to restyle the application. A component change should be explainable as a hierarchy, accessibility, responsive, or consistency improvement, not as a new aesthetic direction.

## Visual Language

### Typography

| Role | Current treatment | Usage rule |
| --- | --- | --- |
| Product wordmark | Jura 500, uppercase, about 14 px, `0.25em` tracking | `PHAZE` beside the header mark; may hide at narrow widths. |
| Workspace heading | Jura 500, uppercase, about 18 px, `0.15em` tracking | One focusable `h1` per swapped workspace. |
| Rail/group label | Jura, uppercase, 10-11 px, `0.2-0.25em` tracking | Pipeline sections and review/apply grouping. |
| Table heading | Jura, uppercase, about 11 px, `0.2em` tracking | Compact scan labels, not body prose. |
| Compact action | Jura 500, uppercase, 11-12 px, wider tracking | Short verbs such as `RUN ANALYSIS`, `APPROVE`, and `PAUSE`. |
| Body/UI copy | Inter 400/600, usually 12-14 px | Descriptions, labels, alerts, counts, and help text. |
| Paths/keys/counts | Monospace, usually 10-12 px, tabular where relevant | Paths, shortcuts, machine-like values, and stable-width counters. |

Jura is structural, not a blanket display font. Inter remains the default `body` family. Long paths and identifiers use monospace and truncate or break only where context demands it.

### Brand marks

Three shipped expressions belong to the same identity:

- The header mark is a thin cyan circle crossed by a curved phase/wave line and center point.
- The empty-state mark is a horizontal cyan waveform.
- The favicon is a heavier circular-wave construction on `phaze-bg` for legibility at small sizes.

The geometry currently differs between placements. Preserve the recognizable cycle/wave idea and cyan treatment; do not silently redraw all variants as part of component cleanup. Consolidation requires an explicit brand decision and small-size testing.

### Color semantics

#### Foundation tokens

| Semantic role | Light expression | Dark expression | Current source |
| --- | --- | --- | --- |
| Canvas | white | `phaze-bg` `#0a0c12` | Page and workspace background. |
| Chrome/panel | gray 50 or white | `phaze-panel` `#10141c` | Header, rail, cards, inputs. |
| Border | gray 200/300 | `phaze-border` `#232832` | Quiet one-pixel separation. |
| Primary text | gray 900 | gray 100/200 | Titles and values. |
| Secondary text | gray 500/600 | gray 400/500 | Captions and metadata. |
| Accent/focus | custom blue 500/600 `#036b85` | custom blue 400-600 (`#1abbdb` to `#036b85`) | Brand, current location, links, primary actions, progress, focus. |

The custom `blue` scale is visually cyan. Documentation and code may call it blue because that is the Tailwind token name; design discussion should say **cyan-blue** when the distinction matters.

#### Semantic families

| Family | Meaning in production | Do not use for |
| --- | --- | --- |
| Cyan-blue | Brand, current navigation, links, primary actions, active/in-flight work, focus | Generic decoration or every selected datum. |
| Amber | Review/apply gate, quota waiting, paused/stalled/recoverable attention, warning | Terminal failure, ordinary navigation, or success. |
| Emerald/green | Agent online, successful completion, approved/applied/proposed-after value | Work merely queued or awaiting review. |
| Red/rose | Terminal failure/destructive error; rose specifically marks the removed/before side of a diff | Recoverable quota waits or neutral skips. |
| Violet | Force-skipped state, explicitly distinct from genuine completion | Ordinary done state. |
| Gray | Idle, unavailable, never seen, not started, supporting text | A state that requires urgent operator action. |

Amber has two related but distinct jobs: it identifies the human review domain and signals operator attention. Context, wording, and `role="alert"` distinguish them. Healthy Kueue quota waiting is amber operational status; Inadmissible configuration is an amber alert; terminal analysis failure is red.

### Surfaces, borders, and depth

- The dark canvas is `#0a0c12`; persistent chrome and lifted controls use `#10141c`.
- One-pixel `#232832` borders create most grouping. Light mode uses gray 200/300 equivalents.
- Cards generally use `rounded-lg` or `rounded-xl`; overlays use `rounded-2xl` and a stronger shadow.
- Dark surfaces avoid routine shadows. Toasts replace a dark shadow with a border/ring; overlays retain shadow where depth is interaction-critical.
- Active pipeline-stage navigation is a translucent cyan-blue fill plus an inset 3 px cyan-blue rail, not a large filled button. Utility-pane nodes use the shell's cyan active treatment too; active navigation must remain distinguishable in both skins.
- Hover usually changes the surface only slightly: gray 50 in light mode or white at about 5% in dark mode.
- Warning/error fills are low-chroma light tints (`amber-50`, `red-50`) and deep dark tints (`amber-950`, `red-950`), bounded by a matching border when the whole block is an alert.

### Spacing and density

The production rhythm follows Tailwind's 4 px scale:

- Persistent header: 56 px (`h-14`), 16 px horizontal inset, 16 px principal gap.
- Navigation rail: 280 px at `lg` and wider. Below `lg`, an off-canvas 280 px drawer preserves visible labels and counts; the former 64 px icon-only rail was retired by `docs/design/0009-responsive-accessibility-baseline.md`.
- Files record dock: 440 px wide at `xl` (1280 px) and wider, as a persistent sibling of the workspace; it takes width from the workspace rather than floating over it.
- Workspace header: 24 px horizontal, 16 px vertical; stage body sections commonly use 24 px.
- Top-level cards: commonly 16 px padding with 12-16 px internal gaps.
- Table headers: 24 px horizontal and 10 px vertical; data rows: 24 px horizontal and 12 px vertical.
- Standard controls: 32-36 px high; compact row actions: 28 px high.
- Pills: 8 px horizontal and 2 px vertical. This deliberately breaks the larger control rhythm to keep matrices scannable. The Files table renders its stage pills as glyph-only cells (see Files workbench); word pills remain on the Files cards below `md`, the Summary stage cards, and the stage matrix.
- Radius hierarchy: 6 px compact controls, 8 px controls/cards, 12 px prominent cards, 16 px overlays.

Density serves comparison and monitoring. Preserve scan lines, nowrap status clusters, truncation with title/accessible context, and horizontal overflow rather than allowing arbitrary row-height growth.

### Icons

- Use inline, single-color `currentColor` SVG for chrome and actions.
- Default navigation icon geometry is a `24 x 24` view box, approximately 20 px rendered, 1.5 stroke, no fill, round line caps and joins.
- Utility icons range from 14-20 px. Their weight should not exceed adjacent text.
- Icons that communicate state remain paired with words or accessible labels. Decorative glyphs are `aria-hidden`.
- Existing lane glyphs (`cloud`, display/local, and Kueue symbol) are a current exception to the outline system. They are semantic shorthand, not a basis for adding decorative emoji.

### Focus and interaction states

- Keyboard focus uses `focus-visible` where supported, otherwise `focus`, with a 2 px cyan-blue ring.
- Filled controls add a ring offset so focus stays visible; dark offsets match the local dark surface.
- The shell provides a skip link to `#stage-workspace`.
- Every stage swap moves focus to the workspace's single `h1[tabindex="-1"]`; history restoration follows the same target.
- Rows that open records are keyboard reachable and support Enter. Nested controls stop row activation.
- Modal layers trap focus, close on Escape/backdrop, and return focus to the invoking control. These are the navigation drawer below `lg`, the command palette, the modal record host (`record_host.html`, used below `xl` and outside Files), and confirmation dialogs.
- Non-modal layers never trap focus, set `aria-modal`, or make the page `inert`: the Files record dock at `xl`+ and the header attention tray. Tab moves through them and the surrounding page normally. Escape closes the dock only while focus is inside it (an open nested dialog or the palette keeps its own Escape) and returns focus to the row that opened it; Escape closes the tray from anywhere and returns focus to the health pill.
- Disabled controls reduce opacity and show a not-allowed cursor; asynchronous controls expose a textual indicator, not only animation.

### Semantic statuses

The stage matrix is the clearest reusable status grammar:

| State | Treatment | Meaning |
| --- | --- | --- |
| Done | Green, check, `done` | Work completed successfully. |
| In flight | Cyan-blue, dot, `in flight`, pulse | Work is actively progressing or queued in the active path. |
| Failed | Red, cross, `failed` | Terminal failure requiring retry or investigation. |
| Skipped | Violet, prohibition glyph, dashed ring, `skipped` | Force-completed without genuine stage work. |
| Queued | Slate, `◌`, `queued` | Accepted for the stage but not yet running. |
| Missing | Red, `!` (glyph cells) or `✕`, `missing` | The file is missing on disk. |
| Not started | Gray, dash, `not started` | No stage work yet. |
| Not applicable | Gray, `∅`, `not applicable` | A media-only stage that was never started for a non-music, non-video file with no output for that stage. |

Additional operational states follow the same redundant-channel rule: emerald `ACTIVE`/`ALIVE`, amber `WAITING`/`STALE`/`STALLED`, gray `IDLE`/`NEVER`/offline, and red `DEAD`/failed. Warnings use `role="alert"` only when operator attention is required; routine progress uses status/live-region semantics or no announcement. In `actionable_alert`, the `info` tone is `role="status"` and every other tone is `role="alert"`.

### Operator surfaces

These surfaces were redesigned together. Each description below is read from the named template, not from the plan that introduced it.

#### Quiet rail, health pill, and attention tray

- **Rail.** Every rail destination has the same rest and active treatment; Review is told apart by its section label and its counts, never by tinting each item. The Review group shows a count per queue (pending changes, duplicate groups, junk groups, cue sheets), amber only while that queue is greater than zero and muted otherwise. A count that is not yet known renders as a dash with an accessible "unavailable" label, never as `0`.
- **Agents inventory.** A second line under the Agents & Clusters rail label shares the workspace heading’s inventory: visible registered agents (offline included) and configured non-local clusters (idle included). Never-heartbeating compute callback identities and revoked agents are excluded. Full-document SSR, Alpine and the existing OOB poll share `visibleAgentCount`/`visibleAgentCountKnown` and `clusterCount`/`clusterCountKnown`; unavailable sources are named explicitly. These counts are separate from online-agent and active-lane signals.
- **Header health pill.** One pill in the header reads `Healthy`, `Attention`, or `Health` (status unavailable) together with the attention count (a dash when unknown). It rides the existing pipeline stats poll; there is no second browser timer. Its accessible label always states the count and that it opens the tray.
- **Attention tray.** The pill opens a non-modal tray whose body is fetched lazily from `/shell/attention` on first open. The tray lists each incident with its title, count, detail, and an action link into the owning stage, then a link to Summary. Opening moves focus to the tray heading; a successful action inside it closes the tray. The tray does not trap focus (see Focus and interaction states). On a phone the tray is a fixed inset panel; from `sm` it anchors under the pill.

#### Announcements and action failures

- A single visually hidden live region, `#incident-announcement` (`role="status"`, `aria-live="polite"`, `aria-atomic`), speaks incident changes. It compares each incident's fingerprint (title, count, tone, detail, link, action) with the previous poll and announces only items that are new or changed, plus one line when previously reported items clear.
- Unmeasured attention announces once as unavailable and is never read as recovery: prior identities are preserved, and a later measurement announces that it is available again.
- Polled replacement does not re-announce retained feedback: preserved response regions are rewritten with `aria-live="off"` so an unrelated health poll stays silent.
- A failed non-GET action (response error, send error, timeout, or target error) appends a persistent `role="alert"` panel next to the action: "Action could not be confirmed…", with a `Dismiss action error` button. It stays until dismissed or until the same control later succeeds, and a second failure of the same control does not stack a second panel. Rows with their own guarded refusal feedback (tag approval, conflict swaps) are excluded.

#### Changes Review triage

Changes Review remains the only workspace that authorizes filename, destination, and tag changes (`docs/design/0008-changes-review-approval-boundary.md`); the triage affordances below act through the same per-row Approve, Edit, Skip, and Undo controls and add no new authority.

- The page states its hotkeys: `J`/`K` move between rows, `A` approve, `E` edit, `S` skip, `Z` undo. Shortcuts press the row's own button, so a disabled or hidden control does nothing, and they pause while a field is being edited, while any row has an edit input open, or while a dialog is open.
- After a triage action settles, focus advances to the next row.
- The filter tabs carry counts (All, Needs Review, Approved, Blocked, Rejected), and the workspace subcount states how many proposals await approval.
- After a bulk approval, `Undo this approval batch` returns only the batch's unchanged approvals to pending after a confirmation; executed or edited proposals are skipped. Duplicate groups keep their own Undo and Undo All, and a junk approval is undoable until the quarantine run is dispatched.

#### Files workbench

- **Glyph stage cells.** The per-stage columns of the Files table are shown at `xl`+, where each cell is a glyph-only pill carrying the state glyph, an `aria-label` of the form `Stage: state`, and a `title`. The word is not drawn; the single legend above the table spells every state. Below `xl` the stage columns are not displayed and a labeled sort control replaces the headers; the cards below `md` keep word pills.
- **Legend.** One legend per surface lists seven bucket states (`✓ done`, `● in-flight`, `◌ queued`, `— not-started`, `✕ failed`, `! / ✕ missing on disk`, `⊘ skipped`) plus `∅ not applicable`.
- **Not applicable.** Only the Metadata, Analyze, and Tracklist cells can read `∅`, and only when the file's type is not music or video, the stage is not started, and the file has no output for that stage. Status and action eligibility are unchanged; an unknown file type is never marked not applicable.
- **Filter chips.** `Failed`, `In flight`, and `Skipped` chips above the table each send exactly one status bucket along the existing stage, scope, sort, and page-size axes, with `aria-current` on the active chip.
- **Row density.** A `Comfortable` / `Compact` pair of toggle buttons (`aria-pressed`) changes cell and card padding. The choice is a client-only preference persisted in `localStorage`, independent of the persisted column widths and untouched by table swaps; unreadable storage falls back to comfortable.
- **Docked inspector.** At `xl`+ on the Files stage, opening a row's record fills a persistent 440 px `<aside id="record-dock">` that lives outside both swap hosts, so it survives sorts, filters, and paging. It has no `role="dialog"`, `aria-modal`, focus trap, or `inert`. Selecting another row replaces the record while focus stays on the row, and the selected row carries `aria-current`. `ArrowUp`/`ArrowDown` move between visible rows and `Enter` opens the focused one. `window._recordHostFor` is the single owner decision (dock when the viewport is `xl`+ and the Files table is mounted, modal otherwise), so one open never produces both. Crossing the `xl` boundary in either direction closes whatever is open. Below `xl`, and on every other stage, the modal `record_host.html` is unchanged.

#### Summary flow board

- The board is an `<svg role="img">` with a `<title>` and a `<desc>` naming every node, edge value, and the bottleneck. It holds no links; the stage cards beneath it stay the navigable links and carry the same status text. The picture is drawn from `md` up; the cards render at every width.
- Edge widths show the files holding each stage's output, and the caption says so. Metadata and Analyze count music and video files only.
- A visually hidden list (`data-flow-text`) carries every node and edge value at every width.
- Unknown is a dash and a dashed fixed-width edge, never a zero-width edge; a measured zero keeps its own hairline and reads `0`.
- The bottleneck line is the top Needs Attention item verbatim and outlines that item's node in amber (a pipeline-wide item outlines none). When attention is unmeasured it reads as unknown and outlines nothing.

### Set projection visuals

The migration-`063` set projection drives a family of linked, non-decorative views:

| Surface | Meaning | Live implementation |
| ------- | ------- | ------------------- |
| Set glyph | Coarse-window Camelot position by hue and energy by lightness | `templates/ui/set_marks.html`, `services/set_glyph_colors.py` |
| Analysis timeline | Fine BPM/key lanes, coarse energy/mood lanes, coverage, and inspection state | `templates/proposals/partials/analysis_timeline.html`, `services/analysis_timeline.py` |
| Harmonic journey | Flicker-filtered key path on the Camelot wheel | `templates/record/partials/_harmonic_wheel.html`, `services/harmonic_journey.py` |
| Track segments | Tracklist intervals annotated with projected BPM, key, mood, and energy | `services/track_segments.py` and the record/tracklist templates |
| Record facts | Stable eight-row summary with explicit gaps for unmeasured values | `templates/record/partials/_record_facts.html`, `services/record_facts.py` |

These views must preserve the existing no-data grammar: an absent projection renders an explicit
gap or empty explanation, never a zero that looks measured. Color remains redundant with labels,
geometry, captions, and accessible text.

### Motion

- Theme/background color changes use a 200 ms transition.
- HTMX indicators fade opacity over 200 ms and become visible only during a request.
- Toast departure uses a 200 ms ease-in opacity fade.
- Stage `in flight` pills and skeletons pulse to indicate ongoing work.
- The lane-detail pane uses a 200 ms transform transition and floats without rearranging the persistent shell. The current record and command-palette overlays do not declare entry/exit transitions.

Motion should explain loading, continuity, or layer changes. Future component evolution may add restrained overlay transitions, but new components should avoid infinite animation except for an active/loading state, avoid large transforms, preserve focus throughout transitions, and add reduced-motion behavior when touching the relevant component.

## Representative States

`docs/ui-reference-fixtures.html` is a single HTML fixture sheet, not served product code. Like production, its typography depends on external Google Fonts stylesheet and font-file requests; all other CSS and all invented, privacy-safe fixture data live in the file. It covers:

| Requirement | Fixture state |
| --- | --- |
| Persistent shell | Header, wordmark, command affordance, agent status, rail, and workspace. |
| Operational | Analyze lane cards and active progress. |
| Review | Before/after rename diff with approve/edit/skip controls. |
| Tabular | Files table with glyph-only stage cells, the single legend including `∅ not applicable`, status filter chips, and the density control. |
| Inspector | Docked non-modal file record beside the Files table at `xl`+, with the selected row marked. |
| Attention | Header health pill and its non-modal attention tray. |
| Flow | Summary flow board: role-`img` SVG, caption, equivalent list, an unknown (dashed) edge, and the bottleneck outline. |
| Empty | No-files first-run surface with waveform mark. |
| Warning | Kueue Inadmissible operator alert. |
| Error | Terminal analysis failure card and retry action. |

The fixture is a static picture of those states: it does not execute the Alpine, HTMX, or live-region behavior described under Operator surfaces, so it evidences appearance and markup shape only, never the behavior. The fixture intentionally does not load production data, call Phaze application endpoints, or simulate product behavior. Its only external requests are the Google Fonts stylesheet and font files described above. Placeholder paths and invented names follow the repository privacy convention. It is evidence for review and discussion, not a parallel component implementation or pixel-perfect visual regression baseline.

## Current Inconsistencies

These are observed production differences. They are inventory items, not authorization to normalize them in unrelated work.

1. **Brand-mark geometry differs by placement.** Header, empty state, and favicon use distinct circle/wave constructions and stroke weights.
2. **Jura weights differ between loaded fonts and historical prototypes.** Production loads Jura 300/500 and Inter 400/600; older design artifacts demonstrate broader weight sets.
3. **Focus selector usage varies.** Shell chrome often uses `focus-visible:ring`; many workspace controls use `focus:ring`, so pointer focus visibility is inconsistent.
4. **Focus color follows semantics in one prominent exception.** The amber bulk-review control uses an amber ring while the general navigation/focus language is cyan-blue. Green approve and red retry controls also use semantic rings.
5. **Blue and cyan naming diverge.** Tailwind classes say `blue`, but the overridden palette is cyan. A few historical assets use default sky/cyan language independently.
6. **Success uses both `green` and `emerald`.** Stage completion is green; approvals, after-values, and online indicators often use emerald.
7. **Error/removal uses both `red` and `rose`.** Terminal failures are red while review `before` values are rose. This distinction is useful but not codified in shared tokens.
8. **Amber spans domain and severity.** Review/apply navigation, quota waiting, pause, stall, and Inadmissible alerts share amber; words and structure currently carry much of the distinction.
9. **Icon treatment has exceptions.** Most navigation uses outline SVG, while lane identity and some alerts use text glyphs/symbols.
10. **Control geometry varies.** Production has 28, 32, and 36 px controls, mixed `rounded-md`/`rounded-lg`, and both filled and outlined primaries according to context.
11. **Dark panel layering can collapse.** Inputs and cards sometimes use `phaze-panel` on a `phaze-panel` parent, relying on borders rather than fill contrast.
12. **Motion reduction has a shared baseline.** `assets/src/app.css` includes a `prefers-reduced-motion: reduce` rule that suppresses animation/transition durations and smooth scrolling; component changes still need keyboard and motion review.
13. **Status vocabularies overlap rather than share one component.** Stage pills, agent pills, lane states, cloud cards, and review lifecycle words use related but separately implemented grammars.
14. **Responsive behavior is concentrated in the shell.** The rail becomes an off-canvas drawer at `max-lg`, while several workspace constructs depend on horizontal scrolling or fixed/max widths rather than a unified narrow-screen pattern.
15. **Dark mode is more deliberately tuned.** Light mode is supported throughout, but the palette hierarchy and product character are strongest in dark mode.

Resolve these only through scoped changes with contrast, keyboard, responsive, and both-theme verification. Until then, match the local production context rather than inventing a third treatment.

## Review Checklist

Use this checklist for future shared UI changes:

- Jura remains structural and Inter remains the body family.
- Wave/cycle marks, tracked wordmark, cyan-blue accent, and dark technical character are intact.
- Dark, light, and auto theme behavior still work without a flash or unreadable intermediate state.
- Review/operator amber is not confused with terminal red or success green/emerald.
- Status meaning survives without color through words, glyphs, shape, or ARIA text.
- Focus is visible, logical, and restored after swaps/overlays; non-modal layers (record dock, attention tray) never trap focus or mark the page inert.
- Unknown values render as a dash or a dashed edge, never as zero; an announcement fires on a change, not on every poll.
- The persistent shell and stage focus target survive HTMX swaps.
- Icon weight and outline treatment match adjacent production controls.
- Density supports scanning without clipping essential status or controls.
- Motion is brief, functional, and reducible.
- Narrow layouts preserve access even if presentation changes.
- Fixtures and screenshots contain no archive filenames, paths, digests, or file UUIDs.
