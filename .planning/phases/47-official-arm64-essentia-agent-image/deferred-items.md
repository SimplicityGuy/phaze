# Phase 47 — Deferred Items (out-of-scope discoveries)

These were surfaced during execution but are NOT caused by the current plan's
changes. Logged per the executor SCOPE BOUNDARY rule; do not fix here.

## semgrep: uv-missing-dependency-cooldown (pyproject.toml `[tool.uv]`)

- **Found during:** plan 47-03, Task 1 (editing `[tool.ruff.lint.per-file-ignores]`).
- **Finding:** `[MEDIUM] uv-missing-dependency-cooldown` at `pyproject.toml:176` — the
  pre-existing `[tool.uv]` block does not set `exclude-newer = "7 days"`.
- **Why deferred:** Pre-existing condition in an unrelated section (line 176); my edit
  was a one-line addition to the ruff per-file-ignores (~line 104). Adding a uv
  dependency-cooldown changes resolution behavior repo-wide and belongs in a dedicated
  supply-chain hardening change, not this parity-toolkit plan.
- **Suggested fix:** add `exclude-newer = "7 days"` under `[tool.uv]` and re-lock.
