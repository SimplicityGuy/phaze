# Phaze Design System

**Project:** Phaze — Align Your Music
**Movement:** Resonant Precision
**Version:** 1.0

---

## Voice & Tone

Sound is invisible architecture. Every visual mark is a waveform — each line, curve, and negative space is a frequency that either reinforces or cancels. Nothing decorative survives. What remains is the skeleton of sound made visible: precise, inevitable, resonant.

- **Technical but approachable** — speak like a mastering engineer explaining to a fellow musician
- **Concise** — silence between notes gives music its rhythm; whitespace gives UI its clarity
- **Confident** — no hedging, no unnecessary qualifiers

---

## Color System

### Brand Accent Palette

| Token | Hex | Usage |
|-------|-----|-------|
| `blue-50` | `#e6f7fb` | Tinted backgrounds, hover states |
| `blue-100` | `#b3e8f3` | Light accents |
| `blue-200` | `#80d9eb` | Secondary highlights |
| `blue-300` | `#4dcae3` | Active borders |
| `blue-400` | `#1abbdb` | **Primary accent (dark mode)** |
| `blue-500` | `#00b0d8` | Primary accent alternate |
| `blue-600` | `#00b0d8` | Links |
| `blue-700` | `#008caf` | **Primary accent (light mode)** |
| `blue-800` | `#006882` | Pressed states |
| `blue-900` | `#004455` | Deep accents |
| `blue-950` | `#002233` | Darkest accent |

### Surface, Border & Text Colors (as implemented)

The build defines exactly **three** surface/border tokens in the Tailwind v4 `@theme` block (`assets/src/app.css`). There are no separate `--bg-*`, `--border-*`, or `--text-*` custom properties — light/dark is handled by Tailwind's `dark:` variant (a `.dark` class flip on the root, see `@custom-variant dark` in `app.css`), **not** by swapping CSS-variable values.

| Token | Value | Tailwind utilities | Usage |
|-------|-------|--------------------|-------|
| `--color-phaze-bg` | `#0a0c12` | `bg-phaze-bg`, `text-phaze-bg` | Page background |
| `--color-phaze-panel` | `#10141c` | `bg-phaze-panel` | Cards, panels |
| `--color-phaze-border` | `#232832` | `border-phaze-border` | Borders, dividers |

Everything else — text, muted captions, hover states, light-mode surfaces — is expressed with **stock Tailwind gray/blue utilities plus `dark:` variants**, e.g.:

- Primary text: `text-gray-900 dark:text-gray-100`
- Secondary / muted text: `text-gray-500 dark:text-gray-400`
- Accent text: `text-blue-400` (dark), `text-blue-700` (light)
- Focus ring: `focus:ring-blue-400/50`

> **Aspirational (NOT yet implemented):** an earlier draft of this system proposed a fuller semantic token set — `--bg-primary/-surface/-raised/-sunken/-hover`, `--border-default/-subtle/-strong/-focus`, and `--text-primary/-secondary/-muted/-inverse/-accent` with explicit dark/light value pairs. **None of these exist in the build.** Treat them as a future roadmap, not as classes you can use today.

### Status Colors

| Status | Color | Background Tint | Usage |
|--------|-------|-----------------|-------|
| Active | `#22c55e` | `#22c55e14` | Completed, online |
| Running | `#1abbdb` | `#1abbdb14` | In-progress, processing |
| Warning | `#eab308` | `#eab30814` | Needs attention |
| Error | `#ef4444` | `#ef444414` | Failed, critical |
| Disabled | `#6b7280` | `#6b728014` | Inactive |

### CSS Custom Properties

These live in the Tailwind v4 `@theme { }` block in `assets/src/app.css` (not `:root`). Declaring them in `@theme` is what generates the `blue-*` and `phaze-*` utility classes at build time.

```css
@theme {
  /* Brand accent */
  --color-blue-50: #e6f7fb;
  --color-blue-100: #b3e8f3;
  --color-blue-200: #80d9eb;
  --color-blue-300: #4dcae3;
  --color-blue-400: #1abbdb;
  --color-blue-500: #00b0d8;
  --color-blue-600: #00b0d8;
  --color-blue-700: #008caf;
  --color-blue-800: #006882;
  --color-blue-900: #004455;
  --color-blue-950: #002233;

  /* Surfaces */
  --color-phaze-bg: #0a0c12;
  --color-phaze-panel: #10141c;
  --color-phaze-border: #232832;
}
```

---

## Typography

### Font Stack

| Role | Family | Fallback | Weight(s) | Usage |
|------|--------|----------|-----------|-------|
| Display / headings | **Jura** | sans-serif | 300 (light), 500 (medium) | Logo text, page headings, nav items |
| Body / UI | **Inter** | sans-serif | 400 (regular), 600 (semibold) | Body text, labels, inputs, buttons |
| Mono / code | System monospace | `ui-monospace, monospace` | 400 | Code blocks, file paths, technical values |

### Type Scale

| Token | Size | Line Height | Letter Spacing | Usage |
|-------|------|-------------|----------------|-------|
| `text-xs` | 12px | 16px | 0 | Badges, captions |
| `text-sm` | 14px | 20px | 0 | Secondary text, table cells |
| `text-base` | 16px | 24px | 0 | Body text |
| `text-lg` | 18px | 28px | 0 | Emphasized body |
| `text-xl` | 20px | 28px | 0 | Section headings |
| `text-2xl` | 24px | 32px | 0.02em | Page headings |
| `text-3xl` | 30px | 36px | 0.04em | Hero headings |
| `display` | 38px+ | 1.1 | 0.08em+ | Logo text (Jura only) |

### Logo Typography

- **"PHAZE"**: Jura 500, uppercase, letter-spacing 12px (0.3em)
- **"ALIGN YOUR MUSIC"**: Jura 300, uppercase, letter-spacing 4px (0.25em), muted color

---

## Spacing

### Base Unit: 4px

| Token | Value | Usage |
|-------|-------|-------|
| `space-0.5` | 2px | Inline gaps, icon padding |
| `space-1` | 4px | Tight gaps |
| `space-2` | 8px | Element gaps, badge padding |
| `space-3` | 12px | Form element padding |
| `space-4` | 16px | Card padding, section gaps |
| `space-5` | 20px | Comfortable padding |
| `space-6` | 24px | Section spacing |
| `space-8` | 32px | Major section gaps |
| `space-10` | 40px | Page-level spacing |
| `space-12` | 48px | Large gaps |
| `space-16` | 64px | Hero spacing |

---

## Border Radius

| Token | Value | Usage |
|-------|-------|-------|
| `rounded-sm` | 4px | Badges, small elements |
| `rounded` | 6px | Buttons, inputs |
| `rounded-md` | 8px | Cards, panels |
| `rounded-lg` | 12px | Modals, large cards |
| `rounded-xl` | 16px | Featured sections |
| `rounded-full` | 9999px | Pills, avatars |

---

## Component Patterns

### Badges

```html
<span class="rounded-sm px-2 py-0.5 text-xs font-medium
  bg-blue-400/10 text-blue-400">  <!-- status: running -->
  Processing
</span>
```

Use status colors with `/10` (10% opacity) background tints.

### Buttons

**Primary:**
```html
<button class="rounded bg-blue-500 px-4 py-2 text-sm font-semibold text-white
  hover:bg-blue-400 transition-colors">
  Approve
</button>
```

**Ghost:**
```html
<button class="rounded px-4 py-2 text-sm text-gray-400
  hover:bg-phaze-panel transition-colors">
  Cancel
</button>
```

### Cards / Panels

```html
<div class="rounded-md border border-phaze-border bg-phaze-panel p-4">
  <!-- content -->
</div>
```

### Tables

- Header row: `bg-phaze-bg` with `text-gray-400`, uppercase `text-xs`, `tracking-wider`
- Body rows: `bg-phaze-panel`, `border-b border-phaze-border`
- Hover: `hover:bg-phaze-bg/50`
- Cell padding: `px-4 py-3`

### Inputs

```html
<input class="rounded border border-phaze-border bg-phaze-bg px-3 py-2
  text-sm text-gray-100 placeholder-gray-500
  focus:border-blue-400 focus:outline-none focus:ring-1 focus:ring-blue-400/50">
```

### Code Blocks

```html
<code class="rounded-sm bg-phaze-bg px-1.5 py-0.5 text-sm font-mono text-blue-300">
  filename.mp3
</code>
```

---

## Quick Reference

```
Background:  #0a0c12 (dark)  |  #eef0f5 (light)
Surface:     #10141c (dark)  |  #ffffff (light)
Border:      #232832 (dark)  |  #d1d5db (light)
Accent:      #1abbdb (dark)  |  #008caf (light)
Text:        #f0f1f5 (dark)  |  #1a1f2e (light)
Muted:       #667c8a (dark)  |  #6b7280 (light)

Font display: Jura 300/500
Font body:    Inter 400/600
Base spacing: 4px
Border radius: 6px (default)
```

---

## File Manifest

### Logos (`design/logos/`)
| File | Format | Dimensions | Variant |
|------|--------|------------|---------|
| `phaze-square-dark.svg` | SVG | 512x512 | Square logo, dark bg |
| `phaze-square-light.svg` | SVG | 512x512 | Square logo, light bg |
| `icon_dark.svg` | SVG | 512x512 | Icon mark, dark bg |
| `icon_light.svg` | SVG | 512x512 | Icon mark, light bg |

### Banners (`design/banners/`)
| File | Format | Dimensions | Variant |
|------|--------|------------|---------|
| `phaze-banner-static.svg` | SVG | 1200x400 | Static banner, dark bg |
| `phaze-banner-animated.svg` | SVG | 1200x400 | Animated banner, dark bg |

### Favicon Sources (`design/favicons/`)
| File | Format | Target Size |
|------|--------|-------------|
| `favicon-{16,32,48,64,128,192,256,512}.svg` | SVG | Matching px |

### Raster Exports (`design/assets/`)
| File | Format | Dimensions | Source |
|------|--------|------------|--------|
| `icon_dark.png` | PNG | 512x512 | `logos/icon_dark.svg` |
| `icon_light.png` | PNG | 512x512 | `logos/icon_light.svg` |
| `square_dark.png` | PNG | 512x512 | `logos/phaze-square-dark.svg` |
| `square_light.png` | PNG | 512x512 | `logos/phaze-square-light.svg` |
| `banner_dark.png` | PNG | 1200x400 | `banners/phaze-banner-static.svg` |
| `banner_light.png` | PNG | 1200x400 | Light variant of static banner |
| `favicon-{16,32,48,64,128,256,512}.png` | PNG | Matching px | `favicons/favicon-{size}.svg` |
| `og_image.png` | PNG | 1200x630 | Social sharing image |
| `design_showcase.png` | PNG | 2400x1800 | `showcase.html` screenshot |

### Deployable Assets (`src/phaze/static/`)
| File | Purpose |
|------|---------|
| `favicon.ico` | Multi-size ICO (16+32+48) |
| `favicon-{16,32}.png` | Browser tab favicons |
| `favicon-{192,512}.png` | PWA icons |
| `apple-touch-icon.png` | iOS home screen (180x180) |
| `site.webmanifest` | PWA manifest |
| `og_image.png` | Open Graph social image |
| `favicon-{16,32,192}.svg` | SVG favicon alternatives |
