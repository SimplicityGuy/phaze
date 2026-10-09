# Tracklist provider identity and persistence compatibility

Date: 2026-10-09. Bead: phaze-vn41h — Specify provider-scoped stored identity and immutable provenance compatibility. Epic: phaze-uquqk — Tracklist provider specification. Status: proposed design for review; implementation remains gated.

This specification complements the [provider contract](tracklist-provider-contract.md). It proposes storage behavior and a future migration plan; it changes no models, schema, migrations, importers or tests. Source inspection used only repository files, with synthetic scenarios. No production/archive data or external provider requests were accessed.

## Baseline and existing readers

The canonical work claim refreshed the epic from main and repointed this untouched child. The source baseline inspected here is `6dd94c836fc85a22c77c5f3cb2d5d71461810934`. This includes the approved contract and the now-landed offline recognition path. The contract's older source baseline remains historical evidence, not a claim that its pending branch is still pending today.

| Source and symbol | Existing invariant or reader implication |
|---|---|
| [Tracklist and its indexes](../../src/phaze/models/tracklist.py) | Internal UUID primary key; `external_id` is nonnullable String(50), globally unique only for canonical rows; `source` is String(30) defaulting to manual, and `source_url` is nonnullable Text. Provider identity currently cannot disambiguate equal external IDs. |
| [Tracklist.is_canonical / is_propagated](../../src/phaze/models/tracklist.py) | Canonical means `propagated_from_set_key IS NULL`. A historical inherited projection must remain distinct from the source's canonical row. Provider identity must not redefine this predicate. |
| [TracklistVersion / TracklistTrack](../../src/phaze/models/tracklist.py) | Version UUIDs, monotonically allocated version numbers unique per tracklist, nullable track fields and original position order exist. Timestamp storage is String(20), without meaning/precision evidence. `latest_version_id` is a nullable UUID rather than a declared foreign key. |
| [get_file_tracklist_review / _local_sources](../../src/phaze/services/tracklist_review.py) | Review selects a stored row by file ID and updated time, loads the pointed version and ordered tracks, and separately recognizes embedded/CUE/text companions. Recognition does not create stored tracklist identities or tracks. Preserve both paths. |
| [get_match_pending_tracklists / _tracklist_sets_page_stmt](../../src/phaze/services/pipeline/tracklists.py) | Internal tracklist/version/track joins define Discogs matching; pages display external ID as a fallback label and count only latest-version tracks. A display label is not an identity lookup. |
| [Tracklist stage sorting](../../src/phaze/routers/column_sort.py) and [pipeline file sorting](../../src/phaze/routers/pipeline/files.py) | Both use external ID in a coalesced display expression. Preserve stable UUID pagination tie breakers; native display labels cannot become unscoped lookup keys. |
| [_get_tracklist_for_file / _get_tracklists_for_files](../../src/phaze/services/tag_comparison.py) | File-associated selection uses match confidence and UUID ordering, then latest-version IDs. A provider migration must not silently alter which approved source supplies tags. |
| [cue eligibility and track assembly](../../src/phaze/services/cue_review.py) and [timestamp conversion](../../src/phaze/services/cue_generator.py) | Existing cue eligibility uses approved/applied file linkage and nonnull timestamps in the latest version. Text alone cannot prove qualified relative offsets; future precision-aware adaptation must be coordinated with the consumer specification. |
| [DiscogsLink](../../src/phaze/models/discogs_link.py) | Discogs links reference track UUIDs, not provider/native keys. Preserve track UUIDs and the links across identity backfill. |
| [FileCompanion](../../src/phaze/models/file_companion.py) | Associations use existing companion/media file UUIDs and unique pairs. Those links survive source identity changes; a path string is not the association's stable identifier. |
| [retirement revision 082](../../alembic/versions/082_remove_1001tracklists.py) and [its migration test](../../tests/integration/test_migrations/test_082_tracklist_retirement.py) | Acquisition-only tables were removed while stored tracklists, versions and provenance were retained. Do not edit/reuse 082 or recreate acquisition state as part of provider identity. |

A source search for `Tracklist.external_id` and equality against `external_id` in `src/phaze` found the three display/sort references above and no current provider-native canonical resolver. The model's canonical-reader rule and [canonical-clause guard](../../tests/shared/test_canonical_tracklist_clause.py) still apply to a future resolver. This is a baseline-specific inventory, not proof that arbitrary SQL or future branches have no identity reads. Implementation must repeat an ORM/raw-SQL/import/export audit at its actual tip.

## Identity decision

Preserve the internal UUID identities of tracklists, versions and tracks. Add a logical provider object identity `(provider_id, native_id)` behind the storage port. Native IDs are opaque: case, slashes, percent escapes and Unicode normalization are not changed by core. A provider may define a canonical upstream identity with explicit evidence, but URL parsing or display-label changes alone cannot silently rename it.

| Identity component | Proposed ownership and lifetime |
|---|---|
| Provider ID | Stable registry identifier under the contract; display-name/parser-version changes do not rename it. |
| Native ID | Stable identity of the source object, independent of URL, path and revision. Keep the complete value; no truncation or lossy numeric conversion. |
| Internal object UUID | Storage identity for provider/native lookup and canonical uniqueness; allocated once after full native-key comparison. |
| Tracklist UUID | Existing canonical row or historical projection. Preserve it during backfill and ordinary revisions. |
| Revision token | Optional upstream evidence; distinct from object identity and API/parser version. |
| Version UUID / number | Immutable imported content and interpretation snapshot. Existing versions retain UUIDs and numbers; new versions append. |
| File UUID | Existing recording/companion association identity. A surviving UUID across a move keeps the same local object; deletion and re-ingestion with a new UUID does not prove identity continuity. |

Local companion identity should use its persisted companion-file UUID within the selected local provider namespace. Embedded content should use the recording-file UUID plus an explicit stable source-channel discriminator. Do not embed current absolute paths, content digests or mutable display filenames in the stable key. A companion used by several recordings remains one source object with separate association evidence; this does not authorize merging different recordings or replacing existing file links.

The current one-`file_id` Tracklist row and historical projection mechanism do not directly model every future multi-recording association. This spec preserves them and requires the local/provider orchestration specs to resolve that extension before implementation. Do not repurpose `propagated_from_set_key` as a generic local-association marker.

## Canonical uniqueness and long keys

The simplest candidate is a composite partial unique index on `(provider_id, native_id)` with the existing canonical predicate. It expresses scope clearly but a fixed String(50) is inadequate; an unconstrained large Text key also cannot promise arbitrary-length indexability. Merely replacing the column type and assuming a B-tree accepts every upstream key is not a complete long-key design.

Recommend a provider-object identity relation holding internal UUID, provider ID, full native text, lookup digest, and key-encoding/version evidence. Tracklists reference the object UUID. A partial unique index on that small object UUID, using the existing canonical predicate, permits exactly one canonical row per object while exempting existing projections. This is the storage form of provider/native canonical uniqueness, not a new canonical definition.

The digest is a bounded lookup accelerator, never equality or identity by itself. Its bucket index is nonunique. Under a transaction-scoped lock for the provider/digest bucket, resolve all bucket members using the complete, byte-exact native key; reuse the matching object or create a new UUID. Distinct full keys with the same digest must coexist. Every writer, including backfill, must use this resolver; a direct insert bypass is unsupported. A fixed lock-key collision may serialize unrelated buckets harmlessly, but must never merge their identities. Implementation must choose and validate the database mechanism, isolation and lock ordering before this design is accepted as runnable.

Native input limits belong to negotiated finite effect/contract budgets; storage preserves every accepted key without truncation. A key exceeding the negotiated bound produces an explicit contract error before any write. Document the selected byte limit and its source coverage at implementation time; do not claim unlimited keys or invent an upstream maximum here. Ordinary 51-character and much longer accepted keys must work without affecting UUID/index width. Never expose the digest as a substitute native ID in the public DTO.

## Immutable snapshots and idempotent import

The storage port accepts a validated snapshot and a separate core association decision. Providers never receive ORM sessions. Imports neither erase approved snapshots on failure nor treat discovery completeness as a storage identity guarantee.

For an evidenced upstream revision, retain `(provider object, revision token, representation version)` plus the exact normalized snapshot comparison. Representation version identifies the parser/interpretation semantics, not the public API major version alone. A repeated token with identical normalized content is idempotent. A repeated token with different content is a conflict requiring explicit evidence/review, not a silent overwrite. A deliberate parser reinterpretation can append a derived version with parent provenance, even if the upstream token is unchanged.

For revisionless sources, keep the revision unknown. Use a deterministic comparison signature over the complete validated normalized content and interpretation semantics for duplicate detection, then verify complete equality before reusing a version. A digest collision is not equal content. Retrieval timestamps, repeated fetches and URLs changing without semantic content changes do not alone require another content version; preserve observations as separate append-only acquisition evidence when useful. A digest of only a truncated prefix must never represent a complete source revision.

Import transaction sequence, proposed rather than executable:

1. Validate provider identity, completeness policy, track positions and bounded provenance before opening a write transaction.
2. Resolve the provider object under the bucket lock, preserving full-key equality.
3. Obtain the canonical row under its stable-object lock and canonical predicate. Never resolve a canonical row from an inherited projection's UUID by applying the predicate to that projection itself.
4. Under a canonical-row lock, compare the revision/representation and complete snapshot. Reuse identical content, surface conflicts, or allocate the next version number and insert the whole version/track set.
5. Update the latest-version pointer only after the version is complete and the import/approval policy permits it. A newer acquisition timestamp does not authorize replacing manually selected/approved content. Keep a pending candidate version if policy requires review.
6. Apply only the separately authorized association change and commit atomically. A failure leaves neither half-written tracks nor a dangling new latest pointer.

Retain the existing unique `(tracklist_id, version_number)` guard as defense in depth. Concurrent equal imports converge on one version; concurrent distinct revisions serialize and append distinct versions. Do not infer upstream chronology from lock order or local version numbers. Bound retries for transient lock/unique races; revision conflicts are explicit outcomes, not an unbounded retry loop. Establish one lock order across identity resolution, canonical rows and version writes to prevent avoidable deadlocks.

## Provenance fields and historical compatibility

Store immutable per-version provenance: provider/native identity as observed, upstream revision and its evidence/unknown state, retrieved time, source URL if supplied, parser/representation version, source format/encoding, completeness reasons and bounded original evidence. Preserve set-field certainty/origin, NULL track artist/title/label/remix fields, original track positions and unresolved entries. Manual corrections append a separate version with actor/action and parent-version evidence; never rewrite historical acquisition facts.

Timestamp evidence must retain original text, interpretation kind, source precision, evidence, rational relative offset when justified, and qualified/approximate/unusable status. Frame offsets retain a 75fps rational representation. Clock/minute/unknown values are not upgraded into precise offsets. Existing legacy timestamp strings get an explicit legacy interpretation-unknown marker until revalidated; do not reconstruct certainty from `mm:ss` syntax. Future consumer eligibility must use those semantics, while historical raw text remains readable.

Rights evidence retains the source statement/URL and acquisition context, asserted license/restrictions or explicit unknown. Missing license is not permission. New provider configuration cannot retroactively change an imported version's rights evidence; later corrections add dated evidence. Consumer/export policy evaluates stored evidence separately. Making the source URL nullable is proposed for new local objects; preserve every existing value, including legacy empty strings, without inventing web URLs or reconstructing removed acquisition history.

Backfill must map existing `source` strings through an explicit reviewed namespace table. Recognized historic provider values preserve their native keys as opaque text; no new adapter or network activation follows. Manual rows use a dedicated manual namespace and deliberate stable identity, preserving the old external ID as provenance; same text does not prove they came from the retired remote provider. Unrecognized/ambiguous source values remain quarantined as legacy namespaces or exceptions for review, not assigned to the first registered provider. Existing projections preserve their propagation key/confidence, UUIDs and file links and attach to the appropriate source object only when evidence is unambiguous.

## Written compatibility scenarios

These are design expectations, not tests run or measured production evidence.

| Synthetic scenario | Required result |
|---|---|
| `local_companion:set-01` and `example_catalog:set-01` | Two object UUIDs and canonical rows; neither overwrites the other. A matching file does not collapse source identity. |
| Repeated same provider/native/revision/representation/content | Reuse the existing version; preserve approved file selection and latest pointer. |
| Same key, distinct revision/content | Append a complete version with original provenance; retain prior version/track UUIDs and Discogs links. Approval policy controls selection. |
| Same revision token, conflicting content | Explicit conflict with both observations; no overwrite or false idempotence. |
| Unknown upstream revision, identical normalized content | Revision stays unknown; full equality permits version reuse, with acquisition evidence retained separately. |
| Forced native-key digest collision | Different complete keys coexist in the bucket; equality verification prevents cross-object import. |
| Long accepted key, or key beyond negotiated byte bound | Long key round-trips exactly; oversized key fails explicitly before writes, never truncates or aliases. |
| Two equal concurrent imports / two distinct concurrent revisions | One version for equal content / serialized distinct appends; unique numbering and no orphan tracks or dangling pointer. |
| Companion moved with same file UUID / deleted and re-ingested with another UUID | Stable local object / explicit continuity decision, not path-based or digest-based automatic reuse. |
| Canonical row and inherited duplicate projection | One canonical uniqueness slot; projection remains exempt and retains propagation provenance/file association. |
| Legacy manual row with NULL artist/title/date and missing latest version | UUIDs, unknown values and pointer state preserved; no invented provider, date or tracks. |
| Existing remote row with stored URL and approved manual link | Backfill preserves URL, source evidence and link; no external lookup or scheduling. |
| Minute cue, ambiguous clock text, or legacy timestamp | Original evidence retained, precision not increased; no new exact export eligibility without qualification. |
| Unsupported/incomplete/unavailable/retry provider outcome | Approved versions and links unchanged; no synthetic empty version or deletion. |
| New rights policy or source license correction | Old evidence immutable; new dated evidence plus consumer decision, never silent retroactive permission. |

## Future migration and rollback plan

Implementation is a separate approval. At its current migration head, allocate the next available revision; do not assume a numeric successor today or modify retirement 082.

Use an expand/backfill/validate/contract sequence. Add identity/provenance structures and nullable compatibility fields first while legacy readers keep using internal UUIDs. Backfill bounded batches with deterministic namespace mapping and a resumable progress record; do not run remote lookups. Preserve IDs, version numbers, track order, NULLs, approved status, file links and propagation metadata. Validate orphan pointers and ambiguous mapping as explicit exceptions rather than repairing them by guesswork. Import write switching and reader adaptation need a coherent deployment plan; competing old/new writers must not defeat resolver locks or canonical uniqueness.

Before replacing the global external-ID index, verify every canonical object maps to exactly one identity, expected projections remain exempt and all identity readers are scoped. Only then install the object-scoped canonical constraint and retire the old global uniqueness requirement. Long native keys belong in the identity relation; a compatibility external-ID display field can retain old values without being used as canonical identity.

Rollback before new-format writes can remove the expansion only after preserving any recorded evidence. After provider collisions, long keys or precision-aware versions have been stored, a lossless downgrade to String(50), global uniqueness and text-only timestamps may be impossible. The downgrade must preflight and refuse that shape, retain the expanded data read-only or require an explicitly reviewed forward recovery/export plan. Never truncate keys, delete one provider's colliding rows or drop provenance to manufacture a green rollback. A reversible compatibility phase requires exact archived old fields and a no-new-format-write condition; merely recreating old indexes is not proof of reversibility.

## Planned validation and remaining decisions

Later implementation should prove synthetic seeded upgrade/backfill/restart and rollback preflight, equal native IDs across providers, forced digest collisions, negotiated key limits, idempotent and conflicting revisionless/revisioned imports, parser reinterpretation and concurrent writers. Compare exact row/UUID/version/NULL/order/link/projection counts before and after; exercise real stored-data review, tag selection, Discogs joins and cue eligibility on the upgraded data, rather than checking schema equality alone. Scope any cluster-wide concurrency queries to the seat database. All runtime tests need isolated Postgres/Redis and their own trustworthy gate summaries.

Remaining implementation decisions: concrete identity-relation/lock enforcement, negotiated byte bounds, namespace map for ambiguous legacy sources, revision comparison serialization, acquisition observation retention and local multi-recording association representation. These are explicit review points, not permission to change storage now. The invariant decisions are fixed by this proposal: complete-key equality, provider scope, immutable snapshot evidence, preserved internal identities/unknowns/manual choices, and refusal of lossy rollback. None of the proposed migration/concurrency/runtime validations was executed for this document.
