---
phase: quick-260629-eev
plan: 01
subsystem: docs
tags: [docs, mermaid, cloud-burst, k8s-burst]
requires: []
provides:
  - "docs/cloud-burst.md mermaid flowchart LR architecture diagram"
  - "docs/k8s-burst.md mermaid flowchart LR architecture diagram"
affects:
  - docs/cloud-burst.md
  - docs/k8s-burst.md
tech-stack:
  added: []
  patterns:
    - "mermaid flowchart LR with subgraph host/cluster groupings (matches docs/architecture.md convention)"
key-files:
  created: []
  modified:
    - docs/cloud-burst.md
    - docs/k8s-burst.md
decisions:
  - "Edge node→node routing detail (A1:22, lux:{5432,6379,8000}) folded into the quoted edge labels so the rsync/HTTP routing survives without standalone connector nodes"
  - "PHAZE_CLOUD_TARGET=local note placed as an italic markdown caption below each mermaid block — it is a config note, not topology"
metrics:
  duration: ~6m
  completed: 2026-06-29
requirements: [DOC-MERMAID-01]
---

# Quick 260629-eev: Convert ASCII Architecture Diagrams to Mermaid Summary

Replaced the two ASCII box-drawing "Architecture at a glance" blocks (docs/cloud-burst.md, docs/k8s-burst.md) with lossless `mermaid flowchart LR` diagrams, relocating each `PHAZE_CLOUD_TARGET=local` note to an italic caption below the block.

## What changed

- **docs/cloud-burst.md** — the plain ``` ASCII topology under "## Architecture at a glance" is now a `mermaid flowchart LR` with three subgraphs (`nox (file server)`, `OCI A1 (compute agent)`, `lux (application server)`). The outer frame title `Tailscale tailnet (default-deny grants ACL)` is a `%%` comment line. Edges `rsync over SSH (nox → A1:22)` and `HTTP API + saq_jobs + cache (A1 → lux:{5432,6379,8000})` are quoted pipe-form labels carrying the original routing detail. The `PHAZE_CLOUD_TARGET=local ⇒ … (all-local)` note is now an italic `_…_` line below the block.
- **docs/k8s-burst.md** — same conversion: `lux (application server / control plane)` and `x64 Kueue cluster` subgraphs, with controller-worker children (`s3_staging`, `submit_cloud_job`, `reconcile_cloud_jobs (*/5 cron)`, `LocalQueue probe (startup)`, the `POST /api/internal/agent/analysis/{file_id}` callback) and cluster objects (`ResourceFlavor phaze-cpu`, `ClusterQueue phaze-cq`, `LocalQueue phaze-lq`, `SA/Role/RoleBinding`, `Secret phaze-agent-token`, the suspended batch Job, the one-shot pod) as nodes. Edges `presign PUT/GET`, `kube POST`, `Kueue admits`, and `POST /api/internal/agent/analysis/{file_id} (the ONLY result channel)` are quoted pipe-form labels. Italic caption relocated below the block.

Both conversions are lossless — every host, service, object name, port, and edge phrase from the original ASCII survives verbatim.

## Deviations from Plan

None - plan executed exactly as written.

## Verification Evidence

```
$ grep -c '```mermaid' docs/cloud-burst.md docs/k8s-burst.md
docs/cloud-burst.md:1
docs/k8s-burst.md:1

$ grep -c '[─│┌┐└┘├┤►▶◀▼]' docs/cloud-burst.md docs/k8s-burst.md
docs/k8s-burst.md:0
docs/cloud-burst.md:0          # (grep exits non-zero — no box-drawing chars remain)

$ grep -c 'flowchart LR' docs/cloud-burst.md docs/k8s-burst.md
docs/k8s-burst.md:1
docs/cloud-burst.md:1

$ git diff --stat   (pre-commit)
 docs/cloud-burst.md | 35 ++++++++++++++++++-----------------
 docs/k8s-burst.md   | 46 ++++++++++++++++++++++++++++------------------
 2 files changed, 46 insertions(+), 35 deletions(-)
```

- Edge-label spot checks: `rsync over SSH` (cloud-burst), `the ONLY result channel` (k8s-burst) present.
- Italic captions: `_PHAZE_CLOUD_TARGET=local` present in both files, below the closing ``` of each mermaid block.
- Lossless token sweep: all 14 cloud-burst tokens and all 16 k8s-burst tokens confirmed verbatim (`grep -F`).
- `pre-commit run --files docs/cloud-burst.md docs/k8s-burst.md` — all hooks Passed (no `--no-verify`). Hooks ran again on commit and Passed.

## Commit

- `267109b` — docs: convert ASCII architecture diagrams to mermaid flowcharts (2 files, +46/-35)

## Self-Check: PASSED

- FOUND: docs/cloud-burst.md (mermaid block present)
- FOUND: docs/k8s-burst.md (mermaid block present)
- FOUND: commit 267109b
- git diff --stat (pre-commit) listed ONLY the two target files — no out-of-scope changes
