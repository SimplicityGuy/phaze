---
quick_id: 260618-sx6
slug: pass-configured-anthropic-openai-api-key
date: 2026-06-19
---

# Quick Task: Pass configured LLM API key to litellm

## Problem (Bug A — live incident, June 2026)

Every `generate_proposals` job on the control worker failed with
`litellm.exceptions.AuthenticationError: Missing Anthropic API Key`. The key *was*
deployed correctly via the `<VAR>_FILE` secret convention
(`ANTHROPIC_API_KEY_FILE=/run/secrets/nlq_api_key`, a real 109-byte key) and loaded
into `ControlSettings.anthropic_api_key`. But the only litellm call site
(`services/proposal.py:213` `acompletion(...)`) passes no `api_key=`, and nothing
exported the bare `ANTHROPIC_API_KEY` to `os.environ` that litellm reads. There were
zero consumers of `settings.anthropic_api_key` anywhere in `src/`. Net effect:
proposals had never succeeded in deployment.

## Fix

- `config.py`: add `export_llm_api_keys(*, anthropic_api_key, openai_api_key)` —
  exports each present `SecretStr` into `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, only
  when the bare env var is unset (operator-set value wins), never logging the secret.
  litellm then resolves the provider key for whatever model is configured.
- `tasks/controller.py`: `startup()` calls the bridge right after `get_settings()`.

## Tests

- `tests/test_config/test_llm_api_key_export.py` — unit: exports anthropic/openai,
  does not override operator env, None is a no-op.
- `tests/test_tasks/test_controller_startup_banner.py` — functional wiring guard:
  drives `controller.startup()` and asserts the key lands in `os.environ`
  (would have caught the original bug, where nothing called the bridge).

## Scope

phaze repo only. Bug B (nox panako/audfprint hostname alias) is a homelab deploy fix,
handled separately.
