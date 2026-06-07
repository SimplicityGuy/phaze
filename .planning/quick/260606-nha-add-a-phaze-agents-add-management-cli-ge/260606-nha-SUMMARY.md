---
phase: quick-260606-nha
plan: 01
subsystem: cli
tags: [cli, agents, provisioning, auth, docs]
requires:
  - phaze.routers.agent_auth.hash_token
  - phaze.database.async_session
  - phaze.models.agent.Agent
provides:
  - phaze.cli.main (console script `phaze`)
  - phaze.cli.add_agent
  - phaze.cli.validate_agent_id
  - phaze.cli.validate_scan_roots
  - phaze.cli.derive_queue_name
affects:
  - pyproject.toml ([project.scripts])
  - docs/deployment.md
  - docs/configuration.md
tech-stack:
  added: []
  patterns:
    - stdlib argparse with subparsers (agents add) for future extensibility
    - token minting via secrets.token_urlsafe, sha256 via existing hash_token (no reimplementation)
    - pre-DB validation (id charset + scan-root absoluteness) gating any session open
key-files:
  created:
    - src/phaze/cli/__init__.py
    - tests/test_cli/__init__.py
    - tests/test_cli/test_agents_add.py
  modified:
    - pyproject.toml
    - docs/deployment.md
    - docs/configuration.md
decisions:
  - "Reused agent_auth.hash_token verbatim; AGENT_ID_RE mirrors the agents.id_charset CheckConstraint exactly"
  - "Suppressed S105/B105 on the public non-secret TOKEN_PREFIX literal with targeted noqa/nosec rather than weakening S-rules"
  - "main() success/duplicate tests are sync (asyncio.run inside main cannot run under a live test loop); used a NullPool engine to avoid cross-loop connection reuse"
metrics:
  duration: ~12m
  completed: 2026-06-07
  tasks: 3
  files: 6
  new_module_coverage: 98.65%
  overall_coverage: 97.12%
  tests_passing: 1388
---

# Phaze Quick 260606-nha: phaze agents add Management CLI Summary

Added a stdlib-only `phaze agents add` CLI that mints an agent bearer token, inserts an `agents` row, and prints the cleartext token exactly once plus the derived `phaze-agent-<id>` queue name — replacing the hand-written SQL INSERT as the primary agent-registration path and documenting the PHAZE_AGENT_QUEUE convention in both docs.

## What Was Built

- **`src/phaze/cli/__init__.py`** — argparse CLI with `agents add` subcommand (subparsers used so future `list`/`revoke` slot in). Functions:
  - `validate_agent_id(id)` — raises `ValueError` unless `^[a-z0-9]+(-[a-z0-9]+)*$` (the exact `agents.id_charset` regex).
  - `validate_scan_roots(roots)` — raises `ValueError` on empty or non-absolute paths (via `pathlib.Path.is_absolute`, no `os.path`).
  - `derive_queue_name(id)` -> `f"phaze-agent-{id}"` (mirrors `agent_worker.py`).
  - `async add_agent(session, id, name, roots) -> str` — mints `"phaze_agent_" + secrets.token_urlsafe(32)`, hashes via the existing `hash_token`, inserts the `Agent`, commits, returns the cleartext token. Lets `IntegrityError` propagate.
  - `main(argv)` — parses, validates **before** any DB access (invalid id / relative root -> stderr + exit 1), runs the insert, prints the token once with a not-recoverable notice + the queue name; maps duplicate-id `IntegrityError` to a friendly stderr message + exit 1.
- **`pyproject.toml`** — `[project.scripts] phaze = "phaze.cli:main"` placed between `[project]` and the first `[tool.*]` table (CLAUDE.md section order).
- **Tests** — pure-function validation, DB-backed `add_agent` happy path (asserts `token_hash == hash_token(token)`, name, scan_roots) and duplicate-id, and `main()` exit-code branches (invalid id, relative root, success print, duplicate).
- **Docs** — `deployment.md` Step 3 now leads with `phaze agents add`; the SQL INSERT is demoted to a collapsible "under the hood / fallback" note. Step 4 annotates the `PHAZE_AGENT_QUEUE == phaze-agent-<agent_id>` convention. `configuration.md` documents the previously-undocumented `PHAZE_AGENT_QUEUE` required agent field, noting there is no queue column and that `agent_worker.py` asserts the derived name at startup.

## Tasks & Commits

| Task | Name | Commit |
| ---- | ---- | ------ |
| 1 | CLI module + `[project.scripts]` entry point | `15f5ed9` |
| 2 | CLI tests (98.65% module coverage) | `b8c8dd6` |
| 3 | deployment + configuration docs | `602488a` |

## Verification

- `uv run ruff format --check .` — 260 files formatted, clean.
- `uv run ruff check .` — all checks passed.
- `uv run mypy .` — success, 135 source files, strict-clean.
- Full suite (ephemeral Postgres :5433 + Redis :6380 via `just test-db`): **1388 passed**, overall coverage **97.12%** (gate 85%).
- New module `src/phaze/cli/__init__.py`: **98.65%** (only the `if __name__ == "__main__"` guard uncovered).
- All commits ran the frozen-SHA pre-commit hooks (ruff, ruff-format, bandit, local mypy) — no `--no-verify`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] S105/B105 hardcoded-secret false positive on the public token prefix**
- **Found during:** Task 1 (ruff check, then bandit pre-commit hook).
- **Issue:** Both ruff `S105` and bandit `B105` flagged the `TOKEN_PREFIX = "phaze_agent_"` literal as a possible hardcoded password. The prefix is a public, non-secret wire marker (the same literal already appears in `conftest.py`, which is bandit-excluded under `-x tests`).
- **Fix:** Added a single targeted `# noqa: S105  # nosec B105 — public wire prefix, not a secret` to that one line rather than disabling the S-rule set or removing the documented constant. S-rules remain fully enabled everywhere else.
- **Files modified:** `src/phaze/cli/__init__.py`.
- **Commit:** `15f5ed9`.

No other deviations — token format, hashing, and the `agents.id` CheckConstraint were left unchanged, and no new third-party dependency was added.

## Notes for Future Work

- Subparser scaffolding is in place; `agents list` / `agents revoke` can be added without restructuring `main()`.
- `main()`'s success and duplicate-id tests are synchronous because `asyncio.run` inside `main()` cannot execute under a live pytest-asyncio loop; they use a `NullPool` engine to avoid cross-event-loop connection reuse with the fixture-created schema.

## Self-Check: PASSED
