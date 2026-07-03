# Phase 67: Backend Registry & Config Model - Research

**Researched:** 2026-07-03
**Domain:** Declarative config surface (pydantic v2 discriminated unions + pydantic-settings TOML source + fail-fast startup validation) for a music-alignment control plane
**Confidence:** HIGH

## Summary

Phase 67 is a **config-model-only** refactor of `src/phaze/config.py`. It replaces the single
`cloud_target: Literal["local","a1","k8s"]` selector and its three flat field-groups
(`s3_*`, `kube_*`, `compute_scratch_dir`) plus three `_enforce_*_when_*` validators with a
declarative `backends.toml` registry: a `list[BackendConfig]` validated by a pydantic v2
discriminated union over `kind`, and a `[[buckets]]` S3 staging registry with scope-based
sharing-cardinality invariants. Every capability is already in the pinned stack — **zero new
dependencies**. `tomllib` is stdlib on Python 3.14 [VERIFIED: `uv run python -c "import tomllib"`],
`pydantic 2.13.4` and `pydantic-settings 2.14.2` are installed and expose everything needed
[VERIFIED: `uv run python -c "import pydantic..."`].

The two genuinely load-bearing findings are (1) the exact idioms — pydantic's
`Field(discriminator="kind")` union with per-variant `model_validator(mode="after")` that
references `self.id` for id-tagged fail-fast messages, and pydantic-settings'
`TomlConfigSettingsSource` located via the `PHAZE_BACKENDS_CONFIG_FILE` env pointer with
absent-file → implicit-local; and (2) the **D-14 call-site boundary**: of the ~11 read sites,
only ~5 are pure config-model swaps (the on/off gate), while ~4 are `a1`/`k8s` **dispatch forks**
that belong to Phase 68's `Backend` protocol and cannot be config-swapped without either leaking
dispatch logic into 67 or leaving a transitional accessor.

**Primary recommendation:** Model the registry as a container `model_validator(mode="after")` on
`ControlSettings` (cross-entry bucket-cardinality + empty-registry checks) over a
`list[Annotated[LocalBackend | ComputeBackend | KueueBackend, Field(discriminator="kind")]]`,
locate/parse `backends.toml` via a `TomlConfigSettingsSource` fed by the env pointer, resolve
inline `*_file` secrets with a NEW per-submodel before-validator that **reuses a shared
strip-vs-verbatim whitespace helper** factored out of the existing `_resolve_secret_files`, and
rewire ONLY the on/off gate to a registry-derived `cloud_enabled` boolean — deferring the `a1`/`k8s`
dispatch-fork rewire to Phase 68 behind a documented transitional accessor so the tree stays green.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Registry declared in a **TOML file** (`backends.toml`, array-of-tables), parsed via
  stdlib `tomllib` — zero new deps (no PyYAML). Chosen over single JSON env var and over
  `env_nested_delimiter` numeric-index for readability/git-diffability.
- **D-02:** File located via a **`PHAZE_BACKENDS_CONFIG_FILE` env-var pointer** with a conventional
  default path (exact default is planner's call).
- **D-03:** No file present (and no explicit pointer) → **implicit all-local default** (single
  `kind=local` backend). Zero-config no-op; structural foundation for BEUI-02's future
  revert-to-all-local toggle.
- **D-04:** Each backend/bucket entry binds secrets via **inline mount-path `*_file` fields** in the
  TOML (`kubeconfig_file`, `sa_token_file`, `access_key_id_file`, `secret_access_key_file`, agent
  tokens). Secrets never sit in the TOML body — always a path to a mounted file, scoped per entry.
- **D-05:** REG-03 reconciliation (NOT a violation): keeps the file-mounted spirit but relocates the
  pointer from a `PHAZE_*_FILE` env var into a TOML `*_file` field. **Split: per-entry registry
  secrets → inline TOML `*_file` paths; control-plane/global secrets (`database_url`/`redis_url`/
  `queue_url`) → env `_FILE`.**
- **D-06:** Existing read-and-strip whitespace handling carries over to inline-path reads
  (`SECRET_FILE_PRESERVE_WHITESPACE` semantics: strip by default; preserve verbatim for key
  material). Load eagerly at config-load → **fail-fast** if a referenced mount path is
  missing/unreadable.
- **D-07:** Buckets live in the **same `backends.toml`** as `[[buckets]]` array-of-tables — each with
  `id`, `endpoint_url`, inline `*_file` creds, and a `scope`.
- **D-08:** Kueue backends bind their bucket set by **explicit `buckets = ["id", …]` id-list** (chosen
  over scope-implicit for auditability). Fail-fast if a referenced bucket id is missing or a Kueue
  backend resolves to an **empty** set.
- **D-09:** **`scope` is a load-bearing invariant.** Enforce sharing cardinality:
  `scope="cluster-specific"` → referenced by **at most one** Kueue backend (fail-fast on two);
  `scope="shared"` → referenceable by many.
- **D-10:** Config-model only this phase — bucket *behavior* (per-file bucket selection, presigning,
  cleanup scoping) is MKUE-02/04 (Phase 70). Control plane stays sole S3 importer/presigner;
  pods/agents stay credential-free (unchanged).
- **D-11:** **No back-compat shim.** Neither cloud path was ever deployed live; only
  `cloud_target=local` (all-local) ever ran. Nothing in the wild depends on `a1`/`k8s` or the flat
  `s3_*`/`kube_*`/`compute_scratch_dir` config.
- **D-12:** Phase 67 **removes** `cloud_target`, the flat `s3_*`/`kube_*`/`compute_*` fields, and the
  three per-target validators. `backends.toml` is the **sole** config surface. Overrides REG-04's
  shim and REG-05's back-compat sentence — both moot.
- **D-13:** Compute/Kueue connection config moves into the per-entry TOML (Kueue entry carries nested
  kube config: kubeconfig_file/namespace/localqueue/image/cpu/mem; compute entry carries
  `agent_ref` + scratch dir). Former top-level flat fields cease to exist.
- **D-14 (scope note — flag for planner):** Removing `cloud_target` **now** makes 67 additive **+
  removal**. Deleting the field breaks the ~10 `settings.cloud_target` call sites; rewiring them was
  Phase 68 work, and it **moots Phase 68's byte-identical characterization-test premise** for the
  a1/k8s paths. **The 67↔68 boundary and 68's acceptance gate must be revisited at plan-time.**
  Live all-local behavior is still preserved.
- **D-15:** **Topology + connection config + creds + buckets → registry entries. Operational tuning
  knobs stay global `ControlSettings` fields** unless a requirement needs them per-entry
  (`push_max_attempts`, `cloud_submit_max_attempts`, S3 presign TTLs, multipart part-size).
  Exception: `cloud_max_in_flight` **becomes per-backend `cap`** (behavior in SCHED-02/Phase 69).
- **Locked upstream:** `rank`/`cap` are operator-assigned integers; `kind ∈ {local, compute, kueue}`
  drives a pydantic v2 discriminated union with per-variant required fields. Zero new dependencies.

### Claude's Discretion
- Exact default path for `PHAZE_BACKENDS_CONFIG_FILE`.
- Exact per-entry TOML field names and nested-table shape for `kube`/`compute`/`bucket` config.
- Precise placement of per-bucket lifecycle TTL / presign knobs (registry vs global) within D-15.
- Discriminated-union validation error-message surfacing (fail-fast wording).

### Deferred Ideas (OUT OF SCOPE)
- **Requirements/Roadmap edit** to reflect no-back-compat (REG-04 → "remove `cloud_target`, no shim";
  drop REG-05 shim sentence; delete the Out-of-Scope "Removing `cloud_target`" row). Do this
  before/at plan-time so the planner does not rebuild the shim from stale requirement text.
- **67↔68 re-sequencing** (D-14) — Phase 68 plan-time.
- **Master revert-to-all-local toggle** — BEUI-02, Phase 71 (D-03 is its foundation).
- **N-lane admin UI** — BEUI-01, Phase 71.
- **Per-backend reconcile cron cadence split** — SREF-01, deferred.
- **Staleness guard on local** — SREF-02, deferred (rank-99 + cap-1 is sufficient).
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REG-01 | Operator declares `backends:` — each with `id`/`kind`/`rank`/`cap` — as single source of truth, replacing the 3-value `cloud_target` Literal | Standard Stack (discriminated union), Pattern 1 (registry model), Pattern 2 (TOML source) |
| REG-02 | Each entry validated per-kind at startup with fail-fast (kueue→kube config, compute→agent_ref, local→neither), consolidating the three `_enforce_*_when_*` validators into one per-entry discriminated-union validator | Pattern 3 (per-variant `model_validator` with `self.id`), Code Example "Per-variant fail-fast" |
| REG-03 | Per-backend secrets load via file-mounted `<VAR>_FILE` convention, scoped per entry (reconciled by D-04/D-05 to inline TOML `*_file` paths) | Pattern 4 (inline `*_file` before-validator + shared whitespace helper), Don't Hand-Roll |
| REG-04 | `cloud_target` + flat fields + three validators **removed**, no shim; absent config → implicit all-local; resolved-registry logged at startup (id/kind/rank/cap only); empty registry fails fast; ~10 call sites rewired to registry-derived reads | Pattern 5 (implicit-local default), Call-Site Rewire Map (D-14), Validation Architecture |
| REG-05 | S3 staging-bucket registry — buckets with endpoint/creds/scope (shared/public vs cluster-specific); Kueue backends assigned a bucket set; fail-fast on non-empty/reachable set + cardinality (cluster-specific ≤1 backend, shared many) | Pattern 6 (container `model_validator` cross-entry cardinality), Code Example "Bucket cardinality" |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Parse `backends.toml` → typed models | Config layer (`config.py`) | — | Config is the single validated surface; no runtime/DB tier involved |
| Per-kind fail-fast validation | Config layer (pydantic submodels) | — | Discriminated union validates at construction, before any I/O |
| Cross-entry bucket cardinality | Config layer (container validator) | — | Registry-level invariant; only the assembled registry can see it |
| Inline `*_file` secret read | Config layer (before-validator) | Filesystem (mount) | Eager fail-fast read of operator-mounted secret paths |
| Locate config file via env pointer | Config layer (settings source) | Process env | Mirrors existing `_FILE` env-pointer style |
| On/off routing gate | Config layer (derived property) → read by API/Task tiers | — | 67 provides the derived boolean; dispatch stays in API/Task tiers (68/69) |
| a1/k8s dispatch forks | **API/Task tiers (Phase 68 `Backend` protocol)** | Config layer (transitional accessor) | NOT config-model-only — see D-14 boundary risk below |

## Standard Stack

Zero new dependencies. Every library is already pinned, installed, and current.

### Core
| Library | Version (installed) | Purpose | Why Standard |
|---------|--------------------|---------|--------------|
| pydantic | 2.13.4 | Discriminated union, per-entry + container validators | Native `Field(discriminator=...)` fail-fast per-variant validation; already the project's config engine [VERIFIED: installed] |
| pydantic-settings | 2.14.2 | `BaseSettings`, `TomlConfigSettingsSource`, `settings_customise_sources` | Blessed idiom for file-backed settings + env precedence; already `ControlSettings`/`AgentSettings` base [VERIFIED: installed] |
| tomllib | stdlib (Python 3.14.5) | Parse `backends.toml` array-of-tables | Stdlib since 3.11; `TomlConfigSettingsSource` uses it on 3.11+ (no `tomli` dep on 3.14) [VERIFIED: `import tomllib` succeeds] |

### Supporting (already in use, no change)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| python-dotenv | >=1.2.2 | `.env` merge for env `_FILE` control-plane secrets (D-05) | Unchanged — global secrets stay on this path |
| structlog | (in stack) | Resolved-registry startup log line (id/kind/rank/cap only) | REG-04 startup-log requirement |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `TomlConfigSettingsSource` | Explicit `tomllib.load()` in a `model_validator(mode="before")` | Explicit loader fits the house `_resolve_secret_files` style and makes the env-pointer + absent-file logic fully visible; the settings-source idiom is more "blessed" but hides file location inside `settings_customise_sources`. Both are valid — see Pattern 2. |
| `Field(discriminator="kind")` | Three separate `_enforce_*_when_*` validators (status quo) | The discriminated union IS the consolidation REG-02 mandates — reject the status quo. |
| Inline TOML `*_file` | pydantic-settings `secrets_dir` / `NestedSecretsSettingsSource` | Would fork the established convention and can't express per-entry paths; SUMMARY explicitly rejects it. |
| PyYAML registry | TOML via stdlib | PyYAML is a new dep; violates zero-new-deps (D-01). |

**Installation:** None — zero new dependencies (D-01/SUMMARY). No `uv add` in this phase.

**Version verification:** [VERIFIED: `uv run python`] pydantic 2.13.4, pydantic-settings 2.14.2,
Python 3.14.5, `import tomllib` succeeds. All three are simultaneously the pinned floor and the
latest release (per milestone SUMMARY) — cooldown-safe, no churn.

## Package Legitimacy Audit

**Not applicable — zero new external packages installed this phase (D-01, SUMMARY confirmed
zero-new-deps).** All libraries used (`pydantic`, `pydantic-settings`, `tomllib`, `python-dotenv`,
`structlog`) are already present in `pyproject.toml` and vetted in prior phases. slopcheck /
registry verification is unnecessary — no install step exists in this phase's plan.

## Architecture Patterns

### System Architecture Diagram

```
                     PHAZE_BACKENDS_CONFIG_FILE (env pointer, D-02)
                                 │
                                 ▼
      ┌───────────────────────────────────────────────────┐
      │  ControlSettings construction (config.py)          │
      │                                                     │
      │  1. settings_customise_sources / before-validator   │
      │     ── locate path (pointer → conventional default) │
      │     ── absent file? ──► inject NOTHING (D-03)        │
      │     ── present? ──► tomllib.load → {backends, buckets}│
      │                                 │                    │
      │                                 ▼                    │
      │  2. Field validation                                 │
      │     backends: list[Annotated[Local|Compute|Kueue,    │
      │                     Field(discriminator="kind")]]    │
      │     buckets:  list[BucketConfig]                      │
      │        │                                             │
      │        ├─ per-variant model_validator(after):        │
      │        │    kueue→kube cfg / compute→agent_ref        │
      │        │    (raises f"backend {self.id!r}: …")  ─────┼──► ValidationError
      │        │                                             │    (fail-fast, id-tagged)
      │        └─ per-submodel before-validator:             │
      │             read inline *_file paths (D-04/D-06)      │
      │             strip|verbatim via shared helper   ──────┼──► ValidationError
      │                                 │                    │    (missing/unreadable path)
      │                                 ▼                    │
      │  3. container model_validator(after) on ControlSettings│
      │     ── empty registry? ──► fail fast (REG-04)         │
      │     ── no backends configured? ──► synthesize local   │
      │        (implicit all-local, D-03)                    │
      │     ── bucket-id map; kueue.buckets refs resolve?     │
      │        empty set? ──► fail fast (D-08)                │
      │     ── scope cardinality: cluster-specific ≤1 kueue   │
      │        (D-09) ──► fail fast on 2                      │
      │                                 │                    │
      │  4. log resolved registry (id/kind/rank/cap ONLY)    │──► startup log (no secrets)
      └───────────────────────────────────────────────────┘
                                 │
                                 ▼
       derived reads: settings.cloud_enabled (on/off gate)  ──► routers/tasks (67 rewire)
       [a1/k8s dispatch forks ──► Phase 68 Backend protocol]
```

### Recommended Project Structure
```
src/phaze/
├── config.py                    # ControlSettings: remove cloud_target + flat fields + 3 validators;
│                                #   add backends/buckets fields, container validator, log line,
│                                #   derived cloud_enabled property, transitional accessor (D-14)
├── config_backends.py  (NEW,    # BackendConfig union submodels (Local/Compute/Kueue),
│   planner names it)            #   BucketConfig, per-variant + inline-*_file validators,
│                                #   shared strip/verbatim whitespace helper (factored from config.py)
```
Splitting the submodels into a new module keeps `config.py` from ballooning and gives the tests a
clean import target. The whitespace helper (`_read_secret_file(path, *, preserve: bool)`) should live
where BOTH `config.py`'s `_resolve_secret_files` and the new inline-`*_file` validators can import it.

### Pattern 1: Discriminated-union registry field
**What:** `backends` is a `list` of an `Annotated[... , Field(discriminator="kind")]` union.
**When to use:** REG-01/02 — per-kind variants with different required fields.
```python
# Source: Context7 /pydantic/pydantic — docs/concepts/unions.md (discriminated union w/ Literal tag)
from typing import Annotated, Literal
from pydantic import BaseModel, Field

class LocalBackend(BaseModel):
    kind: Literal["local"]
    id: str
    rank: int
    cap: int

class ComputeBackend(BaseModel):
    kind: Literal["compute"]
    id: str
    rank: int
    cap: int
    agent_ref: str            # REQUIRED for compute (REG-02)
    scratch_dir: str | None = None   # was ControlSettings.compute_scratch_dir (D-13)

class KueueBackend(BaseModel):
    kind: Literal["kueue"]
    id: str
    rank: int
    cap: int
    kube: KubeConfig          # REQUIRED nested table for kueue (REG-02, D-13)
    buckets: list[str] = Field(default_factory=list)   # id-list bind (D-08)

BackendConfig = Annotated[
    LocalBackend | ComputeBackend | KueueBackend,
    Field(discriminator="kind"),
]
```
A missing per-variant field surfaces natively as `backends.<index>.<kind>.<field>  Field required`
(index-tagged). To get the **entry `id`** in the message (REG-02 fail-fast requirement), add a
per-variant `model_validator` (Pattern 3) rather than relying on the index path.

### Pattern 2: Locate + parse `backends.toml` (two viable idioms)
**What:** Load the array-of-tables file located by the env pointer, feed `backends`/`buckets`.
**Idiom A — `TomlConfigSettingsSource` (blessed):**
```python
# Source: Context7 /pydantic/pydantic-settings — docs/index.md "Load Settings from TOML File"
from pydantic_settings import BaseSettings, TomlConfigSettingsSource, SettingsConfigDict

class ControlSettings(BaseSettings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        path = os.environ.get("PHAZE_BACKENDS_CONFIG_FILE", "/etc/phaze/backends.toml")
        # TomlConfigSettingsSource yields {} when the path does not exist (no raise) → D-03 no-op.
        toml_src = TomlConfigSettingsSource(settings_cls, toml_file=Path(path))
        return (init_settings, env_settings, dotenv_settings, file_secret_settings, toml_src)
```
**Idiom B — explicit loader in a `model_validator(mode="before")` (house style):** mirrors the
existing `_resolve_secret_files` before-validator that already reaches into env/.env and injects into
`data`. Read the pointer, `tomllib.load` if the file exists, inject `data["backends"]`/`data["buckets"]`.
Absent file → inject nothing → the field `default_factory` synthesizes implicit-local (Pattern 5).

**Recommendation:** Idiom B fits the codebase's established injection pattern and keeps the env-pointer
+ absent-file + implicit-local logic in one visible place; Idiom A is more idiomatic pydantic-settings
but resolves the path inside a classmethod and layers a second source. Planner picks; both are HIGH
confidence. **Pitfall:** with Idiom A, `env_nested_delimiter`/complex-field JSON parsing does NOT apply
to a TOML source (the source hands native lists) — do not also set a JSON env var for `backends` or the
two sources will fight on precedence.

### Pattern 3: Per-variant fail-fast with the entry id
**What:** Each submodel validates its own required fields and raises with `self.id`.
```python
from pydantic import model_validator
from typing_extensions import Self

class KueueBackend(BaseModel):
    ...
    @model_validator(mode="after")
    def _require_kube(self) -> Self:
        if self.kube is None:                        # belt-and-suspenders w/ required typing
            raise ValueError(f"backend {self.id!r} (kind=kueue) requires a [kube] config table")
        return self
```
This replaces `_enforce_s3_config_when_k8s` / `_enforce_kube_config_when_k8s` /
`_enforce_compute_scratch_dir_when_a1` (config.py:615-681) with per-variant, id-tagged checks —
the REG-02 consolidation, and the fail-fast-with-offending-id the CONTEXT asks for.

### Pattern 4: Inline `*_file` secret read (D-04/D-06) — parallel, not extend
**What:** Read the file at the path given by a sibling TOML field; populate the secret field;
strip-vs-verbatim per a preserve-set; fail-fast on missing/unreadable.
**Key nuance:** this is a **different resolution mechanism** from the existing env `<VAR>_FILE`
convention. `_resolve_secret_files` (config.py:90-148) reads env var `<ALIAS>_FILE` when the direct
env var is unset. Here the path is a **TOML field value** (`kubeconfig_file = "/run/secrets/..."`),
not an env var. So DO NOT extend `_resolve_secret_files` — instead **factor its strip/verbatim rule
into a shared helper** and write a per-submodel before-validator that consumes TOML field paths.
```python
def _read_secret_file(path: str, *, preserve_whitespace: bool) -> str:
    try:
        contents = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"secret file {path!r} could not be read: {exc}") from exc
    return contents if preserve_whitespace else contents.strip()  # D-06 semantics
```
The preserve-set for inline reads: key material (kubeconfig, SSH-style keys) preserved verbatim;
tokens/access-keys stripped so a heredoc newline hashes identically (mirrors
`SECRET_FILE_PRESERVE_WHITESPACE`, config.py:88/709). Load **eagerly** at construction so a missing
mount path fails fast at startup (D-06), not at first dispatch.

### Pattern 5: Implicit all-local default (D-03)
**What:** No file / no backends configured → single `LocalBackend`.
```python
def _default_local_registry() -> list[BackendConfig]:
    return [LocalBackend(kind="local", id="local", rank=99, cap=1)]

class ControlSettings(BaseSettings):
    backends: list[BackendConfig] = Field(default_factory=_default_local_registry)
```
**Caution:** a `default_factory` only fires when the key is entirely absent. If the TOML source injects
`backends = []` (empty array present in file), the default does NOT fire — the container validator
must then either (a) synthesize local, or (b) fail fast as "empty registry" (REG-04). Decide which:
recommended — **absent** key → implicit local (D-03 zero-config); **present-but-empty** → fail fast
(operator wrote a file that resolves to nothing — the Phase-30 silent-wedge failure mode). Test both.

### Pattern 6: Container-level cross-entry validation (REG-05, D-08/D-09)
**What:** A `model_validator(mode="after")` on `ControlSettings` that sees the whole registry.
```python
# Source: Context7 /pydantic/pydantic — docs/examples/custom_validators.md (outer model validator)
@model_validator(mode="after")
def _validate_registry(self) -> Self:
    if not self.backends:
        raise ValueError("backend registry resolved to empty — refusing to start (REG-04)")
    bucket_by_id = {b.id: b for b in self.buckets}          # dup-id check first
    cluster_specific_refs: dict[str, list[str]] = {}
    for be in self.backends:
        if not isinstance(be, KueueBackend):
            continue
        resolved = [bucket_by_id[bid] for bid in be.buckets if bid in bucket_by_id]
        missing = [bid for bid in be.buckets if bid not in bucket_by_id]
        if missing:
            raise ValueError(f"backend {be.id!r} references unknown bucket ids {missing} (D-08)")
        if not resolved:
            raise ValueError(f"backend {be.id!r} (kueue) resolves to an empty bucket set (D-08)")
        for b in resolved:
            if b.scope == "cluster-specific":
                cluster_specific_refs.setdefault(b.id, []).append(be.id)
    for bid, refs in cluster_specific_refs.items():
        if len(refs) > 1:
            raise ValueError(
                f"bucket {bid!r} is scope=cluster-specific but referenced by {len(refs)} "
                f"kueue backends {refs} — at most one allowed (D-09)")
    return self
```

### Anti-Patterns to Avoid
- **Collapsing the three per-target validators into one `!= "local"` gate** — the existing code
  (config.py:615-681) deliberately keeps `_enforce_s3_config_when_k8s`,
  `_enforce_compute_scratch_dir_when_a1`, `_enforce_kube_config_when_k8s` separate. The discriminated
  union replaces them **per-variant** (kueue owns its kube check, compute owns its agent_ref check) —
  do NOT reintroduce a single scalar gate.
- **Leaking a1/k8s dispatch logic into Phase 67** — see D-14 Call-Site Rewire Map. Rewire only the
  on/off gate; keep dispatch forks for Phase 68.
- **Logging the resolved registry with secret material** — the startup log line is id/kind/rank/cap
  ONLY (REG-04). Never interpolate resolved `*_file` contents or the mount paths of secrets.
- **Extending `_resolve_secret_files` for inline TOML paths** — different mechanism (Pattern 4); share
  the whitespace helper, not the env-`_FILE` resolver.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-kind required-field dispatch | Manual `if kind == "kueue": assert ...` chains | `Field(discriminator="kind")` union | Native fail-fast, typed, index+id-tagged errors; REG-02 mandates the consolidation |
| TOML parsing | Regex/hand parser | stdlib `tomllib` | Zero-dep, spec-correct array-of-tables |
| File-backed settings + env precedence | Custom merge | `TomlConfigSettingsSource` (or house before-validator) | Blessed idiom, absent-file → `{}` no raise |
| Strip-vs-verbatim secret read | Re-implement per field | Shared `_read_secret_file` helper factored from `_resolve_secret_files` | One rule, two call paths (env `_FILE` + inline TOML); keeps D-06 semantics identical |
| Cross-entry cardinality | Ad-hoc post-construction check in a router | Container `model_validator(mode="after")` | Fail-fast at construction, before any dispatch tier runs |

**Key insight:** every capability this phase needs is a documented pydantic/pydantic-settings feature
already exercised elsewhere in `config.py`. The only *new* code is the registry schema, the container
validator, and the inline-`*_file` reader — all thin.

## Runtime State Inventory

> Phase 67 is a config-model refactor. It removes env-var names and adds a TOML surface. No datastore
> keys, live-service config, OS-registered state, or build artifacts embed the removed field names.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | **None** — `cloud_target`/`s3_*`/`kube_*` are process config, never persisted to Postgres (verified: no migration or model column references them; `cloud_job` is keyed by `file_id`, not target). | none |
| Live service config | Env vars `PHAZE_CLOUD_TARGET`, `PHAZE_S3_*`, `PHAZE_KUBE_*`, `PHAZE_COMPUTE_SCRATCH_DIR`, `PHAZE_CLOUD_MAX_IN_FLIGHT` in the homelab compose/`.env`. **The one live deploy runs `cloud_target=local` (or unset default=local)**, so removing these leaves it on the implicit-local path (D-03/D-11). No cloud env vars are set in the live deploy. | Update `.env.example` / compose docs to the `backends.toml` surface (BEUI-03 is Phase 71, but 67 removes the fields so `.env.example` entries for them must go). |
| OS-registered state | **None** — no Task Scheduler/systemd/pm2 entries embed these names. | none |
| Secrets/env vars | `PHAZE_S3_*_FILE`, `PHAZE_KUBE_*_FILE` env `_FILE` pointers become inline TOML `*_file` paths (D-05). `database_url`/`redis_url`/`queue_url` `_FILE` stay on the env path (D-05). Not set in the live all-local deploy. | Code rename; live deploy unaffected (cloud secrets never mounted there). |
| Build artifacts | **None** — pure source change; no egg-info/compiled artifact carries these names. | none |

**Net:** the live all-local deploy needs **zero config edits** (D-03 guarantee). Only docs/`.env.example`
maintenance for the removed cloud env vars.

## Call-Site Rewire Map (D-14 boundary analysis) — CRITICAL

Grep-verified ~11 read sites of `cloud_target` and the flat fields [VERIFIED: `grep -rn` on `src/`].
They split into three classes. **Only Class A is a pure config-model swap; Class B is the D-14 risk.**

### Class A — pure on/off gate (Phase 67 clean swap → registry-derived `cloud_enabled`)
| Site | Current read | Replacement |
|------|--------------|-------------|
| `tasks/release_awaiting_cloud.py:126` | `if cfg.cloud_target == "local": return no-op` | `if not cfg.cloud_enabled:` |
| `routers/pipeline.py:395` | `settings.cloud_target != "local"` (→ `_route_discovered_by_duration` bool arg) | `settings.cloud_enabled` |
| `routers/pipeline.py:699` | `settings.cloud_target != "local"` (backfill router bool arg) | `settings.cloud_enabled` |
| `routers/pipeline.py:763` | `if settings.cloud_target == "local":` backfill no-op guard | `if not settings.cloud_enabled:` |

`cloud_enabled` = a derived `@property` on `ControlSettings`: **True iff the registry contains any
non-local backend.** All-local registry → False → byte-identical no-op behavior for the live deploy.

### Class B — a1/k8s DISPATCH forks (NOT config-model-only — Phase 68 `Backend` protocol territory)
| Site | Current read | Why it is NOT a pure swap |
|------|--------------|---------------------------|
| `tasks/release_awaiting_cloud.py:142` | `if cfg.cloud_target == "a1":` GATE-1 compute-agent probe | Forks on target *kind* to select a dispatch precondition (the GATE-1/GATE-2 asymmetry, SUMMARY Landmine L2) |
| `tasks/release_awaiting_cloud.py:177` | `if cfg.cloud_target == "k8s":` S3-vs-rsync staging fork | Selects the *dispatch body* (`_stage_file_to_s3` vs `_enqueue_push_file`) — exactly the `if/elif` BACK-01 removes |
| `routers/agent_s3.py:112` | `if settings.cloud_target == "k8s":` post-staging seam | S3 callback advances state only on the k8s dispatch path |
| `tasks/controller.py:167` | `if cfg.cloud_target == "k8s":` LocalQueue probe gate | Runtime dispatch-availability probe, per-kueue-backend in Phase 70 (MKUE-03) |
| `routers/agent_push.py:121` | `settings.compute_scratch_dir` | The a1 rsync scratch path — a per-compute-backend field now (D-13); the read needs a *specific backend*, which only exists post-`Backend`-protocol |
| `tasks/release_awaiting_cloud.py:128` | `cfg.cloud_max_in_flight` | Becomes per-backend `cap` (D-15); a single global read has no registry equivalent — that IS Phase 69's per-backend accounting |

**The core D-14 finding:** in a single-selector world "the active target" is a scalar, so these forks
read one field. In a registry world there is **no single active target** — that multiplicity is
precisely what Phases 68/69 introduce. There is **no minimal registry-derived read** that replaces a
Class-B fork without either (a) importing the `Backend` protocol/dispatch logic (that is Phase 68), or
(b) leaving a **transitional accessor** that reduces the registry to the legacy scalar answer.

**Recommended resolution (planner decides at plan-time, D-14 says revisit):**
1. Phase 67 introduces `cloud_enabled` and rewires Class A (clean).
2. For Class B, Phase 67 provides a **narrow transitional accessor** on `ControlSettings` — e.g.
   `active_cloud_kind: Literal["compute","kueue"] | None` = the single non-local backend's kind, and
   `active_compute_scratch_dir` / `active_cap` derived from that single entry — **well-defined only
   for the pre-multiplicity ≤1-non-local-backend registry.** This keeps the tree compiling and
   all-local behavior byte-identical (the accessors are never exercised when `cloud_enabled` is False),
   and hands Phase 68 a clean seam to delete when the `Backend` protocol lands. Document each accessor
   as `# TRANSITIONAL — removed in Phase 68 (BACK-01)`.
3. **Alternative** the planner may prefer: pull the Class-B forks (and the `Backend` protocol) forward
   into 67, merging part of 68. This is heavier and re-opens 68's characterization-test premise
   (D-14). Research recommends **against** merging — keep 67 config-model-only with transitional
   accessors, and let 68 revisit its own acceptance gate (its byte-identical premise for a1/k8s is
   already moot per D-14, since those paths were never live to be identical against).

### Class C — presentation read (must hand the template something)
| Site | Current read | Note |
|------|--------------|------|
| `routers/pipeline.py:572` | `"cloud_target": settings.cloud_target` → lane-card templates | Feeds `analyze_workspace.html`, `_lane_card.html`, `backfill_response.html` (Phase 58 D-05 "not configured" labels). **BEUI-01 N-lane UI is Phase 71** — 67 must still provide a value or the template breaks. Minimal: pass a transitional legacy-shaped string (`"local"` when all-local, else `active_cloud_kind`), or trim the template's cloud_target reference. Flag as a template-coupling touch. |

**Templates referencing `cloud_target`** [VERIFIED: grep]: `templates/pipeline/partials/
analyze_workspace.html`, `_lane_card.html`, `backfill_response.html`. Phase 67 either provides a compat
value or minimally edits these — do not let the removal break the dashboard render.

## Common Pitfalls

### Pitfall 1: Class-B dispatch logic leaking into Phase 67
**What goes wrong:** Rewiring the a1/k8s forks to "real" registry dispatch drags the `Backend` protocol
into 67, blowing the config-model-only boundary and re-opening Phase 68's gate.
**Why it happens:** Removing `cloud_target` breaks the forks, and the obvious fix is to reimplement
dispatch against the registry.
**How to avoid:** Use transitional accessors (Call-Site Rewire Map, resolution step 2); mark them
`# TRANSITIONAL — Phase 68`. Verify all-local behavior unchanged.
**Warning signs:** Phase 67 plan tasks importing `services/kube_staging`, `services/s3_staging`, or a
new `Backend`/protocol type; task bodies growing an `if/elif` over backend kind.

### Pitfall 2: `default_factory` vs present-but-empty array
**What goes wrong:** Operator writes `backends = []` (or a file with only `[[buckets]]`); the
`default_factory` does NOT fire (key present), registry is empty, app either wedges or (worse) boots
with no local backend — the Phase-30 "silent, nothing happens" class.
**Why it happens:** `default_factory` only triggers on an absent key, not an empty value.
**How to avoid:** Container validator (Pattern 6) explicitly fails fast on empty resolved registry
(REG-04). Distinguish **absent** (→ implicit local, D-03) from **present-but-empty** (→ fail fast).
Test both fixtures.
**Warning signs:** A test that only checks "no file → local" but never "empty file → fail fast".

### Pitfall 3: id-less validation errors
**What goes wrong:** A kueue entry missing kube config raises `backends.2.kueue.kube Field required` —
the operator sees a list *index*, not the entry `id`, and can't map it in a large file.
**Why it happens:** The discriminated union tags errors by list index, not by a data field.
**How to avoid:** Per-variant `model_validator(mode="after")` that raises with `self.id` (Pattern 3).
**Warning signs:** Error messages containing only `backends.<int>` with no `id`.

### Pitfall 4: Inline `*_file` read confused with env `<VAR>_FILE`
**What goes wrong:** Planner tries to route inline TOML `kubeconfig_file` through the existing
`_resolve_secret_files` / `SECRET_FILE_FIELDS` env machinery — which reads env var `<ALIAS>_FILE`, not
a TOML field value. Secrets never resolve.
**Why it happens:** Both are "file-mounted secrets" and share the whitespace rule.
**How to avoid:** Two mechanisms, one shared whitespace helper (Pattern 4). Inline reads are a NEW
per-submodel before-validator keyed on TOML `*_file` field values.
**Warning signs:** New entries added to `SECRET_FILE_FIELDS` for per-backend secrets.

### Pitfall 5: Startup log leaking secret material
**What goes wrong:** The resolved-registry log line dumps the full model (including resolved
`SecretStr` values or mount paths), exposing tokens in logs.
**Why it happens:** `logger.info("registry", registry=self.backends)` serializes everything.
**How to avoid:** Log a projection: `[{"id", "kind", "rank", "cap"} for each backend]` ONLY (REG-04).
Use `SecretStr` for resolved secret fields so accidental interpolation prints `**********`.
**Warning signs:** Any log call passing a whole backend/bucket model.

### Pitfall 6: Two config sources fighting over `backends`
**What goes wrong:** With Idiom A (TomlConfigSettingsSource) AND an env/JSON value for `backends`, the
source-precedence tuple order silently picks one, masking the other.
**How to avoid:** `backends`/`buckets` come from exactly ONE source (the TOML file). Do not also expose
them as env vars. Keep env for global control-plane fields only (D-05).
**Warning signs:** A `PHAZE_BACKENDS` env var or `env_nested_delimiter` numeric-index override in tests.

## Code Examples

### Discriminated union — native per-variant fail-fast (index-tagged)
```python
# Source: Context7 /pydantic/pydantic — docs/concepts/unions.md
class Model(BaseModel):
    pet: Cat | Dog | Lizard = Field(discriminator='pet_type')
# Model(pet={'pet_type': 'dog'})  -> ValidationError: pet.dog.barks  Field required [type=missing]
```

### Container validator over a list of submodels (cross-entry)
```python
# Source: Context7 /pydantic/pydantic — docs/examples/custom_validators.md
class Organization(BaseModel):
    forbidden_passwords: list[str]
    users: list[User]
    @model_validator(mode='after')
    def validate_user_passwords(self) -> Self:
        for user in self.users:
            if user.password in self.forbidden_passwords:
                raise ValueError(f'... for user {user.username}.')
        return self
```

### TOML source wiring
```python
# Source: Context7 /pydantic/pydantic-settings — docs/index.md "Load Settings from TOML File"
class Settings(BaseSettings):
    model_config = SettingsConfigDict(toml_file='config.toml')
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (TomlConfigSettingsSource(settings_cls),)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `cloud_target: Literal["local","a1","k8s"]` scalar (config.py:406) | `backends: list[BackendConfig]` discriminated-union registry | Phase 67 (this) | Multi-backend source of truth |
| Three `_enforce_*_when_*` model validators (config.py:615-681) | Per-variant `model_validator` on each submodel | Phase 67 | Consolidated per-kind fail-fast (REG-02) |
| Flat `s3_*`/`kube_*`/`compute_scratch_dir` fields + env `_FILE` | Per-entry TOML config + inline `*_file` paths (D-04/D-13) | Phase 67 | Per-backend scoping |
| Back-compat shim (design §4.1, SUMMARY) | **No shim** (D-11/D-12) | Operator decision 2026-07-03 | Removal, not additive — D-14 boundary |

**Deprecated/outdated:**
- Design doc §4.1 "Back-compat shim" bullet and §7 "one shared bucket" — **superseded** by D-11..D-14
  (no shim) and REG-05 + D-07..D-09 (bucket registry with scope). Do not implement the shim.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The live homelab deploy sets no `PHAZE_CLOUD_*`/`PHAZE_S3_*`/`PHAZE_KUBE_*` env vars (runs implicit/explicit `local`) | Runtime State Inventory | If a cloud env var IS set live, removing it changes that deploy's boot — but MEMORY + D-11 both assert only `cloud_target=local` ever ran, so risk is LOW |
| A2 | `TomlConfigSettingsSource` returns `{}` (no raise) for a nonexistent `toml_file` path | Pattern 2 Idiom A | If it raises, Idiom A can't deliver D-03 absent-file no-op → use Idiom B (explicit loader) which the research already recommends. LOW |
| A3 | No Postgres column / migration persists the removed field names | Runtime State Inventory (Stored data) | If some column stores `cloud_target`, a data migration is needed — but `cloud_job` is `file_id`-keyed and no model grep hit these names. LOW |
| A4 | The lane-card templates will render if handed a transitional/compat value for `cloud_target` | Class C | If templates do richer logic on the literal, may need more than a passthrough value — flagged as a template-coupling touch to verify at plan-time. MEDIUM |

**These `[ASSUMED]` items need confirmation before they become locked plan decisions** — especially A1
(verify the live compose/`.env` has no cloud vars) and A4 (open the three templates during planning).

## Open Questions (RESOLVED)

1. **Idiom A vs Idiom B for TOML loading**
   - What we know: both work; Idiom A is blessed, Idiom B fits the house before-validator style.
   - What's unclear: whether the planner wants the env-pointer + absent-file + implicit-local logic
     centralized (Idiom B) or layered as a settings source (Idiom A).
   - Recommendation: Idiom B for visibility and house-style consistency; either is HIGH confidence.
   - **RESOLVED:** Idiom B chosen — the explicit `tomllib` `model_validator(mode="before")` keyed on
     `PHAZE_BACKENDS_CONFIG_FILE` is implemented in Plan 67-02 Task 1 (env-pointer + absent-file
     implicit-local logic centralized in one visible place, house-style consistent).

2. **Transitional accessors vs merging Phase 68 (D-14)**
   - What we know: Class-B forks are dispatch logic; 67 is config-model-only.
   - What's unclear: whether the planner keeps 67 pure (transitional accessors) or absorbs part of 68.
   - Recommendation: keep 67 pure with `# TRANSITIONAL` accessors; let 68 revisit its own gate.
   - **RESOLVED:** 67 kept config-model-only — the `# TRANSITIONAL — Phase 68` accessors are defined in
     Plan 67-02 and consumed by the Class-B rewires in Plans 67-03 / 67-04 / 67-05; no `Backend`
     protocol is pulled forward. Phase 68 revisits its own byte-identical gate (already moot per D-14).

3. **Per-bucket lifecycle TTL / presign knob placement (D-15 discretion)**
   - What we know: D-15 leaves it to the planner (registry `[[buckets]]` field now vs Phase 70).
   - Recommendation: since bucket *behavior* is Phase 70 (D-10), keep TTL/presign as global
     `ControlSettings` knobs this phase unless a per-bucket value is needed for validation — none is.
   - **RESOLVED:** the TTL / presign / multipart knobs stay global `ControlSettings` fields per D-15 —
     explicitly retained by Plan 67-06's removal wave and still read globally by Plan 67-04's staging
     rewire (no per-bucket value is needed for any Phase-67 validation).

## Environment Availability

> Skipped — Phase 67 is a pure config/code change. No external tools, services, or runtimes beyond the
> already-present Python 3.14 + pydantic stack. `import tomllib` verified available.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (config construction is **sync** — no async needed for these tests) |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) — existing |
| Quick run command | `uv run pytest tests/shared/config/ -x` |
| Full suite command | `uv run pytest --cov --cov-report=term-missing` (85% min per CLAUDE.md) |

### Observable startup behaviors to validate (Nyquist Dimension 8)
| Behavior | Observable | Test seam |
|----------|-----------|-----------|
| Zero-config implicit-local | No `PHAZE_BACKENDS_CONFIG_FILE`, no file → `settings.backends == [LocalBackend(id="local", rank=99, cap=1)]`; `cloud_enabled is False` | Construct `ControlSettings()` with `monkeypatch.delenv` on the pointer + `tmp_path` with no file |
| Per-variant fail-fast (id-tagged) | kueue entry missing kube cfg → `ValidationError` whose message contains the entry `id`; same for compute missing `agent_ref` | `tmp_path` TOML fixture + `pytest.raises(ValidationError, match=r"backend 'kueue-x'")` |
| Scope cardinality rejection | Two kueue backends referencing one `scope="cluster-specific"` bucket → `ValidationError` naming the bucket id; `scope="shared"` referenced by two → OK | TOML fixture with two kueue entries sharing a cluster-specific bucket |
| Missing bucket ref / empty set | kueue `buckets=["nope"]` → fail fast; kueue with `buckets=[]` resolved → fail fast | TOML fixtures |
| Missing/unreadable inline `*_file` | `kubeconfig_file="/nonexistent"` → `ValidationError` naming field + path | `tmp_path` fixture pointing at a non-existent mount |
| Strip-vs-verbatim inline read | token file with trailing `\n` → resolved value stripped; kubeconfig/key material → verbatim (newline preserved) | Write `tmp_path` secret files with trailing whitespace; assert resolved values |
| Present-but-empty registry | file with `backends = []` → fail fast "resolved to empty" | TOML fixture |
| Startup log has no secret material | Resolved-registry log line contains id/kind/rank/cap only; no secret contents, no secret mount paths | `caplog` / capture structlog; assert projection keys, assert secret string absent |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| REG-01 | registry field parses id/kind/rank/cap | unit | `uv run pytest tests/shared/config/test_backend_registry.py -x` | ❌ Wave 0 |
| REG-02 | per-variant fail-fast, id-tagged | unit | `uv run pytest tests/shared/config/test_backend_registry.py -k fail_fast -x` | ❌ Wave 0 |
| REG-03 | inline `*_file` read + strip/verbatim + fail-fast | unit | `uv run pytest tests/shared/config/test_backend_secret_files.py -x` | ❌ Wave 0 |
| REG-04 | cloud_target/flat-field removal, implicit-local, empty→fail-fast, startup log, call-site rewire | unit | `uv run pytest tests/shared/config/test_backend_registry.py tests/shared/tasks/test_release_awaiting_cloud.py -x` | ❌ Wave 0 |
| REG-05 | bucket registry scope/cardinality | unit | `uv run pytest tests/shared/config/test_bucket_registry.py -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/shared/config/ -x`
- **Per wave merge:** `uv run pytest --cov --cov-report=term-missing`
- **Phase gate:** Full suite green before `/gsd:verify-work`; 85% coverage floor (CLAUDE.md).

### Wave 0 Gaps
- [ ] `tests/shared/config/test_backend_registry.py` — REG-01/02/04 (parse, per-variant fail-fast,
      implicit-local, empty→fail-fast, log projection)
- [ ] `tests/shared/config/test_bucket_registry.py` — REG-05 (scope/cardinality/missing-ref)
- [ ] `tests/shared/config/test_backend_secret_files.py` — REG-03 (inline `*_file` strip/verbatim/fail-fast)
- [ ] **DELETE/REWRITE existing tests that assert removed fields** [VERIFIED: grep]:
      `tests/shared/config/test_cloud_target.py`, `test_kube_settings.py`, `test_s3_settings.py`;
      and update the ~6 tests that reference `cloud_target`: `tests/shared/core/test_routing_seam.py`,
      `tests/analyze/core/test_staging_cron.py`, `tests/shared/tasks/test_controller_startup_localqueue.py`,
      `tests/shared/routers/test_pipeline.py`, `tests/shared/core/test_enrich_analyze_workspaces.py`,
      `tests/shared/core/test_config_role_split.py`, `tests/agents/routers/test_agent_s3.py`,
      `tests/BUCKETS.md` (doc).
- [ ] No framework install needed — pytest already configured.

## Security Domain

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | **yes** | pydantic v2 discriminated union + container validator; fail-fast at construction. `endpoint_url` must carry the existing `_validate_s3_endpoint_url` http(s)+netloc SSRF guard (config.py:597) **per bucket** now. |
| V6 Cryptography | partial | Secret material stays `SecretStr`; never logged (REG-04 log projection). No hand-rolled crypto. |
| V7 Error/Logging | **yes** | Startup log line is id/kind/rank/cap only — no secret contents, no secret mount paths (Pitfall 5). |
| V2 Authentication | no | No auth surface changes this phase. |
| V4 Access Control | no | No new endpoints. |

### Known Threat Patterns for this phase
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SSRF via operator `endpoint_url` | Tampering | Carry `_validate_s3_endpoint_url` (http(s) + netloc) to each `BucketConfig.endpoint_url` |
| Secret leakage in registry startup log | Information disclosure | Log projection keys only; `SecretStr` masks accidental interpolation |
| Path traversal / arbitrary read via inline `*_file` | Tampering / Info disclosure | Operator-controlled config on a single-user tool; still fail-fast on unreadable path and never echo file contents. (Not a privilege boundary here — the operator already controls the process.) |
| Silent empty/misconfigured registry (Phase-30 class) | Denial of service | Fail-fast on empty resolved registry + resolved-registry startup log (REG-04) |

## Sources

### Primary (HIGH confidence)
- Context7 `/pydantic/pydantic` — `docs/concepts/unions.md` (discriminated union w/ `Field(discriminator=)`,
  custom discriminator errors, nested unions), `docs/examples/custom_validators.md` (outer/container
  `model_validator`), `pydantic/functional_validators.py` (`model_validator(mode="after")`).
- Context7 `/pydantic/pydantic-settings` — `docs/index.md` + `_autodocs/configuration-file-sources.md`
  (`TomlConfigSettingsSource`, `settings_customise_sources`, `toml_file`, multi-file merge).
- Repo reads (direct, grep- and line-verified): `src/phaze/config.py` (ControlSettings, the three
  `_enforce_*` validators, `_resolve_secret_files`, `SECRET_FILE_*`), `src/phaze/tasks/
  release_awaiting_cloud.py`, `src/phaze/routers/pipeline.py`, `src/phaze/routers/agent_s3.py`,
  `src/phaze/tasks/controller.py`, `src/phaze/routers/agent_push.py`.
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` §1-4 (registry shape, illustrative
  TOML, superseded shim/one-bucket bullets).
- `.planning/phases/67-backend-registry-config-model/67-CONTEXT.md` (D-01..D-15), `.planning/REQUIREMENTS.md`
  (§REG), `.planning/research/SUMMARY.md`.
- Tool verification: `uv run python` → pydantic 2.13.4, pydantic-settings 2.14.2, Python 3.14.5,
  `import tomllib` OK; `grep -rn cloud_target|s3_|kube_|compute_scratch_dir|cloud_max_in_flight` on `src/`.

### Secondary (MEDIUM confidence)
- None required — every claim is either Context7-verified or grep/line-verified in the repo.

### Tertiary (LOW confidence)
- None.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — versions verified installed; all idioms Context7-confirmed; zero new deps.
- Architecture / patterns: HIGH — discriminated union, container validator, TOML source all
  Context7-documented and already used in-repo.
- Call-site rewire (D-14): HIGH on the enumeration (grep-verified), MEDIUM on the *recommended
  resolution* (transitional accessors vs merge) — genuinely a planner decision D-14 flags to revisit.
- Pitfalls: HIGH — grounded in the actual `config.py` structure and the project's Phase-30 incident class.

**Research date:** 2026-07-03
**Valid until:** ~2026-08-03 (stable stack; pydantic/pydantic-settings are pinned + cooldown-locked).
