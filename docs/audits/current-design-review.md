# Current-design documentation review

This review reconciles maintained documentation with the merged implementation at
`51707485909dfd1c8ec88da760f4b548e84c403c`. It is a source review, not a claim that the
personal archive, production services, old measurements or cluster manifests were exercised.
No production/archive probe or destructive remediation was run. Historical quantities,
commands, callable examples and machine-instruction paths retain their meaning.

The ledger covers 92 guide/module and evidence records: 86 preflight/current dependency-guide records, four added developer/authoring records and two landed performance guides/records. “Full text” means the guide was read; it does not turn every
dated table into a fresh benchmark. Explicit partial/provenance-focused dispositions remain
partial. Source line references describe the reviewed baseline and may move after editing.
The landed sidebar and performance changes are reconciled against the final source revision above.

## Current design findings

- Analysis is exhaustive over natural fine/coarse windows. Chunk/model lifetime bounds current
  memory; old duration-linear RSS measurements remain dated pre-fix evidence. Default model
  selection has 12 sweeps; the `all` rollback mode has 34.
- Metadata, Analyze and Tracklists are independently eligible. Explicit stage dispatch differs
  from automatic continuation of already persisted cloud/lookup/companion intent. Proposals
  require both enrichment stages satisfied; selected-source freshness constrains consumption.
- Agent application persistence crosses authenticated HTTP. The SAQ broker independently uses
  Postgres; ORM-free imports do not mean agents have no Postgres socket.
- Runtime scalar policy, provider-policy JSON and backend-registry reload are distinct. Credentials
  remain deployment secrets. S3 control-client connect/read timeouts are not task wall-clock caps;
  agent multipart HTTP requests have a separate policy. SDK `max_attempts=1` allows one retry
  after the initial request; application push retry ceilings are enforced in the report callback.
- Current ORM metadata declares 45 tables and 47 FKs. The 54 Alembic files are baseline `039`
  plus `040` through `092`. Immutable provider observations/events are distinct from mutable
  lookup work, memberships and read cache. `remote_lookup_work.media_id` intentionally lacks a
  file FK; provider-policy JSON shares `runtime_config_override`, not a new physical table.
- Manual SAQ deletion requires proven owner quiescence; an old timestamp alone is insufficient.
  Kubernetes RBAC includes pod listing; model PVC examples account for 3,264,667,851 manifest
  bytes (3.040458868 GiB) plus margin. Read-only mounts do not override storage access modes.
- The committed RSS alert selects analysis job identities, not Linux/cgroup labels. Deployment
  owns that evaluation boundary; this review does not change alert behavior.
- Committed Compose/test harness pins use Valkey 9. This dispatch reused an already-running
  Redis 7 test container to protect concurrent seats; that validation limitation is not a source
  version mismatch. The shared containers were not restarted.
- Chromaprint remains deliberately installed by the permanent retention decision; removal of
  the fingerprint feature does not authorize reopening that decision.

## Per-guide evidence and scope

The [structured evidence ledger](current-design-ledger.json) consolidates the original 86 guide
records plus four added developer/authoring records and two landed performance guides/records against the integrated implementation.
The original preflight was read-only at an earlier revision; this ledger records implementation
review at the revision above. Source traces carried forward apply where implementation is
unchanged. “Partial” remains explicit for long dated evidence tables and reports: these rows
are provenance reviews, not fresh measurements or a full current-design certification.
Sidebar inventory policy and first-paint/OOB behavior are source-traced on all five affected current guides.

| Guide | Review scope | Disposition |
|---|---|---|
| `docs/essentia-analysis.md` | current guide: source-traced review | updated against current source |
| `docs/degenerate-audio.md` | current guide: source-traced review | updated against current source |
| `docs/stranded-analysis-recovery.md` | current guide: source-traced review | updated against current source |
| `docs/agent-queue-lanes.md` | current guide: source-traced review | updated against current source |
| `src/phaze/agent_watcher/README.md` | current guide: source-traced review | updated against current source |
| `docs/telemetry/traces.md` | current guide: source-traced review | updated against current source |
| `docs/telemetry/concurrent-identity.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/telemetry/overhead.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/telemetry/exporter.md` | current guide: source-traced review | updated against current source |
| `docs/telemetry/alerting.md` | current guide: source-traced review | updated against current source |
| `docs/runbook.md` | current guide: source-traced review | updated against current source |
| `docs/architecture.md` | current guide: source-traced review | updated against current source |
| `docs/gates-and-isolation.md` | dated evidence: provenance-focused/partial tables | updated against current source |
| `docs/telemetry/metric-catalogue.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `README.md` | current guide: source-traced review | updated against current source |
| `docs/quick-start.md` | current guide: source-traced review | updated against current source |
| `docs/arm64-agent-image.md` | current guide: source-traced review | updated against current source |
| `docs/multi-compute.md` | current guide: source-traced review | updated against current source |
| `docs/project-structure.md` | current guide: source-traced review | updated against current source |
| `docs/just-recipe-contract.md` | current guide: source-traced review | updated against current source |
| `docs/api.md` | current guide: source-traced review | updated against current source |
| `docs/pipeline-stats-performance.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/postgres-jit.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/deployment.md` | current guide: source-traced review | updated against current source |
| `docs/database.md` | current guide: source-traced review | updated against current source |
| `docs/operations/companion-availability.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/operations/companion-capture.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/operations/companion-viewing.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `.planning/README.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/spikes/README.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/superpowers/specs/README.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/telemetry/measurements/README.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/tracklist-retirement.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/analysis-evaluation-one-variant-candidate.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/analysis-evaluation-report-template.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/analysis-evaluation.md` | current guide: source-traced review | updated against current source |
| `docs/analysis-evaluation-upcloud-runbook.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `tests/analyze/recovery_cloud/README.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `tests/review/capabilities/README.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/bead-feedback-readback.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/bead-feedback-measurement.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/tag-partial-save-undo.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/tag-reviewed-revision.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/AGF.md` | current guide: source-traced review | updated against current source |
| `docs/ui-design-reference.md` | current guide: source-traced review | updated against current source |
| `design/DESIGN_SYSTEM.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `design/resonant-precision.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `CONVENTIONS.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/README.md` | current guide: source-traced review | updated against current source |
| `docs/repository-maintenance-contract.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/historical-evidence-audit-2026-09-11.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/maintenance-audit-2026-09-10.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/cloud-burst.md` | current guide: source-traced review | updated against current source |
| `docs/k8s-burst.md` | current guide: source-traced review | updated against current source |
| `docs/quality-implementation/consumer-mapping-disposition.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/immutable-parse-disposition.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/mixed-validation-disposition.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/selector-equivalence-disposition.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/coverage-generation-disposition.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/telemetry-capture-disposition.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/accepted-increment.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/implementation-outcome.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/pilot-disposition-manifest.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/zero-selection-review-release-verification.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/runtime-config-settings-audit.md` | current guide: source-traced review | updated against current source |
| `docs/ideas/decision-model-opportunities.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/git-topology-and-verification.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `tests/BUCKETS.md` | dated evidence: provenance-focused/partial tables | updated against current source |
| `docs/quality-assessment/coverage-baseline.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/runtime-profile.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/testing-risk-inventory.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/analysis-hunt.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/mutation-hunt.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/companion-hunt.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/assertion-proposal.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/coverage-proposal.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/selector-proposal.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/runtime-optimization-proposal.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-assessment/implementation-plan.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/coverage-baseline-refresh.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/structural-maintenance-audit-2026-09-12.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/structural-maintenance-rebaseline-2026-09-11.md` | dated evidence: provenance-focused/partial tables | reviewed; retained current contract or dated evidence |
| `docs/quality-implementation/final-union-verification.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/configuration.md` | current guide: source-traced review | updated against current source |
| `docs/configuration-inventory.md` | current guide: source-traced review | updated against current source |
| `docs/global-tracklist-providers.md` | current guide: source-traced review | reviewed; retained current contract or dated evidence |
| `docs/operator-copy-inventory.md` | current developer guide: source-traced review | reviewed current implementation/provenance |
| `docs/diagram-authoring.md` | current developer guide: source-traced review | reviewed current implementation/provenance |
| `docs/audits/current-design-review.md` | current developer guide: source-traced review | reviewed current implementation/provenance |
| `scripts/vendor/diagram_design/skills/diagram-design/references/style-guide.md` | current developer guide: source-traced review | reviewed current implementation/provenance |
| `docs/performance/files-workspace.md` | current contract source-traced; dated measurements provenance only | reviewed landed source contract; retained dated evidence |
| `docs/spikes/dashboard-polling-latency.md` | dated evidence: provenance-focused/partial tables | reviewed landed source contract; retained dated evidence |

## Inventory boundaries

[The classified inventory](documentation-inventory.tsv) uses the same documentation-surface
and class rules as `scripts/maintenance_inventory.py`, including non-Markdown records and
assets. Audience/owner/disposition columns separate maintained operator/developer material,
accepted decisions, dated evidence, runtime-loaded prompts, tool-local guidance and explicit
generator ownership. Inventory-only asset/provenance rows are not claimed as deep content
reviews. Historical records are retained as provenance, not mass rewritten into current design.

The diagram manifest accounts for all 25 original source figures and 245 original relation
statements through immutable migration metadata and per-relation dispositions. The current
renderer has no fixed diagram-count expectation; focused views may be added. Relationship
citations and semantic review matter independently of byte hashes or successful rendering.

## Quantity and behavior preservation

The numeric-token comparison against the integrated baseline found **zero removed numeric
tokens** in the six changed measured-evidence guides: Essentia analysis, degenerate audio,
stranded-analysis recovery, and telemetry traces, alerting and exporter guidance. Added tokens
qualify current default model selection or the committed collector expiration; dated samples,
durations, percentages and RSS values remain exact. The structured ledger retains the per-file
comparison. Identifier substitution changes names, never measured quantities.

The three product Python edits are comments/docstrings only. AST comparison against the
integrated baseline, excluding docstring expressions, is identical for `remote_lookup.py`,
`s3_staging.py` and `tasks/_shared/agent_bootstrap.py`. No retry, alert, filesystem or queue
behavior was changed to make documentation true.

## Bounded validation

The [export validation receipt](diagram-validation.json) binds the reviewed 35 exports to
their render inputs and artifact digests. Offline Chrome 155 rendered embedded Inter and Jura
in both skins with no external requests, text overflow, keyed-label collisions or invalid
decision ports. HTML and standalone SVG text geometry matched; every print export occupied
one page and mobile pages retained diagram scrolling. The pinned upstream geometry, skin,
self-check and contrast helpers passed through the documented font adapter.

The focused documentation, operator-copy, Kubernetes runbook and recipe batch passed 195
tests on an exclusive test seat. All repository pre-commit hooks passed. These bounded checks
do not replace the final lifecycle-owned full validation.
