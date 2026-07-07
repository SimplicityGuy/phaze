---
quick_id: 260706-odc
slug: close-audit-review-items
status: complete
completed: 2026-07-07
commits:
  - 06e0c1aa  # 73-WR-03 config-load char guard
  - cced8684  # 73-IN-01 empty-string push config
  - 4ccdc4c4  # 74-IN-01/IN-02 cosmetic nits
---

# Quick Task 260706-odc — Summary

Closed the four low-severity / cosmetic review items surfaced by `2026.7.2-MILESTONE-AUDIT.md`
before milestone completion. GAP-01 left deferred as v2 PROV-01 (out of scope, as planned).

## What changed

1. **73-WR-03 (hardening)** — `src/phaze/config_backends.py`: added module-level `_PUSH_DEST_FORBIDDEN`
   (kept in sync with `PushFilePayload._DEST_HOST_FORBIDDEN`) and extended
   `ComputeBackend._require_dispatch_fields` to reject whitespace / shell metacharacters in `push_host`
   (required) and `ssh_user` (when present), id-tagged, at config-load — instead of surfacing as a
   `pydantic.ValidationError` deep inside the first `push_file` dispatch. 3 regression tests
   (`tests/shared/config/test_backend_registry.py`). Commit `06e0c1aa`.
2. **73-IN-01 (hardening)** — `src/phaze/tasks/push.py`: `_require_push_config` predicate
   `getattr(cfg, name) is None` → `not getattr(cfg, name)`, so an operator-set empty string
   (`PHAZE_PUSH_SSH_USER=""`) fails fast the same as `None`. 1 regression test
   (`tests/analyze/core/test_push_pipeline.py`). Commit `cced8684`.
3. **74-IN-01 / 74-IN-02 (cosmetic)** — `docs/multi-compute.md` grammar ("(do not restated here)" →
   "(not restated here)") + `docker-compose.cloud-agent.yml` header comment points at the moved test
   path `tests/agents/deployment/test_cloud_agent_compose.py`. Commit `4ccdc4c4`.

## Verification

- `uv run ruff check` + `uv run ruff format --check` clean on touched files.
- `uv run mypy src/phaze/config_backends.py src/phaze/tasks/push.py` — Success, no issues.
- `uv run pytest tests/analyze/services/test_backends.py tests/shared/config/test_backend_registry.py tests/analyze/core/test_push_pipeline.py tests/agents/deployment/test_cloud_agent_compose.py` → **116 passed** (against ephemeral test-DB on 5433/6380).
- All pre-commit hooks passed on every commit (no `--no-verify`).

## Out of scope (unchanged)

- **GAP-01** — N-compute-aware `recover_orphaned_work` (`reenqueue.py:374`): deferred as v2 PROV-01.
- 73-REVIEW WR-04 / AR-73-02 ledger RMW race — already closed by Phase 76 HARD-02.
