# Tracklist provider specification review and gated implementation plan

Date: 2026-10-09. Bead: phaze-f5iyc. Epic: phaze-uquqk. Status: assembled specification for human review; all implementation remains separately gated.

The remaining local/orchestration/review sections were drafted as one authorized developer group. This document cross-checks all six specification sections, demonstrates a fictional independent provider, and describes future work without filing implementation beads. It does not implement boundary enforcement, contract tests, provider adapters, models, migrations or application behavior. No production/archive/provider API access was used. Source baseline after supported group refresh is `3ee5bfdb5e1149fdd60eafa3b34e0ea3c636b2c0`.

## Coherent reading order and authority

| Section | Governing decisions |
|---|---|
| [Public contract and registry](tracklist-provider-contract.md) — phaze-drv39 | Operator answer on 2026-10-08, phaze-uquqk: “In-repo providers with a stable interface (Recommended)”, to the question whether the first version should use an explicit registry of in-repo providers or support separately installed third-party packages. Descriptor/version/capabilities, bounded discover/load, typed outcomes and effect ownership; no dynamic package discovery or automatic activation. |
| [Matching policy](tracklist-provider-matching.md) — phaze-rxhn6 | Core evidence/ranking separate from association authorization; manual/approved decisions preserved, artist-only/uncertain-date/provider scores require review. Thresholds remain uncalibrated for set matching. |
| [Provider identity and persistence](tracklist-provider-persistence.md) — phaze-vn41h | Provider-scoped opaque full native keys, UUID identity, canonical/projection distinction, collision-safe lookup, immutable versions/provenance and refusal of lossy rollback. |
| [Local provider](tracklist-provider-local.md) — phaze-hhkbk | Agent-authorized bounded inputs; conservative planned CUE/text/embedded syntax; current recognition remains distinct from successful parsing/import. |
| [Orchestration and consumers](tracklist-provider-orchestration.md) — phaze-9ahjc | Same core ports for every provider, short validated import transactions, explicit decisions and consumer-specific timestamp/rights policy; actual source reader inventory. |
| This review and implementation plan — phaze-f5iyc | Compatibility scenarios, cross-section resolutions and explicit approval gates before code, migration, external traffic or deployment. |

Earlier sections were reviewed/approved as specifications and integrated into the epic. Their older baseline/source descriptions are dated evidence, not a statement that every pending branch remains pending today or that proposed APIs have shipped. MixesDB investigation is separate: findings can supply evidence later, but no adapter support or implementation plan is filed or authorized by this group.

## Cross-section resolutions

| Potential inconsistency | Consistent interpretation / remaining implementation work |
|---|---|
| Older contract describes offline recognition branch as pending | It is present at this group's source tip: [detect_embedded_tracklist / _local_sources](../../src/phaze/services/tracklist_review.py). Preserve the older document as historical evidence; current recognition is still not parsed-provider runtime behavior. |
| Contract has complete/incomplete snapshot evidence but storage requires complete snapshots | Typed incomplete may carry partial snapshot for review; it is not imported as a complete version or selected approved content. A genuinely demonstrated trackless source can be found-complete. Recognition-only cannot. |
| Discovery “complete” versus semantic absence | Complete only means exhausted documented enumeration scope; neither fewer-than-cap nor complete empty results proves alias recall/global absence. Source-object absence must carry narrower evidence and cannot erase approved data. |
| Descriptor capabilities versus per-source success | Parsing/timestamp capability is a possible operation, not a guarantee every format parses or every cue is qualified. Unsupported/partial cases retain typed reasons. Explicit registration does not activate reads or scheduling. |
| Persistence examples use local_companion namespace; proposed descriptor local_tracklist | Examples illustrate scope, not a settled legacy mapping. New local_tracklist keys explicitly discriminate companion UUID and embedded recording UUID/channel. Any historical namespace translation must be an explicit reviewed map, never rename opaque keys by guessing. |
| One Tracklist.file_id versus many-to-many sidecars | Proposed dedicated source-object→recording association relation handles future multi-target decisions. Current model remains unchanged; generic read adapters must precede enabling new multiplicity. Do not misuse historical propagated_from_set_key. Constraints/selection cardinality remain review questions. |
| Approved/manual data versus latest-version/import ordering | Acquisitions append pending versions; selected/approved version and target decisions are authoritative. Existing review updated-time/tag-confidence selection are compatibility shortcomings, not new policy. They require explicit migration/read adaptation before activation. |
| Timestamp syntax versus meaning/precision | [cue_review](../../src/phaze/services/cue_review.py) and [track_segments](../../src/phaze/services/track_segments.py) currently accept raw timestamp text; both need qualified-offset projections. Preserve legacy text as unknown until revalidated; never fabricate frame/second accuracy from minute/clock/unknown text. |
| Provider rights evidence versus export permission | Store immutable statements or unknown evidence; core consumer policy evaluates an action. Neither descriptor, local possession nor URL is a license verdict. Concrete display/export/tag policies need review. |
| Stable source identity versus revisions/moves | File UUID continuity retains local identity; changed content is a distinct observation/version. Truncated-prefix digests are not full revisions, and re-ingestion under new UUID cannot silently inherit identity. |

These resolutions agree with [Tracklist/TracklistVersion/TracklistTrack](../../src/phaze/models/tracklist.py), [FileCompanion](../../src/phaze/models/file_companion.py) and [retirement082](../../alembic/versions/082_retire_external_tracklist_acquisition.py): current stored IDs, NULLs, links and projections must survive; removed acquisition state is not reconstructed. No source or migration file was changed to make the proposal appear already implemented.

## Independent fake-provider walkthrough

Fictional `example_catalog`, shipped in-repo only after separate approval, does not know local sidecar syntax or MixesDB. It declares contract1.0, discover/load, revision and timestamp-evidence capabilities. Its injected transport returns bounded synthetic data. An explicit composition root registers it; importing/listing its descriptor issues zero reads. It receives no ORM session, approval service or scheduler.

1. Core sends a bounded RecordingContext for invented Example Artist / Example Event / known performance date2024-06-15 and a finite discovery budget. The adapter returns provider-scoped native set-01 with query evidence, revisionA and enumeration completeness. If upstream exhaustion is unknown, completeness stays incomplete even with one result under the cap.
2. Core invokes load through allowed injected effects. A complete synthetic source yields positions1 and2, with nullable artist/title for the unresolved second row, set-field certainty, retrieved_at, optional URL, rights unknown and parser representation evidence. Timestamp00:03:15 explicitly describes a qualified recording-relative second offset; another cue marked minute/clock/unknown keeps its original evidence and cannot enter exact exports.
3. Core validates output and runs the same matcher used for local_tracklist. Exact metadata can rank first but requires human association review; retrieval score never authorizes a link. Distinct local_tracklist:set-01 remains a separate source object despite the same native spelling.
4. Storage resolves the full scoped key and imports a pending version or reuses identical complete content. A human selects association/content explicitly. Stored generic read projections supply record/review, tags and eligible Discogs tracks; exact CUE/segments consume only qualified offset evidence after their separate policy checks.
5. A repeat revisionA/content is idempotent. RevisionB appends pending content without overriding manually selectedA or accepted Discogs links. A repeated revisionA with different content is an explicit conflict. Unavailable/retry/partial data retains approvedA.

Future adapter work is bounded to adapter interpretation, explicit registration/configuration, injected effect wiring and adapter fixtures only **after the core boundary and consumer/migration work exist**. A new timestamp meaning, rights policy or multi-target capability outside the negotiated contract needs a versioned domain extension and consumer review; it cannot be smuggled through unknown fields. The architecture does not promise zero core work for fundamentally new semantics.

## Written compatibility / failure matrix

These are expected design results, not executable tests or measured coverage.

| Case | Required boundary / result |
|---|---|
| Equal native IDs in local and fake providers | Different provider objects/canonical rows; full scoped equality before writes, no source collision. |
| Forced lookup-digest collision; long accepted key | Bucket resolver compares full native values; distinct keys coexist, long keys preserved exactly. Beyond negotiated limit fails before writes, never truncates. |
| Malformed output, duplicate positions, unknown provider, major2 | Contract/registration/version error before import; no fallback to another adapter or invented snapshot. |
| Partial/truncated parse, unavailable agent/transport or retry | Evidence retained, incomplete/unavailable/retry explicit; no complete version, approved-data loss or semantic absence. |
| Missing set/date/track metadata, unresolved row | Nullable values and certainty survive; rank/eligibility abstains or requests review, Discogs skips missing artist/title. |
| Tied candidates or uncertain date/alias | Semantic alternatives preserved; stable display ordering does not resolve identity, no automatic association. |
| First cue later than ten minutes, minute cue, clock or legacy text | No magnitude-based clock classification or invented relative offset/precision. Raw evidence visible; exact CUE/segments gated. |
| CUE75fps / multi-file origins | Rational frames preserved; per-FILE origin retained. No guessed concatenation or whole-recording conversion. |
| Same revision/identical content; same token/different content | Idempotent reuse / conflict. Changed representation creates evidenced reinterpretation, not mutation of history. |
| Two concurrent equal/distinct imports | Serialized resolver/canonical version writes; one equal version or distinct complete appends, preserved UUIDs and numbering. |
| Manual selection/rejection, approved legacy projection | Decisions retained; no score/retry overwrite. Historical canonical/projection identity and propagation evidence remain separate. |
| One source targets two recordings; one file disappears | Target-specific associations and origin review; source history not deleted merely because one target disappears. |
| Rights unknown or new statement arrives | Old provenance immutable; dated new evidence and action policy, no retroactive fabricated permission. |
| API minor extension / new required semantics | Only explicitly supported minor negotiation with bounded optional defaults; required/breaking changes require compatible core/consumer review and major policy. |
| Descriptor listing or disabled provider | Zero import-time effects; disabled invocation is explicit unavailable, no automatic activation or schedule. |

## Gated implementation breakdown

This is a proposed sequence for later approval, not new implementation beads, assignments or permission to code. The approval must cover its final scope, acceptance criteria and review/deployment policy.

| Future stage | Deliverable and dependency / planned validation |
|---|---|
| 1. Domain ports and explicit registry | Descriptor/version negotiation, bounded context/candidates/outcomes, nullable snapshot fields and rational timestamps. Prove fake substitution, duplicate/unknown/version rejection, no import-time I/O and forbidden storage/scheduler dependencies. |
| 2. Effects and local parsing | Agent-authorized owner/containment/nofollow/revision-aware bounded reads and documented CUE/text/embedded grammars. Prove BOM/CRLF/encoding, adversarial limits, stale/missing/refused reads and honest recognition-only outcomes. Do not change existing proposal context accidentally. |
| 3. Core matching policy | Pure evidence/ranking/eligibility with existing/manual decision precedence, explicit origins, date-kind ambiguity and ties. Evaluate retrieval and false-link/abstention strata independently before any automatic metadata policy. |
| 4. Storage and association compatibility | Reviewed provider-object/association schema, namespace mapping, complete-key resolver/lock order, immutable version/provenance and explicit selected decisions. Allocate migration revision at actual implementation head; preserve082. Prove seeded upgrade/restart, collisions, long keys, concurrent idempotence/conflicts and refusal of lossy rollback. |
| 5. Consumer adaptation and activation | Generic record/review/proposal/Discogs/tag/CUE/segment/stage/search/lifecycle projections, uncertainty/rights display, qualified exact timestamps and approved selection semantics. Exercise all inventoried consumers against legacy/manual/projection/NULL and multi-target data. Activation only after compatibility checks and explicit effects policy. |
| 6. Independent contract review and optional providers | Demonstrate fake adapter using the completed boundary, rerun boundary/storage/consumer tests and current source audit, then separately evaluate any actual remote provider under its own investigation/request policy. No MixesDB adapter is authorized here. |

Exact stage partitioning may be revised during later planning; stages overlap dependencies and must not enable partial incompatible writers. All future tests use the repository's isolated Postgres/Redis and serial-process guards. Every gate must state what actually ran: these written scenarios do not prove runtime quality, migration reversibility or provider coverage. Draft PR review does not authorize merge, deployment or implementation.

## Review points and validation record

Decisions still needed before implementation: explicit minor-version negotiation rules; concrete byte/character/track/line budgets; narrow TXT/NFO/embedded grammar and supported CUE directives; legacy namespace map; resolver/association constraints and lock enforcement; one-versus-multiple selected sources; decision retention and revisionless comparison serialization; alias authority/calibration; consumer action-specific rights policy and precision projection. These are listed limitations of the proposal, not silent defaults or requests to access a live archive.

Documentation validation for this group is recorded in its draft PR/handoff after the gate runs: local source-link/symbol checks, whitespace, selected-file and commit hooks, and the configured just check-fast documentation gate. No adapter/scorer/contract-suite/schema/consumer runtime changes were introduced. No proposed runtime/migration scenarios were executed as new coverage. Human review of the assembled specification is the next decision; future implementation planning remains separately gated.
