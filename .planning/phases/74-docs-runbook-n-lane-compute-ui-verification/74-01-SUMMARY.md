---
phase: 74-docs-runbook-n-lane-compute-ui-verification
plan: 01
subsystem: docs
tags: [docs, operator-runbook, multi-compute, cost-tiering, backends]
requires: []
provides:
  - "docs/multi-compute.md — the add-a-2nd+-compute-agent cost-tiered operator guide"
  - "Cross-links into the multi-cloud nav (README index, runbook, configuration, cloud-burst)"
affects:
  - docs/multi-compute.md
  - docs/README.md
  - docs/runbook.md
  - docs/configuration.md
  - docs/cloud-burst.md
tech-stack:
  added: []
  patterns:
    - "gsd-doc-writer marker on line 1 + mermaid-over-ASCII diagram convention"
    - "canonical field table stays in configuration.md; scenario doc links out (D-03)"
key-files:
  created:
    - docs/multi-compute.md
  modified:
    - docs/README.md
    - docs/runbook.md
    - docs/configuration.md
    - docs/cloud-burst.md
decisions:
  - "Unaligned the worked backends.toml key spacing (single-space `key = value`) so the plan's literal `kind = \"compute\"` verify grep matches while keeping the block readable"
  - "cloud-burst.md's stale `Held ≤1 (deferred PROV-01 invariant)` line generalized to `one rank-tiered lane among N` with an explicit 'you may declare more than one compute backend' pointer — de-blocks the 2nd-agent recipe (D-01) without folding the recipe into cloud-burst.md"
  - "Documented PHAZE_CLOUD_AGENT_IMAGE + PHAZE_CLOUD_AGENT_CMD as the x86 override knobs per plan action/acceptance (the compose parametrization itself lands in a later 74 plan; this doc documents the intended operator surface)"
metrics:
  duration: ~12 min
  completed: 2026-07-06
  tasks: 2
  files: 5
---

# Phase 74 Plan 01: Docs, Runbook & N-Lane Compute UI Verification — Multi-Compute Operator Guide Summary

Added `docs/multi-compute.md` — the "now add a 2nd+ compute agent, cost-tiered" operator guide over the shipped Phase 72/73 machinery — with a mermaid rank-tiered drain diagram, a worked mixed arm64/x86 `backends.toml`, a cost-tier rationale table, and a per-agent compose recipe covering the arm64→x86 image/command swap; cross-linked it into the existing multi-cloud nav and generalized the stale ≤1-compute framing in `cloud-burst.md`.

## What Was Built

### Task 1 — `docs/multi-compute.md` (commit `c4a416d3`)
- New dedicated operator doc (D-01/D-02/D-03). Line 1 is the exact `<!-- generated-by: gsd-doc-writer -->` marker; line 2 the H1.
- Sections: intro (positions this as the N-compute guide, links to cloud-burst.md as the single-A1 walkthrough and out to `configuration.md#backend-registry-backendstoml` for the canonical field schema — not restated); a mermaid rank-tiered drain diagram (free arm64 A1 rank 10 → paid x86 rank 20 → local rank 99); a schema-verified worked `backends.toml` with two `kind = "compute"` entries (`a1-arm64` rank 10 cap 2, `x86-spill` rank 20 cap 4, distinct `agent_ref`/`push_host`/`scratch_dir`) plus a `kind = "local"` rank 99 catch; a cost-tier rationale table (tier/rank/cap/cost posture); a per-agent compose recipe using `PHAZE_AGENT_ID` only as the mnemonic `<id>` feeding `PHAZE_AGENT_QUEUE=phaze-agent-<id>` / `agent_ref` (no standalone `AGENT_ID` var), documenting the `PHAZE_CLOUD_AGENT_IMAGE` + `PHAZE_CLOUD_AGENT_CMD` x86 overrides and the co-located-agent scratch/project-name collision (Pitfall 3); and a "Reading the lanes" cross-link reusing runbook.md's RANK/in_flight/cap/offline vocabulary.
- Secret hygiene: `*_FILE` pointers only; no inline tokens/SSH keys/DATABASE_URL.

### Task 2 — Cross-link + de-block single-agent framing (commit `bab4b2fd`)
- `docs/README.md`: added a Multi-Compute Agents row to the Operations index.
- `docs/runbook.md`: added a multi-compute.md pointer to the cross-link block.
- `docs/configuration.md § Backend registry`: added a "worked multi-compute scenario" pointer near the compute backend fields, keeping the section as the canonical field reference.
- `docs/cloud-burst.md`: linked into multi-compute.md and replaced the stale `Held ≤1 (deferred PROV-01 invariant)` assertion with "one rank-tiered lane among N — you may declare more than one `kind="compute"` backend"; cloud-burst.md stays the single-A1 provisioning walkthrough.

## Verification

- Task 1 automated verify: line-1 marker + `configuration.md#backend-registry-backendstoml` + `kind = "compute"` + `PHAZE_AGENT_QUEUE=phaze-agent-` + `mermaid` all present → OK. Two compute entries, one local entry, ranks 10/20/99 present. No inline secrets (`ssh-rsa`/`BEGIN … PRIVATE KEY`/`postgres://…` grep empty). `PHAZE_CLOUD_AGENT_IMAGE` + `PHAZE_CLOUD_AGENT_CMD` documented.
- Task 2 automated verify: `multi-compute.md` linked from README, runbook, configuration, cloud-burst → OK. No residual `held ≤1` / `only one compute` / `single compute agent` assertions in cloud-burst.md.
- `uv run pytest tests/shared/core/test_requirements_traceability.py tests/shared/core/test_docs_ia_current.py` → 15 passed (docs-drift traceability tolerates the in-flight MCOMP-07 `[ ]`+Pending state; docs-IA index guard green with the new doc).
- Pre-commit hooks ran on both commits (no `--no-verify`).

## Deviations from Plan

None affecting scope. One formatting adjustment: the worked `backends.toml` was written column-aligned first, but the plan's literal verify grep is `kind = "compute"` (single-spaced), so the TOML key spacing was unaligned to `key = value` to satisfy the exact-match gate while staying readable. Documented in decisions.

## Known Stubs

None — this is a documentation plan with no code, data sources, or UI wiring.

## Self-Check: PASSED
- FOUND: docs/multi-compute.md
- FOUND: docs/README.md, docs/runbook.md, docs/configuration.md, docs/cloud-burst.md (modified)
- FOUND commit: c4a416d3 (Task 1)
- FOUND commit: bab4b2fd (Task 2)
