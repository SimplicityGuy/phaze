# Viewing companion sources

Date: 2026-10-10. Bead: phaze-517k9. Epic: phaze-kf7jr.

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
