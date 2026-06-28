# Phase 55: Routing, state & ledger integration (the live seam) - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-06-28
**Phase:** 55-routing-state-ledger-integration-the-live-seam
**Areas discussed:** K8s branch point, cloud_target selector + master toggle, Backfill trigger & ledger scoping, cloud_phase column + admission cards

---

## K8s branch point in stage_cloud_window

| Option | Description | Selected |
|--------|-------------|----------|
| Branch at both stage + post-push | a1→push_file / k8s→s3_upload at stage; a1→process_file / k8s→submit_cloud_job at the PUSHED callback; reuse PUSHING/PUSHED | ✓ |
| submit_cloud_job does its own S3 staging | stage_cloud_window enqueues submit_cloud_job directly; submit internally stages to S3 | |
| Describe a different split | — | |

**User's choice:** Branch at both stage + post-push (Recommended)
**Notes:** Keeps cloud_target as the only switch; K8s skips the file-server rsync (pod fetches from S3 per Phase 53). → CONTEXT D-01.

---

## cloud_target selector + master toggle

| Option | Description | Selected |
|--------|-------------|----------|
| Toggle gates, target selects; defer hard validation | cloud_burst_enabled stays master gate; cloud_target selects under it; k8s fail-fast deferred to Phase 56 | |
| Add fail-fast validation now | Same gating + pull KDEPLOY-02 k8s validator forward | |
| cloud_target replaces the toggle | Collapse cloud_burst_enabled into cloud_target ('local' == off) | ✓ |

**User's choice:** cloud_target replaces the toggle → follow-up: **Hard replace, no alias**
**Notes:** Flagged the KROUTE-01 wording conflict and the breaking-config implication. User confirmed a hard replace with NO back-compat alias, accepting the in-phase migration of .env/docker-compose/runbook/docs. → CONTEXT D-02. The cloud_burst_enabled-coupled validators get rewritten to cloud_target, pulling the K8s portion of KDEPLOY-02 forward.

### Follow-up: Toggle migration

| Option | Description | Selected |
|--------|-------------|----------|
| Replace, keep deprecated alias | cloud_target authoritative; cloud_burst_enabled maps to it with a deprecation warning | |
| Hard replace, no alias | Remove cloud_burst_enabled entirely; migrate all configs in-phase | ✓ |
| Keep both (revert) | Keep master gate + selector per KROUTE-01 as written | |

**User's choice:** Hard replace, no alias

---

## Backfill trigger & ledger scoping

| Option | Description | Selected |
|--------|-------------|----------|
| Manual dashboard action, ledger-scoped | Operator-initiated "Backfill to K8s"; only analysis_failed ∧ ≥threshold ∧ prior-ledger files | ✓ |
| One-shot CLI command | just/CLI backfill, same ledger-scoping, no UI | |
| Automatic cron backfill | Periodic cron sweep | |

**User's choice:** Manual dashboard action, ledger-scoped (Recommended)
**Notes:** Strong preference for operator control over any automatic sweep — informed by the v4.0.6/v5.0 over-enqueue incidents. → CONTEXT D-03.

---

## cloud_phase column + admission cards

| Option | Description | Selected |
|--------|-------------|----------|
| Reconcile cron writes it; cards in-scope | cloud_phase enum on cloud_job; reconcile _reconcile_one writes it; KROUTE-06 cards ship here | ✓ |
| Reconcile cron writes it; defer the cards | Add column + writer now, defer KROUTE-06 cards | |
| Reconsider vocabulary/writer | Different enum or writer (e.g., submit seeds) | |

**User's choice:** Reconcile cron writes it; cards in-scope (Recommended)
**Notes:** Vocabulary = queued_behind_quota / admitted / running / finished; submit_cloud_job seeds the initial value; KROUTE-06 cards mirror the Phase 54 inadmissible_card pattern. → CONTEXT D-04.

---

## Claude's Discretion

- cloud_phase enum storage form (CHECK varchar vs StrEnum) — follow the CloudJobStatus precedent.
- Whether the cloud_target routing branch lives in the duration-router entry or inside stage_cloud_window — resolve against the live call graph.

## Deferred Ideas

- KROUTE-01 wording amendment (D-02 hard-replaces the master toggle) — surface at /gsd:audit-milestone.
- Phase 56 (KDEPLOY) scope reduction — the K8s fail-fast validator portion of KDEPLOY-02 is absorbed into Phase 55 by D-02.
