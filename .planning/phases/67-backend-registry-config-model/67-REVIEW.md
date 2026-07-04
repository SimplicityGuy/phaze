---
phase: 67-backend-registry-config-model
reviewed: 2026-07-04T00:12:20Z
depth: standard
files_reviewed: 9
files_reviewed_list:
  - src/phaze/config_backends.py
  - src/phaze/config.py
  - src/phaze/routers/agent_push.py
  - src/phaze/routers/agent_s3.py
  - src/phaze/routers/pipeline.py
  - src/phaze/services/kube_staging.py
  - src/phaze/services/s3_staging.py
  - src/phaze/tasks/controller.py
  - src/phaze/tasks/release_awaiting_cloud.py
findings:
  critical: 1
  warning: 2
  info: 2
  total: 5
status: issues_found
---

# Phase 67: Code Review Report

**Reviewed:** 2026-07-04T00:12:20Z
**Depth:** standard
**Files Reviewed:** 9
**Status:** issues_found

## Summary

Reviewed the flat-config → typed `backends.toml` registry migration: the discriminated-union
submodels (`config_backends.py`), the `ControlSettings` integration (`_load_backend_registry`,
`_validate_registry`, the six transitional accessors), and every rewired call site (agent
push/S3 callbacks, kube/S3 staging services, the pipeline router, the controller startup hook,
and the cloud-window cron).

**Secret handling is sound.** `SecretStr` covers every credential field (kube kubeconfig/SA token,
bucket access/secret keys); `_read_secret_file` never echoes file contents on an unreadable path;
`log_effective_registry` projects only `{id, kind, rank, cap}` and can never leak a secret or a
mount path; the per-bucket `endpoint_url` http(s) guard matches its documented scope. **No lingering
references to any removed flat field** exist in code (verified via grep — all remaining mentions are
docstrings/comments).

The material problems cluster around one root cause: the `>1 non-local backend` state is accepted by
`_validate_registry` at construction but then makes the transitional accessors raise at scattered,
partly-unguarded call sites — crashing controller boot and 500-ing the dashboard, in direct
contradiction of the "never abort boot / never 500 / never raise in cron" contracts those paths
document. A secondary defect lets a compute backend with no `scratch_dir` silently produce a
`"None/..."` scratch path.

## Critical Issues

### CR-01: `>1 non-local backend` passes registry validation, then crashes controller boot and 500s the dashboard

**File:** `src/phaze/config.py:413-447` (`_validate_registry`), `src/phaze/tasks/controller.py:174`, `src/phaze/routers/pipeline.py:575`

**Issue:**
`_validate_registry` enforces empty-registry, unknown-bucket, and cluster-specific-cardinality
invariants but does **not** reject a registry with more than one non-local backend. As a result
`ControlSettings()` constructs successfully for e.g. two `compute` backends (confirmed by
`tests/shared/config/test_bucket_registry.py::test_multiple_non_local_backends_accessor_raises`,
which asserts construction succeeds and `cloud_enabled is True`). The `>1 non-local` guard is then
enforced lazily inside `_single_non_local()`, which **raises `ValueError`** on every read of
`active_cloud_kind` / `active_cap` / `active_compute_scratch_dir` / `active_kube` / `active_bucket`.

Several of those reads are unguarded and sit behind a `cloud_enabled` gate that is `True` in exactly
this state, so the raise propagates:

- `controller.py:174` — `if cfg.active_cloud_kind == "kueue":` is **outside** any try/except (the
  guarded block is *inside* the `if` body). A raise here aborts the SAQ `startup` hook and the
  control worker never boots — the exact opposite of the "boot resilience is non-negotiable … a
  recovery failure must NEVER abort controller boot" invariant the same function documents.
- `pipeline.py:575` — `"cloud_lane_kind": "local" if not settings.cloud_enabled else settings.active_cloud_kind`
  inside `build_dashboard_context`, whose docstring promises "this builder never 500s the page." It
  will 500.
- `pipeline.py:810` (`trigger_backfill_cloud`), `agent_s3.py:113` (`report_uploaded`, *after* the
  multipart has already been completed and `cloud_job` flipped — a partial-state 500), and
  `release_awaiting_cloud.py:131`/`145`/`180` (the `stage_cloud_window` cron, which documents a
  "T-50-cron-raise … NEVER raise" no-op contract) are all reached with `cloud_enabled True` and will
  raise for the same reason.

Because the offending config passes validation, there is no guardrail — a single operator misstep
(configuring the multi-cloud registry the milestone is building toward, one phase early) takes down
the control plane at boot rather than failing fast with the intended clear message.

**Fix:** Add the `>1 non-local` fail-fast to `_validate_registry`, mirroring the existing cross-entry
checks, so the unsupported state is rejected once at construction with a clear Phase-69 message
instead of raising lazily at runtime call sites:

```python
@model_validator(mode="after")
def _validate_registry(self) -> "ControlSettings":
    if not self.backends:
        raise ValueError("backend registry resolved to empty — refusing to start (REG-04)")
    non_local = [b for b in self.backends if b.kind != "local"]
    if len(non_local) > 1:
        raise ValueError(
            f"multi-backend dispatch lands in Phase 69 (SCHED): {len(non_local)} non-local backends "
            f"{[b.id for b in non_local]} configured, but Phase 67 supports at most one non-local backend"
        )
    # ... existing bucket / cluster-specific checks ...
```

Update `test_multiple_non_local_backends_accessor_raises` to assert the `ValidationError` at
`ControlSettings()` construction. `active_bucket`'s own `>1 bucket` raise (Phase 70) should be
promoted the same way, or its call site in `s3_staging._staging_config` should catch it and
re-raise as `S3StagingError`.

## Warnings

### WR-01: compute backend without `scratch_dir` produces a literal `"None/<file_id>.<ext>"` scratch path

**File:** `src/phaze/config_backends.py:79-97` (`ComputeBackend`), `src/phaze/routers/agent_push.py:122`

**Issue:**
`ComputeBackend.scratch_dir` is `str | None = None` and its only `model_validator` requires
`agent_ref` — `scratch_dir` is never validated as present. `active_compute_scratch_dir` therefore
returns `None` for a compute backend that omits it, and `report_pushed` interpolates it with no guard:

```python
scratch_path = f"{settings.active_compute_scratch_dir}/{file_id}.{file.file_type}"
# -> "None/3f2a....mp3"
```

That corrupt path is pinned into the enqueued `process_file` payload and handed to the compute agent,
which then reads a nonexistent file — a silent wrong-path failure rather than the fail-loud behavior
every other required per-backend field gets (`agent_ref`, `kube.api_url/namespace/local_queue`,
`kube.job_image/cpu_request/memory_request` all raise a clear message when unset). The problem is
compounded by the submodels not setting `extra="forbid"` (see IN-01): a misspelled `scratchdir =`
key is silently dropped, leaving `scratch_dir` `None`.

**Fix:** Require `scratch_dir` on a `ComputeBackend` (extend `_require_agent_ref` or add a sibling
`model_validator`) so an unset value fails fast with the entry id, matching the compute/kueue
required-field discipline:

```python
@model_validator(mode="after")
def _require_agent_ref(self) -> "ComputeBackend":
    if not self.agent_ref:
        raise ValueError(f"backend {self.id!r} (kind=compute) requires an agent_ref")
    if not self.scratch_dir:
        raise ValueError(f"backend {self.id!r} (kind=compute) requires a scratch_dir")
    return self
```

### WR-02: `PHAZE_BACKENDS_CONFIG_FILE` is read from `os.environ` only, not `.env`, so a `.env` pointer silently falls back to implicit-local

**File:** `src/phaze/config.py:399`

**Issue:**
`_load_backend_registry` resolves the config path with
`os.environ.get("PHAZE_BACKENDS_CONFIG_FILE", "/etc/phaze/backends.toml")`. The sibling secret-file
resolver in the same module (`_resolution_env`, used by `_resolve_secret_files`) deliberately merges
the configured `.env` file *and* process env "so a `<VAR>_FILE` … declared in `.env` — the way every
other documented var in `.env.example` is consumed — is honored." The registry pointer breaks that
convention: an operator who sets `PHAZE_BACKENDS_CONFIG_FILE` in `.env` (as `.env.example:186`
documents it) gets `None` from `os.environ.get`, the default `/etc/phaze/backends.toml` is not found,
and `_load_backend_registry` injects nothing → the `default_factory` silently synthesizes the
implicit all-local registry. Result: the operator's entire cloud configuration is silently ignored
and every long file routes local, with no error. (Tests pass because `conftest` sets the var via
`monkeypatch.setenv`, i.e. process env.)

**Fix:** Resolve the pointer through the same `.env`-aware map the secret resolver uses, e.g.:

```python
env = _resolution_env(cls.model_config)
path = env.get("PHAZE_BACKENDS_CONFIG_FILE", "/etc/phaze/backends.toml")
```

(or document explicitly in `.env.example` that this one pointer must be a process-env var, not a
`.env` entry).

## Info

### IN-01: backend submodels do not set `extra="forbid"`, silently dropping mistyped TOML keys

**File:** `src/phaze/config_backends.py:67-196`

**Issue:** `LocalBackend` / `ComputeBackend` / `KueueBackend` / `KubeConfig` / `BucketConfig` inherit
pydantic's default `extra="ignore"`. A typo in `backends.toml` (`scratchdir`, `agentref`, `apiurl`,
`endpont_url`) is silently discarded rather than rejected, so a required field falls back to its
`None`/default and surfaces later as an opaque runtime error (see WR-01). For an operator-authored
config file, fail-fast on unknown keys is the safer default.

**Fix:** Add `model_config = ConfigDict(extra="forbid")` to the backend/bucket/kube submodels so an
unknown key raises a `ValidationError` naming the offending field at construction.

### IN-02: dead `backend.kind == "local"` branch in `active_cloud_kind`

**File:** `src/phaze/config.py:479-482`

**Issue:** `_single_non_local()` returns only non-local backends (or `None`), so the `or backend.kind == "local"`
disjunct in `active_cloud_kind` is unreachable. Harmless but misleading — it implies `_single_non_local`
could return a local backend.

**Fix:** Drop the dead disjunct: `return None if backend is None else backend.kind`.

---

_Reviewed: 2026-07-04T00:12:20Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
