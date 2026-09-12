# phaze-tj1b2.1 structural rebaseline

Date: 2026-09-11

## Verdict

The focused maintenance molecule is landed, and every proposed follow-on seam has enough
post-landing evidence to proceed. The dispositions are:

| Bead | Candidate | Disposition |
|---|---|---|
| `phaze-tj1b2.2` | Review workspace read models | **GO** |
| `phaze-tj1b2.3` | Proposal parsing, context, persistence, and provider orchestration | **GO** |
| `phaze-tj1b2.4` | Configuration domains behind `phaze.config` | **GO, facade required** |
| `phaze-tj1b2.5` | Execution filesystem engine vs SAQ/reporting orchestration | **GO, facade required** |
| `phaze-tj1b2.6` | Recovery classification/planning vs persistence and queue adapters | **GO, facade required** |
| `phaze-tj1b2.7` | Cloud observation/classification vs reconciliation effects | **GO, cycle break retained** |
| `phaze-tj1b2.8` | Review/proposal test organization | **GO after `.2` and `.3`** |
| `phaze-tj1b2.9` | Execution test organization | **GO after `.5`** |
| `phaze-tj1b2.10` | Recovery/cloud test organization | **GO after `.6` and `.7`** |

There is no unsupported seam that needs replanning, and no candidate should be dropped. The
module boundaries in the implementation beads remain north stars rather than predetermined file
counts. A later bead must return to planning if it cannot preserve a facade, a patch point, a
framework registration, or an invariant recorded below.

## Landing and evidence provenance

- Focused molecule: `phaze-sfofk`, *Focused repository maintenance: comments, bounded
  simplification, Just workflows, and documentation*.
- Beadhive state: `closed`, close reason `molecule landed`.
- Merge revision: `464c97f127cbfcc9ac2f68dc5947e21bffbd6a51`.
- The rebaseline worktree, local `main`, and local `origin/main` all resolved to that revision.
- The focused range began at `6aae689115d885174c9c11016d1b4212b386eda1`. Five candidate
  files changed in that range, but their changes were comment normalization: `config.py`,
  `services/{proposal,review}.py`, and `tasks/{reenqueue,reconcile_cloud_jobs}.py`.
  `tasks/execution.py` did not change. This rebaseline nevertheless reads and counts the live
  `464c97f1` files rather than assuming the earlier inventory still applies.

### Repowise freshness

`repowise update -v` completed successfully in the bead worktree. Its live graph pass reported
17,308 symbols, 49,389 structural edges, 5,642 framework edges, and 279 dynamic-hint edges at
`464c97f1`; `.repowise/state.json` records both `last_sync_commit` and `last_docs_commit` as the
full merge revision.

The persisted health/wiki metadata did not advance with that refresh. `wiki.db` and every MCP
response still report `indexed_commit=6aae689115d8`, `live_head=464c97f127cb`, and
`index_behind=true`. The six coverage rows are older still: they were ingested on 2026-08-29 at
`7fb68365eb70`. This Repowise inconsistency was escalated as `hq-7xo`.

Consequences for this document:

1. Repowise health, history, and coverage values are labelled **stored**, not current.
2. No GO rests on a history-derived score. Each GO also has live-HEAD source/AST evidence and a
   live pytest collection baseline.
3. Implementers must refresh Repowise after committing their change and must not claim a
   before/after health or coverage improvement until MCP, `wiki.db`, and live HEAD agree.

## Reproduction commands

Run from a clean checkout of `464c97f1` (or record the newer revision before comparing):

```bash
git rev-parse HEAD main origin/main
bh work issue phaze-sfofk --json
repowise update -v
jq -r '.last_sync_commit, .last_docs_commit, .written_by_version' .repowise/state.json
sqlite3 -header -column .repowise/wiki.db \
  'select head_commit, updated_at from repositories;'

repowise health --file src/phaze/config.py --format json
repowise health --file src/phaze/services/review.py --format json
repowise health --file src/phaze/services/proposal.py --format json
repowise health --file src/phaze/tasks/execution.py --format json
repowise health --file src/phaze/tasks/reenqueue.py --format json
repowise health --file src/phaze/tasks/reconcile_cloud_jobs.py --format json

repowise risk --target src/phaze/config.py --target src/phaze/services/review.py \
  --target src/phaze/services/proposal.py --target src/phaze/tasks/execution.py \
  --target src/phaze/tasks/reenqueue.py \
  --target src/phaze/tasks/reconcile_cloud_jobs.py --full

repowise context src/phaze/config.py src/phaze/services/review.py \
  src/phaze/services/proposal.py src/phaze/tasks/execution.py \
  src/phaze/tasks/reenqueue.py src/phaze/tasks/reconcile_cloud_jobs.py \
  --include callers --include callees --include metrics --include decisions \
  --include health --full

repowise why --target src/phaze/config.py --target src/phaze/services/review.py \
  --target src/phaze/services/proposal.py --target src/phaze/tasks/execution.py \
  --target src/phaze/tasks/reenqueue.py \
  --target src/phaze/tasks/reconcile_cloud_jobs.py --full

wc -l src/phaze/config.py src/phaze/services/review.py \
  src/phaze/services/proposal.py src/phaze/tasks/execution.py \
  src/phaze/tasks/reenqueue.py src/phaze/tasks/reconcile_cloud_jobs.py

rg -n 'phaze\.(config|services\.review|services\.proposal|tasks\.execution|tasks\.reenqueue|tasks\.reconcile_cloud_jobs)' \
  src tests scripts alembic
rg -n '(monkeypatch\.(setattr|delattr)|patch\(|patch\.object|importlib|__import__)' src tests
uv run pytest --collect-only -q
```

The AST inventory counted tracked Python imports, literal patch paths, and attributes read through
module imports. Reproduce it with `git ls-files '*.py'` plus Python's `ast` module under
`uv run`; do not substitute a prose search for that pass. The full suite collection baseline is
**8,796 tests**.

## Common invariants

Every implementation bead is behavior-preserving. These rules apply across all six production
seams:

- Preserve public names, import paths, argument/default behavior, return shapes, exception types,
  warning/error copy, and supported private patch points through a compatibility facade.
- Dependencies continue to point toward domain policy. Database, filesystem, HTTP/network,
  framework, queue, and task adapters remain explicit at their boundary.
- Do not move transaction ownership, commits, rollbacks, advisory locks, callback ordering, queue
  ownership, or retry/terminal-token semantics merely to make a module smaller.
- Do not replace named responsibility boundaries with `helpers`, `utils`, ordinal, `partN`, or
  returned-variable modules.
- Existing framework registration must continue to receive the same callable object at the same
  supported import path. Deferred imports that break a real cycle remain deferred until a tested
  replacement boundary exists.
- Tests may move only after the production facade settles. Pre/post collection counts and named
  scenarios must be identical; helper extraction may clarify setup but may not hide assertions.
- Configuration/metaclass work, destructive filesystem work, and all database integration work use
  isolated Postgres and Redis seats. Never run two non-collect pytest processes against one seat.
- New production modules have a 90% line-coverage floor. Touched-file branch coverage may not
  decrease. Stored Repowise coverage is orientation only until refreshed at the implementing SHA.

## Source and stored-health baseline

Physical lines are live at `464c97f1`. NLOC, coverage, health, history, and graph reach are the
stored Repowise snapshot described above.

| Candidate file | Physical lines | Stored NLOC | Health | Line / branch coverage | Direct / two-hop dependents | Prior fixes | Hotspot |
|---|---:|---:|---:|---:|---:|---:|---:|
| `src/phaze/config.py` | 961 | 786 | 5.90 | 100.00% / 100.00% | 160 / 760 | 19 | 99% |
| `src/phaze/services/review.py` | 926 | 734 | 4.60 | 97.53% / 95.45% | 12 / 22 | 17 | 98% |
| `src/phaze/services/proposal.py` | 1,039 | 674 | 5.19 | 99.61% / 98.65% | 7 / 22 | 10 | 97% |
| `src/phaze/tasks/execution.py` | 1,392 | 1,077 | 5.47 | 99.29% / 96.55% | 7 / 17 | 20 | 99% |
| `src/phaze/tasks/reenqueue.py` | 1,472 | 1,092 | 5.69 | 99.69% / 98.65% | 13 / 32 | 14 | 99% |
| `src/phaze/tasks/reconcile_cloud_jobs.py` | 965 | 707 | 5.71 | 96.53% / 91.25% | 5 / 24 | 12 | 98% |

The primary Repowise biomarker for every row is change entropy, not a current behavior defect.
The values justify caution and review depth, not the extraction itself.

## `.2` — Review workspace read models: GO

**Affected boundary and north star.** Keep `phaze.services.review` as the import-compatible facade.
Move changes-review, tag-write, dedupe, and CUE read-model construction behind four responsibility-
named boundaries. Routers consume facade functions; capability modules own their queries and row
construction.

**Independent GO signals.** First, the live file has four visible capability clusters with separate
public entry points (`get_changes_review_page`, `get_tagwrite_review_page`, `get_dedupe_groups`, and
`get_cue_review_cards`) and only small shared formatting/paging policy. Second, the test closure is
already strong and capability-shaped: 106 collected tests across 2,909 physical lines in the six
core characterization files, while stored coverage is 97.53% line / 95.45% branch. Repowise also
finds 17 prior fixes and 25 co-change partners, so a facade reduces import churn without weakening
the review ruler.

**Consumers and compatibility.** The live AST pass found 10 direct import files: three production
routers (`routers/duplicates.py`, `routers/shell/stage_context.py`, and `routers/tags.py`) and seven
tests. Repowise finds 12 direct and 22 two-hop dependents. Preserve the exported named tuples,
formatters, workspace functions, constants `_MAX_REVIEW_ROWS`, `_REVIEW_SCAN_BATCH`, and
`_MAX_REVIEW_SCAN_BATCHES`, plus facade-level `generate_cue_content` and `get_proposals_page` patch
resolution. Literal patch paths occur in three test files. There is no direct runtime import loader;
FastAPI registration is indirect through the consuming routers.

**Governing decisions and risks.** ADR-0008 keeps rename approval, tag decisions, counts, warning
copy, and status vocabulary distinct even when one workspace renders them together. ADR-0015 says
shared-session concurrency is not intrinsically unsafe, but concurrency remains an explicit
transaction/snapshot/degrade-path decision. Preserve ordering, pagination, bounded scans,
degrade-safe empty results, per-workspace transaction ownership, and complete row values.

**Measured acceptance.** The four capability modules each have one named responsibility and at
least 90% line coverage; the facade preserves all names above; the six-file 106-test collection and
named scenarios remain unchanged; row shapes, warnings, pagination, ordering, degrade behavior,
and transaction ownership are differential-tested.

## `.3` — Proposal boundaries: GO

**Affected boundary and north star.** Keep `phaze.services.proposal` as the supported facade. Separate
response parsing/salvage, prompt and companion context loading, proposal persistence, and provider
orchestration. Rate limiting remains at the provider-facing edge.

**Independent GO signals.** First, the live source has four separable clusters: Pydantic response and
malformed-completion policy, pure context/cleanup functions, SQL persistence/companion loaders, and
`ProposalService`/LiteLLM orchestration. Second, five focused files collect 130 tests over 3,102
physical lines and stored coverage is 99.61% line / 98.65% branch. Repowise independently records
seven direct and 22 two-hop dependents plus 10 prior fixes.

**Consumers and compatibility.** The AST pass found seven direct import files: production
`tasks/controller.py` and `tasks/proposal.py`, plus five tests. Preserve response models,
`MalformedCompletionError`, `ProposalService`, `load_prompt_template`, `build_file_context`,
`check_rate_limit`, `store_proposals`, companion loaders, and `_date_convention_guidance`. Tests
patch facade-level `acompletion`, `asyncio`, and `pg_insert` in two files; those patch points must
continue to intercept the code under test. Prompt templates are loaded from the package filesystem,
and LiteLLM dispatch is runtime provider behavior, but there is no dynamic Python module import.
The controller constructs `ProposalService` during startup and the proposal task calls the facade.

**Governing decisions and risks.** ADR-0015 preserves current sequential persistence and makes any
future concurrency a separate, evidence-backed transaction/snapshot/degrade decision. Preserve
malformed-completion telemetry, exact exception classes, salvage and finish-reason behavior,
companion precedence and limits, prompt lookup, rate-limit semantics, stored context, transaction
ownership, and return models.

**Measured acceptance.** Four named capability boundaries, each at least 90% covered; all facade and
patch names above still resolve; 130 tests and their node ids collect before and after; parsing,
salvage, context, persistence, provider wire shapes, rate limiting, and transaction behavior compare
identically.

## `.4` — Configuration domains: GO, facade required

**Affected boundary and north star.** Preserve `phaze.config` as the only public settings facade.
Move only cohesive field/validator domains into responsibility-named bases or collaborators that
retain Pydantic's class construction and validator ordering. The existing
`config_backends`, `config_registry_policies`, `config_secrets`, and `config_redis` boundaries are
the proven local pattern.

**Independent GO signals.** First, `config.py` still contains three materially different settings
domains (`BaseSettings`, `ControlSettings`, and `AgentSettings`) across 786 stored NLOC; Repowise's
specific structural finding is low cohesion in `ControlSettings`, not merely file size. Second,
the earlier mixin extractions prove that class-based movement can preserve Pydantic behavior, and
the focused configuration closure currently collects 350 tests over 4,656 physical lines. That is
independent of the stored 100% line/branch coverage signal.

**Consumers and compatibility.** The AST pass found 159 direct importing files (66 outside tests,
93 tests); Repowise finds 160 direct and 760 two-hop dependents. Preserve `Role`, `BaseSettings`,
`ControlSettings`, `AgentSettings`, `Settings`, `get_settings`, `export_llm_api_keys`,
`_build_default_settings`, and the import-time `settings` singleton. Two test files patch the
literal `phaze.config.get_settings` path. Environment aliases, secret-file resolution, defaults,
serialization, validation messages, cache behavior, and role dispatch are runtime contracts.
Pydantic metaclass/class inheritance is the framework registration surface; fields cannot be moved
to plain helper objects without proving equivalent model fields and validator order.

**Governing decisions and risks.** Existing source/history records require: direct environment
values win over secret files; alias search order is stable; registry policies execute in their
documented order; agent entry points call role-specific `get_settings`; the legacy module singleton
stays control-typed; `Settings` remains the `ControlSettings` compatibility alias. The 160-import
blast radius and import-time construction are the main risks. Do not treat the stored 5.90 health
score as a target; the primary deduction is history-derived change entropy, and the proposed
extract-class plan itself does not address that primary signal.

**Measured acceptance.** The live public facade and its object identities/types behave identically;
all 350 focused tests collect; validator ordering, inheritance, aliases, defaults, environment and
secret precedence, role dispatch, caching, import-time behavior, serialization, and error strings
are differential-tested; each new module clears 90% line coverage.

## `.5` — Execution filesystem engine: GO, facade required

**Affected boundary and north star.** Keep `phaze.tasks.execution.execute_approved_batch` as the SAQ
task facade. A narrow filesystem engine owns destination resolution, no-clobber claims, same- and
cross-filesystem movement, copy markers, streaming copy, and hash verification. The adapter owns
SAQ context, UUID seeding, API reporting, progress, retries, and terminal coordination.

**Independent GO signals.** First, the live 1,392-line file already has a contiguous filesystem
mechanics region followed by reporting/orchestration functions, with `_MoveStep` and
`_ReportContext` as natural data boundaries. Second, six focused files collect 94 tests over 4,340
physical lines and stored coverage is 99.29% line / 96.55% branch. Repowise independently records
20 prior fixes and a 99% hotspot, with its strongest co-change partners being the progress and move-
strategy tests.

**Consumers and compatibility.** The only production importer is `tasks/agent_worker.py`, which
registers `execute_approved_batch` in the agent worker function map. Six tests import the module or
symbols directly; Repowise finds seven direct and 17 two-hop dependents. Preserve the facade plus
test patch/read points: `get_settings`, `_same_filesystem`, `_atomic_cross_fs_copy`,
`_atomic_same_fs_move`, `_committed_copy_marker_path`, `_unique_tmp_path`, `_streamed_copy`,
`_COPY_TMP_SUFFIX`, `ExecBatchTerminalReportError`, `_ReportContext`, `_report_success`, and
`_verify_hash_or_raise`. No dynamic import loader was found; SAQ's callable registration is the
framework surface.

**Governing decisions and risks.** Preserve containment before mutation, destination no-clobber,
case-only rename behavior, source/destination hash checks, 16 MiB bounded copy chunks, unique temp
paths, committed-copy markers, same/cross-filesystem semantics, crash/replay corroboration,
per-proposal isolation, reporting order, progress finalization, and terminal-token retry behavior.
The agent import-boundary guard must continue to prove no application ORM or SQLAlchemy import leaks
into the agent worker graph.

**Measured acceptance.** The engine has no SAQ/API dependency; the adapter does no raw filesystem
move mechanics; every compatibility name above resolves; the 94-test collection is identical; the
same/cross-filesystem, failure/replay, reporting/progress, retry, and isolation cases compare
identically; each new module clears 90% line coverage.

## `.6` — Recovery classification and replay planning: GO, facade required

**Affected boundary and north star.** Preserve `phaze.tasks.reenqueue` as the recovery facade.
Separate done-set queries, pure completion/orphan classification, pure owner/replay planning,
owner-specific replay/regeneration adapters, and SAQ-ledger backfill.

**Independent GO signals.** First, the live file already exposes the proposed seams as separate
types and functions: `_DoneSets`, `_is_orphaned`, `_OwnerPlan`, `_ReplayPlan`, regenerators, and
backfill parsing. Second, ten focused files collect 172 tests over 5,884 physical lines; stored
coverage is 99.69% line / 98.65% branch. Repowise independently finds 13 direct / 32 two-hop
dependents and 14 prior fixes.

**Consumers and compatibility.** Production consumers are `routers/pipeline/recovery.py`,
`tasks/controller.py`, and, importantly, `services/pipeline/orphans.py`, which imports seven private
classification/query names. Ten tests import additional private planning, regeneration, parser, and
domain-completion names. Preserve the facade and current patch paths for `get_settings`,
`count_inflight_jobs`, `get_live_job_keys`, `cloud_staging.redrive_upload`,
`cloud_staging.flush_pending_s3_enqueues`, and `_build_done_sets`. The controller registers
`recover_orphaned_work`, runs it at startup, and registers `backfill_ledger_from_saq_jobs`; the
operator recovery route calls the same facade. There is no candidate-specific dynamic module load.

**Governing decisions and risks.** ADR-0004 requires expiring payloads to be regenerated from durable
inputs at replay time. ADR-0006 makes badge/recovery completion parity definitional and requires the
reaper to clear what recovery ignores. ADR-0003 permanently accepts the narrow backfill/enqueue
residual window instead of adding a coarse lock. Preserve per-row failure isolation, live-key
deduplication, cloud ownership exclusions, nonfatal startup backfill, session/transaction
boundaries, owner routing, replay safety, and truthful failure tallies.

**Measured acceptance.** Pure classification/planning modules have no database/queue dependency;
adapters retain effect ownership; all production-private and test patch names above resolve; the
172-test collection and scenarios remain unchanged; every new module clears 90% line coverage.

## `.7` — Cloud observation/classification: GO, cycle break retained

**Affected boundary and north star.** Separate observation and pure state classification from
persistence, cleanup, redrive, and queue effects. Keep `reconcile_cloud_jobs` as the controller
facade and keep `_reconcile_one` reachable from its present path for the Kueue backend. Do not merge
this ownership with `release_awaiting_cloud`.

**Independent GO signals.** First, the live file already distinguishes observation helpers
(`_job_counter`, condition readers, wedge detection) from effectful transitions
(`_record_success`, spill, redrive, terminal handling), joined by `_RowReconcile`. Second, three
focused files collect 77 tests over 2,510 physical lines and stored coverage is 96.53% line /
91.25% branch. Repowise independently reports five direct / 24 two-hop dependents, 12 prior fixes,
and a 98% hotspot.

**Consumers and compatibility.** `tasks/controller.py` imports and cron-registers
`reconcile_cloud_jobs` every minute. `services/backends/kueue.py` imports private `_reconcile_one`
and owns the per-row advisory lock around its use. Tests import the facade and module constants
`PENDING_SUBMIT_CONFIRMATION_SECONDS` and `NO_POD_PROBE_SECONDS`, and patch the facade-level
`get_settings`. At the bottom of the facade, `resolve_backends` remains a deferred import that
breaks the real `backends <-> reconcile_cloud_jobs` cycle. This is the one explicit dynamic import
constraint: do not make it eager unless the implementation first supplies and tests a replacement
dependency direction.

**Governing decisions and risks.** Preserve per-backend ownership, callback primacy, per-row
advisory-lock scope, pending-submit confirmation, admission and quota holds, terminal and node-loss
classification, retry/redrive ceilings, wedge handling, cleanup/commit ordering, and phase-preserving
spill behavior. Any `pg_locks` or `pg_stat_activity` assertion remains scoped to the current
database. Commit-before-return paths release transaction advisory locks and are behavior, not
formatting.

**Measured acceptance.** A checked-in transition table maps every observed state to the identical
effect/order; pure classification does no persistence/network/queue work; the facade, `_reconcile_one`,
constants, patch points, cron registration, and deferred cycle break remain compatible; the 77-test
collection is identical; each new module clears 90% line coverage.

## `.8` — Review/proposal tests: GO after production facades settle

**Independent GO signals.** The two focused closures contain 236 collected tests across 6,011
physical lines. Independently, three files exceed 1,000 lines
(`test_review_apply_workspaces.py`, `test_proposal.py`, and
`test_proposal_provider_wire_shapes.py`), while the scenario families already map to the eight
planned capability boundaries. The literal facade patch paths above show why this move must follow,
not precede, `.2` and `.3`.

**North star and acceptance.** Organize changes review, tag write, dedupe, CUE, parsing/salvage,
context, persistence, and provider orchestration into discoverable files. Preserve all 236 node ids
or record an explicit old-to-new mapping with an identical total; do not hide assertions inside
generic scenario helpers; coverage may not decrease.

## `.9` — Execution tests: GO after the engine facade settles

**Independent GO signals.** The six-file closure contains 94 tests across 4,340 physical lines;
`test_execute_approved_batch_progress.py` alone is 1,923 lines. Independently, those tests directly
patch both facade orchestration and private filesystem mechanics, proving that the current files
cross the exact engine/adapter boundary `.5` will establish.

**North star and acceptance.** Separate filesystem strategy, batch orchestration,
progress/reporting, audit, retry, and recovery contracts. Keep all 94 scenarios explicit and the
collected total identical; engine tests cover same/cross-filesystem and replay/failure semantics;
adapter tests cover reporting, progress, terminal tokens, and per-proposal isolation.

## `.10` — Recovery/cloud tests: GO after both production seams settle

**Independent GO signals.** The unique recovery/cloud closure contains 241 tests across 8,145
physical lines; `test_recovery.py` is 2,635 lines and `test_reconcile_cloud_jobs.py` is 1,987.
Independently, the current tests mix orphan/replay, owner routing, ledger backfill,
pending/admission/terminal/redrive/wedge states, and advisory locking, matching two production
boundaries and multiple transaction owners.

**North star and acceptance.** Give each state/ownership family one discoverable home. Preserve the
241-test collected total and every named scenario, keep fixtures at the narrowest useful scope,
keep cluster-wide catalogue queries database-scoped, and do not abstract transition assertions into
generic data that obscures order or ownership.

## Exact validation ruler

Wave 0 and every handoff use the repository's declared commands, never bare Python/pytest/mypy:

1. This evidence bead: `just check-fast`.
2. Each production implementation: its focused characterization/differential tests under an
   isolated Postgres and Redis seat, then `just branch-check` and `just check-fast`. Ruff and mypy
   must pass; use the bead's printed gate rather than substituting a narrower command.
3. Each test-organization bead: record pre/post `uv run pytest --collect-only -q` totals for its
   exact file set, run that focused set on an isolated seat where it is not collection-only, then
   `just check-fast`.
4. Terminal molecule gate: refresh Repowise at the committed SHA, verify facade reachability and
   architecture/ownership docs, run focused suites plus `just branch-check` for touched source, and
   finish with `just check-all`.

An implementation is not accepted because a stored health number rises. Acceptance is the
observable behavior, compatibility, coverage, collection, dependency direction, and effect-order
ruler above.
