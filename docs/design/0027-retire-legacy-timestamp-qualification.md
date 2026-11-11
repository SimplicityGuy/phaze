# ADR-0027: Retire legacy timestamp qualification

Date: 2026-11-11. Status: accepted. Bead: phaze-zimli. Epic: phaze-2al7q. Durable operator record: the
decision comment on bead phaze-zimli (2026-11-11), quoted below.

## Context

`docs/design/tracklist-provider-implementation.md` made legacy tracklist timing "qualified only by
validated `timestamp_evidence`" on `tracklist_tracks`. The evidence's original value had to match the
stored timestamp, and its origin had to be `recording:<UUID>` for the actual target. Two readers applied
that rule: `qualified_legacy_offset` in `src/phaze/services/cue_generator.py` (CUE rendering and analysis
segments) and `qualified_legacy_timestamp_clause` in `src/phaze/services/cue_review.py` (the CUE review
"eligible" set). `docs/design/tracklist-provider-persistence.md` expected legacy strings to stay
interpretation-unknown "until revalidated".

Read from the code at `44caf4d2`, not assumed:

| Fact | Where |
|---|---|
| No code path writes `tracklist_tracks.timestamp_evidence`; it is only read. Revision 085 added the column. | `alembic/versions/085_provider_source_observations.py`; the two readers above |
| No revalidation job exists. Nothing could produce the evidence the readers wait for. | the whole tree |
| Selecting a CUE never creates a tracklist. `decide_local_source` stores a `ProviderRecordingSelection` with its reviewed FILE mapping and clears `tracklist_id` / `version_id`. | `src/phaze/services/local_source_import.py` |
| A selected source's target-bound qualified offsets are projected at read time, only while the selection is `current`. CUE generation and analysis segments already read that projection. | `_project_tracks`, `get_selected_recording_source`, `cue_tracks_from_selected`, `track_segments._timestamped` |
| External acquisition, the only source of scraped legacy timestamps, is retired. | `docs/design/0024-tracklist-source-retirement.md`, revision 082 |
| Syntax is never proof of a recording offset. | `docs/design/tracklist-provider-contract.md`, "Timestamp meaning and precision" |

A revalidation job would therefore have nothing to check against. The retired source cannot be asked
again, and the stored text alone cannot qualify itself.

Production, read-only aggregates measured by the operator on 2026-11-11: 0 `tracklist_tracks` rows carry
`timestamp_evidence`; 34 rows carry a raw `timestamp`, across 2 tracklist versions; 15 approved tracklists
have a file; 1 tracklist selection exists.

## Decision

Operator decision 2026-11-11 (AskUserQuestion via the dispatcher), durable record: comment on bead
phaze-zimli.

- **Question as put:** "The old 'qualified legacy offset' code waits for a revalidation job that can never
  have anything to check: scraped-tracklist acquisition was retired, and syntax is never proof of an
  offset. Production: 0 rows have timestamp evidence, 34 raw timestamps sit in 2 tracklist versions, and
  there are 15 approved tracklists with a file. Both options get an ADR in docs/design/. Remove the code,
  or keep it dormant?"
- **Answer as given:** "R1: remove it (Recommended)".

The same dispatch narrowed the bead's first criterion. Removal then left the legacy per-tracklist CUE
generation path unreachable, which was put to the operator as a second question:

- **Question as put:** "phaze-zimli is blocked on this. Removing the legacy offset code (R1, which you
  approved) means phaze's old scraped-tracklist CUE generation path can never produce anything. POST
  /cue/{tracklist_id}/generate would always refuse, and its success path, the 'eligible' half of the CUE
  review cards, the old APPROVE button and cue_row.html would be unreachable. It has produced nothing
  since 2026-10-10. Only source code is removed: no CUE sheets on disk and no database rows. The
  selected-CUE path keeps working. How far should it go?"
- **Answer as given:** "X: remove dead path (Recommended)".

The conditions attached to that answer, from the dispatcher's relay: keep `WriteCueSheetPayload`
unchanged, keep legacy tracklists readable as gated cards, and move tests that exercised generic
behaviour onto selected-source or `ProviderTrack` fixtures so coverage is kept. Everything below that is
not one of those conditions is the implementer's reading.

### What is removed

- `qualified_legacy_offset` (`src/phaze/services/cue_generator.py`).
- `qualified_legacy_timestamp_clause` and the CUE review "eligible" set it fed (`eligible_tracklist_stmt`,
  `get_eligible_tracklist_query` and their shared base) in `src/phaze/services/cue_review.py`. Every
  approved, applied legacy tracklist is now one of the review set's gated, display-only cards.
- The legacy branch of `track_segments._timestamped`. A `TracklistTrack` never yields a segment boundary,
  and `build_track_segments` no longer takes a `media_id`.
- Legacy CUE track assembly (`build_cue_tracks_for_versions`). `build_cue_tracks_for_file` reads only a
  recording's selected source and returns no tracks without one.
- The per-tracklist generate route `POST /cue/{tracklist_id}/generate` and every helper only it reached
  (`src/phaze/routers/cue.py`). Also the eligible half of the CUE review cards
  (`src/phaze/services/review_cue.py`), the legacy APPROVE markup and preview in
  `pipeline/partials/_cue_preview.html`, the workspace's timestamp prerequisite chips, and the templates
  `cue/partials/cue_row.html`, `cue/partials/toast.html` and `cue/partials/cue_status.html`. The last
  was included only by `cue_row.html`; `tests/shared/core/test_dead_template_guard.py` flags it as an
  orphan once that file is gone, so removing it is part of the same dead path.

### What stays

- The `tracklist_tracks.timestamp_evidence` column. Dropping it is a schema change that would touch the
  read-only analytics views in flight on phaze-p22p8. It has no reader and no writer; a later migration may
  drop it.
- The raw `tracklist_tracks.timestamp` text, readable and displayed exactly as before.
- Every selected-source path. A reviewed selection's projected offsets remain the only source of exact CUE
  boundaries and analysis segments, and `POST /cue/files/{media_id}/generate` the only CUE write. Its
  form now carries the `hx-disabled-elt` double-submit guard (phaze-efu0) that the removed surface
  had and this one lacked.
- `WriteCueSheetPayload` and its `tracklist_id` wire variant, unchanged, so a queued or replayed legacy
  payload still validates on the agent; the API no longer produces that variant.

## Blast radius (CLAUDE.md rule 4)

This changes the CUE and analysis-segment paths for every legacy tracklist. Measured on production
2026-11-11 (operator, read-only aggregates): 15 approved tracklists with a file, holding 34 raw track
timestamps across 2 versions, with 0 rows of `timestamp_evidence`. 1 recording has a selected tracklist
source. What currently works that this could break: a legacy tracklist producing a CUE preview, a CUE
write, a segment boundary or a timeline tick. The measured count of each is 0, because no evidence
row exists. The other thing it could break is the selected-source CUE path, which is untouched. The
tests that prove it:

- `tests/integration/test_selected_source_consumers.py::test_legacy_timing_is_display_only_even_with_formerly_qualifying_evidence`
  (the evidence shape the old code accepted now yields no CUE tracks and no segment).
- `tests/review/capabilities/cue/test_degrade.py::test_a_legacy_tracklist_with_formerly_qualifying_evidence_is_a_gated_card`.
- `tests/review/routers/test_cue.py::test_legacy_per_tracklist_generate_route_is_gone`.
- `tests/integration/test_companion_workflow.py::test_captured_cue_native_mapping_real_broker_authenticated_write`
  and `tests/integration/test_selected_cue_dispatch.py` (the selected path, end to end).
- `tests/review/test_cue_path_handoff.py`, now driven through the selected route and the real worker.

## Consequences

- **Behaviour in production is unchanged.** With 0 evidence rows, `qualified_legacy_offset` returned
  `None` for every one of the 34 timed legacy rows, and the eligible set was empty for all 15 approved
  tracklists with a file. After removal, the same 34 rows yield no offset and the same 15 tracklists are
  gated.
- **Tests that used legacy fixtures carrying synthetic evidence** to exercise generic segment, record-page,
  CUE `FILE`-line or NFC behaviour now use a real selected CUE (`tests/_selected_cue.py`) or `ProviderTrack`
  fixtures. `tests/shared/services/test_track_segments.py` pins that a `TracklistTrack` with a timestamp
  yields no segment.
- **Retired tests:** the CUE parity and query-count comparisons against historical `services/review.py`
  blobs, because history is no longer a valid oracle for legacy cue behaviour, and the per-tracklist route's
  own refusal and toast tests. The historical blobs still load for the remaining parity tests, through
  stand-ins injected for their exec only.
- **Recovering legacy timing** now means importing the timing as a local source (a companion or embedded
  tag) and reviewing it as a selection. That path already exists, and no legacy-specific path will be
  added.

## General form (CLAUDE.md rule 5)

A reader that waits for evidence nothing can produce is dead code that reads as a guard. It looks like
protection, but no input reaches its "yes" branch. Its tests prove only that synthetic evidence the
product never writes round-trips. The lesson is the "a record that reads as a status is not one" family
in `CLAUDE.md`: before treating a gate as live, find its writer.
