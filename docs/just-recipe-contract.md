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

| Recipe | Classification | Verified consumer or current operator use |
|---|---|---|
| `agent-down` | operator convenience | file-server agent topology teardown |
| `bandit` | internal helper | `security-all`; focused local Python SAST |
| `branch-check` | external contract | per-bead branch-coverage runbook |
| `check` | external contract | Beadhive postland/union validation and manual full gate |
| `check-all` | external contract | Beadhive molecule validation |
| `check-fast` | external contract | Beadhive check/submit/merge validation |
| `cloud-agent-down` | operator convenience | cloud-agent deployment teardown |
| `cloud-agent-up` | operator convenience | cloud-agent deployment bring-up |
| `corpus-distribution` | historical one-off | reproduce ADR-0012 archive-bound measurements |
| `coverage-combine` | external contract | `.github/workflows/tests.yml` coverage fan-in |
| `db-current` | operator convenience | inspect the active Alembic revision |
| `db-downgrade` | operator convenience | migration rollback during development |
| `db-history` | operator convenience | inspect Alembic history |
| `db-revision` | operator convenience | create a reviewed Alembic revision |
| `db-upgrade` | operator convenience | migrate the configured database |
| `default` | external contract | bare `just` command-selection contract |
| `detect-code-changes` | external contract | `.github/workflows/ci.yml` docs-only gate |
| `docker-build` | operator convenience | local application image build |
| `docker-compose-validate` | operator convenience | local Compose syntax validation |
| `docker-ps` | operator convenience | inspect the application-server topology |
| `docker-shell` | operator convenience | open an application API shell |
| `docker-validate` | operator convenience | local Dockerfile lint mirror |
| `down` | operator convenience | application-server topology teardown |
| `down-all` | operator convenience | combined application/agent topology teardown |
| `down-dev` | operator convenience | live-reload development topology teardown |
| `download-models` | external contract | model provisioning runbooks and image parity workflows |
| `fmt` | operator convenience | apply Ruff formatting |
| `image-build-arm64` | operator convenience | native arm64 agent image fallback build |
| `image-push` | operator convenience | local GHCR application-image publish fallback |
| `image-push-arm64` | operator convenience | local GHCR arm64-image publish fallback |
| `install` | external contract | README local UI bootstrap |
| `integration-test` | operator convenience | isolated full-suite integration diagnosis |
| `integration-test-down` | operator convenience | recover stale dedicated integration containers |
| `lint` | internal helper | `check` and focused Ruff validation |
| `lint-fix` | operator convenience | apply Ruff lint fixes |
| `lock-upgrade` | external contract | `scripts/update-project.sh` dependency refresh |
| `logs` | operator convenience | follow application-server logs |
| `parity-check` | operator convenience | local arm64/x86 analysis parity mirror |
| `parity-dump` | external contract | `.github/workflows/docker-publish.yml` parity jobs |
| `parity-golden-regen` | operator convenience | regenerate a local x86 parity reference |
| `perf-db-down` | historical one-off | dispose the retained performance-measurement database |
| `perf-db-up` | historical one-off | reproduce retained large-corpus performance evidence |
| `perf-explain` | historical one-off | reproduce the retained pipeline-stats query measurement |
| `perf-seed` | historical one-off | reproduce the retained synthetic performance corpus |
| `pip-audit` | internal helper | `security-all`; focused dependency audit |
| `pre-commit` | external contract | `check-all` and CI code-quality workflow |
| `rebuild` | operator convenience | rebuild/restart the application-server topology |
| `repowise-coverage` | operator convenience | refresh the local Repowise coverage map |
| `repowise-coverage-ci` | operator convenience | refresh Repowise from a CI coverage artifact |
| `security-all` | operator convenience | aggregate local dependency/SAST checks |
| `setup` | external contract | Beadhive worktree provisioning and dependency refresh |
| `tailwind` | internal helper | `install`, `up-dev`, and `test-browser` UI asset build |
| `test` | operator convenience | fast fail-first local pytest loop |
| `test-browser` | external contract | browser CI and local UI verification |
| `test-browser-install` | operator convenience | one-time local browser installation |
| `test-browser-install-ci` | external contract | browser CI installation |
| `test-bucket` | external contract | `.github/workflows/tests.yml` test matrix |
| `test-cov` | internal helper | full-suite coverage primitive used by validation recipes |
| `test-cov-parallel` | internal helper | default local full-suite runner |
| `test-db` | internal helper | shared harness bootstrap used by seat provisioning |
| `test-db-down` | operator convenience | guarded shared-harness teardown after all seats stop |
| `test-db-for` | external contract | AGENTS.md per-worktree isolation contract |
| `test-db-gc` | operator convenience | reap old unregistered test databases |
| `test-db-reclaim` | operator convenience | reclaim stale registered test seats |
| `test-db-release` | external contract | AGENTS.md non-destructive seat cleanup |
| `test-db-seats` | operator convenience | inspect test-seat ownership and liveness |
| `test-fast` | internal helper | `check-fast` change-selected pytest step |
| `test-file` | operator convenience | focused test-file iteration |
| `test-validate` | internal helper | full-suite runner used by `check` and `check-all` |
| `test-validate-serial` | internal helper | explicit serial full-suite fallback |
| `typecheck` | internal helper | `check` and focused mypy validation |
| `up` | operator convenience | production application-server bring-up |
| `up-agent` | operator convenience | file-server agent bring-up |
| `up-all` | operator convenience | combined local application/agent bring-up |
| `up-dev` | operator convenience | live-reload UI development bring-up |
| `update-hooks` | operator convenience | frozen pre-commit hook refresh |
| `vulture` | operator convenience | non-blocking dead-code candidate sweep |
| `worker-health` | operator convenience | application-server SAQ worker probe |
| `worker-logs` | operator convenience | follow application-server worker logs |
| `worker-restart` | operator convenience | restart the application-server worker |

## Compatibility rules

`default`, `setup`, `check-fast`, and `check-all` are hard external contracts. The CI-facing
recipes named above may change implementation only with their callers in the same commit. Validation
grades remain distinct: `test` is local fail-fast iteration, `check-fast` is change-selected,
`check` is the full coverage gate, and `check-all` adds the complete pre-commit suite.

`test-ci` was deleted because no workflow or script consumed it and it omitted the explicit line
floor. The duplicate `sync` recipe was deleted; `setup` is now the one dependency-sync primitive,
used by `install` and `scripts/update-project.sh`. The former broad `security` name now reads
`bandit`, while `security-all` accurately describes the two local checks it aggregates rather than
claiming to reproduce every CI security scanner.
