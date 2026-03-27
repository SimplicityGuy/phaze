# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**phaze** — A music alignment tool. Python 3.13, MIT licensed.

## Development Setup

- **Python**: 3.13 exclusively
- **Package manager**: `uv` only — never use bare `pip`, `python`, `pytest`, or `mypy`. Always prefix with `uv run`.
- **Pre-commit**: Must be installed and active. All hooks must pass before commits.

### Key Commands

```bash
uv sync                    # Install dependencies
uv run pytest              # Run tests
uv run pytest tests/test_foo.py::test_bar  # Run a single test
uv run pytest --cov --cov-report=term-missing  # Run tests with coverage
uv run ruff check .        # Lint
uv run ruff format .       # Format
uv run mypy .              # Type check
pre-commit run --all-files # Run all pre-commit hooks
```

## Code Quality

### Ruff Configuration

Line length: 150. Target: Python 3.13.

**Enabled rule sets**: `ARG`, `B`, `C4`, `E`, `F`, `I`, `PLC`, `PTH`, `RUF`, `S`, `SIM`, `T20`, `TCH`, `UP`, `W`, `W191`

**Ignored rules**: `B008`, `C901`, `E501`, `S101`

**Per-file ignores**: Allow `T201` (print) in CLI/entry points and tests. Tests also ignore `PLC` and `S105`.

**isort**: `lines-after-imports = 2`, `combine-as-imports = true`, `split-on-trailing-comma = true`, `force-sort-within-sections = true`. Set `known-first-party` to project package name.

**Format**: `quote-style = "double"`, `indent-style = "space"`, `docstring-code-format = false`.

### Mypy Configuration

```toml
[tool.mypy]
python_version = "3.13"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
disallow_untyped_decorators = true
no_implicit_optional = true
warn_redundant_casts = true
warn_unused_ignores = true
warn_no_return = true
warn_unreachable = true
strict_equality = true
explicit_package_bases = true
exclude = "^tests/"
```

Override for tests: `disallow_untyped_decorators = false`.

### Pre-commit Hooks

Use frozen SHAs (not just tags) for all hooks. Required hooks:

- **pre-commit-hooks**: large files, merge conflicts, TOML, YAML, JSON, EOF fixer, trailing whitespace, mixed line endings
- **ruff-pre-commit**: `ruff --fix` + `ruff-format`
- **bandit**: `-x tests -s B608`
- **check-jsonschema**: GitHub workflows/actions validation
- **actionlint**: GitHub Actions linting
- **yamllint**: strict mode
- **shellcheck-py**: `--shell=bash --severity=warning`
- **Local mypy hook**: `uv run mypy .` with `pass_filenames: false`

## Testing

- Minimum **85% code coverage** required
- Upload coverage to Codecov with service-specific flags
- Codecov config: precision 2, round down, range 70-100%, project target auto with 1% threshold, patch target 80% with 5% threshold

## Workflow: Features and PRs

- **Every feature gets its own git worktree** — no cross-contamination between features
- **Every feature gets its own PR** — one PR per feature, no mixing unrelated changes
- Never push directly to main

## CI (GitHub Actions)

Follow the discogsography pattern:

- **Reusable workflows** via `workflow_call` — separate jobs for code quality, tests, security
- **Code quality job**: runs all pre-commit hooks
- **Test job**: runs pytest with coverage, uploads to Codecov with flags and `disable_search: true`
- **Security job**: pip-audit, bandit, Semgrep, TruffleHog secret scanning
- **Concurrency groups** with `cancel-in-progress` on PR workflows
- Emoji prefixes on all step names

## Code Style

- 150-character line length
- Type hints on all functions
- Double quotes for strings
- PEP 8 conventions
- `pyproject.toml` section order: `[build-system]` → `[project]` → `[project.scripts]` → `[tool.*]` → `[dependency-groups]`, with alphabetically sorted dependencies
