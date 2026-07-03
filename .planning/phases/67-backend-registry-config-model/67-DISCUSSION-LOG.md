# Phase 67: Backend Registry & Config Model - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-07-03
**Phase:** 67-backend-registry-config-model
**Areas discussed:** Backends config format, Per-backend secret binding, S3 bucket registry shape (REG-05), Shim & flat-field coexistence

---

## Backends config format (REG-01)

| Option | Description | Selected |
|--------|-------------|----------|
| Single JSON env var | `PHAZE_BACKENDS='[{...}]'` — zero-dep, one var; fiddly to hand-edit, no inline secrets | |
| TOML/JSON config file | Array-of-tables file read via a config-file source; most readable/git-diffable; stdlib `tomllib`, still zero new pip deps | ✓ |
| `env_nested_delimiter` index | `PHAZE_BACKENDS__0__ID=…` — one var/field, deep-overridable; very verbose, index-error-prone | |

**User's choice:** TOML/JSON config file → **TOML**.
**Follow-ups:**
- File location: **`PHAZE_BACKENDS_CONFIG_FILE` env-var pointer + conventional default** (chosen over a fixed-path-only scheme).
- Encoding: **TOML** (stdlib `tomllib`; chosen over JSON for readability/comments).

---

## Per-backend secret binding (REG-03)

| Option | Description | Selected |
|--------|-------------|----------|
| Derived-from-id convention | Secret env var derived from backend id (`id=kueue-homelab` → `PHAZE_BACKEND_KUEUE_HOMELAB_KUBECONFIG_FILE`); fewest knobs but id becomes load-bearing (rename = re-mount) | |
| Named-secret indirection | Entry names a secret handle → resolved via existing `_resolve_secret_files` (research-recommended; rename-safe; more verbose) | |
| Inline mount-path ref | Entry names the mounted secret file path directly (`kubeconfig_file = "/run/secrets/…"`); simplest mental model; forks the `<VAR>_FILE` env indirection | ✓ |

**User's choice:** Inline mount-path ref.
**Notes:** Reconciled against REG-03 ("via the existing `<VAR>_FILE` convention") — keeps the file-mounted spirit but relocates the pointer into a TOML `*_file` field. Control-plane/global secrets keep the env `_FILE` convention; only per-entry registry secrets use inline paths. Read-and-strip whitespace semantics + fail-fast on missing path carry over.

---

## S3 bucket registry shape (REG-05)

| Option | Description | Selected |
|--------|-------------|----------|
| Same TOML, bind by id-list | `[[buckets]]` in `backends.toml`; kueue backend names `buckets = ["id",…]`; explicit, one file, fail-fast on missing/empty | ✓ |
| Same TOML, bind by scope | Backend selects buckets by scope rather than id; less typing but implicit/harder to audit | |
| Separate buckets.toml | Bucket registry in its own file + pointer; cleaner separation but two files to sync | |

**User's choice:** Same TOML, bind by id-list.
**Follow-up — scope semantics:**

| Option | Description | Selected |
|--------|-------------|----------|
| Enforce sharing cardinality | `cluster-specific` → ≤1 backend may reference (fail-fast on 2); `shared` → many. Scope is a load-bearing invariant | ✓ |
| Informational only | scope is documentary metadata; no cardinality check | |

**User's choice:** Enforce sharing cardinality.

---

## Shim & flat-field coexistence → Back-compat removal

**Clarification from operator (mid-question):** *"We don't need the backwards compat. Neither of the cloud solutions ever were deployed yet."*

This superseded the original coexistence framing entirely. The v5.0 OCI A1 (`cloud_target=a1`) and v6.0 Kueue (`cloud_target=k8s`) rollouts were always deployment-gated and deferred; the only live deploy runs `cloud_target=local`. Nothing in the wild depends on `cloud_target=a1/k8s` or the flat `s3_*`/`kube_*`/`compute_*` config.

Reformulated question — how far to go on removing the selector:

| Option | Description | Selected |
|--------|-------------|----------|
| Remove `cloud_target` now (in 67) | Delete `cloud_target` + flat fields + 3 per-target validators; `backends.toml` sole surface; no file → all-local default. Cleanest; makes 67 additive+removal and overlaps phase-68 call-site rewire | ✓ |
| Registry-only in 67, delete `cloud_target` in 68 | Build registry with all-local default but leave dead `cloud_target` + call-site reads until 68 | |

**User's choice:** Remove `cloud_target` now (in 67).
**Notes:** Requires a REQUIREMENTS.md/ROADMAP.md edit (REG-04 shim, REG-05 back-compat sentence, Out-of-Scope "Removing cloud_target" row all become inaccurate) and a revisit of the 67↔68 boundary + phase-68 byte-identical characterization-test premise. Live all-local behavior is preserved.

---

## Claude's Discretion

- Exact default path for `PHAZE_BACKENDS_CONFIG_FILE`.
- Exact per-entry TOML field names / nested-table shape for `kube`/`compute`/`bucket` config.
- Placement of per-bucket lifecycle TTL and presign/part-size knobs (registry vs global) within the topology-vs-tuning split.
- Discriminated-union / fail-fast validation error-message wording.

## Deferred Ideas

- **REQUIREMENTS.md/ROADMAP.md edit** to reflect no-back-compat (before/at plan-time) — so the planner doesn't rebuild the shim.
- **67↔68 re-sequencing** — revisit phase boundary + phase-68 characterization test.
- Master "revert-to-all-local" toggle — BEUI-02, Phase 71 (built on the zero-config all-local default).
- N-lane admin UI — BEUI-01, Phase 71.
- Per-backend reconcile cron cadence split — SREF-01, deferred.
- Staleness guard on local — SREF-02, deferred.
