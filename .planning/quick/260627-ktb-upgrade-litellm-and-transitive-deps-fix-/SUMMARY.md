---
status: complete
date: 2026-06-27
---

# Quick Task 260627-ktb — Supply-chain cooldown (relative 7-day window)

## Original ask

`update-project.sh --major` flagged four packages it couldn't auto-bump
(litellm, importlib-metadata, pydantic-core, typer). Investigate why, get them
upgraded, fix breakage. Evolved into: enforce a uniform supply-chain cooldown.

## Why those four won't upgrade (constraint-blocked, not a resolver bug)

`update-project.sh` reports *PyPI-latest*, not *constraint-feasible* versions:

| Package | Held by |
|---|---|
| litellm | Deliberate `<1.86.0` cap (supply-chain pin) |
| importlib-metadata | litellm requires `importlib-metadata<9.0` |
| typer | huggingface-hub (via litellm) requires `typer<0.26.0` |
| pydantic-core | 2.47.0 ships only with pydantic 2.14.0a1 (alpha) |

## Approach (final)

A blocking semgrep hook (`uv-missing-dependency-cooldown`) requires a
`[tool.uv] exclude-newer`. Adopted the **canonical relative window**
`exclude-newer = "7 days"` across all three pyproject.toml (root + audfprint +
panako) — no fixed date, no per-package overrides.

A relative window only resolves while every dependency floor is already ≥7 days
old. Two sets of floors were too fresh:

1. **Yesterday's `chore: update deps` (c8574dc)** bumped 7 floors to <7-day-old
   versions — reverted to their prior values (all ≥7 days old):
   alembic 1.18.4, fastapi 0.138.0, litellm 1.85.6, mutagen 1.47.0,
   numpy 2.4.6, greenlet 3.5.2, ruff 0.15.18.
2. **redis** — #160 (a routine Dependabot bump, not a security pin) pinned
   `>=8.0.1` (2026-06-23, <7d old). Relaxed to `>=8.0.0` (2026-05-28, ≥7d old) —
   still redis 8; Dependabot will lift it back to 8.0.1 once that clears the
   cooldown.

## Resilience (so the window stays satisfiable)

- **Dependabot cooldown** (`.github/dependabot.yml`): `cooldown.default-days: 7`
  on all three ecosystems — Dependabot never opens a PR pinning a floor younger
  than the window, so it never fights `uv lock`.
- **update-project.sh** `ensure_cooldown_window()`: re-asserts
  `exclude-newer = "7 days"` uniformly across manifests; `--major` report notes
  that `uv pip list --outdated` ignores the cooldown.

## Other changes

- Root pyproject.toml reorganized: headings ordered (build-system, project,
  project.scripts, `[tool.*]` alphabetized, dependency-groups) and settings
  sorted within each table.

## Verification

- ruff check / ruff format --check (ruff reverted to 0.15.18) and mypy: all green.
- pytest: unit suite passes; integration tests error locally on Postgres :5432
  (no local DB) — pre-existing/environmental. CI runs them with services.
- pre-commit (check-toml, yamllint, shellcheck, shfmt, EOF, whitespace): pass.

## Lock effect

`uv.lock` files reverted the fresh versions to ≥7-day-old releases (redis 8.0.0,
litellm 1.85.6, numpy 2.4.6, openai 2.43.0, greenlet 3.5.2, ruff 0.15.18, …) and
record the relative-window placeholder. As each newer release ages past 7 days,
Dependabot/`uv lock --upgrade` adopt it normally.
