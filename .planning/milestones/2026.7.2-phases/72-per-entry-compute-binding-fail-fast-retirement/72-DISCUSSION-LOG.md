# Phase 72: Per-Entry Compute Binding & Fail-Fast Retirement - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-05
**Phase:** 72-per-entry-compute-binding-fail-fast-retirement
**Areas discussed:** agent_ref → Agent key, Config validation strictness, Behavior-preserving proof, 72/73 scope line

---

## agent_ref → Agent key

| Option | Description | Selected |
|--------|-------------|----------|
| Agent.id | Resolve against the PK (constrained slug, existing FK target on files/scan_batches/cloud_job); unambiguous, no collisions | ✓ |
| Agent.name | Resolve against free-form String(128) display name; readable but collidable, not the FK key | |
| Accept either | id-first with name fallback; flexible but ambiguous, more test surface | |

**User's choice:** Agent.id
**Notes:** Matches how `cloud_job.backend_id` already keys per-backend; operator writes the agent's id in `backends.toml`.

---

## Config validation strictness

| Option | Description | Selected |
|--------|-------------|----------|
| Boot fail-fast on duplicate agent_ref; runtime hold on unregistered | Static id-tagged boot guard on duplicate agent_ref; not-yet-registered agent degrades to a runtime hold (absent→False), agents check in dynamically | ✓ |
| Only duplicate check, no missing-agent handling in 72 | Just the duplicate-agent_ref boot guard; defer runtime absence to Phase 73 | |
| No new validation in 72 | Pure retirement; add nothing, defer all validation to Phase 73 | |

**User's choice:** Boot fail-fast on duplicate agent_ref; runtime hold on unregistered
**Notes:** Preserves today's absent-agent behavior and the T-68-05 cron no-op discipline; mirrors the KueueBackend validator style for the duplicate guard.

---

## Behavior-preserving proof

| Option | Description | Selected |
|--------|-------------|----------|
| Golden byte-identical characterization + zero-compute regression | Phase-68 D-01 golden precedent for the ≤1-compute path + explicit all-local regression | ✓ |
| Standard unit + integration coverage | Normal tests to 90% without a dedicated byte-identical golden | |
| You decide | Let planner/researcher pick the proof strategy | |

**User's choice:** Golden byte-identical characterization + zero-compute regression
**Notes:** Honors the project's behavior-preserving culture and the ≥90% coverage floor.

---

## 72/73 scope line

| Option | Description | Selected |
|--------|-------------|----------|
| Keep 72 pure groundwork; defer per-agent widening to 73 | Only retire the >1 raise; keep ≤1 resolution of active_compute_scratch_dir / the /pushed callback byte-identical; agent_push.py untouched | ✓ |
| Introduce the per-entry scratch seam now | Add a per-entry scratch-dir lookup seam in 72; more done now but blurs the behavior-preserving line and touches the /pushed hot path early | |
| You decide | Let research draw the line | |

**User's choice:** Keep 72 pure groundwork; defer per-agent widening to 73
**Notes:** Cleanest behavior-preserving boundary; per-agent scratch/push/reconcile (MCOMP-03/06) lands in Phase 73.

---

## Claude's Discretion

- Exact placement of the duplicate-`agent_ref` validator (container `_validate_registry` vs a submodel-list validator).
- Whether the binding is read as-is from `self.config.agent_ref` or lifted to a typed attribute at construction (mirror `KueueBackend._kube()`).
- Whether `resolved_non_local_kind`'s compute-only branch still returns `"compute"` for N compute (confirm during planning).

## Deferred Ideas

None — discussion stayed within phase scope.
