# Tracklist provider contract and built-in registry

Date: 2026-10-08. Bead: phaze-drv39. Epic: phaze-uquqk. Status: proposed specification; implementation awaits operator review.

This document specifies an API; it does not add providers, parsers, registries, storage changes or tests. The operator authorized investigation, specifications and draft PRs, without merge or deployment. No production, archive or remote access was used for this section.

## Decision and authority

Operator question recorded on phaze-uquqk: “For provider plugins, should the first version use an explicit registry of providers shipped inside phaze, or support separately installed third-party packages too?” Selected answer verbatim: **“In-repo providers with a stable interface (Recommended)”**. Date: 2026-10-08.

Propose an explicit built-in registry and a source-neutral domain port. Providers interpret source content through injected effects. Core policy owns set-to-recording matching, storage/version import, association authorization and consumer eligibility. Local companion/embedded content is the first planned provider. MixesDB remains a separately investigated possible adapter; no support or remote-provider implementation is authorized here. Packaging through entry points, third-party installation and directory scans is outside this decision.

## Source baseline and evidence

Read baseline: `331b2af27fe10bceabdcdc4fcb6a95e8941728d6`. Source links below refer to that revision unless explicitly marked otherwise. The retirement/offline-review work is ongoing; this specification must not be described as its implementation or proof of its completion.

| Evidence | Current behavior and consequence |
|---|---|
| [Tracklist, TracklistVersion, TracklistTrack](../../src/phaze/models/tracklist.py) | Canonical `external_id` uses a global partial unique index and String(50); `source_url` is nonnullable. Nullable artist/title already represent unknown tracks. Versions enforce `(tracklist_id, version_number)` uniqueness. Provider-scoped identity, optional URL and richer immutable provenance require a later persistence design. |
| [Tracklist.is_canonical / is_propagated](../../src/phaze/models/tracklist.py) | Canonical rows and inherited projections have different identities; no provider DTO may flatten that storage distinction. |
| [FileCompanion](../../src/phaze/models/file_companion.py) and [CompanionContentFeatures](../../src/phaze/models/companion_content.py) | Existing association and content evidence are inputs to the local provider; a recognized sidecar does not itself establish a parsed tracklist. |
| [extract_content_features](../../src/phaze/services/companion_features.py) | Content recognition is separate from parsing into ordered tracks. Reuse evidence without inventing successful parser output. |
| [read_companion_bounded_sync](../../src/phaze/services/companion_read.py) | Existing reads use a decoded-character bound, UTF-8-sig, TXT/NFO CP437 fallback and optional nofollow opening. Proposed provider effects must preserve bounded reads and report truncation explicitly. |
| [read_companion_files / _read_all_sync](../../src/phaze/tasks/companion_read.py) | Authorized reads happen on the owning agent after containment resolution; current missing/refused reads are skipped. New typed outcomes must distinguish failures rather than interpreting a shortened list as absence. |
| [get_file_tracklist_review / FileTracklistReview](../../src/phaze/services/tracklist_review.py) | Baseline reads latest stored tracks without remote acquisition. Pending branch `wt/bead/issue/phaze-3zqi7` at `8bc9ab0ff1dd300a3aea99b6acbc4778d4759f6d` additionally has `detect_embedded_tracklist`, `_local_sources` and `local_sources`: bounded descriptive-tag recognition and linked CUE/TXT/NFO evidence, not parsed rows. That branch is evidence of planned compatibility, not a landed baseline guarantee. |
| [_approved_applied_tracklist_base](../../src/phaze/services/cue_review.py), [parse_timestamp_string / generate_cue_content](../../src/phaze/services/cue_generator.py) | Current eligibility checks nonnull timestamp text and export converts text to seconds. They do not prove timestamp meaning/precision. Qualified offsets below require later consumer adaptation. |

Repowise `get_context` was attempted but returned “not available to the model”; evidence was verified directly from repository source and the named local branch. No indexed architectural claim is assumed.

## Proposed port and descriptors

Illustrative signatures, documentation only:

```text
Provider.descriptor -> ProviderDescriptor
Provider.discover(context: RecordingContext, budget: DiscoveryBudget,
                  cursor: OpaqueCursor | None, effects: ProviderEffects)
    -> DiscoveryOutcome
Provider.load(candidate: ProviderCandidate, budget: LoadBudget,
              effects: ProviderEffects) -> LoadOutcome
```

Operations are asynchronous and cancellation-aware. Descriptor construction and registry import perform no I/O. Effects are supplied per invocation; importing, registering or listing a descriptor never activates a provider. A provider cannot obtain storage sessions, approval services or a global network client through this port.

| Descriptor field | Contract |
|---|---|
| `id` | Stable lowercase identifier, proposed restricted syntax `[a-z][a-z0-9_]*`; unique in the registry. Native IDs are never registry IDs. |
| `contract_version` | Explicit major/minor pair; initial proposal `1.0`. This describes the port, not the upstream source revision. |
| `capabilities` | Explicit set: discovery, load, local sidecar, embedded metadata, remote transport, revision evidence, track parsing, timestamp evidence. Claims describe possible features, never guarantees on each result or permission to activate an effect. |
| `display_name` | Human label; excluded from identity and matching authority. |

A provider must support load. Discovery capability can be absent for an explicit-candidate-only provider; discover then returns unsupported. A provider declaring track parsing may still return recognition-only or incomplete outcomes for particular inputs. Timestamp capability does not assert every timestamp is usable as an offset.

| Input | Bounded data and ownership |
|---|---|
| `RecordingContext` | Stable internal recording reference, explicitly supplied filename/artist/event/date hypotheses with uncertainty and origin, optional duration, and authorized references to linked local companions or embedded metadata. No recursive archive paths or arbitrary controller file access. Provider receives only fields needed for declared capabilities. |
| `DiscoveryBudget` | Maximum returned candidates, response bytes/decoded characters, request/read count and deadline. Core sets finite positive limits and enforces them at injected effects; provider also bounds parsing/line count. Exact defaults await implementation evidence. |
| `OpaqueCursor` | Provider-issued continuation bound to provider, normalized query, contract version and source snapshot if available. Never an arbitrary executable URL; reject context/provider mismatch. Core treats token as opaque, bounded data. |
| `ProviderCandidate` | `(provider_id, native_id)`, optional observed revision, optional URL, bounded display metadata with certainty, discovery evidence and candidate origin. Native identity is stable independently of content revision and display/path changes. Candidate similarity is evidence, never association authorization. |
| `LoadBudget` | Finite response/track/line limits, read/request count and deadline. Expected revision comes from the candidate; a changed revision yields explicit evidence and cannot silently stand for the originally discovered snapshot. |

## Discovery result and completeness

`DiscoveryOutcome` is either a candidate batch or one of the typed failure outcomes below. A candidate batch contains bounded candidates, `completeness` (`complete` or `incomplete`), machine-readable `reason`, bounded evidence and optional continuation cursor. Reasons include source enumeration exhausted, candidate cap, source cap, read truncation, deadline, unavailable page and changed source snapshot. Partial candidates remain available with incomplete evidence; never silently promote them to a complete result.

“Complete” means the provider exhausted the stated query/enumeration scope under the source's demonstrated rules. It does **not** guarantee artist-alias recall, all naming variants, all upstream records or semantic absence. Fewer results than a cap is insufficient evidence of exhausted enumeration. An empty complete batch means no candidates for that scoped query, not “this recording has no tracklist”. Explicit `absent` requires a reason and evidence identifying the narrower object/scope that was absent; consumers cannot negative-cache a recording-wide assertion from it. A source which cannot demonstrate exhaustion returns incomplete even without a cursor.

## Snapshot domain fields

A successful load yields a snapshot independent of ORM models. Recognition without parsed tracks returns incomplete with recognition evidence and optional partial snapshot, never a found snapshot fabricated with invented tracks. An explicitly trackless source object may be found only with complete, demonstrated empty-track evidence.

| Field | Semantics |
|---|---|
| `identity` | Provider ID and provider-native ID, unabridged at the domain boundary. Same native ID in different providers is distinct. Storage representation/long-key compatibility is delegated to phaze-vn41h. |
| `revision` | Optional opaque upstream revision plus evidence; explicit unknown if unavailable. Distinct from stable identity. A content digest used as revision must identify the bounded/full content scope honestly. |
| `retrieved_at` | Timezone-aware acquisition time supplied by the effect boundary; parse time is separate if recorded. It does not imply publication time. |
| `url` | Optional source reference for attribution; not an identity requirement or automatic permission to fetch. Local sources may have no URL. |
| `set_metadata` | Nullable set artist/event/date, each accompanied by certainty (`known`, `inferred`, `unknown`, `conflicting`) and origin evidence. Preserve partial or ambiguous date text without manufacturing an exact date. |
| `tracks` | Ordered entries with unique positive positions, nullable artist/title, optional label/remix/mashup metadata, timestamps and bounded source evidence. Preserve unresolved rows and original ordering. Position gaps are allowed when supplied by source; preserve them rather than treating a gap as proof of missing rows. |
| `completeness` | Track enumeration/parse completeness with reasons/evidence, separate from discovery completeness. Unknown fields survive as bounded documented extension evidence, not flattened into matching authority. |
| `rights_evidence` | Source statements/links, asserted license or restrictions if present, retrieval context and unknown state. Evidence is not a provider's permission verdict; consumer/export policy evaluates it. |
| `provenance` | Source format, encoding, parse/interpretation version and bounded original fragments or references. Immutable for an imported snapshot; manual edits create separate provenance. |

Typed validation rejects duplicate positions, invalid identity, impossible dates represented as certain, oversized payloads and unbounded extension data. Nullable title/artist remain nullable, never literal `ID` or empty strings coerced to certainty. Partial malformed records yield incomplete with affected positions and reasons, or unsupported if syntax cannot be interpreted; invalid provider output is a contract error and cannot be imported.

## Timestamp meaning and precision

Each timestamp preserves `original`, `kind` (`offset`, `clock`, `unknown`), `precision` (CUE frame at 75fps, second, minute, or unknown), interpretation evidence, optional rational offset and `offset_usability` (`qualified`, `approximate`, `unusable`). Precision describes what the source supports; conversion never increases it. Qualification requires demonstrated recording-relative meaning, a known origin, valid range/order and sufficiently precise source evidence. Core revalidates consumer-specific eligibility. Minute offsets may support approximate display/ranking, but cannot become exact export cues. Clock/unknown values have no invented recording-relative seconds.

A first cue later than ten minutes alone **does not prove clock time**: a valid recording can start with a long intro or partial listing. Conversely, a syntactically valid `mm:ss` is not proof of an offset. Explicit clock annotations, source format semantics, recording-origin evidence and ambiguity must be recorded. Multi-file CUE indexes are relative to the referenced file; conversion to a recording-wide origin is a separate evidenced mapping, never automatic concatenation. Preserve CUE75fps as rational frame offsets instead of rounding to whole seconds.

## Typed outcomes and retry policy

Every non-found outcome includes stable code, bounded human detail, scope and evidence. Provider exceptions may not collapse into empty found results.

| Outcome | Meaning and core handling |
|---|---|
| `found` | Valid snapshot for exactly the provider-native object; validation/import and association remain separate core steps. |
| `absent` | Demonstrated missing native object or exhausted narrowly identified scope. Never erases approved stored data or implies global semantic absence. |
| `incomplete` | Bounded/truncated/partial enumeration or parse; may retain partial snapshot/candidates and cursor. Not a successful full import or definitive negative. |
| `ambiguous` | Multiple plausible identities, conflicting metadata or timestamp interpretations; carries alternatives/evidence for ranking or human decision. No automatic link. |
| `unsupported` | Format, capability or interpretation outside provider contract. No blind retry; explicit-candidate providers may return this for discovery. |
| `unavailable` | Permission refused, source unreadable, missing agent, disabled policy or persistent access restriction; preserves reason, no conflation with absence. |
| `retry` | Transient transport/read failure or stale revision requiring re-discovery; optional bounded retry-after evidence. Core chooses retry budget/backoff and honors cancellation. |
| `contract_error` | Malformed provider output, duplicate registry ID, unknown provider, incompatible version or mismatched candidate/cursor. Fail closed before import; remediation is configuration/adapter correction. |

No outcomes delete historical rows, replace manual selections, bypass approval or revive retired acquisition scheduling. Request pacing, admission, credentials, allowed hosts/paths and retry scheduling belong to injected core effects. Providers request authorized reads/transports and interpret returned evidence; they neither schedule external work nor write storage. No remote traffic is authorized by this specification.

## Registration and compatibility

The composition root explicitly constructs known adapters with injected effects and registers descriptors before use. Registry enumeration is deterministic. Duplicate IDs are errors even if implementations look equal. Unknown IDs do not fall back to another provider. Disabled descriptors can be visible for explanation, but invocation fails unavailable/disabled until core explicitly enables the capability. No import-time I/O, automatic activation, entry-point discovery or package scans.

Core accepts exactly supported major versions and explicitly supported minor versions. Additive optional fields need documented defaults and bounded extension preservation; unknown required capabilities/fields fail compatibility negotiation. A breaking semantic or required-field change increments major and needs consumer/persistence review before activation. Adapter parser revision changes independently of API version and is recorded in provenance. No implicit downgrade or coercion of ambiguous timestamps to preserve apparent compatibility.

## Written independent substitution scenarios

These are design walkthroughs, not runnable tests or executed coverage. Fictional provider `example_catalog` is independent of local formats and MixesDB.

| Scenario | Expected port behavior and downstream implication |
|---|---|
| Explicit synthetic object `set-01`, artist “Example Artist”, event “Example Event”, known date, two tracks (second unresolved) | Registry selects `example_catalog`; load returns nullable artist/title in position2. Same core matching/import policy consumes the snapshot; no provider-specific storage/UI/queue branch is required. |
| Local native `set-01` and example native `set-01` | Distinct provider-scoped identities; neither overwrites the other. Matching may propose both for the same recording and requires core authorization. |
| Example changes revisionA to revisionB after discovery | Load reports changed-revision evidence/retry; core re-discovers or reviews explicitly. Existing approved revisionA remains intact. |
| Example discovery returns3 candidates with a requested cap10 but source exhaustion unknown | Batch is incomplete with reason; no recording-wide absence or alias-recall claim follows. |
| Example has one precise relative cue and one ambiguous `12:35` | Preserve interpretation and precision per entry; only evidenced qualified offsets can enter an exact-cue consumer. First cue magnitude does not decide meaning. |
| Example read returns malformed or truncated text | Unsupported/incomplete with evidence; no successful invented empty tracklist or deletion of approved rows. |
| Register a second `example_catalog`; invoke missing ID; invoke unsupported major2 | Separate duplicate/unknown/version contract errors before effects or storage. |
| List descriptors while example transport would fail | Listing succeeds without a transport call; enabling later still requires explicit core policy. |

Planned validation after implementation approval: independent fake adapter substitution, descriptor/version rejection, effect-budget/cancellation enforcement, cursor binding, malformed/partial outcomes, provider-scoped storage collisions, revision concurrency and qualified timestamp consumer checks. None was run here. phaze-hhkbk, phaze-rxhn6, phaze-vn41h and phaze-9ahjc must refine local parsing, matching, persistence and consumer policies against this contract; phaze-f5iyc assembles their compatibility review before an implementation plan can proceed.

## Remaining review points

The interface shape and effect ownership are proposed for human review. Exact budget defaults, persisted unknown-field representation, revisionless-source comparison and compatible-minor policy require implementation evidence; they do not block this documentation dispatch. Numeric matching thresholds belong to the separate matching specification and must not be invented as measured accuracy here. All application, tests, dependency/build changes and migrations remain gated for later approval.
