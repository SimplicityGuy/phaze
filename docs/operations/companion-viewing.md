# Viewing companion sources

Date: 2026-10-10. Beads: phaze-517k9, phaze-yjuzv. Epic: phaze-kf7jr.

Open a music or video file's detail page, or its Details drawer. Both presentations show local
tracklist previews and attributed release fields beside extracted tags. Track parsing and release
parsing are independent: the same NFO can contribute both, even when its latest observation is a
raw read or only one interpretation. Small previews expose three tracks and six release fields per
source kind; the full stored observation, history and original text are loaded on demand.

Companion files and local sources shows actual linked filenames, locations, file types and link
derivation evidence. Historical links with no retained derivation evidence are labeled
`historical_unknown`. Source-file links open ordinary readable detail pages, with a reverse list
of linked recordings. Companion and source pages have independent cursors. Missing, stale,
unlinked and ambiguous inventory states do not erase retained text or reviewed choices.

Browse a source's history to inspect raw reads, tracklists and release metadata, including pending,
rejected, conflicting and selected observations. Original companion text comes from its authorized
raw parent when available, preserving whitespace, encoding and truncation evidence. Text is escaped
and paged in 8192-character chunks. Intrinsic CUE FILE evidence and timestamp precision/usability
remain visible; they are not guessed into a recording timeline.

Refresh companion text explicitly requests a new bounded read on the owning agent. Rendering never
performs acquisition. The status panel uses the database clock and durable received-attempt ordinal,
so agent clock skew, reused semantic observations and HTTP retries do not fabricate a new receipt.
An authorized capture received after the request is labeled as an observed receipt; without a job
correlation handle it does not assert that this exact request completed. Queue failure, changed
ownership/link/revision, and a two-minute period without a new report are visible. Retained text
remains readable offline. Status polling updates only that panel, not Changes Review controls.

Reimport stored text uses the retained database read and creates parsed candidates. Import a
descriptive embedded tag by its exact stored channel name. Selecting a complete candidate is a
separate decision with source/media hashes, revision and current selection token. Conflicts need
explicit acknowledgement. CUE selection needs the FILE ordinal explicitly belonging to the target
recording; multiple FILE parts are not concatenated. Incomplete, unavailable, empty or unrecognized
observations remain inspectable and cannot replace reviewed authority. Media, tag and path changes
continue through Changes Review.

The existing local-source JSON APIs retain their request/response schemas and JSON validation
errors. Native HTMX forms negotiate HTML status/error fragments at those same routes. Shared read
routes likewise return typed JSON to API clients and escaped HTML fragments to the served controls.
Synthetic PostgreSQL and compiled-CSS browser fixtures verify music/video parity, keyboard use,
narrow layouts, source paging, raw-parent text, explicit CUE mapping and stale review errors.

## From local inputs to reviewed changes

Open the Tracklists workspace to browse local tracklist candidates and their source evidence.
The Metadata workspace shows companion release facts separately from embedded-tag extraction;
the Cue workspace distinguishes existing companion sheets from generated artifacts. Each local
source links to the recording detail, where you can read complete rows, attributed fields and
original text before selecting anything. Refresh reads the owning agent; reimport uses stored text.
See [local companion import and backfill](../design/companion-import-backfill-implementation.md) for deployment prerequisites and
the supported explicit import workflow.

For example, `notes-a.nfo` can supply the reviewed tracklist while `notes-b.nfo` supplies reviewed
release metadata for `recording.mp3`. Select the two kinds independently and acknowledge any
conflict explicitly. A newer candidate, reimport or rejected alternative does not replace either
choice. Existing manual versions, accepted Discogs history and embedded tags remain stored.
Pending content is visible for inspection but does not grant reviewed metadata/search authority or
tracklist stage completion.

Use the selected recording's Discogs controls to match reviewed tracks, inspect the proposed
release identity and accept or dismiss each link. Accepted identity is bound to that source and
track position. Tag comparisons and filename proposals consume the reviewed choices; approve the
proposed changes in Changes Review before writing tags or moving files. Selecting a source alone
does not edit embedded tags. If inventory, selected version or accepted identity changes while a
review is open, refresh the comparison and review again.

To generate a CUE artifact, first apply the recording's proposed path, select the correct source
and explicitly map its FILE part to this recording. Preview the reviewed CUE, then generate it.
The owning meta worker must advertise selected-source CUE support; it rechecks the selected
source, inventory and content before writing a versioned sibling file. Qualified 75fps offsets
retain frame precision, including `00:01:01`. Different FILE parts remain independent recordings.
An unknown timestamp, clock time, coarse minute annotation or unmapped part remains readable but
cannot create an exact timeline segment or generated CUE boundary. Historical raw timestamp text
also remains readable; syntax alone does not qualify it as a recording-relative offset.

The synthetic integration suite joins physical companion reads through the real broker and
authenticated storage to native import, full page/drawer viewing and selected-source CUE writes.
Browser checks separately exercise served keyboard controls and narrow layouts; they do not
represent access to a production archive. No production backfill is part of validation.
