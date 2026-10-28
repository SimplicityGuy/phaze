# Maintaining documentation diagrams

The editable source is [the relationship manifest](diagrams/manifest.json). Each entry owns
its HTML, standalone SVG and committed PNG preview. Markdown embeds the PNG so GitHub readers
see the diagram, then links the HTML and SVG for full-size reading. HTML contains inline SVG;
there is no product frontend build or required network resource. Wide figures scroll within
the diagram on a phone. Keyboard focus exposes node labels, and every SVG has a title and
description. Source evidence remains available in the HTML disclosure table and manifest.

## Layout and source evidence

Choose a diagram-design visual type that expresses the claim: architecture boundaries,
flowchart decisions, sequence messages, state transitions, or entity relationships. Split
dense views by capability or data lifetime. Layout does not establish a prerequisite or a
foreign key: check the implementation before adding an arrow. In particular, stage eligibility
is distinct from automatic enqueue, and durable provider evidence differs from mutable lookup
work and disposable read caches. Label logical references that deliberately lack a SQL FK.

Every edge cites repository source. The original migration records retain the source revision,
source-body digest and a disposition for every original relationship. Split views may carry
the corresponding edges; a correction explains the old claim and the source-grounded reason.
Keep cardinalities, guards, retry limits and trust boundaries readable. A digest proves byte
identity, not semantic accuracy: review the arrows against those sources after source changes.

## Phaze profile and offline typography

[profile.json](diagrams/profile.json) uses the existing Phaze palette and typography. Jura
300/500 and Inter 400/600 Latin WOFF2 fonts are embedded as data URLs in both HTML and SVG.
The [font provenance](diagrams/fonts/provenance.json) records exact download URLs and hashes;
[Inter](diagrams/fonts/inter-OFL.txt) and [Jura](diagrams/fonts/jura-OFL.txt) use the SIL Open
Font License 1.1. Other glyphs use the declared system fallback. Font QA checks actual rendered
custom fonts through Chromium's CDP API, as well as loaded FontFace entries; a CSS family name
alone would not prove the font was used. Light and dark skins preserve text and connector
contrast; faint decorative rules never carry a relationship by themselves.

## Regeneration and checks

Use Python 3.14 and the repository's frozen dependencies. Install the existing browser group
with `uv sync --frozen --group browser` when running browser exports; do not change `uv.lock`.
Chrome can be installed locally or Chromium provisioned through Playwright's documented setup.

```bash
uv run python scripts/render_documentation_diagrams.py
uv run python scripts/verify_diagram_design.py
uv run --group browser python scripts/verify_documentation_diagrams.py \
  --output /tmp/phaze-diagram-review --export
just check-docs
```

The browser command retains light/dark screenshots and detailed receipts in the selected
scratch directory, refreshes committed PNGs and records exact source/export digests. Review
representative and dense images at readable size before committing. Static geometry checks do
not replace browser BBoxes, offline font checks, mobile scrolling, print review or a semantic
review of relationships. `just check-docs` deterministically checks asset digests, orphan assets,
accessible identities, source paths, original-relationship dispositions, links and anchors. It
rejects newly introduced Mermaid fences in maintained guides. It has no Node dependency.

## Upstream provenance and historical evidence

The helpers are from [cathrynlavery/diagram-design at the pinned revision](https://github.com/cathrynlavery/diagram-design/tree/d97c5c7477e54cee17cfa6459190610da0643a05),
plugin version 2.6.99 (skill metadata 2.6), under the MIT license, copyright 2025 Cathryn Lavery.
[UPSTREAM.json](../scripts/vendor/diagram_design/UPSTREAM.json) records the exact revision and
helper hashes; [LICENSE](../scripts/vendor/diagram_design/LICENSE) preserves the license text.
The five helper files are unmodified. Upstream rejects all non-fragment CSS URLs, including offline data fonts. The first-party `verify_diagram_design.py` adapter verifies exact font/helper hashes, removes only the exact audited font declarations from scratch copies, then runs upstream skin and accessibility checks unchanged; geometry checks the original files and browser QA checks the real embedded-font output. The [repository-local style guide](../scripts/vendor/diagram_design/skills/diagram-design/references/style-guide.md)
is explicitly adapted for Phaze. The SVG adapter restores the exact shared local stylesheet and embedded font declarations after
upstream's CSS filtering (which otherwise omits root sizing and eyebrow styling); `--system-fonts` alone does not embed fonts.

Narrow third-party exclusions preserve these upstream bytes from formatting and type rewrites;
the asset gate verifies their hashes instead. First-party renderer/verifier code remains under
normal Ruff and strict mypy checks. Do not edit a helper silently; update its pin, license and
integrity record together after review.

Dated planning, spikes, accepted decisions and measurements retain their provenance and original
Mermaid where applicable. They are not rewritten as current design or pulled into the maintained
renderer. The optional `scripts/check_historical_mermaid.py` API retains the bounded lifecycle
checks and pinned CLI for deliberate reproduction of explicitly supplied historical sources.
