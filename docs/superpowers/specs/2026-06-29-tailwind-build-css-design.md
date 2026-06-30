# Build-time Tailwind via the standalone binary

**Date:** 2026-06-29
**Status:** Approved — implementing
**Worktree/branch:** `worktree-tailwind-build`

## Problem

Phaze currently ships the Tailwind v4 **browser build** — a 276 KB
`static/vendor/tailwindcss-browser-4.3.2.min.js` that compiles utility classes
**in the browser at runtime** on every page load, reading an inline
`<style type="text/tailwindcss">` config block. This config block is duplicated
verbatim in both `base.html` and `shell/shell.html`.

Costs: 276 KB of JS on every page, a client-side compile pass before styles
settle, and config duplicated across two templates.

## Goal

Replace the runtime in-browser compiler with a **pre-built static stylesheet**,
compiled by the pinned **standalone Tailwind v4 binary** (the cronduit pattern —
no Node, no npm). The browser loads a plain `<link rel="stylesheet">`; no
client-side compilation.

## Approach

Standalone-binary build (chosen over the discogsography Node/npm builder stage):
no Node toolchain added to the image, exact-version pinning matches phaze's
existing "vendored, deterministic, not CDN" philosophy, and it affords a clean
`just tailwind` local-dev recipe.

### Components

1. **Input CSS — `assets/src/app.css`** (new). Carries everything currently
   inlined in the two template heads:
   - `@import "tailwindcss";`
   - `@source "../../src/phaze/templates";` — scans all 111 Jinja templates.
   - `@custom-variant dark (&:where(.dark, .dark *));`
   - the `@theme` block (the Phaze blue palette + `--color-phaze-bg/panel/border`)
   - the plain rules: `body { font-family: 'Inter' }`, `.font-jura`,
     `.htmx-indicator` / `.htmx-request .htmx-indicator`

2. **`just tailwind` recipe** (local dev, in the `build` group). Downloads the
   pinned `tailwindcss-${OS}-${ARCH}` binary into `./bin/` if absent
   (macos/linux × x64/arm64), then:
   `./bin/tailwindcss -i assets/src/app.css -o src/phaze/static/css/app.css --minify`

3. **Dockerfile build step**. Download `tailwindcss-linux-${ARCH}` for the build
   arch and compile `assets/src/app.css → src/phaze/static/css/app.css` before
   the runtime image ships. Pinned to the same version as the justfile recipe.

4. **Templates** (`base.html` + `shell/shell.html`). Replace the
   `<script src="/static/vendor/tailwindcss-browser-4.3.2.min.js">` **and** the
   `<style type="text/tailwindcss">` config block with a single
   `<link rel="stylesheet" href="/static/css/app.css">`. The unrelated plain
   `<style>` rules move into `app.css`. Delete the vendored 276 KB JS file.

5. **Local-build wiring**. `just install` (and `just up`) depend on
   `just tailwind` so the CSS is always built before the app runs locally.

6. **Git/artifacts**. The generated CSS is **not committed** (build-time only).
   `.gitignore`: `src/phaze/static/css/app.css` and `/bin/`.

### Pinning

Standalone binary pinned to exact **v4.3.2** (matches the already-audited browser
build version), kept in sync between the justfile recipe and the Dockerfile. The
GitHub release asset name is verified at implementation time
(`tailwindcss-{macos,linux}-{x64,arm64}`).

## Risk: build-time class scanning

The browser JIT compiled whatever classes appeared in the live DOM; build-time
scanning only emits classes literally present in the `@source` files.

**Verified during design** — no class strings originate outside scanned templates:
- `_status_pill.html` uses literal per-branch palette classes.
- `dag_canvas.html` macros (`enqueue_button`, `node_bar`) take class strings as
  parameters, but every call site passes a **literal** string in the template
  text (`bg-blue-600 dark:bg-blue-700 …`, `bg-rose-600`, `bg-indigo-600`,
  `bg-purple-600`), so the scanner sees them.
- The Alpine `:class` bindings (`comparison_table.html`, `proposal_row.html`)
  inline their literal class strings.

No safelist is required. If a future class is added only in Python, it must also
appear in a scanned template (or be safelisted in `app.css`).

## Testing / UAT

- `just tailwind` produces a non-empty `src/phaze/static/css/app.css`.
- Docker image build emits the same file into the image.
- Run the app locally; load each workspace (pipeline dashboard / DAG, shell,
  proposals, tracklists, tags, search, execution, admin agents) and confirm
  styling is intact in **both light and dark** mode, including the status pills
  and DAG node/button colors.
- No `tailwindcss-browser` script and no `text/tailwindcss` block remain in any
  template; the vendored JS file is gone.

## Out of scope

- Self-hosting Google Fonts (Jura/Inter) — stays a CDN `<link>`.
- HTMX / Alpine / SSE CDN scripts — unchanged.
- Deduplicating the near-identical `base.html` / `shell.html` heads.
