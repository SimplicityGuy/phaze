# Phaze UI Design Reference

**Status:** Compatibility contract for the production UI as of 2026-08-14
**Scope:** Visual identity and interaction language only; this is not a redesign specification
**Fixture:** [Privacy-safe reference states](ui-reference-fixtures.html)

This reference records the visual and interaction rules already expressed by the production templates. It is a baseline for evaluating shared-component changes: preserve the identity-bearing characteristics, while allowing components to improve hierarchy, accessibility, and responsive behavior. Where production is inconsistent, this document reports the inconsistency rather than selecting a new style.

## Source Of Truth

The served application remains authoritative. This contract was derived from:

- `assets/src/app.css`: palette overrides, dark surfaces, font families, HTMX indicator transition, and Alpine cloak.
- `src/phaze/templates/shell/shell.html`: persistent shell, auto/dark/light behavior, independent scrolling, focus handoff, and theme transition.
- `src/phaze/templates/shell/partials/header.html`: wordmark, circular wave mark, command affordance, status strip, and theme control.
- `src/phaze/templates/shell/partials/rail.html`: DAG navigation, responsive icon rail, outline icons, active state, and amber review grouping.
- `src/phaze/templates/pipeline/partials/_workspace_scaffold.html`: stage heading, action placement, and content spacing.
- `src/phaze/templates/pipeline/partials/_diff_row.html`: review diff language and compact controls.
- `src/phaze/templates/pipeline/partials/files_table_view.html` and `_stage_pill.html`: tabular density and semantic stage states.
- `src/phaze/templates/pipeline/partials/empty_state.html`, `inadmissible_card.html`, and `analysis_failed_card.html`: empty, warning, stalled, and terminal-error treatments.
- `src/phaze/static/favicon-32.svg`: compact favicon form of the brand mark.

The older prototypes under `docs/superpowers/specs/2026-06-28-ui-redesign-assets/` explain the chosen direction, but production templates take precedence when prototype and shipped behavior differ.

## Compatibility Boundary

### Identity-bearing constraints

These characteristics make the interface recognizably Phaze and must remain stable across component work:

1. **Jura plus Inter.** Jura identifies the product and pipeline structure; Inter carries readable operational content. Replacing either family, or using Jura for long-form body copy, changes the identity.
2. **Tracked technical headings.** Product, stage, group, table, and compact action labels use uppercase Jura with generous tracking. The result should remain technical and instrument-like, not editorial or decorative.
3. **Wave/cycle brand marks.** Preserve the blue circular-wave header mark, the waveform empty-state mark, the compact favicon, and the tracked `PHAZE` wordmark as one related family. Do not substitute a generic music-note logo.
4. **Dark technical character.** Dark mode is the primary visual character: near-black canvas, slightly lifted cool panels, quiet cool-gray borders, dense information, and restrained depth. It must not become a generic flat black dashboard or a high-gloss neon interface.
5. **Cyan-blue accent family.** The project overrides Tailwind blue with cyan (`#00b0d8` at 500/600). It identifies the brand, navigation/current state, links, primary actions, progress, and keyboard focus.
6. **Amber review and operator-attention family.** Amber separates human review/apply work and recoverable attention states from normal pipeline flow. It is not the generic color for every highlighted control.
7. **Persistent console shell.** The compact top header and left pipeline/DAG rail remain the stable frame around a swappable stage workspace. The flow stays visible while work changes.
8. **Thin outline icon language.** Navigation and utility icons are simple inline SVG outlines, normally `24 x 24`, `currentColor`, round caps/joins, and about `1.5` stroke width. Icons support labels rather than replace meaning.
9. **Compact operational density.** Controls and rows are intentionally concise, with strong alignment, tabular/monospace data where useful, and progressive disclosure for detail. Spacious marketing-page rhythm is outside the product character.
10. **Visible blue keyboard focus.** Interactive elements expose an approximately 2 px cyan-blue ring. Focus is an interaction state, not optional decoration.
11. **State is never color alone.** Statuses combine color with a word and, where compact, a glyph or shape. For example, `✓ done`, `● in flight`, `✗ failed`, `⊘ skipped`, and `- not started` remain distinguishable without hue.
12. **Restrained motion.** Motion communicates state changes: 200 ms color/opacity transitions, loading pulse, the lane-detail pane transition, and HTMX activity. It is functional, short, and never ambient spectacle.

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
| Accent/focus | custom blue 500/600 `#00b0d8` | custom blue 400-600 | Brand, current location, links, primary actions, progress, focus. |

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
- Active pipeline-stage navigation is a translucent cyan-blue fill plus an inset 3 px cyan-blue rail, not a large filled button. Utility-pane nodes such as Audit and Agents use a neutral gray active fill without the cyan inset.
- Hover usually changes the surface only slightly: gray 50 in light mode or white at about 5% in dark mode.
- Warning/error fills are low-chroma light tints (`amber-50`, `red-50`) and deep dark tints (`amber-950`, `red-950`), bounded by a matching border when the whole block is an alert.

### Spacing and density

The production rhythm follows Tailwind's 4 px scale:

- Persistent header: 56 px (`h-14`), 16 px horizontal inset, 16 px principal gap.
- Expanded rail: 280 px; compact rail: 64 px at `max-lg`, with labels visually hidden but retained for assistive technology, counts hidden, and icons centered.
- Workspace header: 24 px horizontal, 16 px vertical; stage body sections commonly use 24 px.
- Top-level cards: commonly 16 px padding with 12-16 px internal gaps.
- Table headers: 24 px horizontal and 10 px vertical; data rows: 24 px horizontal and 12 px vertical.
- Standard controls: 32-36 px high; compact row actions: 28 px high.
- Pills: 8 px horizontal and 2 px vertical. This deliberately breaks the larger control rhythm to keep matrices scannable.
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
- Drawers and command overlays trap focus, close on Escape/backdrop, and return focus to the invoking control.
- Disabled controls reduce opacity and show a not-allowed cursor; asynchronous controls expose a textual indicator, not only animation.

### Semantic statuses

The stage matrix is the clearest reusable status grammar:

| State | Treatment | Meaning |
| --- | --- | --- |
| Done | Green, check, `done` | Work completed successfully. |
| In flight | Cyan-blue, dot, `in flight`, pulse | Work is actively progressing or queued in the active path. |
| Failed | Red, cross, `failed` | Terminal failure requiring retry or investigation. |
| Skipped | Violet, prohibition glyph, dashed ring, `skipped` | Force-completed without genuine stage work. |
| Not started | Gray, dash, `not started` | No stage work yet. |

Additional operational states follow the same redundant-channel rule: emerald `ACTIVE`/`ALIVE`, amber `WAITING`/`STALE`/`STALLED`, gray `IDLE`/`NEVER`/offline, and red `DEAD`/failed. Warnings use `role="alert"` only when operator attention is required; routine progress uses status/live-region semantics or no announcement.

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
| Tabular | File stage matrix with all core status pills. |
| Empty | No-files first-run surface with waveform mark. |
| Warning | Kueue Inadmissible operator alert. |
| Error | Terminal analysis failure card and retry action. |

The fixture intentionally does not load production data, make requests, or simulate application behavior. Placeholder paths and invented names follow the repository privacy convention. It is evidence for review and discussion, not a parallel component implementation or pixel-perfect visual regression baseline.

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
12. **Motion reduction is not systematic.** Pulse and transition utilities are present, but there is no shared `prefers-reduced-motion` treatment in `app.css`.
13. **Status vocabularies overlap rather than share one component.** Stage pills, agent pills, lane states, cloud cards, and review lifecycle words use related but separately implemented grammars.
14. **Responsive behavior is concentrated in the shell.** The rail compacts at `max-lg`, while several workspace constructs depend on horizontal scrolling or fixed/max widths rather than a unified narrow-screen pattern.
15. **Dark mode is more deliberately tuned.** Light mode is supported throughout, but the palette hierarchy and product character are strongest in dark mode.

Resolve these only through scoped changes with contrast, keyboard, responsive, and both-theme verification. Until then, match the local production context rather than inventing a third treatment.

## Review Checklist

Use this checklist for future shared UI changes:

- Jura remains structural and Inter remains the body family.
- Wave/cycle marks, tracked wordmark, cyan-blue accent, and dark technical character are intact.
- Dark, light, and auto theme behavior still work without a flash or unreadable intermediate state.
- Review/operator amber is not confused with terminal red or success green/emerald.
- Status meaning survives without color through words, glyphs, shape, or ARIA text.
- Focus is visible, logical, and restored after swaps/overlays.
- The persistent shell and stage focus target survive HTMX swaps.
- Icon weight and outline treatment match adjacent production controls.
- Density supports scanning without clipping essential status or controls.
- Motion is brief, functional, and reducible.
- Narrow layouts preserve access even if presentation changes.
- Fixtures and screenshots contain no archive filenames, paths, digests, or file UUIDs.
