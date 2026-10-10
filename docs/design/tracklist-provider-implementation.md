# Provider contract implementation

Date: 2026-10-09. Bead: phaze-1nqe2. Implements the six provider specifications available at
base `77e6601de666`. This records implementation choices for the local-provider foundation
bead; the synthetic boundary tests do not measure archive accuracy.

The boundary is `phaze.tracklist_providers`. The domain and Protocol ports import no ORM,
HTTP routes, queues, filesystem or transport clients. Registry invocation owns negotiation,
authorized effect budgets and typed outcomes. The local adapter discovers only explicitly
supplied linked references; its interpreter and owning-agent read effects are injected.
A fictional catalog in the contract tests substitutes without consumer branches.

The selected boundary scores 2 for cohesion, coupling, ownership, narrowness, replacement,
dependency direction and test isolation: one source-interpretation capability, no higher-layer
imports and no implicit effects. Alternatives were extending the ORM Tracklist model as the
port (65 indexed dependents, storage coupling) or importing existing proposal text (already
truncated and not parse-complete). Neither provides an independent interpretation contract.
No existing consumer is migrated in this foundation bead; subsequent beads own persistence,
reads, parsers and file-detail consumers. Importing/listing never schedules work or activates
reads. Existing acquisition retirement and proposal behavior remain unchanged.

## Public API

- `TracklistProvider.descriptor`, async `discover(context, budget, cursor, effects)` and
  async `load(candidate, budget, effects)` define the provider seam.
- `ProviderRegistry.discover(provider_id, context, budget, effects, cursor=None)` and
  `load(candidate, budget, effects)` enforce exact contract 1.0 and source identity.
- `ProviderEffects.read(source, budget)` receives a remaining allowance on each call, never
  a storage/approval session. References are opaque authorized IDs, not paths to open.
- `SourceInterpreter.interpret(candidate, content, budget)` is a synchronous bounded parser.
  `local_registry(interpreter=None)` explicitly registers the shipped local adapter.
  Without an interpreter, loaded text is an incomplete recognition snapshot, never invented
  tracks. Parser capability is not advertised until implemented.

Snapshot includes immutable identity/revision/revision scope, aware acquisition time,
set facts, release facts, track rows, decoded text, encoding, parser version, completeness,
provenance and rights evidence. Release facts retain original/normalized values, units and
line evidence. Timestamps retain meaning, precision, origin and rational offsets; each
origin's qualified offsets must be ordered. CUE frames remain exact fractions at 75fps.
Unknown or clock cues cannot acquire offsets. Certain set dates must be valid ISO dates;
uncertain/partial text is retained with its certainty.

`Snapshot.normalized_payload()` excludes only acquisition time and attribution URL; all
interpretation evidence, source revision, text and parser version remain for storage equality.
Read failures can be stored separately using SourceRead without inventing a snapshot.
Found trackless objects require explicit empty-track evidence. Partial snapshots must carry
incomplete completeness. Absence/unavailability/retry cannot carry successful snapshots.

## Finite bounds and validation

Default invocation limits: 64 candidates, 16 reads, 262144 bytes, 131072 decoded characters,
4096 lines, 1024 tracks and 30 seconds. Maximum accepted negotiated limits: 256 candidates,
256 reads, 1048576 bytes, 524288 characters, 16384 lines, 4096 tracks and 120 seconds.
Actual stored text is at most 262144 characters (1MiB worst-case UTF-8), native identities
4096 characters (16KiB worst-case UTF-8), revisions 2048 characters, parser IDs 128 characters,
release facts 256 and evidence 64 items of 4096 characters. Snapshot JSON is at most 4MiB.
These conservative limits are synthetic boundary choices, not archive-distribution claims.

Read budgets are cumulative: the effect receives only remaining bytes/characters/lines,
and attempted unauthorized reads or allowance changes remain sticky even if an adapter
catches the exception. Output limits also cover providers that do not read through effects.
Limits produce incomplete results; contract violations fail closed. Cancellation propagates.
Async deadlines bound cooperative I/O; they cannot preempt synchronous CPU work. Interpreters
must remain linear over bounded input and enforce line/track bounds, rather than claim CPU
preemption from asyncio timeout.

Balanced validation: baseline is no provider package and clean base; the existing application
sources are unchanged. Standalone tests can run with
`uv run pytest tests/identify/services/test_tracklist_provider_contract.py --confcutdir=tests/identify/services --no-cov`.
The same file belongs to the existing identify CI shard. Boundary tests exercise independent
substitution, exact negotiation, descriptor/import effects, nullable rows, evidence/precision,
malformed output, cumulative budgets, deadlines and cancellation. Full configured validation
and hooks run before handoff; final evidence is recorded in the bead handoff.
