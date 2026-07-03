# Phase 67: Backend Registry & Config Model - Pattern Map

**Mapped:** 2026-07-03
**Files analyzed:** 12 (4 new + 8 modified, incl. 3 templates and a test-suite delete/rewire set)
**Analogs found:** 12 / 12 (all new code has a strong in-repo analog — this is a refactor of `config.py`, not greenfield)

All analogs live in the same repo the executor is editing. Every pattern below is a **house-style** excerpt from `src/phaze/config.py` or its existing test modules — copy these, do not invent new idioms.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/config_backends.py` (NEW) | config / model | transform (TOML→typed) | `src/phaze/config.py` `AgentSettings` field-groups + validators | role-match (same module family; new file split) |
| `src/phaze/config.py` (MOD) | config | transform | itself (remove `cloud_target` block lines 397-681; add registry) | exact (self-refactor) |
| `src/phaze/routers/pipeline.py` (MOD) | route | request-response | itself lines 395/572/699/763 | exact (Class A/C rewire) |
| `src/phaze/routers/agent_s3.py` (MOD) | route | request-response | itself line 112 | exact (Class B transitional) |
| `src/phaze/routers/agent_push.py` (MOD) | route | request-response | itself line 121 | exact (Class B transitional) |
| `src/phaze/tasks/controller.py` (MOD) | task (cron/startup) | event-driven | itself line 167 | exact (Class B transitional) |
| `src/phaze/tasks/release_awaiting_cloud.py` (MOD) | task (cron) | event-driven | itself lines 126/128/142/177 | exact (Class A + Class B) |
| `src/phaze/templates/pipeline/partials/*.html` (MOD, 3 files) | template | presentation | itself (`analyze_workspace.html` L54-75) | exact (Class C) |
| `tests/shared/config/test_backend_registry.py` (NEW) | test | request-response | `tests/shared/config/test_cloud_target.py` | exact |
| `tests/shared/config/test_bucket_registry.py` (NEW) | test | request-response | `tests/shared/config/test_cloud_target.py` | exact |
| `tests/shared/config/test_backend_secret_files.py` (NEW) | test | file-I/O | `tests/shared/config/test_secret_file_resolution.py` | exact |
| `tests/shared/config/test_{cloud_target,kube_settings,s3_settings}.py` (DELETE/REWRITE) | test | — | (assert removed fields) | delete/rewrite |

---

## Pattern Assignments

### `src/phaze/config_backends.py` (NEW — config models module)

**Analog:** `src/phaze/config.py` (whole module; the `AgentSettings` field-groups + `model_validator` set are the closest shape for per-entry submodels).

Research (Recommended Project Structure) names this NEW module so `config.py` doesn't balloon and tests get a clean import target. It holds: the `LocalBackend | ComputeBackend | KueueBackend` discriminated union, `KubeConfig` + `BucketConfig` submodels, per-variant validators, the inline-`*_file` before-validator, the shared `_read_secret_file` whitespace helper, and the implicit-local default factory.

**Imports pattern** — mirror `config.py:10-19` (house import ordering is `force-sort-within-sections`, `combine-as-imports`):
```python
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
```

**Discriminated-union field** (RESEARCH Pattern 1 — copy verbatim shape):
```python
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
    kube: KubeConfig          # REQUIRED nested table (REG-02, D-13)
    buckets: list[str] = Field(default_factory=list)   # id-list bind (D-08)

BackendConfig = Annotated[
    LocalBackend | ComputeBackend | KueueBackend,
    Field(discriminator="kind"),
]
```

**Per-variant fail-fast validator (id-tagged)** — this REPLACES the three `_enforce_*_when_*` validators at `config.py:615-681`. Copy the `model_validator(mode="after")` shape from `config.py` (e.g. `_enforce_required_agent_fields`, lines 911-923), but raise with `self.id` so the operator sees the entry id, not a list index (RESEARCH Pattern 3 / Pitfall 3):
```python
@model_validator(mode="after")
def _require_kube(self) -> "KueueBackend":
    if self.kube is None:
        raise ValueError(f"backend {self.id!r} (kind=kueue) requires a [kube] config table")
    return self
```
Note the analog validators use the string-literal return type `-> "ControlSettings"` (config.py:616, 637, 658), not `typing_extensions.Self` — match the file's existing convention.

**`endpoint_url` SSRF guard on `BucketConfig`** — lift `_validate_s3_endpoint_url` verbatim from `config.py:597-613` (it is now PER-bucket, REG-05 / Security V5):
```python
@field_validator("endpoint_url")
@classmethod
def _validate_endpoint_url(cls, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"endpoint_url must be a well-formed http(s) URL with a host, got {value!r}")
    return value
```

**Inline `*_file` secret read (D-04/D-06)** — a NEW per-submodel `model_validator(mode="before")`. Do NOT extend `_resolve_secret_files` (different mechanism: TOML field value, not env `<VAR>_FILE`). Instead FACTOR the strip/verbatim rule out of `config.py:145` into a shared helper both call paths import (RESEARCH Pattern 4 / Pitfall 4):
```python
def _read_secret_file(path: str, *, preserve_whitespace: bool) -> str:
    try:
        contents = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"secret file {path!r} could not be read: {exc}") from exc
    return contents if preserve_whitespace else contents.strip()  # mirrors config.py:145 (D-06)
```
The existing strip-vs-verbatim rule to preserve is at `config.py:81-88` (`SECRET_FILE_PRESERVE_WHITESPACE`) and `config.py:143-145`. Key material (kubeconfig / SSH-style) → verbatim; tokens/access-keys → stripped.

---

### `src/phaze/config.py` (MODIFIED — ControlSettings registry)

**Analog:** itself. Remove `cloud_target` (406-410), `cloud_max_in_flight` (417-423), `compute_scratch_dir` (453-457), the `s3_*` block (459-525), the `kube_*` block (527-595), and the three validators `_enforce_s3_config_when_k8s` / `_enforce_compute_scratch_dir_when_a1` / `_enforce_kube_config_when_k8s` (615-681). Also trim `SECRET_FILE_FIELDS` (348-355): drop `s3_access_key_id`, `s3_secret_access_key`, `kube_kubeconfig`, `kube_sa_token` (those secrets move to inline TOML `*_file`); KEEP `openai_api_key`, `anthropic_api_key` and the inherited `database_url`/`redis_url`/`queue_url` (D-05 — control-plane secrets stay on env `_FILE`).

**KEEP unchanged (D-15 global tuning knobs):** `push_max_attempts` (427-433), `cloud_submit_max_attempts` (440-446), `cloud_route_threshold_sec` (389-395), and the `s3_presign_*` / `s3_lifecycle_ttl_days` / `s3_multipart_part_size_bytes` knobs stay as global fields (RESEARCH Open Q3 — no per-bucket value is needed for validation this phase).

**Bounded-field pattern for `backends`/`buckets` and the implicit-local default** (RESEARCH Pattern 5) — the `default_factory` idiom already exists at `config.py:740-751` (`scan_roots`) and `741`:
```python
def _default_local_registry() -> list[BackendConfig]:
    return [LocalBackend(kind="local", id="local", rank=99, cap=1)]

backends: list[BackendConfig] = Field(default_factory=_default_local_registry)
buckets: list[BucketConfig] = Field(default_factory=list)
```
CAUTION (RESEARCH Pitfall 2): `default_factory` fires only on an ABSENT key. `backends = []` present-but-empty must fail fast in the container validator, not silently boot.

**TOML load — Idiom B (house before-validator, RECOMMENDED)** — mirror the existing `_resolve_secret_files` `model_validator(mode="before")` at `config.py:90-148` that reaches into env/`.env` and injects into `data`. Read `PHAZE_BACKENDS_CONFIG_FILE` (env-pointer style matches the `<VAR>_FILE` pointers), `tomllib.load` if present, inject `data["backends"]`/`data["buckets"]`; absent file → inject nothing (D-03 no-op). `tomllib` import is stdlib on 3.14.

**Container cross-entry validator** (REG-05, RESEARCH Pattern 6) — a `model_validator(mode="after")` on `ControlSettings`, same shape as the existing `_enforce_*` validators (config.py:615-681) but sees the whole registry: empty-registry fail-fast (REG-04), bucket-id resolution + missing-ref/empty-set fail-fast (D-08), scope cardinality (D-09). Full excerpt in RESEARCH Pattern 6.

**Derived `cloud_enabled` property + transitional accessors (D-14)** — NEW. `cloud_enabled` = True iff registry has any non-local backend. Class-B accessors (`active_cloud_kind`, `active_compute_scratch_dir`, `active_cap`) are the ≤1-non-local-backend reductions; mark each `# TRANSITIONAL — removed in Phase 68 (BACK-01)` (RESEARCH Call-Site Rewire Map step 2).

**Startup log projection (REG-04 / Pitfall 5)** — log `[{"id","kind","rank","cap"} …]` ONLY, never a whole model. structlog is already the project logger.

---

### `src/phaze/tasks/release_awaiting_cloud.py` (MODIFIED — Class A + Class B)

**Analog:** itself, lines 122-193 (read above). `cfg = get_settings()` pattern stays.

- **Class A (line 126)** — pure swap: `if cfg.cloud_target == "local":` → `if not cfg.cloud_enabled:`
- **Class B (128)** `cfg.cloud_max_in_flight` → transitional `cfg.active_cap` (per-backend `cap`, D-15; single global read has no registry equivalent — Phase 69 territory).
- **Class B (142)** `if cfg.cloud_target == "a1":` GATE-1 compute-agent probe → transitional `cfg.active_cloud_kind == "compute"`.
- **Class B (177)** `if cfg.cloud_target == "k8s":` S3-vs-rsync dispatch fork → transitional `cfg.active_cloud_kind == "kueue"`.

The `# type: ignore[attr-defined]` comments on 126/128/142/177 exist because the module-level `get_settings()` returns `BaseSettings`-typed; keep them (or migrate to the new accessor names) but do NOT change the no-op contract — every early return stays a clean no-op.

---

### `src/phaze/routers/pipeline.py` (MODIFIED — Class A + Class C)

**Analog:** itself.

- **Class A (395)** `settings.cloud_target != "local"` (bool arg to `_route_discovered_by_duration`) → `settings.cloud_enabled`.
- **Class A (699)** same bool-arg pattern (backfill router) → `settings.cloud_enabled`.
- **Class A (763)** `if settings.cloud_target == "local":` backfill no-op guard → `if not settings.cloud_enabled:`.
- **Class C (572)** `"cloud_target": settings.cloud_target` into the template ctx → hand the template a transitional legacy-shaped string (`"local"` when all-local, else `active_cloud_kind`) so `analyze_workspace.html` L54-75 keeps rendering (RESEARCH Class C / Assumption A4 — open the templates at plan-time).

Note line 791-805 has a mypy-narrowing comment block that assumes `cloud_target` is statically `'a1'`/`'k8s'` after the local early-return — this dispatch fork is Class B and needs the transitional accessor.

---

### `src/phaze/routers/agent_s3.py` (line 112) · `src/phaze/tasks/controller.py` (line 167) · `src/phaze/routers/agent_push.py` (line 121) — Class B transitional

All three are dispatch forks, NOT pure swaps (RESEARCH Pitfall 1 — do NOT drag the `Backend` protocol into 67):
- `agent_s3.py:112` `if settings.cloud_target == "k8s":` → `if settings.active_cloud_kind == "kueue":` (post-staging seam).
- `controller.py:167` `if cfg.cloud_target == "k8s":` LocalQueue probe gate → `if cfg.active_cloud_kind == "kueue":` (keep the OWN try/except boot-safety block intact).
- `agent_push.py:121` `settings.compute_scratch_dir` → `settings.active_compute_scratch_dir` (per-compute-backend field now, D-13).

Mark each accessor use as reading a `# TRANSITIONAL — Phase 68` seam.

---

### Templates (3 files, Class C)

`templates/pipeline/partials/analyze_workspace.html` (L25, 54-75), `_lane_card.html` (L12), `backfill_response.html` (L15) reference `cloud_target`. Either pass the transitional compat string (recommended, minimal) or trim the references. Do NOT let the removal break the dashboard render (RESEARCH Class C). BEUI-01 N-lane UI is Phase 71 — no redesign here.

---

### `tests/shared/config/test_backend_registry.py` (NEW) & `test_bucket_registry.py` (NEW)

**Analog:** `tests/shared/config/test_cloud_target.py` (read above — copy its exact structure).

Copy the analog's shape: module docstring, `from __future__ import annotations`, a `_CLEAR_ENV` tuple + `_clear_*_env(monkeypatch)` helper (analog lines 29-49), one test-per-behavior with a docstring, `ControlSettings()` constructed directly (sync, no DB/Redis), `pytest.raises(ValueError, match=...)` for fail-fast. Point new tests at `tmp_path` TOML fixtures + `monkeypatch.setenv("PHAZE_BACKENDS_CONFIG_FILE", ...)`. Behaviors to cover are enumerated in RESEARCH "Observable startup behaviors" (implicit-local, per-variant id-tagged fail-fast, scope cardinality, missing-ref/empty-set, present-but-empty fail-fast, startup-log projection).

Id-tagged assertion example (mirrors analog line 96):
```python
with pytest.raises(ValidationError, match=r"backend 'kueue-x'"):
    ControlSettings()
```

### `tests/shared/config/test_backend_secret_files.py` (NEW)

**Analog:** `tests/shared/config/test_secret_file_resolution.py` (read above). Copy its `tmp_path` secret-file pattern (lines 52-64): write a file with trailing `\n`, assert the resolved value. For inline `*_file`: assert token/access-key STRIPPED, kubeconfig/key-material VERBATIM (D-06); assert a missing path fails fast naming field + path. Also copy the `get_settings.cache_clear()` autouse fixture (analog lines 32-39) if the test constructs via `get_settings()`.

### DELETE / REWRITE (RESEARCH Wave 0 Gaps)

- **DELETE (assert removed fields):** `tests/shared/config/test_cloud_target.py`, `test_kube_settings.py`, `test_s3_settings.py`.
- **UPDATE the ~6 tests referencing `cloud_target`:** `tests/shared/core/test_routing_seam.py`, `tests/analyze/core/test_staging_cron.py`, `tests/shared/tasks/test_controller_startup_localqueue.py`, `tests/shared/routers/test_pipeline.py`, `tests/shared/core/test_enrich_analyze_workspaces.py`, `tests/shared/core/test_config_role_split.py`, `tests/agents/routers/test_agent_s3.py`, plus doc `tests/BUCKETS.md`.

---

## Shared Patterns

### Bounded / fail-fast field validation (apply to every new numeric/enum field)
**Source:** `src/phaze/config.py:374-380` (`straggler_threshold_sec`), `389-395` (`cloud_route_threshold_sec`).
```python
straggler_threshold_sec: int = Field(
    default=6600, gt=0, lt=86400,
    validation_alias=AliasChoices("PHAZE_STRAGGLER_THRESHOLD_SEC", "straggler_threshold_sec"),
    description="...",
)
```
Fail-fast-at-startup posture: an out-of-range operator value raises at construction, never reaches runtime. `rank`/`cap` should carry the same bounded style.

### `model_validator(mode="before")` inject-into-data pattern
**Source:** `src/phaze/config.py:90-148` (`_resolve_secret_files`).
**Apply to:** the TOML loader (Idiom B) and the inline `*_file` reader. The house pattern is: `if not isinstance(data, dict): return data`, resolve from env/`.env`/file, mutate `data[...]`, return `data`. A missing/unreadable path raises `ValueError` naming the var+path (never a silent fallback).

### `model_validator(mode="after")` fail-fast guard
**Source:** `src/phaze/config.py:615-681` (the three `_enforce_*_when_*` being removed) and `911-957` (`AgentSettings` guards, staying).
**Apply to:** every per-variant submodel validator and the container registry validator. Return type is the string-literal class name (`-> "ControlSettings"`), not `Self`.

### Secret handling — `SecretStr` + never-log
**Source:** `config.py:317-321, 486-495, 586-595`; `export_llm_api_keys` (974-993).
**Apply to:** all resolved secret fields on `BucketConfig`/`KubeConfig` (access keys, SA token, kubeconfig) → type `SecretStr` so accidental interpolation prints `**********` (Pitfall 5). Startup log is a projection, never the model.

### `<VAR>_FILE` shared whitespace rule (factor, don't fork)
**Source:** `config.py:81-88` (`SECRET_FILE_PRESERVE_WHITESPACE`) + `143-145`.
**Apply to:** BOTH the surviving env `_FILE` path AND the new inline TOML `*_file` reader — via the single extracted `_read_secret_file(path, *, preserve_whitespace)` helper. One rule, two call sites (D-06).

### Test module skeleton
**Source:** `tests/shared/config/test_cloud_target.py` (env-clear tuple + helper + sync `ControlSettings()` + `pytest.raises(match=)`) and `test_secret_file_resolution.py` (`tmp_path` secret files + `get_settings.cache_clear()` autouse fixture).
**Apply to:** all three new test modules. No async, no DB, no Redis.

---

## No Analog Found

None. Every construct this phase introduces (discriminated union, container validator, TOML source, inline `*_file` reader, bounded fields, fail-fast validators, config test modules) has a concrete in-repo analog listed above. The only genuinely new artifact is the registry *schema shape* itself, which RESEARCH Patterns 1-6 specify line-for-line.

## Metadata

**Analog search scope:** `src/phaze/config.py`, `src/phaze/routers/{pipeline,agent_s3,agent_push}.py`, `src/phaze/tasks/{controller,release_awaiting_cloud}.py`, `src/phaze/templates/pipeline/partials/`, `tests/shared/config/`.
**Files scanned:** config.py (1026 lines, full), 5 call-site modules (targeted), 2 test analogs (test_cloud_target.py full, test_secret_file_resolution.py head), grep across `src/` + `templates/`.
**Pattern extraction date:** 2026-07-03
