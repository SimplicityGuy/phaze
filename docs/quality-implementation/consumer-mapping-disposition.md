# Files-table and revision-085 consumer mappings: exclusion disposition

Task **phaze-mbtzg (SE)** records the bounded consumer-mapping disposition for
implementation epic **phaze-75qpj**. Its approved design is the
[implementation plan](../quality-assessment/implementation-plan.md), assessment PR #716,
source `75e586ef9d1d412dfcf8ecb34818ecdda2576e70`.

**Disposition: EXCLUDE the proposed mapping pilot from current adoption.** Existing
selector authority, full escalation, floors, contexts, coverage thresholds and legitimate
skips remain unchanged. No executable mapping is added. Explicit exclusion is an approved
completion path when the prerequisites for a safe pilot are unsupported.

## Feasibility and authority

The [coverage-generation disposition](coverage-generation-disposition.md) records that
installed Repowise 0.55.0 lacks the verified immutable generation, pinned-reader and atomic
publication protocol. The [selector-equivalence disposition](selector-equivalence-disposition.md)
therefore excludes its shadow pilot. SE cannot establish a new template or migration fast path
by inventing a private mapping store or treating inferred consumer names as execution evidence.

The existing selector rejects unknown files, filename/import/graph inference and unrecognized
inference labels, and checks source movement and missing consumer files. Reading existing
consumers establishes potential scope; it does not pair every changed artifact byte and include
edge to a proven, collectable, unskipped consumer population. Those refusals remain in force.

**Operator decision, 2026-10-11.** Question as put: *"Before the velocity pilots run, which
observation cohort should they use? Repairs and coverage backfills can proceed without this
choice."* Answer as given (selected option label, verbatim): *"Next 20 completed beads, grouped
by change type"*. Durable record: the recovered-decision comment on bead `phaze-x45ht` and its
follow-up comment, which records the offered options; the
[PD manifest](pilot-disposition-manifest.md#observation-cohort-and-outcome-authority) gives the
recovery. The decision covers cohort membership only. The [measurement readback](../bead-feedback-readback.md) retains unknown original work
starts. This incomplete cohort does not supply matched accepted-bead latency or permission to
claim velocity gains from a reduced consumer run.

## Actual consumers and retained limits

| Artifact | Existing actual consumer | What it establishes and what remains unproved |
| --- | --- | --- |
| `src/phaze/templates/pipeline/partials/files_table_view.html` | `tests/integration/test_files_table_card.py` | Real ASGI rendering with PostgreSQL-seeded file rows; shared card, pager containment, empty state and sort gutter. These assertions do not certify native selection submission or every target/include dependency. |
| Native form dispatch | `tests/browser/test_htmx_native_submit_guard.py` | Existing browser population remains intact. SE executes no browser pilot and makes no files-table-specific browser-completion claim; ENV-B remains deferred under canonical `phaze-5jo4c`. |
| `alembic/versions/085_provider_source_observations.py` | `tests/integration/test_migrations/test_085_provider_source_observations.py` | Real upgrade from seeded 084, exact legacy field/version/link preservation, unused expansion round-trip, restart-idempotent backfill, immutable observation update refusal and evidence-preserving lossy-downgrade refusal. These existing oracles are not a validated selection mapping. |

The rendered partial directly imports `ui/primitives.html` and
`pipeline/partials/_record_row.html`. It includes `_stage_matrix.html`,
`_status_filter_bar.html`, `_tracklist_pill.html`, `_files_stage_control.html` and
`_files_current_status.html` under `pipeline/partials/`; some includes occur in both desktop
and mobile branches. This is a source readback of direct dependencies, not a complete transitive
include manifest. Conditional branches, imports, dynamic dependencies and native form actions
must be accounted for before any byte-specific mapping can authorize a reduced run.

Revision 085's actual PostgreSQL trigger refuses observation UPDATE or DELETE; its existing
migration consumer explicitly tests UPDATE refusal. The downgrade checks retained provider
objects or timestamp evidence before refusing evidence loss. The one consumer cannot establish
all untested trigger operations or schema states merely because its filename names 085.
A proposed mapping for 085 must never authorize neighboring revisions, a new revision, shared
`alembic/env.py`, fixture changes or resource configuration without independent consumer proof.

## Execution boundary and future proof

This bounded preflight reads source and the approved evidence. It does not publish a mapping,
refresh the shared index, inject faults into production/template/migration files, execute new
browser tooling or access production data. No denied pilot is disguised as a successful one.
Nonbrowser PostgreSQL/render/migration scope remains independently feasible for future work;
ENV-B is a separate browser prerequisite, not a reason to declare nonbrowser consumers absent.

A future adopted pilot must demonstrate real partial field/target/include faults and actual
migration seeded-field/trigger/downgrade-refusal faults against its named consumer population.
It must retain immutable update and downgrade refusal, prove missing/moved/skipped/unresolved
consumer escalation and reject shared-environment or neighboring-revision shortcut attempts.
Consumer setup and mapping invalidation costs need matched measurements with exact source,
bytes, ordered nodes, isolation and tool versions. None of these new mapping fault demonstrations
or paired timing samples was executed for this exclusion.

PD must record SE as excluded, with no new regression-detection, runtime, stable p95 or
accepted-bead velocity claim. G0 still validates the actual assembled canonical population;
A0 still reviews coverage and measured outcomes. The existing real consumers and full fallback
continue to provide their present authority. This disposition does not predeclare a provider
verdict, integration or main landing; hooks, review and exact-tree validation are recorded in
Beadhive and the task handoff.
