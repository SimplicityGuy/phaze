# Documentation Audit — 2026-08-19

This audit covers every tracked human-facing documentation surface in the repository, plus the
new documentation surfaces introduced by this refresh. It records the scope explicitly so later
audits can reproduce the count instead of relying on an informal “all docs” claim.

## Result

| Measure | Count |
| --- | ---: |
| Reviewed | 1370 |
| Changed | 24 |
| Unchanged | 1346 |
| Supporting source/config comments corrected outside this inventory | 1 |

The inventory includes every tracked Markdown, MDX, reStructuredText, and text file; every tracked
file under `docs/` and `design/` (including reference HTML and visual assets); both environment
examples; `LICENSE`; and `alembic/README`. Source code, application templates, workflow YAML,
and ordinary configuration files are source-of-truth inputs rather than documentation surfaces.
The one non-inventory edit is a comment-only correction in `pyproject.toml`; it changes no
package constraint or product behavior.

| Classification | Surfaces |
| --- | ---: |
| historical planning evidence | 1244 |
| dated investigation evidence | 37 |
| documentation asset | 29 |
| current documentation | 19 |
| architecture decision record | 12 |
| dated design evidence | 10 |
| repository documentation | 5 |
| test documentation/fixture | 4 |
| design reference | 3 |
| configuration reference | 2 |
| archive boundary | 2 |
| embedded documentation | 2 |
| audit record | 1 |

## Method

The current, operator-facing set was read semantically and checked against the live tree:
`pyproject.toml` and `uv.lock`; Alembic revisions and `uv run alembic heads`; FastAPI routers,
services, templates, and CLI help; all Compose files; `just --list`; CI workflows; test bucket
metadata; and accepted architecture decisions. Repowise supplied structural orientation and
co-change/risk context, but every claim changed here was verified against files in this worktree.

The historical planning, specification, spike, incident, and milestone corpus was scanned
exhaustively for broken repository links, stale-current language, old routes and commands,
superseded architecture terms, version claims, and local identifiers. Rather than rewriting
historical outcomes, this change adds explicit archive boundaries and preserves their exact
measurements, dates, sample sizes, and recorded decisions. Historical application routes and
deliberately removed source paths remain evidence, not broken current instructions.

## Material corrections

- Replaced the main README with a current product, operator workflow, architecture, setup,
  validation, browser-test, Repowise-coverage, deployment, and documentation overview.
- Restored the approval boundary: proposal generation is not authorization, Changes Review owns
  filename and destination approval, Execute approved performs preflight/apply, and tags use a
  separate reversible authorization after execution.
- Documented the actionable Summary and current four-group responsive navigation; removed current
  guidance for the deleted corpus-wide high-confidence approval route and icon-only rail.
- Corrected the durable queue backend to PostgreSQL and limited Redis to cache, rate limiting,
  progress, and counters.
- Corrected runtime/dependency floors from the live project config, CalVer examples to
  `2026.8.4`, the Alembic inventory to 24 files, and the migration head to `062`.
- Corrected first-start model bootstrap from the obsolete ~150 MB/2–5 minute text to the live
  ~3.1 GB/68-file locked validation and download behavior.
- Corrected agent-watcher Compose commands, its import-boundary test path, and the manual Discover
  catch-up route.
- Recorded exhaustive fine/coarse window analysis, chunk/process memory bounds, progress liveness,
  and full audio-fingerprint removal.
- Added archive boundaries for the frozen GSD planning tree and dated design specifications.
- Repaired 22 planning-ledger links after their quick-task directories moved into the milestone
  archive; the recorded task text and measurements are unchanged.
- Added the missing AGF lifecycle guide referenced by the managed repository instructions.
- Corrected the production UI accent tokens and responsive rail contract from the current CSS,
  templates, and accepted accessibility decisions.

No product behavior, dependency constraint, workflow, migration, or historical measurement was
changed.

## Validation

- Inventory self-check: 1,370 unique rows; 24 changed; 1,346 unchanged.
- Internal-link check: zero missing local targets across 39 current documentation and architecture
  decision surfaces. Historical application routes and source paths were classified separately.
- Command/path checks: `uv run alembic heads` reported `062 (head)`; `uv run phaze --help`,
  `uv run phaze backfill reenqueue-incomplete-analyses --help`, `just --list`, and dry runs of
  every documented `just` recipe completed successfully.
- Focused documentation, shell, deployment, change-gate, and Repowise-coverage tests: 158 passed.
- `just lint`: passed.
- `just typecheck`: passed (297 source files).
- `uv run pre-commit run --all-files`: every hook passed.
- `git diff --check`: passed.

The full pytest suite, real browser suite, live deployment procedures, destructive migration
downgrades, and the approximately 21-minute Repowise coverage refresh were not run for this
documentation-only change. Their commands and static contracts were validated; CI remains the
integration gate.

## Post-audit drift (not part of the original 2026-08-19 result)

This audit is a dated snapshot, not a maintained index: it has never been edited since the
commit that created it (`908e3f82`), and every row below reflects the tree as it stood on
2026-08-19. The `Result` classification in each row is therefore left exactly as recorded —
rewriting a historical row to match a later tree would make the record say something it did
not say on its date (`docs/design/0012-verification-fidelity-and-operator-attribution.md`
rule 2). Paths in the table can still drift out of date after
the fact; known drift is logged here instead, dated, without touching the rows themselves.

- `docs/design/0004-tracklist-candidate-sets.md` (`architecture decision record`, `unchanged`
  in the table below) was renumbered to `docs/design/0014-tracklist-candidate-sets.md` by
  phaze-kbue9 on 2026-08-23. The row's path and classification were correct on 2026-08-19;
  the file no longer exists at that path. Logged 2026-08-24 (phaze-x2z38). (Line numbers are
  deliberately not cited here — this section's own insertion above the table shifts every row
  below it, so a line number pinned at write time goes stale on the next edit; the path is the
  stable key.)
- `tests/browser/FLAKE_RECORD.md` (`test documentation/fixture`, `unchanged` in the table
  below) was deleted by phaze-8p1uq on 2026-08-21 (`4a6c595a`), which moved its "what to watch
  when CI runs start" guidance into `docs/design/0009-responsive-accessibility-baseline.md`
  and removed the rest as superseded by the browser contract job's promotion to blocking. The
  row was correct on 2026-08-19; the file no longer exists at that path. Logged 2026-08-24
  (phaze-x2z38) — found while re-verifying this document's paths for phaze-x2z38 and out of
  that bead's scope, so filed separately as phaze-r5vz0 rather than fixed here.
- The phaze-kbue9 renumber above has a second residue this table cannot show at all: it freed
  the number 0004 (this audit's own row above for the old path already records a genuine
  duplicate — `docs/design/0004-ledger-replay-safety.md` and the old
  `docs/design/0004-tracklist-candidate-sets.md` were both `0004` as of 2026-08-19) and
  assigned the freed 0014 to a new file. A bare `ADR-0014` or `ADR-0004` prose citation written
  before the rename can therefore silently resolve to the WRONG document after it, rather than
  to nothing — undetectable by any path-based check, including the ones this section uses.
  Demonstrated live: a bead cited "ADR-0014" meaning the shared-`AsyncSession`-gather decision,
  which is actually `docs/design/0015-shared-session-gather.md`. Logged 2026-08-24
  (phaze-x2z38), filed separately as phaze-f70y9 rather than fixed here or in phaze-x2z38 —
  disposition needs a census of the bead corpus, not just tracked files, which is its own
  scope. Cite ADRs by filename, not bare number, in anything written after this point.

## Exact inventory

| Path | Classification | Result |
| --- | --- | --- |
| `.env.example` | configuration reference | unchanged |
| `.env.example.agent` | configuration reference | changed |
| `.planning/MILESTONES.md` | historical planning evidence | changed |
| `.planning/PROJECT.md` | historical planning evidence | changed |
| `.planning/README.md` | archive boundary | changed |
| `.planning/REQUIREMENTS.md` | historical planning evidence | changed |
| `.planning/RETROSPECTIVE.md` | historical planning evidence | changed |
| `.planning/ROADMAP.md` | historical planning evidence | changed |
| `.planning/STATE.md` | historical planning evidence | changed |
| `.planning/debug/agent-image-missing-system-libs.md` | historical planning evidence | unchanged |
| `.planning/debug/analyze-4h-timeouts.md` | historical planning evidence | unchanged |
| `.planning/debug/run-analysis-payload-invalid.md` | historical planning evidence | unchanged |
| `.planning/graphs/GRAPH_REPORT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.0-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.0-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/67-backend-registry-config-model/67-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/67-backend-registry-config-model/67-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/67-backend-registry-config-model/67-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/67-backend-registry-config-model/67-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/67-backend-registry-config-model/67-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/67-backend-registry-config-model/67-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/68-backend-protocol-3-implementations/68-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/68-backend-protocol-3-implementations/68-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/68-backend-protocol-3-implementations/68-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/68-backend-protocol-3-implementations/68-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/68-backend-protocol-3-implementations/68-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/69-tiered-drain-scheduler/69-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/69-tiered-drain-scheduler/69-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/69-tiered-drain-scheduler/69-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/69-tiered-drain-scheduler/69-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/69-tiered-drain-scheduler/69-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/70-multi-kueue-n-clusters/70-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/70-multi-kueue-n-clusters/70-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/70-multi-kueue-n-clusters/70-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/70-multi-kueue-n-clusters/70-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/70-multi-kueue-n-clusters/70-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/71-deployment-config-docs-n-lane-ui/71-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/71-deployment-config-docs-n-lane-ui/71-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/71-deployment-config-docs-n-lane-ui/71-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/71-deployment-config-docs-n-lane-ui/71-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.1-phases/71-deployment-config-docs-n-lane-ui/71-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/72-per-entry-compute-binding-fail-fast-retirement/72-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/73-per-agent-dispatch-liveness-scratch-failure-isolation/73-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/74-docs-runbook-n-lane-compute-ui-verification/74-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/75-engineering-hygiene-guard-hardening-tech-debt-stale-tracking/75-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.2-phases/76-compute-push-hardening/76-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/77-additive-schema-rescan-wipe-fix-migration-032/77-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/79-shadow-compare-gate-live-corpus/79-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/80-recovery-re-enqueue-cutover/80-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/81-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/81-per-stage-failure-persistence-retry-paths/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/82-counts-pending-set-cutover/82-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-07-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-07-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-07-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/83-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/83-cloud-routing-sidecar-cutover/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-06-DEFERRED.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/84-dedup-fingerprint-progress-cutover/84-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/85-executed-gate-revival/85-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/86-proposals-cutover/86-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-07-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-07-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-08-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-08-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-09-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/87-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/87-operator-ui-stage-matrix-failure-retry-eligibility-trace-pri/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/88-lane-agent-drill-in/88-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/89-legacy-scan-path-deletion-sentinel-reattribution/89-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-04-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/90-destructive-migration-writer-removal/90-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/91-milestone-close-hygiene/91-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/92-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.5-phases/92-milestone-close-tech-debt-cleanup/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/2026.7.7-phases/102-alembic-migration-chain-flatten/VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/01-infrastructure-project-setup/01-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/02-file-discovery-ingestion/02-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/03-companion-files-deduplication/03-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/03-companion-files-deduplication/03-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/03-companion-files-deduplication/03-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/03-companion-files-deduplication/03-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/03-companion-files-deduplication/03-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/03-companion-files-deduplication/03-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/03-companion-files-deduplication/03-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/03-companion-files-deduplication/03-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/04-task-queue-worker-infrastructure/04-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/05-audio-analysis-pipeline/05-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/06-ai-proposal-generation/06-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/07-approval-workflow-ui/07-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/08-safe-file-execution-audit/08-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/09-pipeline-orchestration/09-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/09-pipeline-orchestration/09-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/09-pipeline-orchestration/09-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/09-pipeline-orchestration/09-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/09-pipeline-orchestration/09-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/09-pipeline-orchestration/09-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/09-pipeline-orchestration/09-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/09-pipeline-orchestration/09-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/10-ci-config-bug-fixes/10-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/10-ci-config-bug-fixes/10-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/10-ci-config-bug-fixes/10-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/10-ci-config-bug-fixes/10-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/11-polish-cleanup/11-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/11-polish-cleanup/11-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/11-polish-cleanup/11-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/11-polish-cleanup/11-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/11-polish-cleanup/11-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/11-polish-cleanup/11-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v1.0-phases/11-polish-cleanup/11-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/12-infrastructure-audio-tag-extraction/12-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/13-ai-destination-paths/13-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/14-duplicate-resolution-ui/14-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/15-1001tracklists-integration/15-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/16-fingerprint-service-batch-ingestion/16-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v2.0-phases/17-live-set-matching-tracklist-review/17-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/18-unified-search/18-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/19-discogs-cross-service-linking/19-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/20-tag-writing/20-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/21-cue-sheet-generation/21-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/22-tracklist-integration-fixes/22-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/22-tracklist-integration-fixes/22-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/22-tracklist-integration-fixes/22-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/22-tracklist-integration-fixes/22-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/22-tracklist-integration-fixes/22-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/23-v3-polish-wiring-fixes/23-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/23-v3-polish-wiring-fixes/23-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v3.0-phases/23-v3-polish-wiring-fixes/23-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/24-schema-foundation-agent-registry/24-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-07-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-07-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-08-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-08-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-REVIEW-gap-closure.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/25-internal-agent-http-api-bearer-auth/25-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-07-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-07-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-08-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-08-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-09-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-09-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-10-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-10-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-11-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-11-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-12-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-12-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-13-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-13-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-NYQUIST.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/26-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/26-task-code-reorg-http-backed-agent-worker/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-07-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-07-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-UAT-GAPS-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/27-watcher-service-user-initiated-scan/27-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/28-distributed-execution-dispatch/28-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-07-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-07-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-08-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-08-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/29-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/29-deployment-hardening-agents-admin/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/30-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/30-fix-systemic-control-plane-saq-queue-misrouting-every-manual/CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/31-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/31-windowed-time-series-audio-analysis/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-00-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-00-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/32-pipeline-reboot-resilience-re-enqueue/32-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-00-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-00-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/33-saq-monitoring-ui-mounted-in-phaze-api/33-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-00-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-00-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/34-pipeline-queue-depth-status-double-enqueue-guard/34-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-STAGE-DEPENDENCIES.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/35-pipeline-determinism-idempotency-per-job-type-observability/35-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-HOMELAB-CHANGE-PROMPT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/36-pipeline-queue-backend-migration-redis-to-postgres-saq/36-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/37-per-stage-pause-and-priority-control-plane-table-api-worker-/37-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/38-pipeline-dag-pause-priority-ui-and-rescan-button-removal/38-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/39-tracklist-search-dag-node-bulk-manual-search-tracklist-trigg/39-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/39-tracklist-search-dag-node-bulk-manual-search-tracklist-trigg/39-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/39-tracklist-search-dag-node-bulk-manual-search-tracklist-trigg/39-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/40-tracklist-fingerprint-scan-dag-node-bulk-manual-scan-live-se/40-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/40-tracklist-fingerprint-scan-dag-node-bulk-manual-scan-live-se/40-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/40-tracklist-fingerprint-scan-dag-node-bulk-manual-scan-live-se/40-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/41-scrape-and-match-dag-triggers-bulk-scrape-pending-scrape-and/41-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/41-scrape-and-match-dag-triggers-bulk-scrape-pending-scrape-and/41-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/41-scrape-and-match-dag-triggers-bulk-scrape-pending-scrape-and/41-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/42-recovery-only-pipeline-automation-gate-reenqueue-discovered-/42-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/42-recovery-only-pipeline-automation-gate-reenqueue-discovered-/42-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/42-recovery-only-pipeline-automation-gate-reenqueue-discovered-/42-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/42-recovery-only-pipeline-automation-gate-reenqueue-discovered-/42-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/42-recovery-only-pipeline-automation-gate-reenqueue-discovered-/42-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/42-recovery-only-pipeline-automation-gate-reenqueue-discovered-/42-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/42-recovery-only-pipeline-automation-gate-reenqueue-discovered-/42-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/46-heartbeat-starvation-fix/46-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/46-heartbeat-starvation-fix/46-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/46-heartbeat-starvation-fix/CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v4.0-phases/46-heartbeat-starvation-fix/VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/47-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/47-official-arm64-essentia-agent-image/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/48-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/48-compute-agent-type/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/49-duration-routing-backfill/49-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-00-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-00-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-07-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-07-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/50-push-pipeline/50-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-HOMELAB-CHANGE-PROMPT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-phases/51-deployment-config-docs/51-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260410-kco-add-docker-image-publishing-to-ghcr-foll/260410-kco-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260410-kco-add-docker-image-publishing-to-ghcr-foll/260410-kco-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260414-quo-add-discord-notification-to-docker-publi/260414-quo-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260414-quo-add-discord-notification-to-docker-publi/260414-quo-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260502-lqb-remove-discord-notification-step-from-do/260502-lqb-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260502-lqb-remove-discord-notification-step-from-do/260502-lqb-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260520-bcl-dedicated-local-integration-test-databas/260520-bcl-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260520-bcl-dedicated-local-integration-test-databas/260520-bcl-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-mpm-fix-release-tags-not-publishing-version-/260606-mpm-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-mpm-fix-release-tags-not-publishing-version-/260606-mpm-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-n0y-reconcile-ghcr-image-paths-stop-orphanin/260606-n0y-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-n0y-reconcile-ghcr-image-paths-stop-orphanin/260606-n0y-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-n7g-switch-audfprint-panako-sidecars-in-dock/260606-n7g-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-n7g-switch-audfprint-panako-sidecars-in-dock/260606-n7g-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-nha-add-a-phaze-agents-add-management-cli-ge/260606-nha-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-nha-add-a-phaze-agents-add-management-cli-ge/260606-nha-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-pjd-make-ci-yml-detect-changes-robust-to-for/260606-pjd-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-pjd-make-ci-yml-detect-changes-robust-to-for/260606-pjd-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-qgu-fix-flaky-cdn-sri-test-jsdelivr-serves-t/260606-qgu-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260606-qgu-fix-flaky-cdn-sri-test-jsdelivr-serves-t/260606-qgu-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-i21-harden-agent-model-bootstrap-against-tra/260608-i21-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-i21-harden-agent-model-bootstrap-against-tra/260608-i21-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-i21-harden-agent-model-bootstrap-against-tra/260608-i21-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-jbg-validate-model-integrity-on-bootstrap-vi/260608-jbg-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-jbg-validate-model-integrity-on-bootstrap-vi/260608-jbg-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-jbg-validate-model-integrity-on-bootstrap-vi/260608-jbg-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-jbg-validate-model-integrity-on-bootstrap-vi/260608-jbg-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-mbc-fix-three-scan-incident-issues-in-one-pr/260608-mbc-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-mbc-fix-three-scan-incident-issues-in-one-pr/260608-mbc-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-u8g-model-bootstrap-local-validation/PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260608-u8g-model-bootstrap-local-validation/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-f96-fix-scan-directory-10s-timeouterror-regi/260609-f96-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-f96-fix-scan-directory-10s-timeouterror-regi/260609-f96-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-glv-fix-metadata-write-500-strip-nul-bytes-f/260609-glv-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-glv-fix-metadata-write-500-strip-nul-bytes-f/260609-glv-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-oar-populate-scanbatch-total-files-via-pre-c/PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-oar-populate-scanbatch-total-files-via-pre-c/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-on2-poll-wire-recent-scans-table-and-stage-c/PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-on2-poll-wire-recent-scans-table-and-stage-c/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-pr2-scan-completed-at-elapsed/PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-pr2-scan-completed-at-elapsed/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-pr3-structlog-observability/PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-pr3-structlog-observability/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-pr4-scan-activity-indicator/PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-pr4-scan-activity-indicator/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-pr5-delete-scans/PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260609-pr5-delete-scans/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260610-fp9-add-audio-system-deps-to-dockerfile-so-e/260610-fp9-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260610-fp9-add-audio-system-deps-to-dockerfile-so-e/260610-fp9-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260613-t7k-widen-pipeline-dag-node-chips-and-make-m/260613-t7k-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260613-t7k-widen-pipeline-dag-node-chips-and-make-m/260613-t7k-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260614-sg8-fix-trigger-scan-dead-letter-enqueue-sca/260614-sg8-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260614-sg8-fix-trigger-scan-dead-letter-enqueue-sca/260614-sg8-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260615-cyp-fix-dag-canvas-xdata-quote/260615-cyp-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260615-cyp-fix-dag-canvas-xdata-quote/260615-cyp-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260618-sx6-pass-configured-anthropic-openai-api-key/PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260618-sx6-pass-configured-anthropic-openai-api-key/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260620-jvu-harden-ledger-ack-warnings/260620-jvu-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260620-jvu-harden-ledger-ack-warnings/260620-jvu-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260622-i0w-add-scanned-deduped-unique-reconciliatio/260622-i0w-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/260622-i0w-add-scanned-deduped-unique-reconciliatio/260622-i0w-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v5.0-quick-archive/README.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/52-job-runner-image-one-shot-entrypoint/52-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/53-s3-object-staging-leg/53-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/54-kube-submit-watch-reconcile-cron/54-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/55-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/55-routing-state-ledger-integration-the-live-seam/deferred-items.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-00-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-00-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-01-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-01-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-02-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-02-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-03-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-03-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-04-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-04-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-05-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-05-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-06-PLAN.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-06-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-CONTEXT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-DISCUSSION-LOG.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-HOMELAB-CHANGE-PROMPT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-HUMAN-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-PATTERNS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-RESEARCH.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-REVIEW.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-SECURITY.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-UAT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-UI-SPEC.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-VALIDATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v6.0-phases/56-deployment-runbook-config-docs/56-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/milestones/v7.0-MILESTONE-AUDIT.md` | historical planning evidence | unchanged |
| `.planning/milestones/v7.0-REQUIREMENTS.md` | historical planning evidence | unchanged |
| `.planning/milestones/v7.0-ROADMAP.md` | historical planning evidence | unchanged |
| `.planning/phases/95-analyze-view-browser-slowdown/95-BASELINE.md` | historical planning evidence | unchanged |
| `.planning/phases/95-analyze-view-browser-slowdown/95-STATS-BUDGET.md` | historical planning evidence | unchanged |
| `.planning/phases/95-analyze-view-browser-slowdown/95-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/quick/260627-ktb-upgrade-litellm-and-transitive-deps-fix-/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260628-wzq-fix-job-env-contract-inject-pod-runtime-/260628-wzq-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260628-wzq-fix-job-env-contract-inject-pod-runtime-/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260629-eev-convert-the-two-ascii-architecture-at-a/260629-eev-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260629-eev-convert-the-two-ascii-architecture-at-a/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260706-odc-close-audit-review-items/260706-odc-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260706-odc-close-audit-review-items/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260706-q90-add-optional-models-pvc-mount-to-kueue-j/260706-q90-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260706-q90-add-optional-models-pvc-mount-to-kueue-j/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260706-vqz-fix-cloud-burst-presign-download-status-/260706-vqz-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260706-vqz-fix-cloud-burst-presign-download-status-/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-c3a-remove-redundant-pipeline-header-scan-bu/260707-c3a-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-c3a-remove-redundant-pipeline-header-scan-bu/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-c9o-add-y-axis-scale-and-labels-to-bpm-fine-/260707-c9o-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-c9o-add-y-axis-scale-and-labels-to-bpm-fine-/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-cvz-give-deepen-analysis-a-live-progress-sur/260707-cvz-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-cvz-give-deepen-analysis-a-live-progress-sur/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-d79-add-retry-affordance-for-analysis-failed/260707-d79-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-d79-add-retry-affordance-for-analysis-failed/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-dh1-implement-per-lane-agent-queues-per-appr/260707-dh1-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-dh1-implement-per-lane-agent-queues-per-appr/260707-dh1-VERIFICATION.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-dh1-implement-per-lane-agent-queues-per-appr/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-g84-fix-inert-compute-agent-memory-safety-ca/260707-g84-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-g84-fix-inert-compute-agent-memory-safety-ca/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-rc4-advance-filerecord-state-to-metadata-ext/260707-rc4-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-rc4-advance-filerecord-state-to-metadata-ext/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-ryn-lean-db-connection-footprint-pool-hygien/260707-ryn-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-ryn-lean-db-connection-footprint-pool-hygien/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-s44-hide-revoked-agents-legacy-application-s/260707-s44-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-s44-hide-revoked-agents-legacy-application-s/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-ser-exclude-compute-kind-agents-from-the-tri/260707-ser-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-ser-exclude-compute-kind-agents-from-the-tri/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-sq3-add-placeholder-summary-page-as-the-defa/260707-sq3-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260707-sq3-add-placeholder-summary-page-as-the-defa/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260713-kg6-document-essentia-usage-analysis-and-rep/260713-kg6-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260713-kg6-document-essentia-usage-analysis-and-rep/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/quick/260714-hb3-bump-version-2026-7-6/260714-hb3-PLAN.md` | historical planning evidence | unchanged |
| `.planning/quick/260714-hb3-bump-version-2026-7-6/260714-hb3-SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/research/ARCHITECTURE.md` | historical planning evidence | unchanged |
| `.planning/research/FEATURES.md` | historical planning evidence | unchanged |
| `.planning/research/PITFALLS.md` | historical planning evidence | unchanged |
| `.planning/research/STACK.md` | historical planning evidence | unchanged |
| `.planning/research/SUMMARY.md` | historical planning evidence | unchanged |
| `.planning/sketches/001-pipeline-dag-view/README.md` | historical planning evidence | unchanged |
| `.planning/sketches/MANIFEST.md` | historical planning evidence | unchanged |
| `.planning/todos/completed/alembic-migration-chain-flatten.md` | historical planning evidence | unchanged |
| `.planning/todos/completed/analysis-completed-at-backfill.md` | historical planning evidence | unchanged |
| `.planning/todos/completed/wr-01-review-builder-limit-before-filter.md` | historical planning evidence | unchanged |
| `.planning/todos/done/wr03-push-timeout-coupling.md` | historical planning evidence | unchanged |
| `.planning/todos/pending/2026-07-14-analysis-pod-progress-post-connecttimeout-spam-event-loop-st.md` | historical planning evidence | unchanged |
| `.planning/todos/pending/2026-07-14-human-friendly-analysis-pod-console-logs-with-file-context-a.md` | historical planning evidence | unchanged |
| `CLAUDE.md` | repository documentation | changed |
| `CONVENTIONS.md` | repository documentation | unchanged |
| `LICENSE` | repository documentation | unchanged |
| `README.md` | repository documentation | changed |
| `alembic/README` | repository documentation | unchanged |
| `design/DESIGN_SYSTEM.md` | design reference | unchanged |
| `design/assets/banner_dark.png` | documentation asset | unchanged |
| `design/assets/banner_light.png` | documentation asset | unchanged |
| `design/assets/design_showcase.png` | documentation asset | unchanged |
| `design/assets/favicon-128.png` | documentation asset | unchanged |
| `design/assets/favicon-16.png` | documentation asset | unchanged |
| `design/assets/favicon-256.png` | documentation asset | unchanged |
| `design/assets/favicon-32.png` | documentation asset | unchanged |
| `design/assets/favicon-48.png` | documentation asset | unchanged |
| `design/assets/favicon-512.png` | documentation asset | unchanged |
| `design/assets/favicon-64.png` | documentation asset | unchanged |
| `design/assets/icon_dark.png` | documentation asset | unchanged |
| `design/assets/icon_light.png` | documentation asset | unchanged |
| `design/assets/og_image.png` | documentation asset | unchanged |
| `design/assets/square_dark.png` | documentation asset | unchanged |
| `design/assets/square_light.png` | documentation asset | unchanged |
| `design/banners/phaze-banner-animated.svg` | documentation asset | unchanged |
| `design/banners/phaze-banner-static.svg` | documentation asset | unchanged |
| `design/favicons/favicon-128.svg` | documentation asset | unchanged |
| `design/favicons/favicon-16.svg` | documentation asset | unchanged |
| `design/favicons/favicon-192.svg` | documentation asset | unchanged |
| `design/favicons/favicon-256.svg` | documentation asset | unchanged |
| `design/favicons/favicon-32.svg` | documentation asset | unchanged |
| `design/favicons/favicon-48.svg` | documentation asset | unchanged |
| `design/favicons/favicon-512.svg` | documentation asset | unchanged |
| `design/favicons/favicon-64.svg` | documentation asset | unchanged |
| `design/logos/icon_dark.svg` | documentation asset | unchanged |
| `design/logos/icon_light.svg` | documentation asset | unchanged |
| `design/logos/phaze-square-dark.svg` | documentation asset | unchanged |
| `design/logos/phaze-square-light.svg` | documentation asset | unchanged |
| `design/resonant-precision.md` | design reference | unchanged |
| `design/showcase.html` | design reference | unchanged |
| `docs/AGF.md` | current documentation | changed |
| `docs/README.md` | current documentation | changed |
| `docs/agent-queue-lanes.md` | current documentation | unchanged |
| `docs/api.md` | current documentation | changed |
| `docs/architecture.md` | current documentation | changed |
| `docs/arm64-agent-image.md` | current documentation | changed |
| `docs/cloud-burst.md` | current documentation | changed |
| `docs/configuration.md` | current documentation | unchanged |
| `docs/database.md` | current documentation | changed |
| `docs/deployment.md` | current documentation | changed |
| `docs/design/0001-audiomuse-ai-no-go.md` | architecture decision record | unchanged |
| `docs/design/0002-fingerprint-removal.md` | architecture decision record | unchanged |
| `docs/design/0003-backfill-ledger-race-residual-window.md` | architecture decision record | unchanged |
| `docs/design/0004-ledger-replay-safety.md` | architecture decision record | unchanged |
| `docs/design/0004-tracklist-candidate-sets.md` | architecture decision record | unchanged |
| `docs/design/0005-analyze-job-memory-limits.md` | architecture decision record | unchanged |
| `docs/design/0006-ledger-completion-coverage.md` | architecture decision record | unchanged |
| `docs/design/0007-windowed-analysis.md` | architecture decision record | unchanged |
| `docs/design/0008-changes-review-approval-boundary.md` | architecture decision record | unchanged |
| `docs/design/0009-responsive-accessibility-baseline.md` | architecture decision record | unchanged |
| `docs/design/0010-colour-contrast-tokens.md` | architecture decision record | unchanged |
| `docs/design/0011-bug-hunt-cadence.md` | architecture decision record | unchanged |
| `docs/documentation-audit-2026-08-19.md` | audit record | changed |
| `docs/essentia-analysis.md` | current documentation | unchanged |
| `docs/k8s-burst.md` | current documentation | unchanged |
| `docs/multi-compute.md` | current documentation | unchanged |
| `docs/project-structure.md` | current documentation | changed |
| `docs/quick-start.md` | current documentation | changed |
| `docs/runbook.md` | current documentation | unchanged |
| `docs/spikes/2026-08-analyze-overhaul-summary.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-0ni3v-easyloader-seek.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-0ni3v/bench_easyloader.py` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-0ni3v/compare.py` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-0ni3v/make-corpus.sh` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-0ni3v/run_bench.sh` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-0ni3v/verify_easyloader.py` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-37i1.1-audit-log-diagnosis.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-3j67-concurrent-extractor-capacity.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-7i0k-linux-memory-measurement.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-8r6t4-concurrency-knee-recheck.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-esut-analysis-memory-profile.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03-essentia-seek.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/bench_decode.py` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/bench_seek.py` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/make-synthetic-corpus.sh` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/patches/0001-build-use-sysconfig-instead-of-the-removed-distutils.patch` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/patches/0002-fix-audioloader-set-pkt_timebase-so-decoded-frame-ti.patch` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/patches/0003-feat-audioloader-add-startTime-endTime-seeking-to-Au.patch` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/patches/0004-feat-easyloader-let-EasyLoader-and-EqloudLoader-seek.patch` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/patches/0005-test-io-cover-startTime-endTime-seeking-and-its-boun.patch` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/run_bench.sh` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-han03/verify_accuracy.py` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-i93a-cpp-rewrite-evaluation.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-mqq5-alternative-model-runtimes.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-p3hj.1-audfprint-total-outage-diagnosis.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-rc1q-streaming-vs-standard-mode.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-u1n7j-vox-fix-verification.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-wcrb-oom-multiplier-forensics.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-ytgo.1-purpose-rubric.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-ytgo.2-essentia-embeddings.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-ytgo.3-clap-umap-deps.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-ytgo.4-sidecar-seam.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-ytgo.5-agpl-mit-compliance.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-ytgo.6-vector-index.md` | dated investigation evidence | unchanged |
| `docs/spikes/phaze-ytgo.7-verdict.md` | dated investigation evidence | unchanged |
| `docs/superpowers/specs/2026-06-10-windowed-analysis-design.md` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-06-28-ui-redesign-assets/aesthetic-C3-evolved.html` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-06-28-ui-redesign-assets/alt-A-mission-control.html` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-06-28-ui-redesign-assets/alt-B-file-workbench.html` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-06-29-multi-cloud-backends-design.md` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-06-29-tailwind-build-css-design.md` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-07-07-agent-queue-lanes-design.md` | dated design evidence | unchanged |
| `docs/superpowers/specs/2026-07-14-alembic-baseline-flatten-design.md` | dated design evidence | unchanged |
| `docs/superpowers/specs/README.md` | archive boundary | changed |
| `docs/tracklist-scraping.md` | current documentation | unchanged |
| `docs/ui-design-reference.md` | current documentation | changed |
| `docs/ui-reference-fixtures.html` | current documentation | unchanged |
| `src/phaze/agent_watcher/README.md` | embedded documentation | changed |
| `src/phaze/prompts/naming.md` | embedded documentation | unchanged |
| `tests/BUCKETS.md` | test documentation/fixture | unchanged |
| `tests/browser/FLAKE_RECORD.md` | test documentation/fixture | unchanged |
| `tests/identify/fixtures/tracklist_render/README.md` | test documentation/fixture | unchanged |
| `tests/identify/fixtures/tracklist_search/README.md` | test documentation/fixture | unchanged |
