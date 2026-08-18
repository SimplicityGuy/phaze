# ADR-0010 — Repaint the colour ramp for WCAG AA, rather than shifting rungs or renaming utilities

| | |
| --- | --- |
| **Status** | Accepted — decided 2026-08-18, not yet implemented |
| **Date** | 2026-08-18 |
| **Bead** | `phaze-qvid8` (implementation) · `phaze-coypu` (follow-up, blocked on it) |
| **Extends** | ADR-0009 §"Where each rule is enforced" — this closes the computed-contrast gap that ADR records |

## Decision

1. **Repaint the failing rungs of the Tailwind ramp** in `assets/src/app.css`'s `@theme` block.
   Do not shift utilities to different rungs, and do not introduce semantic names yet.
2. **Target 5.5:1 minimum against `#f3f4f6`**, the worst light background in the app.
3. **Fix all 18 failing colours**, not the three groups the browser suite happened to measure.
4. **Fold `color-contrast` into `tests/browser/axe.py`'s `RULES`** so it becomes a blocking gate,
   deleting the separate `CONTRAST_RULES` split.

## The problem, measured

`tests/browser/test_accessibility.py::test_a_populated_workspace_meets_wcag_aa_contrast` runs a
computed-contrast check on every invocation and fails on every workspace in both themes. It is
recorded as a strict xfail across 10 parametrised cells (2 themes × 5 stages), not disabled, so it
cannot be forgotten.

That check sees only what the five seeded workspaces render. A static sweep of every `text-*`
utility in `src/phaze/templates`, scored against the four real backgrounds, finds substantially
more:

| | failing colours | class-string uses |
| --- | --- | --- |
| Light theme (`#ffffff`, `#f3f4f6`) | **15** | **508** |
| Dark theme (`#10141c`, `#0a0c12`) | **3** | **62** |

The worst case is `text-gray-400` on `bg-gray-100` at **2.36:1** — a little over half the
requirement, so this is not an argument about borderline pairs.

### Why the dark theme is nearly clean

The two themes use the ramp asymmetrically. Light-theme text uses the 500/600/700 rungs; dark-theme
text uses 200/300/400. The two sets are almost disjoint, which is why repainting the light rungs
does not damage the dark theme. The three dark failures are not wrong *values* — they are templates
reaching for a light-theme rung (`dark:text-gray-500`, `dark:text-gray-600`,
`dark:text-violet-500`). Those are fixed by swapping the rung, not by repainting it.

That asymmetry is the load-bearing fact of this ADR. It is also why a single-token repaint cannot
serve both themes, and why the dark rung swap becomes **mandatory** once `gray-500` darkens.

## The values

### Light theme — repaint in `@theme`

| utility | uses | now | on `#f3f4f6` | **new** | new ratio | on `#fff` |
| --- | --- | --- | --- | --- | --- | --- |
| `text-gray-500` | 264 | `#6a7282` | 4.39 | **`#5b6272`** | 5.56 | 6.11 |
| `text-gray-400` | 49 | `#99a1af` | 2.36 | **`#5b6370`** | 5.51 | 6.06 |
| `text-amber-600` | 25 | `#e17100` | 2.91 | **`#9c4c01`** | 5.53 | 6.09 |
| `text-blue-700` | 23 | `#008caf` | 3.55 | **`#006b87`** | 5.53 | 6.08 |
| `text-blue-600` | 21 | `#00b0d8` | 2.33 | **`#036b85`** | 5.54 | 6.09 |
| `text-red-600` | 19 | `#e7000b` | 4.33 | **`#c7080d`** | 5.50 | 6.05 |
| `text-green-600` | 15 | `#00a63e` | 2.92 | **`#027229`** | 5.54 | 6.10 |
| `text-green-700` | 13 | `#008236` | 4.49 | **`#00722f`** | 5.53 | 6.09 |
| `text-emerald-600` | 13 | `#009966` | 3.32 | **`#00714a`** | 5.51 | 6.07 |
| `text-blue-500` | 10 | `#00b0d8` | 2.33 | **`#036b85`** | 5.54 | 6.09 |
| `text-rose-600` | 5 | `#ec003f` | 4.11 | **`#c40b35`** | 5.54 | 6.09 |
| `text-yellow-700` | 4 | `#a65f00` | 4.48 | **`#925300`** | 5.52 | 6.07 |
| `text-orange-600` | 4 | `#f54900` | 3.27 | **`#b43506`** | 5.53 | 6.08 |
| `text-violet-500` | 2 | `#8e51ff` | 4.00 | **`#7935e5`** | 5.53 | 6.08 |
| `text-yellow-600` | 1 | `#d08700` | 2.67 | **`#895800`** | 5.52 | 6.07 |

`blue-500` and `blue-600` are already the **same value** (`#00b0d8`) in the existing Phaze accent
override, so they take the same replacement.

### Dark theme — rung swap in the templates

| utility | uses | now | on `#10141c` | **swap to** | new ratio |
| --- | --- | --- | --- | --- | --- |
| `dark:text-gray-500` | 57 | `#6a7282` | 3.81 | **`dark:text-gray-400`** | 7.09 |
| `dark:text-gray-600` | 3 | `#4a5565` | 2.44 | **`dark:text-gray-400`** | 7.09 |
| `dark:text-violet-500` | 2 | `#8e51ff` | 4.19 | **`dark:text-violet-400`** | 6.47 |

### White-on-fill buttons — stock rungs, separate fix

The text repaint does not reach fill colours.

| fill | uses | now | white on it | **swap to** | new ratio |
| --- | --- | --- | --- | --- | --- |
| `bg-emerald-500` | 6 | `#00bc7d` | **2.47** | `bg-emerald-700` `#007a55` | 5.36 |
| `bg-emerald-600` | 2 | `#009966` | 3.65 | `bg-emerald-700` `#007a55` | 5.36 |
| `bg-blue-600` | 7 | `#00b0d8` | 2.56 | (covered by the `blue-600` repaint → 6.09) |
| `bg-blue-700` | 6 | `#008caf` | 3.91 | (covered by the `blue-700` repaint → 6.08) |

The two `bg-blue-*` rows are the **Phaze brand accent** and are already overridden in `@theme`. The
light-theme repaint fixes them as a side effect. Confirm the brand still reads as Phaze cyan at that
darkness; if it does not, split text from fill — keep the fill vivid and darken the button text
instead (`#00b0d8` fill with `text-blue-950` is 8.9:1).

### Non-text uses to check before shipping

`gray-400`/`gray-500` are almost entirely text, but not entirely: `bg-gray-400` ×4,
`placeholder-gray-400` ×2, `placeholder-gray-500` ×1. A darkened placeholder is the one most likely
to read wrong.

## Method

WCAG 2.1 relative luminance. Candidates were derived by lowering OKLCH **L** at fixed hue and
chroma, binary-searched to the target ratio, with chroma reduced only where the result left sRGB
gamut. **No value was nudged until axe stopped complaining** — every one is the solution to a stated
target, which is what makes the set reproducible if a background ever changes.

The four backgrounds scored against are `#ffffff` and `#f3f4f6` (light), and
`--color-phaze-panel` `#10141c` and `--color-phaze-bg` `#0a0c12` (dark). The dark pair are **not**
Tailwind grays; a repaint validated against `gray-900` would be measuring the wrong thing.

## Alternatives rejected

**Set A — the same repaint at 4.75:1.** Rejected. It passes by 0.25 of a ratio point on every
colour, close enough that a future `bg-gray-50` card or a hover state puts it back under 4.5. The
extra 0.75 buys a visual difference you have to look for.

**Set C — stock Tailwind rung shift, no repaint.** Rejected on three counts: ~570 class-string edits
against one file; coarse jumps that land marginal (`text-amber-600` → `amber-700` is 4.57:1, passing
by 0.07); and it fixes nothing structurally — the next template can still reach for
`text-blue-600` on white. Its one real advantage, that stock values are predictable to anyone
reading a template, is bought back by the comment block the repaint requires.

**Semantic tokens now.** Deferred to `phaze-coypu`, blocked on this ADR's implementation. It is the
right end state and it is a redesign of the class vocabulary, not a repair. Folding it into this
change would collide on every template and make a contained, reviewable palette diff unreviewable.

## The cost this decision accepts

**A repainted `amber-600` no longer means Tailwind's `amber-600`.** Anyone reading a template cannot
predict the rendered colour from the class name. This is the real price of the chosen strategy and
it must be paid down with an explicit comment block in `assets/src/app.css` stating that the ramp is
deliberately overridden for contrast, with the target ratio and a pointer to this ADR.

`phaze-coypu` is the structural answer to that cost. Until it lands, the comment block is the only
thing standing between the next reader and a wrong assumption.

## Consequences

- The palette does **not** go monochrome. Every hue is preserved; only lightness moves. The status
  vocabulary stays chromatically distinct — emerald for approved, amber for needs-review, red for
  blocked — which matters because ADR-0009 §"Controls" already requires that status is never carried
  by colour alone, and this change must not weaken the colour half of that pairing.
- `color-contrast` moves into `RULES` and becomes blocking. A new violation on a seeded surface
  turns the browser suite red. Surfaces the browser suite does not seed remain unguarded — that gap
  is what `phaze-coypu`'s build-time guard closes.
- The 10 `xfail(strict=True)` cells XPASS, which **fails** the suite under the strict marker.
  Removing the marker is part of the implementation, not a follow-up.
- Measured after-ratios from the browser — not the computed ones in this ADR — are what the
  implementation reports.
