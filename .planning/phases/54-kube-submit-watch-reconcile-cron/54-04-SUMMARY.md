---
phase: 54-kube-submit-watch-reconcile-cron
plan: 04
subsystem: pipeline-dashboard
tags: [dashboard, htmx, oob, cloud-job, operator-alert, degrade-safe]
requires:
  - "cloud_job.inadmissible column (Plan 02, D-09)"
  - "Phase 34/44 pipeline DAG dashboard + 5s /pipeline/stats OOB poll"
provides:
  - "get_inadmissible_count(session): degrade-safe COUNT(cloud_job WHERE inadmissible) reader"
  - "inadmissible_card.html: OOB warning card, shown only when count > 0"
  - "inadmissible_count seeded into dashboard() + /pipeline/stats contexts"
affects:
  - "src/phaze/services/pipeline.py"
  - "src/phaze/routers/pipeline.py"
  - "src/phaze/templates/pipeline/dashboard.html"
  - "src/phaze/templates/pipeline/partials/stats_bar.html"
tech-stack:
  added: []
  patterns:
    - "Degrade-safe _safe_count reader (never 500s the hot 5s poll)"
    - "OOB hx-swap card outside #pipeline-stats, re-pushed by stats_bar.html (oob=True)"
    - "Conditional banner body gated behind {% if inadmissible_count %} with an always-emitted empty <section> carrier"
key-files:
  created:
    - "src/phaze/templates/pipeline/partials/inadmissible_card.html"
    - "tests/test_services/test_pipeline_counts.py"
    - "tests/test_routers/test_pipeline_inadmissible.py"
  modified:
    - "src/phaze/services/pipeline.py"
    - "src/phaze/routers/pipeline.py"
    - "src/phaze/templates/pipeline/dashboard.html"
    - "src/phaze/templates/pipeline/partials/stats_bar.html"
decisions:
  - "Mirrored the awaiting_cloud_card pattern exactly: the dashboard references the card by its include filename, so the hyphenated id `inadmissible-card` lives in the partial + stats_bar, not literally in dashboard.html — it appears in all three render paths functionally (asserted by the dashboard render test)."
  - "Always emit the empty <section id=\"inadmissible-card\"> carrier even when count == 0 so the OOB swap has a stable target and a previously-shown banner clears once the count returns to 0."
metrics:
  duration: "~12 min"
  completed: "2026-06-28"
  tasks: 2
  files: 7
requirements: [KSUBMIT-04]
---

# Phase 54 Plan 04: Inadmissible Operator Alert Summary

Surfaced the D-06 Inadmissible alert on the Phase 34/44 pipeline DAG dashboard via a degrade-safe `COUNT(cloud_job WHERE inadmissible)` reader and an additive OOB warning card ("K8s Jobs not admitting — check LocalQueue config") shown only when the count is non-zero — healthy `Pending` quota waits stay silent, mirroring the `awaiting_cloud` card pattern.

## What Was Built

### Task 1 — `get_inadmissible_count` reader + router context wiring (TDD)
- Added `get_inadmissible_count(session)` to `services/pipeline.py`, mirroring `get_awaiting_cloud_count`: `_safe_count(session, select(func.count(CloudJob.id)).where(CloudJob.inadmissible.is_(True)), node="inadmissible")`. Degrades to 0 on any DB error (T-54-10) — never raises into the hot 5s poll.
- Imported `CloudJob` into the service (isort-ordered after `analysis`).
- Seeded `inadmissible_count` into BOTH the `dashboard()` context build and the `/pipeline/stats` poll context, using the same service-owns-degrade idiom (no extra router try/except).
- RED commit (`test(54-04)`) proved the import/`ImportError` failure first; GREEN commit (`feat(54-04)`) made the 3 reader tests pass (count==N for N seeded inadmissible rows, ==0 when all admissible, ==0 on forced DB error).

### Task 2 — Inadmissible card partial + dashboard include + OOB stats push
- Created `inadmissible_card.html`: a `<section id="inadmissible-card" {% if oob %}hx-swap-oob="true"{% endif %}>` whose amber warning banner is gated behind `{% if inadmissible_count %}` (D-06 — no body when 0). The integer count + a static string are the only interpolated values (Jinja autoescape; T-54-11).
- Registered the include in `dashboard.html` OUTSIDE `#pipeline-stats`, alongside the existing cloud cards.
- Added the OOB re-push for `#inadmissible-card` in `stats_bar.html`'s `oob_counts` block (rendered with `oob=True`) so the alert stays live on the 5s poll.
- Router test (`test_pipeline_inadmissible.py`) proves: warning copy + id present with an inadmissible row; warning copy ABSENT (carrier still present) with only admissible rows; dashboard still renders on the all-zero path; the /pipeline/stats poll re-pushes the card OOB (`hx-swap-oob="true"` + warning copy).

## Verification

- `uv run pytest tests/test_services/test_pipeline_counts.py tests/test_routers/test_pipeline_inadmissible.py` — 7 passed.
- `uv run pytest tests/test_services/test_pipeline.py tests/test_routers/test_pipeline.py` — 166 passed (no regressions).
- `uv run ruff check .` — clean.
- `uv run mypy src/phaze/services/pipeline.py src/phaze/routers/pipeline.py` — clean.

## Deviations from Plan

None affecting behavior. One pattern-fidelity note: the acceptance criterion suggested `grep -n "inadmissible-card"` would match `dashboard.html` directly. As with the established `awaiting_cloud_card`, the dashboard references the card by its include filename (`inadmissible_card.html`), so the hyphenated id lives in the partial + `stats_bar.html`. The id is present in all three render paths functionally — the dashboard render test asserts `id="inadmissible-card"` in the `/pipeline/` HTML output. Mirroring the existing pattern took precedence over the literal source-grep shape.

## Threat Surface

Both registered threats are satisfied by the implementation:
- **T-54-10 (DoS on the 5s poll)** — `get_inadmissible_count` is degrade-safe via `_safe_count` (returns 0 + rolls back on any DB error); a degrade test asserts it never raises.
- **T-54-11 (info disclosure)** — the alert interpolates only an integer count and a static string through Jinja autoescape; no operator free-text or PII.

No new security surface introduced beyond the planned threat model.

## Known Stubs

None.

## Self-Check: PASSED

- src/phaze/templates/pipeline/partials/inadmissible_card.html — FOUND
- tests/test_services/test_pipeline_counts.py — FOUND
- tests/test_routers/test_pipeline_inadmissible.py — FOUND
- src/phaze/services/pipeline.py (get_inadmissible_count) — FOUND
- Commit 1f7b5f9 (RED) — FOUND
- Commit 653f1bd (Task 1 GREEN) — FOUND
- Commit b2b145c (Task 2) — FOUND
