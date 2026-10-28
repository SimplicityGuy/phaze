# Public Just recipe contract

This file inventories the public recipes printed by `just --summary`. It is executable
documentation: `tests/shared/test_just_recipe_contract.py` fails when the inventory and the
justfile diverge. A recipe is public only when it is listed by bare `just`; private shell helpers
belong in `scripts/`.

The classifications are:

- **external contract** — invoked by Beadhive, CI, an update script, or a maintained runbook;
- **operator convenience** — a supported interactive command for a current development or
  deployment task;
- **internal helper** — public because it is useful for focused diagnosis, but normally reached
  through another recipe;
- **historical one-off** — retained only to reproduce named historical evidence. New recipes must
  not enter this class; either give the capability a current name/use or archive the tool.

The final column is the verified consumer or current use. Historical design documents are evidence,
not consumers; where a recipe exists to reproduce historical evidence, that is stated explicitly.

| Recipe | Classification | Verified consumer or current operator use | Old name (alias) |
|---|---|---|---|
| `build-docker` | operator convenience | local application image build | `docker-build` |
| `build-image-arm64` | operator convenience | native arm64 agent image fallback build | `image-build-arm64` |
| `build-tailwind` | internal helper | `install`, `up-dev`, and `test-browser` UI asset build | `tailwind` |
| `check` | external contract | Beadhive postland/union validation and manual full gate | — |
| `check-all` | external contract | Beadhive molecule validation | — |
| `check-branch-coverage` | external contract | per-bead branch-coverage runbook | `branch-check` |
| `check-docs` | operator convenience | deterministic maintained-document and source-cited diagram-asset gate | `docs-check` |
| `check-fast` | external contract | Beadhive check/submit/merge validation | — |
| `check-parity` | operator convenience | local arm64/x86 analysis parity mirror | `parity-check` |
| `check-worker-health` | operator convenience | application-server SAQ worker probe | `worker-health` |
| `check-worktree-integrity` | internal helper | first step of `check` and `check-fast` (checkout-integrity probe, phaze-ofrxb); focused diagnosis of a hollowed seat | `worktree-integrity` |
| `clean-integration-test` | operator convenience | reap stale integration-test runs this checkout owns (replaces `integration-test-down RUN_ID`) | — |
| `combine-coverage` | external contract | `.github/workflows/tests.yml` coverage fan-in | `coverage-combine` |
| `create-db-revision` | operator convenience | create a reviewed Alembic revision | `db-revision` |
| `default` | external contract | bare `just` command-selection contract | — |
| `detect-code-changes` | external contract | `.github/workflows/ci.yml` docs-only gate | — |
| `down` | operator convenience | application-server topology teardown | — |
| `down-agent` | operator convenience | file-server agent topology teardown | `agent-down` |
| `down-all` | operator convenience | combined application/agent topology teardown | — |
| `down-benchmark-db` | operator convenience | dispose the dedicated synthetic benchmark database | `benchmark-db-down` |
| `down-cloud-agent` | operator convenience | cloud-agent deployment teardown | `cloud-agent-down` |
| `down-dev` | operator convenience | live-reload development topology teardown | — |
| `down-test-db` | operator convenience | guarded shared-harness teardown after all seats stop | `test-db-down` |
| `downgrade-db` | operator convenience | migration rollback during development | `db-downgrade` |
| `download-models` | external contract | model provisioning runbooks and image parity workflows | — |
| `dump-parity` | external contract | `.github/workflows/docker-publish.yml` parity jobs | `parity-dump` |
| `explain-benchmark-db` | operator convenience | measure query plans and pipeline-stat latency | `benchmark-explain` |
| `gc-test-dbs` | operator convenience | reap old unregistered test databases | `test-db-gc` |
| `install` | external contract | README local UI bootstrap | — |
| `install-test-browser` | operator convenience | one-time local browser installation | `test-browser-install` |
| `install-test-browser-ci` | external contract | browser CI installation | `test-browser-install-ci` |
| `lint` | internal helper | `check` and focused Ruff validation | — |
| `list-test-db-seats` | operator convenience | inspect test-seat ownership and liveness | `test-db-seats` |
| `logs` | operator convenience | follow application-server logs | — |
| `logs-worker` | operator convenience | follow application-server worker logs | `worker-logs` |
| `measure-analyze-workspace` | operator convenience | measure Analyze workspace and pipeline-stat scaling | `benchmark-analyze` |
| `measure-corpus-distribution` | historical one-off | reproduce ADR-0012 (verification fidelity and operator attribution) archive-bound measurements | `corpus-distribution` |
| `provision-test-db-seat` | external contract | AGENTS.md per-worktree isolation contract | `test-db-for` |
| `ps-docker` | operator convenience | inspect the application-server topology | `docker-ps` |
| `push-image` | operator convenience | local GHCR application-image publish fallback | `image-push` |
| `rebuild` | operator convenience | rebuild/restart the application-server topology | — |
| `reclaim-test-db-seats` | operator convenience | reclaim stale registered test seats | `test-db-reclaim` |
| `refresh-repowise-coverage` | operator convenience | refresh the local Repowise coverage map | `repowise-coverage` |
| `refresh-repowise-coverage-ci` | operator convenience | refresh Repowise from a CI coverage artifact | `repowise-coverage-ci` |
| `regen-parity-golden` | operator convenience | regenerate a local x86 parity reference | `parity-golden-regen` |
| `release-test-db-seat` | external contract | AGENTS.md non-destructive seat cleanup | `test-db-release` |
| `run-bandit` | external contract | `.github/workflows/security.yml` and the local pre-commit `bandit` hook | `bandit` |
| `run-benchmarks` | operator convenience | one-shot benchmark suite: up, seed, explain, analyze, always down | — |
| `run-pip-audit` | external contract | `.github/workflows/security.yml` dependency audit | `pip-audit` |
| `run-pre-commit` | external contract | `check-all` and CI code-quality workflow | `pre-commit` |
| `seed-benchmark-db` | operator convenience | provision the reusable synthetic scale corpus | `benchmark-seed` |
| `setup` | external contract | Beadhive worktree provisioning and dependency refresh | — |
| `show-db-history` | operator convenience | inspect Alembic history | `db-history` |
| `show-db-revision` | operator convenience | inspect the active Alembic revision | `db-current` |
| `test` | operator convenience | fast fail-first local pytest loop | — |
| `test-alerts` | internal helper | `check-all`; CI code-quality workflow (promtool test rules against alerts/phaze-alerts.test.yml) | `alerts-test` |
| `test-browser` | external contract | browser CI and local UI verification | — |
| `test-bucket` | external contract | `.github/workflows/tests.yml` test matrix | — |
| `test-cov` | internal helper | full-suite coverage primitive used by validation recipes | — |
| `test-cov-parallel` | internal helper | default local full-suite runner | — |
| `test-fast` | internal helper | `check-fast` change-selected pytest step | — |
| `test-integration` | operator convenience | isolated full-suite integration diagnosis | `integration-test` |
| `test-validate` | internal helper | full-suite runner used by `check` and `check-all` | — |
| `test-validate-serial` | internal helper | explicit serial full-suite fallback | — |
| `typecheck` | internal helper | `check` and focused mypy validation | — |
| `up` | operator convenience | production application-server bring-up | — |
| `up-agent` | operator convenience | file-server agent bring-up | — |
| `up-all` | operator convenience | combined local application/agent bring-up | — |
| `up-benchmark-db` | operator convenience | start or verify the dedicated synthetic benchmark database | `benchmark-db-up` |
| `up-cloud-agent` | operator convenience | cloud-agent deployment bring-up | `cloud-agent-up` |
| `up-dev` | operator convenience | live-reload UI development bring-up | — |
| `up-test-db` | internal helper | shared harness bootstrap used by seat provisioning | `test-db` |
| `update-hooks` | operator convenience | frozen pre-commit hook refresh | — |
| `upgrade-db` | operator convenience | migrate the configured database | `db-upgrade` |
| `upgrade-lock` | external contract | `scripts/update-project.sh` dependency refresh | `lock-upgrade` |
| `validate-compose` | operator convenience | local Compose syntax validation | `docker-compose-validate` |
| `validate-dockerfiles` | operator convenience | local Dockerfile lint mirror | `docker-validate` |

## Naming, groups and aliases

The justfile header states both rules, and `tests/shared/test_just_recipe_contract.py` pins them:

- **Naming: verb first, then the noun it acts on** (`upgrade-db`, `build-docker`, `logs-worker`).
  The ambiguous cases are settled by four rules:
  - Compose/docker subcommands are verbs: `up`, `down`, `logs`, `ps`, `build`, `rebuild`,
    `restart`.
  - `test` is a **verb** when the recipe runs pytest (`test`, `test-cov`, `test-fast`,
    `test-browser`, `test-integration`, `test-alerts`). `test-db` is a **noun** (the shared
    test-database harness), so its recipes are `up-test-db`, `down-test-db`, `gc-test-dbs` and
    `<verb>-test-db-seat(s)` for the per-worktree seats.
  - `check-*` returns a pass/fail verdict (`check`, `check-fast`, `check-all`, `check-docs`, ...).
  - A recipe named only for the tool it runs, with no truer verb, is `run-<tool>` (`run-bandit`,
    `run-pre-commit`, `run-pip-audit`).

  `check`, `check-fast`, `check-all` and `setup` are never renamed: Beadhive's validation
  boundaries and worktree provisioning call them.
- **Groups.** A recipe sits in the `[group()]` named for the thing it operates on: `setup`,
  `stack`, `docker`, `gate`, `lint`, `test`, `ci`, `harness`, `benchmark`, `db`, `repowise`,
  `security`, `models`, `maintenance`.
- **Docs.** Each `[doc()]` is one line of at most 60 characters, so `just --list` never wraps; the
  detail lives in the comment block above the recipe.
- **Aliases.** Every renamed recipe keeps its old name as a `[private]` just `alias` (the last
  column above), so CI, scripts, the Beadhive config, muscle memory and incident write-ups that
  quote an old name all still resolve. Private aliases keep `just --list` to one line per recipe;
  `just --summary` (what this inventory tracks) lists only the new names. phaze-aje5d renamed 47
  recipes this way; `tests/shared/test_just_recipe_contract.py` fails if an old name stops running
  its recipe.

## Compatibility rules

`default`, `setup`, `check-fast`, and `check-all` are hard external contracts. The CI-facing
recipes named above may change implementation only with their callers in the same commit. Validation
grades remain distinct: `test` is local fail-fast iteration, `check-fast` is change-selected,
`check` is the full coverage gate, and `check-all` adds the complete pre-commit suite.

`test-ci` was deleted because no workflow or script consumed it and it omitted the explicit line
floor. The duplicate `sync` recipe was deleted; `setup` is now the one dependency-sync primitive,
used by `install` and `scripts/update-project.sh`. The former broad `security` name now reads
`run-bandit` (once `bandit`). Its aggregate, `security-all`, was removed in phaze-aje5d with the other
recipes below.

## Integration tests: run, then clean up

```
just test-integration          # one run on its own Postgres + Valkey; torn down on success, failure or Ctrl-C
just clean-integration-test    # reap leftovers (e.g. after kill -9); never a live run, never another worktree's
```

## Removed recipes

Operator decisions 9 and 10c on bead phaze-aje5d (2026-10-28) removed these outright, with no
alias: `docker-shell`, `fmt`, `lint-fix`, `security-all`, `vulture`, `worker-restart`,
`image-push-arm64`, `integration-test-down` and `test-file`. Their direct equivalents are
`docker compose -f docker-compose.yml exec api bash`, `uv run ruff format .`,
`uv run ruff check . --fix`, `just run-pip-audit && just run-bandit`,
`uv run vulture src/phaze vulture_whitelist.py --min-confidence 80`,
`docker compose -f docker-compose.yml restart worker`, `bash scripts/container-images.sh push arm64 <tag>`,
`just clean-integration-test` and `uv run pytest <file> -x -v`.
