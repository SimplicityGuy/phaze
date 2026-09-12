# Structural maintenance audit — 2026-09-12

## Verdict

The expanded structural-maintenance boundaries are coherent and retain their recorded behavior.
All six compatibility facades remain necessary. The audit removed no shim: each facade has a live
production or framework consumer, and every private compatibility name retained by the
implementation is either a current patch/import seam or an explicit public-shape commitment in the
[rebaseline](structural-maintenance-rebaseline-2026-09-11.md). Repository-only reachability cannot
prove that a documented exported type has no external consumer.

This is a behavior-preserving audit, not a claim that history-derived health improved. Repowise's
whole-molecule change-risk classification is elevated because the assembled diff is large and
distributed. That signal sets review depth; it does not contradict the focused behavioral ruler.

## Scope and method

The audit began from epic revision `9c9d594430606562b20731349d241b471b0f972e`, after the recovery
and cloud test organization landed. It used the same ruler as the rebaseline:

1. Read every facade and capability module at the live revision.
2. Parse tracked Python ASTs under `src/`, `tests/`, `scripts/`, and `alembic/` for direct imports,
   module-attribute reads, and literal patch paths.
3. Search maintained docs, environment examples, Compose files, and `pyproject.toml` for runtime
   entry points and configuration references.
4. Verify capability modules do not import back through their facade and that framework callables
   still resolve through the stable path.
5. Exercise configuration, review/proposal, execution, recovery/cloud, authorization, docs, and
   import-boundary suites on an isolated Postgres and Redis seat.
6. Refresh Repowise at the committed audit revision and run the repository's terminal gates.

The pre-change collection was 9,032 tests total: 8,843 selected and 189 deselected by the default
suite configuration. The terminal contract guards add four explicit assertions without replacing
or parameterizing away an existing scenario.

## Ownership and compatibility decisions

| Stable facade | Owned capabilities | Live consumer evidence | Decision |
| ------------- | ------------------ | ---------------------- | -------- |
| `phaze.config` | shared, control, and agent Pydantic settings domains | application composition, Alembic, controller and agent entry points, services, scripts, and literal `phaze.config.get_settings` patches | **Retain.** `Settings is ControlSettings`, the import-time singleton, role dispatch, aliases, validators, and secret/env precedence remain compatibility behavior. |
| `phaze.services.review` | changes, tag-write, dedupe, and CUE read models | shell, duplicate, and tag routers plus facade patch paths | **Retain.** The facade injects mutable scan bounds and collaborators; concrete readers own degrade-safe nested transactions. `ProposalWorkspacePage` currently has no direct repository import, but it is a documented exported row shape and cannot be proven external-consumer-free. |
| `phaze.services.proposal` | context, parsing, persistence, and provider edge | controller startup, proposal task, response-model imports, and `acompletion`/`asyncio`/`pg_insert` patch paths | **Retain.** The facade preserves provider and persistence injection plus supported response types and exception identities. |
| `phaze.tasks.execution` | guarded destructive filesystem engine | the agent worker function registry and filesystem/reporting patch seams | **Retain.** The registered task object, containment, no-clobber, copy/verify/delete semantics, progress, reporting, and terminal-token behavior stay on the established path. |
| `phaze.tasks.reenqueue` | backfill, pure recovery policy, database queries, and replay/regeneration adapters | controller startup, operator recovery route, pipeline reads, and deferred/private service imports | **Retain.** Transaction release before network replay, queue ownership, live-key deduplication, owner routing, and per-row failure isolation remain facade behavior. |
| `phaze.tasks.reconcile_cloud_jobs` | read-only observation/classification | controller cron registration and Kueue's private `_reconcile_one` import | **Retain.** The deferred backend import still breaks a real cycle; advisory-lock, callback, commit, cleanup, redrive, and spill ordering remain effect-layer responsibilities. |

The maintained ownership table and source-cited import diagram live in
[Project Structure](project-structure.md#structural-capability-ownership). The executable terminal
guard rejects a reversed owner-to-facade dependency, a missing registered consumer, changed object
identity, or an ownership map that omits a boundary.

## Behavioral audit

| Contract | Evidence exercised |
| -------- | ------------------ |
| Public imports and object identities | facade characterization/parity suites plus `test_structural_boundary_contracts.py` |
| Authorization and control/agent isolation | internal-agent auth/request guards and the subprocess ORM-import boundary tests |
| Configuration | role split, field/default serialization, Pydantic construction, aliases, validator ordering, cache/import behavior, Redis passwords, and secret-file precedence |
| Review and proposal | eight capability families covering degradation, pagination, ordering, parsing/salvage, context, provider wire shape, rate limiting, and transaction-neutral persistence |
| Destructive filesystem semantics | containment before mutation, destination no-clobber, same/cross-filesystem movement, bounded copy, hash verification, committed-copy markers, crash replay, and reporting/terminal order |
| Recovery | completion/orphan classification, owner planning, ledger backfill, queue replay, regeneration, per-row isolation, and transaction release before network effects |
| Cloud reconciliation | callback primacy, pending/admission/terminal/redrive/wedge transitions, advisory locking, node-loss budgets, commit-before-cleanup, and the deferred backend cycle break |

No production source changed in this terminal bead. Its changes are maintained architecture and
ownership documentation plus executable contract guards. The absence of production edits is
deliberate: the consumer proof did not authorize any compatibility deletion.

## Same-ruler structural evidence

Physical lines use the same `wc -l` measure as the rebaseline. They show responsibility moved out
of six facades; they do not claim lower defect risk or improved history metrics.

| Facade | Rebaseline physical lines | Final physical lines | Capability owners after extraction |
| ------ | ------------------------: | -------------------: | ---------------------------------- |
| `config.py` | 961 | 84 | `config_base.py` 301; `config_control.py` 360; `config_agent.py` 254 |
| `services/review.py` | 926 | 204 | `review_changes.py` 269; `review_tagwrite.py` 199; `review_dedupe.py` 101; `review_cue.py` 125 |
| `services/proposal.py` | 1,039 | 111 | `proposal_context.py` 197; `proposal_parsing.py` 203; `proposal_persistence.py` 111; `proposal_provider.py` 74 |
| `tasks/execution.py` | 1,392 | 520 | `execution_filesystem.py` 339 |
| `tasks/reenqueue.py` | 1,472 | 120 | `recovery_backfill.py` 85; `recovery_policy.py` 199; `recovery_queries.py` 81; `recovery_replay.py` 220 |
| `tasks/reconcile_cloud_jobs.py` | 965 | 910 | `cloud_reconcile_observation.py` 207 |

The terminal coverage ruler is stricter than the molecule acceptance: every new production module
must remain at or above 90% line coverage, touched-source branch coverage may not decrease, and the
repository-wide 95% line floor still applies. Exact results belong to the submitted revision's
Beadhive validation verdict rather than a mutable prose snapshot.

The cross-domain focused closure passed 1,036 tests on the isolated seat. The 17 extracted
production modules measured 99.07% combined line/branch coverage; every module cleared 97% total
coverage, so all exceed the required 90% line floor. This focused result supplements rather than
replaces the submitted revision's terminal repository gate.
