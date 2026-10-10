# Shared provider orchestration and consumer compatibility

Date: 2026-10-09. Bead: phaze-9ahjc. Epic: phaze-uquqk. Status: proposed integration specification, no runtime routing or consumer changes.

Read baseline `3ee5bfdb5e1149fdd60eafa3b34e0ea3c636b2c0`; the approved [contract](tracklist-provider-contract.md), [matching](tracklist-provider-matching.md), [persistence](tracklist-provider-persistence.md) and proposed [local provider](tracklist-provider-local.md) govern this section. Earlier baseline descriptions in those documents remain historical. No provider/production/archive requests were made.

## Core-owned sequence and ports

```text
explicit core request + allowed descriptor/capabilities + bounded context
 -> injected read/transport effects (outside write transaction)
 -> discover/load typed outcome + source evidence
 -> core validation + provider-neutral matching assessment
 -> candidate review / separate association and content decisions
 -> StoragePort import transaction (identity, complete snapshot, pending version)
 -> selection/association transaction authorized by recorded decision
 -> generic stored read projection for record/review/Discogs/tags/CUE/segments
```

Descriptor lookup is explicit, rejects duplicate/unknown/incompatible identifiers, and performs no import-time I/O or automatic activation. Effects own allowed paths/hosts, credentials, admission, budgets, pacing, cancellation and retries. Providers cannot schedule tasks, write storage or approve changes. All providers use the same core ports; no provider-specific UI, queue or matching branch is proposed. A provider supplies metadata/rights/timestamp evidence, not consumer policy. No automatically scheduled remote acquisition is revived.

A proposed StoragePort accepts only a validated complete snapshot, representation/revision evidence and a separate authorized association/selection decision. Return typed idempotent reuse, new pending version, conflict or bounded transient-retry results. Recognition/incomplete snapshots may be held as bounded review evidence without becoming a complete imported tracklist version. The storage contract must distinguish evidence acquisition, pending version and selected approved version: “found” never means “approved”. Read projections select explicitly approved content/association rather than choosing the latest arrival by wall clock.

The [persistence design](tracklist-provider-persistence.md) specifies full provider/native-key equality under a bucket resolver lock, canonical-object uniqueness, canonical-row version serialization and immutable provenance. Preserve internal UUIDs, unique version numbering, NULL fields, ordered positions, projections and accepted Discogs links. Equal content/revision/representation reuses a version; conflicting reuse of a revision token is a conflict; new revisions append pending versions. Revisionless comparison verifies full normalized equality after a comparison signature, never a truncated-head digest. Complete transaction or rollback: no orphan tracks, half-version or dangling pointer. No read/transport occurs while the import holds write locks.

## Association multiplicity and existing selection

Recommend a dedicated provider-source-object association relation (implementation gated) to express one source object related to multiple recordings, with source version, target identity, actor/policy, revision/target evidence and decision state. Existing Tracklist.file_id and historical projections remain compatibility data. Keep one canonical source row, and expose the selected source/version for each recording through a generic read port. Do not duplicate canonical rows to fake multi-target identity or encode local sharing in propagated_from_set_key, which remains a historical duplicate-projection marker.

For single-target imported rows, retain current file links during compatibility rollout. A read adapter can bridge legacy rows to the new selected-association projection; new multi-target writes require consumer adaptation before enabling them. Concrete relation constraints, lock ordering and choice of one selected source versus multiple displayed sources need review before schema work. This resolves the architectural direction without claiming the current schema supports it. No legacy link is silently converted into human approval, and no arrival of a higher-ranked source overwrites an explicit manual/approved selection.

## Source-cited consumer inventory

An audit searched imports/references to Tracklist, TracklistVersion, TracklistTrack and build_track_segments across src/phaze, then read the paths below. This is a baseline-specific inventory, not a guarantee for later branches. Names like “match” in the existing pipeline refer to Discogs track matching, not the new set matcher.

| Current consumer and symbols | Existing behavior / future compatibility obligation |
|---|---|
| [record.build_file_record_context](../../src/phaze/routers/record.py), [get_file_tracklist_review / _local_sources](../../src/phaze/services/tracklist_review.py), [record tracklist partial](../../src/phaze/templates/record/partials/_tracklist_review_body.html) | Record drawer/page/poster loads stored tracks plus separate local recognition. Review chooses by updated_at; preserve recognized-but-unparsed local sources and existing stored display. Future selection must honor explicit approved decisions rather than latest-arrival ordering. Optional URLs render honestly. |
| [review facade](../../src/phaze/services/review.py), [SqlCueReviewReader](../../src/phaze/services/review_cue.py), [SqlTagwriteReviewReader](../../src/phaze/services/review_tagwrite.py) | Existing core reader ports already separate SQL from read models. Add generic selected-source/provenance evidence through those boundaries; no new provider branches in card assembly. Keep approve/write decisions separate. |
| [load_companion_targets / fetch_companion_contents / build_file_context](../../src/phaze/services/proposal_context.py), [generate_batch](../../src/phaze/services/proposal_provider.py) | Filename proposals receive file metadata and bounded raw companion context. They do not currently consume provider snapshot rows as a substitute. Preserve context/read-session split; any future selected-snapshot addition must mark source/uncertainty and stay within prompt budgets. LLM output cannot authorize a provider link or file move. |
| [match_tracklist_to_discogs](../../src/phaze/tasks/discogs.py), [match_track_to_discogs](../../src/phaze/services/discogs_matcher.py), [DiscogsLink](../../src/phaze/models/discogs_link.py) | Latest-version tracks with known artist/title get candidate release matches; accepted links are preserved. Maintain track UUIDs/nullable skips and short read/network/write phases. Do not reuse release confidence for set association. New pending versions must not replace an accepted track selection implicitly. |
| [cue eligibility / build_cue_tracks_for_versions](../../src/phaze/services/cue_review.py), [generate_cue_content / parse_timestamp_string](../../src/phaze/services/cue_generator.py), [cue routes](../../src/phaze/routers/cue.py), [write_cue_sheet](../../src/phaze/tasks/cue_write.py) | Current eligibility uses approved/applied status and a nonnull latest-version timestamp; exact timestamp meaning is not qualified. Future eligibility and track projection must require qualified relative origins/precision before exact export. Agent-local contained write and separate approval stay intact. |
| [_get_tracklist_for_file / _get_tracklists_for_files](../../src/phaze/services/tag_comparison.py), [compute_proposed_tags](../../src/phaze/services/tag_proposal.py), [enqueue_tag_write](../../src/phaze/services/tag_writer.py) | Current source choice sorts match confidence, then UUID. Tag overlay priority is accepted Discogs, tracklist, metadata, filename; date year is fallback. Do not silently change approved tag source while adding provider identity. Unknown metadata must not become fabricated strings; actual writes remain separately authorized. |
| [build_track_segments / _timestamped](../../src/phaze/services/track_segments.py), [poster title/layout](../../src/phaze/services/poster.py) | Segments parse raw text into relative seconds today, then join windows; record/presentation ticks share those segments. Future qualified offsets must gate precise joins too, not just CUE export. Approximate or clock/unknown timestamps can remain visible as evidence without precise segment placement. Preserve no-timestamp, out-of-range, nonmonotonic and open-final behavior. |
| [pipeline tracklist reads](../../src/phaze/services/pipeline/tracklists.py), [pipeline routes](../../src/phaze/routers/pipeline/tracklists.py), [stage_status](../../src/phaze/services/stage_status.py), [pipeline stages](../../src/phaze/services/pipeline/stages.py) | Existing pending match, paged sets, untracked files and existence-based stage state are stored-data views. Keep bounded paging and stable UUID ties; do not equate recognized local input with completed imported tracks or set-match approval. New states need explicit generic migration before activation. |
| [search_queries._tracklist_branch](../../src/phaze/services/search_queries.py), [column sorting](../../src/phaze/routers/column_sort.py), [pipeline file sorting](../../src/phaze/routers/pipeline/files.py) | Search/display external IDs are labels, not unscoped canonical lookup keys. Preserve internal result identity, filters/pagination and unknown fields; optional native display labels do not change canonical identity. |
| [scan_deletion](../../src/phaze/services/scan_deletion.py), [stale_rows](../../src/phaze/services/stale_rows.py), [pipeline skip](../../src/phaze/routers/pipeline/skip.py) | Deletion, stale-row reconciliation and stage skipping read stored links/operator state. New source/association relations need explicit lifecycle and cascade review; a missing file cannot justify deleting shared source history or silently rematching another recording. |

## Outcomes, provenance and rights at boundaries

| Provider/core outcome | Orchestration and stored-data effect |
|---|---|
| Found complete | Validate bounds/identity/positions and source certainty; assess match; import as pending or reuse idempotently. Selection requires separate recorded approval/policy. |
| Empty complete discovery | No candidates in stated enumeration only. Never recording-wide negative or proof of alias recall. |
| Explicit absent | Record scoped absence evidence; existing approved data remains. No invented empty version or unrequested deletion. |
| Incomplete / recognition-only | Preserve bounded evidence and partial content for review; no full snapshot import or definite absence. |
| Ambiguous / metadata conflict | Show generic alternatives/conflict and block automatic association; human decision remains explicit. |
| Unsupported / contract error | Fail before import; explain format/version/output issue. Never silently fall back to another provider. |
| Unavailable / retry / changed revision | Core decides bounded re-discovery/retry according to effects policy; retain stored approved content/links. |

Rights policy is evaluated at consumer/export boundaries from immutable source evidence. Unknown licensing stays unknown; local presence, optional URL or descriptor capability is not permission. Consumer policy must explicitly distinguish display of stored attribution/evidence from copy/export/tag actions. No legal permission is inferred or new automatic restriction asserted in this documentation. Policy decisions need review when implementation defines those actions.

Timestamp migration must retain raw legacy text while marking interpretation unknown until qualified. Exact CUE and precise window-segment joins must use validated recording-relative origins and source precision. Minute offsets are approximate, clock/unknown cues are unusable for exact offsets, and a large first cue does not classify clock time. Rational CUE75fps survives storage/projection; rounding to whole seconds cannot manufacture accuracy. An approved association alone cannot qualify a timestamp.

## Written end-to-end substitution scenarios

| Synthetic walkthrough | Required conceptual result |
|---|---|
| local_tracklist companion with two parsed tracks, exact authorized recording reference | Same matcher/storage/read port; pending content and explicit-origin eligibility remain separate. After explicit content/association selection, record/tags/Discogs see the selected snapshot. Qualified CUE offsets can enter exact CUE/segments; unknown fields remain NULL. |
| Independent example_catalog returns the same domain fields for native set-01 | Same generic import and consumers, no provider-specific SQL/UI/queue condition. Different provider/native namespace prevents overwriting a local object with the same native key. |
| Example revisionB arrives while human selected revisionA | Append pending version or conflict evidence; selected revisionA, UUIDs, accepted Discogs links and tag source stay intact. |
| Local companion used by two recordings | One provider source object, separate target decisions; unresolved multi-part origins prevent automatic exact export. Existing historical projections are not repurposed. |
| TXT recognition-only, unavailable agent or discovery cap | Local recognition and approved stored review remain visible; no fabricated empty snapshot, approved-data erasure or completed-stage claim. |
| Legacy timestamp parseable as 12:35 | Raw display remains; exact CUE/segments do not gain eligibility without meaning/precision evidence. Migration cannot silently classify it as relative offset. |

These are future behavioral checks, not executed integration tests. Implementation must audit actual consumers again, seed legacy/manual/projection/NULL data, verify source selection and UUID/link retention, exercise local and fake providers through the same ports, and test partial/retry/conflicting versions, multi-target associations and precision-aware display/export. No runtime routing, stored-data migration or consumer test was performed for this document.
