# Immutable parse pilot disposition

**EXCLUDE R1 (`phaze-2jvjd`) from execution and adoption in this implementation round.**
This bounded preflight leaves existing guards and their runtime behavior intact. It adds no cache,
selector authority, inventory redesign, renderer result reuse or competing checker. It claims no
runtime, coverage or accepted-bead velocity improvement.

The approved [implementation plan](../quality-assessment/implementation-plan.md) permits a reviewed
explicit exclusion. The [runtime proposal](../quality-assessment/runtime-optimization-proposal.md)
defines the immutable work hypothesis and its safety boundaries; it does not establish a speedup.
This disposition records an unexecuted conditional pilot, rather than a failed implementation or
proof that immutable parse reuse can never help.

## Execution prerequisites

**Operator decision, 2026-10-11.** Question as put: *"Before the velocity pilots run, which
observation cohort should they use? Repairs and coverage backfills can proceed without this
choice."* Answer as given (selected option label, verbatim): *"Next 20 completed beads, grouped
by change type"*. The reply arrived at 19:03:57 UTC; the cohort is the next 20 completed beads
after 2026-10-11 19:04:16 UTC. Durable record: the recovered-decision comment on bead
`phaze-x45ht` and its follow-up comment, which records the offered options; the
[PD manifest](pilot-disposition-manifest.md#observation-cohort-and-outcome-authority) gives the
recovery. The decision covers cohort membership only. Cohort selection is available. M0's readback is integrated, but original
work-start and permit authority remain unavailable for joined accepted-bead velocity. Missing starts
are censored, not reconstructed from validation timestamps. SC's supported selection/shadow
disposition is still outstanding at this preflight. The filed R1 acceptance requires M0 outcome
authority and approved preflight before pilot execution, and M0/SC before adoption. Those conditions
are not satisfied, so the three paired targeted screens do not run.

No comparator, candidate timing population, p95 estimate or supported-fast adoption measurement is
reported. The assessment's historical timings remain observations at their recorded revisions,
not measurements of this leaf. This exclusion does not relax G0's final assembled validation or
A0's acceptance requirements; PD must incorporate it explicitly.

## Source inspection

The inspected worktree starts at `cf7b4cada47d809b27889534b4dcf7063c2eb281`.
Direct source inspection confirms these existing boundaries:

| Consumer | Existing work | Boundary retained |
| --- | --- | --- |
| `tests/shared/test_historical_evidence_audit.py` | `_original_source_identifiers` already uses process-local `functools.cache`, returning a frozen set derived from fixed historical bytes | This is existing reuse, not a new optimization opportunity delivered here |
| The same module's `_snapshot` | Each mutation fixture extracts a fresh archive and creates an isolated Git index using shared read-only objects | Mutable fixture trees remain independent; no snapshot or audit verdict is cached |
| `scripts/audit_historical_evidence.py` | `_historical_tree_at_revision` reads an archive into a byte map on each call | A future cache would need resolved object identity, parser/helper version and configuration; unresolved revision labels or mutable returned maps are insufficient |
| The same script's `_current_historical_paths` and `audit` | Current tracked and nonignored untracked paths and current bytes are enumerated and compared | New files, removed baseline files, numeric drift, identifiers and live links remain fresh inputs |
| `scripts/check_documentation_integrity.py` | `check_documentation` gathers current paths/texts, indexes, links, generators, migrations and Mermaid sources | Parse reuse cannot retain a live inventory, link-target verdict, external observation or overall success |
| `tests/shared/test_documentation_integrity.py` | Current deterministic test calls the default `render_mermaid=False`; it asserts 25 blocks and pinned CLI version 11.12.0 | Its runtime is not a real Mermaid rendering measurement; neither the count nor renderer contract changes here |
| `render_mermaid_sources` | Fresh temporary directories per block; child lifecycle, complete SVG and cleanup checks | Existing renderer cleanup remains; no output reuse or browser installation is introduced |

Retained historical tests explicitly cover numeric changes, removed checkpoint files and identifier
leaks added after the baseline. Retained documentation tests cover malformed source, local links,
anchors, generator/migration relationships, partial renderer output, nonzero exit and process-group
cleanup. Their presence in source is not a claim that a new R1 negative-control run occurred.
No guard changed, so removing a new guard for a negative control would be inapplicable.

Repowise reports historical-audit hotspot score 0.8147, an increasing trend, three direct dependents
and one counted fix over six months. Its indexed owner has bus factor 1. Documentation integrity
has two direct dependents; maintenance inventory has five. These are indexed historical signals,
not runtime failure probabilities. The index is at `d1835b022f99` and behind the inspected source;
the source above governs this disposition. No shared-index refresh or source edit was performed.

## A future reconsideration

Reconsider only with execution authority and a concrete eligible immutable operation profile.
Use the existing guard owners, not another checker. Keys must include repository/object identity,
resolved commit/tree, immutable input digest, helper/parser version and every semantic configuration
input. Source paths still participate in link resolution. Cache values must be immutable or copied
before mutation. Retained storage must be bounded and task-owned, with atomic publication and
failure/interruption cleanup. Unknown provenance is a miss.

Re-enumerate all live paths and bytes on every guard run; preserve fresh mutable historical
snapshots, numeric and identifier inventories, malformed-source refusals and interruption controls.
An independently specified exact diagram inventory is required before changing the fixed count.
Canonical `phaze-e31sv` remains outside this disposition; its precedent is not settled here.
Any rendering claim requires a separate actual pinned-renderer consumer and failure/cleanup
population; parser timing cannot supply that evidence. After preflight and authority, three paired
targeted screens are only screening evidence, followed by supported gates and the selected joined
cohort. They cannot establish a stable tail latency or manufacture a velocity claim.

Only this report and its maintained index entry change. Fresh independent review and the
authoritative exact-tree Beadhive provider verdict remain required before integration.
