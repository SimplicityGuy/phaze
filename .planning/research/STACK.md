# Stack Research

**Domain:** Declarative multi-backend config registry + tiered drain scheduler for a distributed Python analysis pipeline (phaze control plane)
**Researched:** 2026-07-03
**Confidence:** HIGH

## Verdict First

**No new dependencies are needed for this milestone.** Every capability the design calls
for — a validated `list[BackendConfig]` on `ControlSettings`, per-entry `_FILE` secret
support, an internal `Backend` Protocol with three bodies, a rank/cap drain scheduler, and
N-cluster Kueue submission — is already covered by the pinned stack:

- **pydantic v2** (`2.13.4`) — nested `BaseModel` list fields are a first-class, natively
  validated pydantic feature. Per-entry fail-fast validators are ordinary field/model
  validators on the nested model. Zero new lib.
- **pydantic-settings** (`2.14.2`) — a `list[BackendConfig]` is a "complex" settings field
  that loads from env (JSON blob **or** `env_nested_delimiter` numeric-index deep override)
  or a config file, all built-in. The existing project-local `_resolve_secret_files`
  `mode="before"` validator is the established `_FILE` idiom and is the right seam to extend
  for per-entry secrets — that is application code, not a dependency.
- **kr8s** (`0.20.15`) — already supports N clusters: `kr8s.asyncio.api(url=…, kubeconfig=…,
  context=…)` returns a distinct arg-cached client per cluster, and objects bind to a chosen
  client via `Job({…}, api=api)`. No multi-cluster helper library is warranted.
- **aioboto3** (`15.5.0`) — unchanged; one shared S3 bucket across all Kueue clusters,
  control plane stays the sole importer (DIST-01 preserved).

This is a pure **application-code refactor** milestone. The rest of this document records the
evidence behind that verdict and the plan-time integration care points.

## Recommended Stack

### Core Technologies (all already present — no change)

| Technology | Version (pinned floor / installed) | Purpose in this milestone | Why it already suffices |
|------------|-----------|---------|-----------------|
| pydantic | `>=2.13.4` (installed 2.13.4, latest 2026-05-06) | Typed `BackendConfig` submodel + per-entry validators + discriminated union on `kind` | Nested `BaseModel` list fields with per-field validators are core pydantic v2. A `Literal["local","compute","kueue"]` discriminator + `Field(discriminator="kind")` gives fail-fast per-kind validation with clear errors — the exact "per-entry fail-fast validator" the design asks for, replacing the three current per-target `_enforce_*_when_*` validators. |
| pydantic-settings | `>=2.14.2` (installed 2.14.2, latest 2026-06-19) | Load the `backends:` list onto `ControlSettings` from env / config; keep the `_FILE` convention | `list[SubModel]` is treated as a complex field and parsed as JSON from a single env var, or built via `env_nested_delimiter='__'` numeric indices (`PHAZE_BACKENDS__0__ID=a1`), or from a config-file source. The project's bespoke `_resolve_secret_files` before-validator is already the `_FILE` mechanism and extends cleanly to per-entry secrets. |
| kr8s | `>=0.20.15` (installed 0.20.15, latest 2026-01-16) | Per-cluster async kube client for N Kueue backends | `kr8s.asyncio.api(url=/kubeconfig=/context=)` is per-call and arg-cached; objects take an explicit `api=`. One client per backend entry = native multi-cluster. No `kubernetes`/`kubernetes_asyncio` needed. |
| aioboto3 | `>=15.5.0` (installed) | Shared S3 staging bucket across all Kueue clusters | Design locks one shared bucket, control plane sole importer. No per-cluster bucket, so no change to the aioboto3 surface. |
| SAQ + Postgres queue | current | `LocalBackend.dispatch` → `process_file`; compute drain queues | Unchanged; backends wrap existing dispatch paths. |

### Supporting Libraries (already present — used, not added)

| Library | Version | Purpose | When used |
|---------|---------|---------|-------------|
| python-dotenv | `>=1.2.2` | `.env` layer that `_resolution_env` already consults for `<VAR>_FILE` siblings | Already a direct dep; the per-entry `_FILE` extension reuses `_resolution_env` verbatim. |
| `typing.Protocol` (stdlib) | 3.13 | The internal `Backend` protocol (`is_available`/`in_flight_count`/`dispatch`/`reconcile`) | stdlib — the design explicitly scopes this as an **internal** protocol (no third-party plugin loading), so `typing.Protocol` is exactly right. No plugin framework (no `pluggy`, no `stevedore`, no entry-points). |

### Development Tools (no change)

| Tool | Purpose | Notes |
|------|---------|-------|
| ruff / mypy / pytest | lint / type / test | The discriminated-union `BackendConfig` is fully typed; mypy strict handles `Field(discriminator=…)` unions natively. No config change. |
| moto (`ThreadedMotoServer`) | S3 test double | Already wired for aioboto3; unchanged. |

## Installation

```bash
# Nothing to install. The three load-bearing libraries are already pinned and installed:
#   pydantic          2.13.4   (>=2.13.4)
#   pydantic-settings 2.14.2   (>=2.14.2)
#   kr8s              0.20.15  (>=0.20.15)
#   aioboto3          15.5.0   (>=15.5.0)
# uv sync is a no-op for this milestone's stack.
```

## Q1 — pydantic-settings: a validated LIST of nested models with per-entry `_FILE`

**Finding: idiomatic and native for the list; per-entry `_FILE` is a small extension of the
project's EXISTING custom validator, NOT a new settings source or new dependency.**

### The list-of-models itself is fully idiomatic
- A field `backends: list[BackendConfig]` where `BackendConfig(BaseModel)` is a plain pydantic
  model is a standard pydantic v2 construct. Validation, defaults, and per-entry errors all
  work out of the box.
- **Per-kind fail-fast validation** is best done as a **discriminated union**:
  `BackendConfig = Annotated[LocalBackendCfg | ComputeBackendCfg | KueueBackendCfg,
  Field(discriminator="kind")]`. Each variant carries only its own required fields (a
  `kueue` entry requires `kube.*`; a `compute` entry requires `agent_ref`; `local` requires
  neither). pydantic raises a precise, entry-scoped `ValidationError` at construction — this
  **replaces** the three current cross-field `_enforce_s3_config_when_k8s` /
  `_enforce_compute_scratch_dir_when_a1` / `_enforce_kube_config_when_k8s` model validators
  with per-variant field requirements, which is cleaner and matches design §7.
- **Loading onto `ControlSettings`** (pydantic-settings treats `list[SubModel]` as complex):
  1. **Single JSON env var** — `PHAZE_BACKENDS='[{"id":"a1","kind":"compute","rank":10,"cap":1,"agent_ref":"oci-a1"}, …]'` (built-in complex-field JSON parsing). Simplest; works today.
  2. **Deep env override** — `env_nested_delimiter='__'` + numeric index:
     `PHAZE_BACKENDS__0__ID=a1`, `PHAZE_BACKENDS__0__KIND=compute`, … (built-in). Verbose but
     avoids JSON-in-env.
  3. **Config file source** — a TOML/YAML `backends.toml` via pydantic-settings' built-in
     `TomlConfigSettingsSource` (stdlib `tomllib`, **no new dep**) added in
     `settings_customise_sources`. YAML would require `pydantic-settings[yaml]`/PyYAML — a new
     dep — so **prefer TOML or JSON-env** to keep the near-zero-dep guarantee.

### Per-entry `_FILE` secrets: extend the existing before-validator (no new dep, no new source)
The current `_resolve_secret_files` (`config.py:90-148`) walks only **flat top-level** fields
named in `SECRET_FILE_FIELDS` and reads their `<ALIAS>_FILE` env siblings. It does **not**
descend into list entries, so a per-Kueue-backend `kubeconfig`/`sa_token` will not resolve
automatically. Two clean options, both application-code only:

- **Option A (recommended) — secret-name indirection, reuse top-level `_FILE` unchanged.**
  Each backend entry references its secret material by a stable key, exactly like the design's
  own `agent_ref` and the existing `kube_env_secret_name` / `kube_ca_secret_name` /
  `kube_env_configmap_name` pattern. Kueue kubeconfig/SA-token stay as **top-level**
  `SecretStr` fields resolved by the unchanged `_resolve_secret_files`, keyed per backend id
  (e.g. `PHAZE_BACKEND_KUEUE_HOMELAB_KUBECONFIG_FILE`). This keeps the proven flat `_FILE`
  machinery, keeps secrets out of the JSON blob, and needs **zero** change to the resolver.
- **Option B — teach `_resolve_secret_files` to walk the `backends` list.** Add a second pass
  that, for each entry index/id, resolves `PHAZE_BACKENDS__<i>__KUBE__KUBECONFIG_FILE` (or a
  per-id form) via the same `_resolution_env` map. This is ~15-20 lines mirroring the existing
  loop; still no new dependency and no `PydanticBaseSettingsSource` subclass required.

**Do NOT** adopt pydantic-settings' built-in `secrets_dir` / `NestedSecretsSettingsSource` for
this. It exists and works, but it is a *different* convention from the project-wide `<VAR>_FILE`
one used by every other secret (DB, Redis, agent token, S3, kube, SSH). Diverging would split
the operator's mental model. Stay on `<VAR>_FILE`.

**Bottom line Q1:** list-of-models = native pydantic; per-entry `_FILE` = extend the existing
custom before-validator or use secret-name indirection. **No custom settings source strictly
required; no new dependency.**

## Q2 — Multi-cluster kube: does kr8s already cover N clusters?

**Finding: YES. kr8s natively supports N clusters. No new library.**

- `kr8s.asyncio.api(url=…, kubeconfig=…, context=…, serviceaccount=…)` is a per-call factory.
  It **caches by arguments** ("calling `api()` with the same arguments always returns the same
  cached object"), so distinct per-cluster `url`/`kubeconfig`/`context` values yield distinct
  clients automatically.
- Objects are bound to a specific cluster explicitly: `Job({…}, api=cluster_api)` /
  `Job.get(…, api=cluster_api)`. The current `kube_staging.py` already passes a per-call
  `url`/`namespace`; generalizing to "one client per backend entry" is the same call inside a
  loop keyed on the backend's kube config.
- **Plan-time care points (design decisions, not dependency needs):**
  - *Arg-keyed cache collisions.* Two Kueue backends sharing the same `url`+`namespace` but
    different credentials would map to the **same** cached client. The current code's
    post-construction mutation (`api.auth.token = token; await api._create_session()`,
    `kube_staging.py:104-105`) is unsafe across a shared cached client. Cleanest fix: give each
    backend a **distinct `kubeconfig`** (write the `_FILE`-mounted kubeconfig contents to a
    per-backend path and pass `kubeconfig=`), or a distinct `context=`, so cache keys differ
    and the token hack is retired. This is exactly the multi-cluster idiom kr8s documents.
  - Keep the existing `KubeStagingError` fail-loud discipline per client.
- The already-noted "exact auth/constructor form is a Phase-56 live-cluster verification item"
  (`kube_staging.py:94`) remains the one thing to confirm against a real cluster — but it is a
  verification item, not a library gap.

**No `kubernetes`, `kubernetes_asyncio`, or any multi-cluster wrapper is warranted.** kr8s
0.20.15 is current and covers it.

## Q3 — Version currency vs. the 7-day exclude-newer cooldown

**Finding: all three libraries are simultaneously the latest PyPI release AND already the
pinned/installed floor AND far older than 7 days. Zero version churn, cooldown trivially
satisfied.**

| Package | Installed / floor | Latest on PyPI | Released | Cooldown status |
|---------|-------------------|----------------|----------|-----------------|
| pydantic | 2.13.4 (`>=2.13.4`) | 2.13.4 | 2026-05-06 | ✅ ~2 months old; satisfied |
| pydantic-settings | 2.14.2 (`>=2.14.2`) | 2.14.2 | 2026-06-19 | ✅ ~2 weeks old; satisfied |
| kr8s | 0.20.15 (`>=0.20.15`) | 0.20.15 | 2026-01-16 | ✅ ~6 months old; satisfied |
| aioboto3 | 15.5.0 (`>=15.5.0`) | (unchanged use) | — | ✅ unchanged |

The nested-models, `env_nested_delimiter`, complex-field JSON parsing, discriminated-union,
custom-source, and `TomlConfigSettingsSource` APIs are all stable in pydantic-settings 2.x
(present well before 2.14.2). `NestedSecretsSettingsSource` exists in the current release if
ever wanted (not recommended — see Q1). **No version bump is required by this milestone**, so
the exclude-newer window is never contended.

## Integration points into existing `config.py`

| Existing surface | Change | Nature |
|------------------|--------|--------|
| `cloud_target: Literal["local","a1","k8s"]` (`config.py:406`) | Superseded by `backends: list[BackendConfig]`; keep a `cloud_target` back-compat shim (a `model_validator` that synthesizes a one-entry list when `backends` is empty and `cloud_target` is set) | Additive + shim, per design §4.1 |
| `cloud_max_in_flight` (`config.py:417`) | Becomes per-entry `cap` on `BackendConfig`; global knob retained only as a shim default | Field move |
| `_enforce_s3_config_when_k8s` / `_enforce_compute_scratch_dir_when_a1` / `_enforce_kube_config_when_k8s` (`config.py:615-681`) | Replaced by per-variant required fields on the discriminated `BackendConfig` union | Refactor, behavior-preserving |
| `SECRET_FILE_FIELDS` + `_resolve_secret_files` (`config.py:79,90`) | Extended for per-backend secrets via Option A (secret-name indirection, unchanged resolver) or Option B (list-walking pass) | Application code, no new dep |
| `_api()` in `kube_staging.py:87` | One kr8s client **per backend entry**, keyed on that entry's `kubeconfig`/`context`; retire the shared-cache token mutation in favor of per-backend `kubeconfig=` | Refactor |
| `cloud_job` model | Gains `backend_id` column (additive migration) so in-flight counts + reconcile are per-backend | Design §4.4 |

## What NOT to Add

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `kubernetes` / `kubernetes_asyncio` official client | kr8s already does per-cluster clients; adding a second kube client library duplicates the whole submission/reconcile surface and violates the near-zero-dep goal | kr8s `api(url=/kubeconfig=/context=)` per backend |
| `pluggy` / `stevedore` / entry-point plugin frameworks | Design §6 explicitly scopes the `Backend` seam as **internal only** — no third-party plugin loading | stdlib `typing.Protocol` + three in-repo implementations |
| `pydantic-settings[yaml]` / PyYAML for a `backends.yaml` | Adds a dependency purely for config file format; the design's YAML is illustrative | JSON-in-env, `env_nested_delimiter` indices, or built-in `TomlConfigSettingsSource` (stdlib `tomllib`) |
| pydantic-settings `secrets_dir` / `NestedSecretsSettingsSource` | Works, but is a *second* secret convention alongside the project-wide `<VAR>_FILE` used by every other secret; splits operator mental model | Extend the existing `_resolve_secret_files` `<VAR>_FILE` idiom |
| Any cloud-provider SDK (boto3-ec2, OCI SDK, google-cloud-*) | Design §3/§6: static routing, **no provisioning**, no new concrete providers this milestone | Nothing — backends are operator-deployed and merely routed to |
| A dollar-cost/pricing library or spend API | Cost-tier = operator-assigned integer `rank` + `cap`, not an automated dollar model (design decision 4) | Plain `rank: int` / `cap: int` fields |

## Stack Patterns by Variant

**If per-backend secrets stay few and Kueue-only:**
- Use **Option A** (secret-name indirection): top-level `_FILE` SecretStr per backend id,
  referenced from the entry. Zero resolver change. Recommended default.

**If backends proliferate and inline secrets are desired:**
- Use **Option B**: extend `_resolve_secret_files` with a list-walking pass over
  `PHAZE_BACKENDS__<i>__…_FILE`. Still no new dep.

**If operators want a human-editable multi-backend file:**
- Add pydantic-settings' built-in `TomlConfigSettingsSource` in `settings_customise_sources`
  (stdlib `tomllib`, no new dep). Avoid YAML to preserve the zero-dep guarantee.

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| pydantic 2.13.4 | pydantic-settings 2.14.2 | pydantic-settings 2.14.x targets pydantic v2.x; discriminated unions + complex-field parsing stable. Installed together already. |
| kr8s 0.20.15 | Python 3.13 | Pure-Python, async client already in production use (Phase 54-56). Per-cluster `api()` caching is the documented multi-cluster path. |
| pydantic-settings 2.14.2 | stdlib `tomllib` | `TomlConfigSettingsSource` needs no third-party TOML lib on 3.13 (`tomllib` is stdlib). |

## Sources

- Context7 `/pydantic/pydantic-settings` — nested submodels from env, `env_nested_delimiter`
  numeric list indices, complex-field JSON parsing, custom `PydanticBaseSettingsSource`,
  `NestedSecretsSettingsSource` — **HIGH** (official docs mirror)
- Context7 `/kr8s-org/kr8s` — `kr8s.api()` / `kr8s.asyncio.api()` parameters (`url`,
  `kubeconfig`, `context`, `serviceaccount`), arg-based client caching, explicit `api=` object
  binding — **HIGH** (official docs mirror)
- PyPI JSON API — current versions + release dates for pydantic (2.13.4, 2026-05-06),
  pydantic-settings (2.14.2, 2026-06-19), kr8s (0.20.15, 2026-01-16) — **HIGH**
- `uv run python -c "import …; print(__version__)"` — installed versions confirmed equal to
  latest — **HIGH**
- Repo `src/phaze/config.py` (`_resolve_secret_files`, `SECRET_FILE_FIELDS`, `cloud_target`,
  kube_* surface) and `src/phaze/services/kube_staging.py` (`_api`, per-call `kr8s.asyncio.api`,
  token-mutation caveat) — **HIGH** (direct read)
- `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` — internal-protocol scope,
  no-new-providers, shared-bucket, rank/cap model — **HIGH** (locked design)

---
*Stack research for: multi-backend config registry + tiered scheduler (phaze 2026.7.1)*
*Researched: 2026-07-03*
