# Phase 67: Backend Registry & Config Model - Context

**Gathered:** 2026-07-03
**Status:** Ready for planning

<domain>
## Phase Boundary

Deliver the **declarative config surface** for multi-cloud backends: a `backends:` registry
(id/kind/rank/cap + per-kind connection config) as the single source of truth for which execution
targets exist, per-kind discriminated-union validators (fail-fast at startup), and the REG-05 S3
staging-bucket registry (endpoint/creds/scope). **Config-model-only** — no dispatch, scheduler, or
protocol change lands this phase (those are Phases 68/69).

**Behavior anchor:** the one live deploy (homelab, effectively cloud-off / all-local) must keep
running **unchanged** with zero config edits. This is guaranteed by a zero-config all-local default
(no `backends.toml` present → an implicit local-only registry), **not** by a back-compat shim.

Covers **REG-01..05**. Full requirements in `.planning/REQUIREMENTS.md` (REG section) — but note the
back-compat clauses below are superseded by an operator decision this discussion (see `<decisions>`
"Back-Compat Removal" and `<deferred>` "Requirements/Roadmap edit required").

</domain>

<decisions>
## Implementation Decisions

### Backends config format (REG-01)
- **D-01:** The operator declares the registry in a **TOML config file** (`backends.toml`, array-of-tables),
  parsed via stdlib **`tomllib`** — **zero new dependencies** (no PyYAML). Chosen over a single JSON env
  var and over `env_nested_delimiter` numeric-index for readability/git-diffability across N backends.
- **D-02:** The file is located via a **`PHAZE_BACKENDS_CONFIG_FILE` env-var pointer** (mirrors the
  existing `_FILE` pointer style) with a **conventional default path** (e.g. `/etc/phaze/backends.toml`
  or `./backends.toml` — exact default is planner's call).
- **D-03:** **No file present (and no explicit pointer) → implicit all-local default.** The registry
  resolves to a single `kind=local` backend so the current live deploy needs **zero config**. This is the
  safe no-op and the natural home for BEUI-02's future master "revert-to-all-local" toggle.

### Per-entry secrets (REG-03)
- **D-04:** Each backend/bucket entry binds secrets via **inline mount-path `*_file` fields** in the TOML
  (e.g. `kubeconfig_file = "/run/secrets/homelab-kc"`, `sa_token_file`, `access_key_id_file`,
  `secret_access_key_file`, agent tokens). **Secrets never sit in the TOML body** — always a path to a
  mounted file, scoped per entry.
- **D-05:** **REG-03 reconciliation (do not treat as a violation):** REG-03 says "via the existing
  `<VAR>_FILE` convention." This decision keeps the *file-mounted* spirit but **relocates the pointer**
  from a `PHAZE_*_FILE` env var into a TOML `*_file` field. The existing **env `_FILE` convention still
  governs control-plane secrets** (`database_url`/`redis_url`/`queue_url` and any remaining global secret
  fields). Split: **per-entry registry secrets → inline TOML `*_file` paths; control-plane/global secrets
  → env `_FILE`.**
- **D-06:** The existing read-and-strip whitespace handling carries over to inline-path reads
  (`SECRET_FILE_PRESERVE_WHITESPACE` semantics: strip by default so tokens hash identically; preserve
  verbatim for key material like SSH keys/known_hosts). Load eagerly at config-load → **fail-fast** if a
  referenced mount path is missing/unreadable.

### S3 bucket registry (REG-05)
- **D-07:** Buckets live in the **same `backends.toml`** as a `[[buckets]]` array-of-tables — each with
  `id`, `endpoint_url`, inline `*_file` creds (D-04), and a `scope`.
- **D-08:** Kueue backends bind to their bucket set by **explicit `buckets = ["id", …]` id-list**
  (chosen over scope-implicit selection for auditability). Fail-fast if a referenced bucket id is missing
  or a Kueue backend resolves to an **empty** set.
- **D-09:** **`scope` is a load-bearing invariant, not a label.** Validation enforces **sharing
  cardinality**: `scope="cluster-specific"` → the bucket may be referenced by **at most one** Kueue
  backend (fail-fast if two list it); `scope="shared"` (Internet-reachable/public) → referenceable by
  many. This catches the real footgun: pointing a cloud cluster at a homelab-only bucket it cannot reach.
- **D-10:** Config-model only this phase — bucket *behavior* (deterministic per-file bucket selection
  when a set holds >1, presigning, cleanup scoping) is MKUE-02/04 (Phase 70). The control plane remaining
  the **sole S3 importer/presigner** and pods/agents staying credential-free is preserved and unchanged.

### Back-Compat Removal (supersedes REG-04 + REG-05 back-compat clause) — OPERATOR DECISION 2026-07-03
- **D-11:** **No back-compat shim.** Neither cloud path was ever deployed live — v5.0 OCI A1
  (`cloud_target=a1`) and v6.0 Kueue (`cloud_target=k8s`) rollouts were always deployment-gated and
  deferred. The only deploy that ever ran is `cloud_target=local` (all-local). **Nothing in the wild
  depends on `cloud_target=a1/k8s` or on the flat `s3_*`/`kube_*`/`compute_scratch_dir` config.**
- **D-12:** Phase 67 **removes `cloud_target`, the flat `s3_*`/`kube_*`/`compute_*` `ControlSettings`
  fields, and the three per-target validators** (`_enforce_s3_config_when_k8s`,
  `_enforce_compute_scratch_dir_when_a1`, `_enforce_kube_config_when_k8s`). `backends.toml` is the **sole**
  config surface. This overrides REG-04's "shim / `cloud_target` not removed this milestone" and REG-05's
  "existing single global S3 config back-compat-shims to a one-entry shared bucket" — both are moot.
- **D-13:** Compute/Kueue **connection config moves into the per-entry TOML** (e.g. a Kueue entry carries a
  nested `[backends.kube]` table: kubeconfig_file/namespace/localqueue/image/cpu/mem/…; a compute entry
  carries `agent_ref` + scratch dir). The former top-level flat fields cease to exist.
- **D-14 (scope note — flag for planner):** "Remove `cloud_target` **now** (in 67)" means 67 is
  **additive + removal**, not purely additive. Deleting the field breaks the ~10 call sites that read
  `settings.cloud_target` (`pipeline.py`, `controller.py`, `agent_s3.py`, …) — rewiring them to
  registry-derived reads is work the roadmap had assigned to **Phase 68**, and it **moots Phase 68's
  "byte-identical characterization test" premise** for the a1/k8s paths (never live to be identical
  against). **The 67↔68 boundary and 68's acceptance gate must be revisited at plan-time.** Live all-local
  behavior is still preserved (that is the only behavior that ever ran).

### Derived field-placement split (planner may refine)
- **D-15:** **Topology + connection config + creds + buckets → registry entries.** **Operational tuning
  knobs stay as global `ControlSettings` fields** unless a requirement needs them per-entry:
  `push_max_attempts`, `cloud_submit_max_attempts`, S3 presign TTLs, multipart part-size. Exception:
  `cloud_max_in_flight` **becomes per-backend `cap`** (already an entry field per REG-01; behavior in
  SCHED-02/Phase 69). Per-bucket lifecycle TTL is a per-bucket concern (MKUE-04) — planner decides whether
  it's a `[[buckets]]` field now or Phase 70.

### Locked upstream (not re-litigated here)
- `rank`/`cap` are operator-assigned integers; `kind ∈ {local, compute, kueue}` drives a **pydantic v2
  discriminated union** with per-variant required fields (REG-01/02, research SUMMARY).
- Zero new dependencies — pure application-code refactor on the pinned stack (research SUMMARY).

### Claude's Discretion
- Exact default path for `PHAZE_BACKENDS_CONFIG_FILE`.
- Exact per-entry TOML field names and nested-table shape for `kube`/`compute`/`bucket` config.
- Precise placement of per-bucket lifecycle TTL / presign knobs (registry vs global) within the D-15 rule.
- Discriminated-union validation error-message surfacing (fail-fast wording).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & roadmap (authoritative scope — but see the back-compat supersession)
- `.planning/REQUIREMENTS.md` §REG (REG-01..05) — the backend config registry requirements. **NOTE:**
  REG-04's shim and REG-05's back-compat sentence are **superseded** by D-11..D-14 (no back-compat).
- `.planning/ROADMAP.md` — 2026.7.1 milestone, Phase 67 line + execution discipline (PR-per-phase on a
  worktree branch; dependency-strict 67→71). The 67/68 boundary is affected by D-14.

### Design spine
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` — locked design (PR #182).
  - §4.1 Backend config registry (illustrative `backends:` shape; YAML is illustrative only — we use TOML).
  - §6 Non-goals / §7 Deferred-to-plan-time — this phase resolves the "exact `backends:` schema and
    validation" + "migration sequencing" deferred items. **§7's "one shared bucket" is superseded** by
    REG-05 + D-07..D-09 (bucket registry with scope).

### Research (deferred-question resolution — read before planning)
- `.planning/research/SUMMARY.md` — confirms zero-new-deps; discriminated union for per-kind validation;
  pydantic-settings complex-field loading; per-entry `_FILE` secret handling recommendations.
- `.planning/research/FEATURES.md`, `.planning/research/PITFALLS.md` — feature/anti-feature table and the
  in-flight-accounting correctness edge (relevant to 68/69, context for 67).

### Existing code to modify
- `src/phaze/config.py` — `ControlSettings` (lines ~400-681): `cloud_target` Literal, `cloud_max_in_flight`,
  the flat `s3_*`/`kube_*`/`compute_scratch_dir` fields, and the three `_enforce_*_when_*` model validators
  — all **removed/replaced** this phase. `BaseSettings._resolve_secret_files` +
  `SECRET_FILE_FIELDS`/`SECRET_FILE_PRESERVE_WHITESPACE` (lines ~69-165) — the whitespace/read semantics
  (D-06) that inline-path reads must mirror.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `BaseSettings._resolve_secret_files` (config.py) — the `mode="before"` validator + strip-vs-verbatim
  whitespace rule to reuse for inline `*_file` reads (D-06). Extend/parallel it rather than fork it.
- pydantic v2 discriminated unions — native per-kind fail-fast validation, replacing the three cross-field
  `_enforce_*_when_*` validators with per-variant required fields (research SUMMARY).
- stdlib `tomllib` — already available on Python 3.14; no new dep for TOML parsing.

### Established Patterns
- All config is `pydantic-settings` `BaseSettings`/`ControlSettings`/`AgentSettings`; env-var + `.env`
  driven; secrets via `<VAR>_FILE`. The TOML file is a **new config surface** for the registry only —
  keep global/control-plane secrets on the existing env `_FILE` path (D-05).
- Fail-fast-at-startup validation posture throughout (bounded `gt/lt` fields, `_enforce_*` validators).
  The new per-kind + per-bucket validation follows the same fail-fast style.

### Integration Points
- `settings.cloud_target` is read at ~10 call sites (`routers/pipeline.py`, `routers/agent_s3.py`,
  `tasks/controller.py`, `tasks/release_awaiting_cloud.py`). Removing the field (D-12) forces these to be
  rewired this phase — the overlap with Phase 68 that D-14 flags.

</code_context>

<specifics>
## Specific Ideas

Concrete TOML shapes the operator endorsed during discussion (illustrative, planner finalizes exact fields):

```toml
# backends.toml — sole config surface; absent file => implicit all-local
[[buckets]]
id = "homelab-minio"
scope = "cluster-specific"                 # ≤1 backend may reference (fail-fast on 2)
endpoint_url = "https://minio.homelab:9000"
access_key_id_file = "/run/secrets/hl-s3-key"
secret_access_key_file = "/run/secrets/hl-s3-sec"

[[backends]]
id = "a1-oci"
kind = "compute"
rank = 10
cap = 1
agent_ref = "oci-a1"

[[backends]]
id = "kueue-homelab"
kind = "kueue"
rank = 10
cap = 4
buckets = ["homelab-minio"]                # explicit id-list bind
kubeconfig_file = "/run/secrets/homelab-kc"
sa_token_file = "/run/secrets/homelab-tok"
# ... namespace / localqueue / image / cpu / mem (nested table, planner's shape)

[[backends]]
id = "local"
kind = "local"
rank = 99
cap = 1
```

</specifics>

<deferred>
## Deferred Ideas

- **Requirements/Roadmap edit required (do this before/at plan-time):** Update `.planning/REQUIREMENTS.md`
  and `.planning/ROADMAP.md` to reflect **no back-compat** — REG-04 becomes "remove `cloud_target`, no
  shim"; REG-05 drops its "single global S3 config back-compat-shims" sentence; the Out-of-Scope row
  "Removing `cloud_target` — the only live deploy depends on it" is deleted (nothing live depends on it).
  Captured here so the planner does **not** rebuild the shim from the stale requirement text. — this phase / roadmap maintenance
- **67↔68 re-sequencing** (D-14): revisit the phase boundary and Phase 68's byte-identical characterization
  test now that `cloud_target` removal + call-site rewire moves into 67. — Phase 68 plan-time
- **Master "revert-to-all-local" toggle** — BEUI-02, Phase 71. The zero-config all-local default (D-03) is
  its structural foundation but the toggle UI/mechanics are out of scope here.
- **N-lane admin UI** generalizing v7.0 Phase 58's 3 cards — BEUI-01, Phase 71.
- **Per-backend reconcile cron cadence split** (compute vs kueue) — SREF-01, deferred (keep single `*/5`).
- **Staleness guard on local** — SREF-02, deferred (rank-99 + cap-1 is sufficient structural protection).

### Reviewed Todos (not folded)
None — no pending todos matched this phase.

</deferred>

---

*Phase: 67-backend-registry-config-model*
*Context gathered: 2026-07-03*
