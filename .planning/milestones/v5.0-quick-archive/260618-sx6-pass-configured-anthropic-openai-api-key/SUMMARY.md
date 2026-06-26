---
quick_id: 260618-sx6
slug: pass-configured-anthropic-openai-api-key
date: 2026-06-19
status: complete
---

# Summary: Pass configured LLM API key to litellm (Bug A)

## What changed

- **`src/phaze/config.py`** — added `export_llm_api_keys(*, anthropic_api_key, openai_api_key)`:
  exports each present `SecretStr` into the bare `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`
  env vars litellm reads, only when that env var is unset (operator value wins), never
  logging the secret.
- **`src/phaze/tasks/controller.py`** — `startup()` calls the bridge right after
  `get_settings()`, so the control worker (which runs `generate_proposals`) wires the
  file-loaded key into litellm before any `acompletion()` call.

## Why

The key was deployed correctly (`ANTHROPIC_API_KEY_FILE` → real key, loaded into
`ControlSettings.anthropic_api_key`) but had zero consumers in `src/` — litellm reads
the bare env var, which was never set. Every `generate_proposals` raised
`AuthenticationError`; proposals had never succeeded in deployment.

## Tests

- `tests/test_config/test_llm_api_key_export.py` — 4 unit tests (export anthropic,
  export openai, operator-env wins, None is a no-op), with a snapshot/restore autouse
  fixture so the SUT's direct `os.environ` mutation can't leak.
- `tests/test_tasks/test_controller_startup_banner.py` — functional wiring guard:
  drives `controller.startup()` and asserts the key lands in `os.environ` (would have
  caught the original bug, where nothing called the bridge), with env restore.
- Updated the `fake_cfg` mocks in `test_controller_startup_banner.py` and
  `test_controller_reenqueue.py` to set `anthropic_api_key`/`openai_api_key = None`,
  since `startup()` now reads those fields.

## Verification

- Full suite (randomized, against ephemeral PG+Redis): **1888 passed, 0 failed**.
- Coverage **97.64%** (gate 85%).
- ruff, ruff-format, mypy (156 files), bandit, all pre-commit hooks: pass.

## Scope

phaze repo only. Bug B (nox panako/audfprint hostname-alias DNS failure) is a homelab
deploy fix handled separately.
