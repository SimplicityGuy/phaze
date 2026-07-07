---
quick_id: 260706-odc
slug: close-audit-review-items
description: Close the four carried review items surfaced by the 2026.7.2 milestone audit
created: 2026-07-07
status: in-progress
---

# Quick Task 260706-odc: Close 2026.7.2 carried review items

Close the four low-severity/cosmetic review items surfaced (and recorded as deferred debt) by
the `2026.7.2-MILESTONE-AUDIT.md` before milestone completion. **GAP-01 is intentionally OUT of
scope** — it is the already-deferred v2 PROV-01 feature, not a fix.

Scope-locked to exactly these four items. No behavior change to the verified N-compute dispatch
path. Each logical change is an atomic commit.

## Tasks

### Task 1 — 73-WR-03 (hardening): id-tagged char guard on compute `push_host`/`ssh_user`
- **Files:** `src/phaze/config_backends.py`, `tests/shared/config/test_backend_registry.py`
- **Action:** `ComputeBackend._require_dispatch_fields` only fails fast on a *missing* `push_host`,
  never on an *unsafe* one — a stray space / shell metachar in `push_host` or `ssh_user` in
  `backends.toml` passes config-load and only surfaces as a `pydantic.ValidationError` deep inside
  the first `push_file` dispatch (`PushFilePayload._dest_host_safe`). Add a module-level
  `_PUSH_DEST_FORBIDDEN` frozenset (kept in sync with `PushFilePayload._DEST_HOST_FORBIDDEN` in
  `schemas/agent_tasks.py`) and extend `_require_dispatch_fields` to reject those chars in
  `push_host` (required) and `ssh_user` (only when present) with the id-tagged `ValueError`. Keep the
  presence checks and `ssh_user`-optional semantics unchanged.
- **Verify:** new tests — space in `push_host` and metachar in `ssh_user` each raise
  `ValidationError` naming the entry id; a clean compute backend still constructs; existing registry
  suite green.
- **Done:** `uv run pytest tests/shared/config/test_backend_registry.py -q` green; ruff + mypy clean.

### Task 2 — 73-IN-01 (hardening): treat empty-string push config as missing
- **Files:** `src/phaze/tasks/push.py`, `tests/analyze/core/test_push_pipeline.py`
- **Action:** `_require_push_config`'s predicate `getattr(cfg, name) is None` lets an operator-set
  empty string (`PHAZE_PUSH_SSH_USER=""`) pass and fall through as the `dest_ssh_user or
  cfg.push_ssh_user` source, producing a broken `"@host:..."` remote spec. Change the predicate to
  `not getattr(cfg, name)` so `""` fails fast the same as `None`.
- **Verify:** new test — `push_ssh_user=""` raises the `"missing required push config"` RuntimeError.
- **Done:** `uv run pytest tests/analyze/core/test_push_pipeline.py -q` green; ruff + mypy clean.

### Task 3 — 74-IN-01 / 74-IN-02 (cosmetic): doc + comment nits
- **Files:** `docs/multi-compute.md`, `docker-compose.cloud-agent.yml`
- **Action:** fix grammar `docs/multi-compute.md:176` "(do not restated here)" → "(not restated
  here)"; update the moved test path in `docker-compose.cloud-agent.yml:22`
  `tests/test_deployment/...` → `tests/agents/deployment/test_cloud_agent_compose.py`. Prose/comment
  only, no behavior change.
- **Done:** grep confirms both strings updated; compose still `yaml.safe_load`-valid.

## Verification (whole task)
- `uv run ruff check` + `uv run mypy` clean on touched source files.
- `uv run pytest tests/analyze/services/test_backends.py tests/shared/config/test_backend_registry.py tests/analyze/core/test_push_pipeline.py tests/agents/deployment/test_cloud_agent_compose.py -q` green.
- All pre-commit hooks pass; never `--no-verify`.

## Out of scope
- GAP-01 (N-compute-aware `recover_orphaned_work`) — deferred as v2 PROV-01.
- 73-REVIEW WR-04 / AR-73-02 (ledger RMW race) — already closed by Phase 76 HARD-02.
