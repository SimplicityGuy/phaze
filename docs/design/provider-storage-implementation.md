# Provider storage implementation

Date: 2026-10-09. Bead: phaze-gq28d. Epic: phaze-kf7jr.

The storage layer implements the identity and observation portion of the [provider persistence design](tracklist-provider-persistence.md). It adds no filesystem or network reads and does not overwrite media metadata or legacy tracklist selections.

## Tables and downstream API

`ProviderSourceObject` stores the opaque complete `(provider_id, native_id)` identity. Local binding contains the companion file UUID or embedded recording UUID and channel, with a durable original UUID separate from the nullable current inventory foreign key. Moves preserve identity; deletion does not erase the source. The digest index is nonunique. The resolver takes a transaction-scoped advisory lock for the digest bucket and compares full UTF-8 keys; digest collisions serialize safely and remain distinct objects.

`ProviderSourceObservation` is immutable source content or read/error evidence. Its normalized JSON payload preserves provider facts, original/normalized release values, source line, uncertainty, NULL tracks and rational timestamp semantics. Decoded text, encoding, revision scope, parser version and source URL remain independently available to database-only viewers. A complete read can produce an incomplete parse: parse completeness never implies text truncation. Read observations retain actual read truncation. SQL triggers reject mutation of stored observations.

`ProviderRecordingCandidate` stores pending target-specific evidence. `ProviderRecordingSelection` stores the explicit current choice separately for `tracklist` and `release_metadata`. `ProviderSelectionEvent` preserves every reviewed choice with actor, time, target and observation. Replacing a choice retains accepted candidates and past events. Optional legacy tracklist/version references use SET NULL on deletion; deleting a companion never cascades observations or another recording's selection.

The service is [provider_persistence.py](../../src/phaze/services/provider_persistence.py):

- `store_snapshot(session, snapshot, ...)` returns `StoredObservation(source, observation, reused, conflict)`; new content remains independent of target authority.
- `store_source_read(session, identity, read, parser_version=..., ...)` stores typed read failures and bounded text without inventing parsed tracks.
- `add_candidate(session, media_id, observation_id, evidence=...)` is an idempotent pending association.
- `select_observation(session, media_id, observation_id, kind=..., actor=..., accept_conflict=False)` records an explicit choice only for a complete parsed candidate belonging to that target. It does not create legacy projections.
- `source_availability(session, observation, media_id)` returns current inventory/link state independently of retained historical authority. Missing, ambiguous, unlinked and stale sources remain viewable as history.
- `backfill_legacy_identities(session, batch_size=100)` resumes through unexpanded canonical rows, preserving legacy values and unambiguous projection links. Legacy namespaces are inert hashes of the original source string; manual native keys use row UUIDs. No namespace activates an adapter.

All calls use a caller-owned transaction and never commit. Imports lock digest buckets, then bound source inventory with UPDATE, then source objects before content comparisons. Unbound provider sources use bucket then object. Capture takes the same bucket before rechecking inventory and storing its read. Inventory precedes object locks so file deletion's SET NULL updates cannot invert the order; it also prevents a deleted file binding from being inserted. Selection locks recording/source inventory in original-path order before companion pairs, matching deletion. It refreshes current database state before granting authority. Snapshot versions are not selected merely because they arrived later.

Composed target imports must acquire their provider buckets with `lock_source_bucket` first, then all media and companion inventory rows in global original-path order, before resolving source objects, adding candidates, selecting sources or changing legacy projections. Acquiring files before buckets would invert capture's order. Capturing raw text itself creates no target candidate or selection and needs only its bound source inventory row.

## Revisions and finite bounds

Complete normalized equality, including interpretation semantics, determines retry idempotence; acquisition time and URL alone do not create new content. Repeated full revision with conflicting content is retained as an explicit conflict. A new parser with unchanged source text can append reinterpretation with parent evidence; changing text under the same full revision remains a conflict across parser versions. Revisionless observations remain revisionless. Digests never substitute for full equality.

A companion read whose full revision is a SHA-256 of complete source bytes supplies the explicit provenance marker `revision:sha256:full`. Only that evidenced companion revision is compared with inventory hashes for stale detection. Opaque hexadecimal tokens and embedded tag revisions do not become media-byte fingerprints by their shape.

Accepted storage limits are native key 4096 characters and 16KiB UTF-8; revision 2048 characters and 8KiB UTF-8; text 262144 characters and 1MiB UTF-8; parser version 128 characters (at most 512 UTF-8 bytes); complete normalized JSON 4MiB; tracks 4096; release facts 256; candidate evidence 64 entries of at most 4096 characters. Limits fail before storage effects rather than truncating identities. Parser/read caps remain explicit evidence. NUL string values are rejected because PostgreSQL text/JSON cannot represent them.

## Compatibility and rollback

Migration 085 expands storage and nullable legacy fields without rewriting existing UUIDs, versions, track ordering, NULL fields, approved statuses, file associations or Discogs links. Canonical identity remains `propagated_from_set_key IS NULL`. Source-object uniqueness governs expanded rows, while unexpanded rows retain legacy external-ID uniqueness. Raw legacy timestamps remain unchanged, with NULL evidence denoting an unknown interpretation until separately qualified.

An unused expansion can downgrade. Once source objects or timestamp evidence exist, downgrade refuses before deleting any data; recovery is a forward operation. No migration truncates long keys, discards provider collisions, or deletes observations to recreate old uniqueness. A separate resumable backfill workflow may invoke the bounded identity helper; it must not interpret unknown historical provider strings as registered adapters.

Seeded migration and real-PostgreSQL tests cover legacy preservation, restart, full-key/digest collisions, concurrent retries and distinct revisions, error evidence, parser reinterpretation, retained selection audit, relinking/deletion and rollback refusal. These tests validate storage primitives; parser production, import projection and file-detail rendering are separate epic beads.
