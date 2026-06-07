---
status: complete
---

# Quick Task 260606-qgu — Fix flaky CDN SRI test

## Outcome

Fixed the intermittently-failing `tests/test_base_html_sri.py::test_cdn_sri_hashes_match_served_content` **without weakening Subresource Integrity**.

## DEVIATION from plan (important)

The PLAN proposed **multi-hash SRI** — listing a second hash (`sha384-AIH1kL7…`, the body served to GitHub's CI runner) alongside the audited `sha384-d5Pc0U2…` in `base.html`. During execution an automated **security review flagged this HIGH: "Weakened Subresource Integrity (SRI bypass)"** — pinning a second, un-audited hash lets more than one body satisfy SRI.

Investigation proved the security review correct and the multi-hash premise wrong:

- The bare jsdelivr URL `@tailwindcss/browser@4.3.0` resolves to the immutable static file `/dist/index.global.min.js`; its canonical (decoded) SHA-384 is **always** `d5Pc0U2…` (stable locally, == bare URL == explicit file).
- `AIH1kL7…` (and the brotli/gzip raw hashes) are **compressed representations** of that same file, not a distinct build. `httpx.get(...).content` returns *compressed* bytes when the client advertises an encoding it can't transparently decode (brotli, when no brotli lib is installed — the CI situation).
- Browsers compute SRI over the **decoded** resource, so a browser always validates against `d5Pc0U2…`. There was **no production risk**, and the single hash is correct.

So the multi-hash approach was discarded (its worktree commits were never merged). `base.html` is unchanged (keeps the single audited hash).

## Actual change

`tests/test_base_html_sri.py` — the live integration test now fetches with `headers={"Accept-Encoding": "identity"}`, so it hashes the same canonical decoded bytes the browser validates. Deterministic across environments regardless of installed compression libs. The https-only guard, algo allowlist, and bounded retry are preserved; real pin-drift (a body matching none of the pinned hash) still fails.

## Verification

- `uv run pytest tests/test_base_html_sri.py -m "" -q` → 3 passed (incl. live fetch)
- `uv run ruff check` / `uv run mypy` → clean
- `base.html` contains 0 occurrences of the compressed-byte hash — single audited hash only; SRI not weakened.

## Files

- `tests/test_base_html_sri.py` (edited)
- `src/phaze/templates/base.html` (intentionally unchanged)
