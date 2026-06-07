---
status: complete
---

# Quick Task 260606-qgu — Fix flaky CDN SRI test (Tailwind)

## Outcome

`tests/test_base_html_sri.py::test_cdn_sri_hashes_match_served_content` failed intermittently/persistently in CI for the Tailwind `<script>`. Root-caused and fixed by **self-hosting the audited Tailwind build** — no SRI weakening, deterministic delivery.

## Investigation (two rejected approaches before the real fix)

1. **Multi-hash SRI** (original plan) — list both the local hash (`d5Pc0U2…`) and the CI-edge hash (`AIH1kL7…`) in `base.html`. **Rejected:** an automated security review flagged it HIGH "Weakened SRI (bypass)" — pinning an un-audited second body weakens the integrity guarantee. Correct.
2. **`Accept-Encoding: identity` in the test** — hypothesis was that CI hashed compressed bytes. **Rejected:** CI still served `AIH1kL7…` with identity, and that hash can't be reproduced locally via any encoding. So it's a genuinely different *body*, not a compression artifact.

## Root cause

jsDelivr **minifies the bare package URL** `@tailwindcss/browser@4.3.0` on the fly (file header: "Minified by jsDelivr using Terser v5.39.0"). Different jsDelivr edges run different Terser versions → **different bytes for the same versioned URL across edges**. So a single SRI hash can never match everywhere, and SRI could block the stylesheet for a client routed to a divergent edge.

## Fix (self-host)

- Vendored the audited build `@tailwindcss/browser@4.3.0/dist/index.global.min.js` (the explicit pre-built file, `sha384-d5Pc0U2…`, 273477 bytes) to `src/phaze/static/vendor/tailwindcss-browser-4.3.0.min.js`.
- `base.html` now loads it same-origin from `/static/vendor/...` (no SRI/crossorigin needed). The app already mounts `/static` (`main.py:137`); the wheel/Docker ship `src/phaze/static/`.
- `.pre-commit-config.yaml`: excluded `src/phaze/static/vendor/` from end-of-file-fixer / mixed-line-ending / trailing-whitespace so the vendored bytes stay identical to upstream (verified post-commit: still `d5Pc0U2…`).
- The remaining CDN scripts (htmx, htmx-sse, alpine) are explicit pre-built files (byte-stable) and keep their SRI hashes; the SRI tests still guard them. The earlier `Accept-Encoding: identity` test change is kept (deterministic for those).

Benefits: deterministic CI, no per-edge SRI risk, and the admin UI's core stylesheet works on an isolated/private homelab network with no internet.

## Verification

- `uv run pytest tests/test_base_html_sri.py -m "" -q` → 3 passed (now scoped to htmx/htmx-sse/alpine; Tailwind is same-origin so it drops out of the CDN-SRI checks)
- `uv run ruff check .` / `uv run mypy .` clean; frozen-SHA pre-commit green, no `--no-verify`
- Vendored file sha384 == audited `d5Pc0U2…` after commit (hook exclude verified)

## Files

- `src/phaze/static/vendor/tailwindcss-browser-4.3.0.min.js` (new, vendored)
- `src/phaze/templates/base.html` (CDN → self-hosted)
- `.pre-commit-config.yaml` (exclude vendor dir from whitespace/EOF hooks)
- `tests/test_base_html_sri.py` (Accept-Encoding: identity — kept for remaining CDN scripts)
