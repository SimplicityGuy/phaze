# TrackId revision signal: is the latest detection process id plus endDate usable?

Date: 2026-10-12. Bead: phaze-rjsdy (epic phaze-oprdu). Status: amendments signal unproven; a list-versus-detail finding changes how any signal must be read.

## Question

Can the latest detection process id plus its `endDate` (and/or `amendments`) serve as a REVISION signal for a TrackId stream, so that discovery can tell a stream changed between two reads?

## Method

Read-only GETs against the TrackId Integration API, serial, at least 5 s between requests, honest identifying User-Agent, credentials supplied only through the 1Password CLI. No POST and no submission. No response body is stored in the repository; this document reports counts and shapes only.

Request budget, recorded on bead phaze-rjsdy on 2026-10-11 (planning session for epic phaze-oprdu). Request as put, verbatim: "request budget, let's say 250 calls over an hour at most for the investigation, ok?" Clarifying question as put: "Does the 250-call, one-hour budget cover both probes combined, or each probe separately?" Selected option label, verbatim: "250 each". Grant for this bead: at most 250 HTTP requests within any single hour, a ceiling and not a target, read-only GETs only. The durable record is the bead comment on phaze-rjsdy.

Three phases:

1. **Shape check (1 request).** One list call confirmed the response is wrapped in a `result` object holding the stream array and `rowCount`.
2. **List scan (36 requests).** Thirty-six list pages of 100 with `status=Ready`, `pageSize=100`, at `currentPage` values spread across the whole index (about 0 to 3,900 of roughly 3,921 pages). Each item was checked for non-empty `amendments`, more than one entry in `detectionProcesses`, any `isReprocessing` true, and non-empty `audioStreamReprocesses`. The scan was meant to stop early once candidates were found; none were.
3. **Detail probe (8 requests).** A small extra batch, capped at 8 and relayed by the team lead as approved, prompted by the list data: 4 detail GETs of Ready streams whose list `trackCount` was 0, 2 detail GETs of ordinary Ready streams, then one re-fetch of each of the 2 ordinary streams after a gap of at least 3 minutes, comparing a SHA-256 of the response body between fetches.

One earlier invocation of the first runner made 1 request and stopped on a parser error (the `result` wrapper); it is counted.

## Evidence

**Requests: 45 used against the approved ceiling of 250**: 1 shape-check list call (its page was fetched again in the scan and is not part of the 3,600), 36 list pages, 6 detail GETs and 2 re-fetches. Every request returned HTTP 200. No 401, 403, 429 or 5xx, and no rate-limit header, appeared. No more than 45 requests were made in any hour.

**List scan.** `rowCount` of Ready streams was 392,082 at the time of the scan. Streams inspected: 3,600 (36 pages x 100), about 0.92% of Ready. They span `addedOn` years 2023 (900), 2024 (1,000), 2025 (700) and 2026 (1,000).

| List-level property | Streams (of 3,600) |
|---|---|
| Non-empty `amendments` | 0 |
| More than one entry in `detectionProcesses` | 0 (all 3,600 carried exactly 1) |
| Any `isReprocessing` true | 0 |
| Non-empty `audioStreamReprocesses` | 0 |
| `canReprocess` true | 0 |
| `endDate` null | 0 |
| `trackCount` 0 | 52 (1.44%) |

**Detail probe, 6 streams.**

| Property | Result |
|---|---|
| Streams whose detail showed more than one detection process | 6 of 6 (counts of 2, 2, 2, 3, 2, 3) |
| Streams with `isReprocessing` true on a later process | 6 of 6 |
| Streams with a process of `detectionProcessType` 3 | 2 of 6 |
| Streams with non-empty `amendments` | 0 of 6 |
| Streams with non-empty `audioStreamReprocesses` | 6 of 6 (1, 1, 1, 2, 1, 2 entries) |
| Streams whose detail `canReprocess` was true | 5 of 6 (the list reported false for all 6) |
| Streams where the list carried only the first process of the detail's process list | 6 of 6; the list entry was always the original, non-reprocessing process |
| Streams with a process whose `endDate` was null | 1 of 6 (a reprocess still in flight; its track array was empty) |
| Detail `trackCount` | 0 for all 6, including the 2 whose list `trackCount` was 10 and 21 |
| List `trackCount` against the sum of tracks over all detail processes | equal for the 2 ordinary streams (10 = 10 + 0; 21 = 17 + 1 + 3) |

**Re-fetch stability.** Both ordinary streams were fetched twice, at least 3 minutes apart. The response body hash was identical on both pairs, and the detection process ids, `endDate` values and per-process track counts were identical, including for the stream whose latest process still had a null `endDate`. n = 2 pairs, one short interval: this shows no churn over minutes, not stability over days.

## Verdict

**Not proven.** No stream with non-empty `amendments` was found in 3,600 list entries (about 0.92% of Ready) or 6 detail fetches, so `amendments` as a revision signal remains unproven; its shape and behaviour are unobserved.

The evidence did settle one thing the epic had assumed wrongly. The list endpoint is **blind to reprocessing**: it shows only the original detection process of a stream, so "latest process id" and `endDate` read from a list item are always the first process and cannot change when a stream is reprocessed. The earlier observation that sampled Ready streams had exactly one process was a list-level artefact, not a property of the streams. At detail level all 6 streams sampled had a reprocess appended as a later process with a larger id, and a stream's list `trackCount` equalled the sum of tracks across all of its processes.

Whether "highest detection process id plus its `endDate`" taken from the DETAIL response is a usable revision signal is **plausible but not proven**: it is stable across two re-fetches minutes apart and a new reprocess appears as a new, larger process id, but no change between two fetches of the same stream was ever observed (no before/after pair across a reprocess), a process in flight has a null `endDate`, and the detail `trackCount` field is not usable (0 everywhere sampled).

### Observations beyond the question

- **Zero-track Ready streams.** 52 of 3,600 Ready list entries (1.44%) reported `trackCount` 0. Four were fetched in detail: all four had 2 or 3 detection processes, every process had an empty `detectionProcessMusicTracks` array, and every one had a completed (non-null) `endDate`. Detail `trackCount` was 0, agreeing with the list. The detail response has no field stating that the stream is deliberately empty (the stream-level keys carry no message, reason or flag beyond `status`, `trackHitRate` and `timeHitRate`, the last two null on all six), so a zero-track stream is empty by absence of rows only. All four had been reprocessed at least once with the same empty result. n = 4 of 52.
- **List and detail disagree on shape.** `canReprocess` was false in the list and true in detail for 5 of 6; `detectionProcessType` was absent in list items and present in detail; `audioStreamReprocesses` was empty in the list and populated in detail; detail `trackCount` was 0 while the list `trackCount` summed the process tracks.

## Recommendation

1. Discovery treats the revision as **unproven**. Record the highest detection process id and its `endDate`, taken from the DETAIL response, as evidence only; never derive a revision from list items.
2. A change in that evidence between discovery and load means retry or a new pending version, never a silent switch of the stored version.
3. Treat a process with a null `endDate` as in flight: the stream is not at rest, and its evidence is provisional.
4. Do not read detail `trackCount`; count rows from the processes. Decide explicitly which process or processes supply the tracks, since list `trackCount` is the sum across processes.
5. A Ready zero-track stream **may not** be treated as explicitly empty on the evidence available: the API gives no explicit signal, only absent rows. Treat it as "no tracks observed" and keep it re-readable. Whether to treat it as final is a question for the operator, not an inference from 4 detail fetches.
6. If the signal must be proven, a before/after pair is needed: find a stream with a reprocess in flight (a null `endDate` was seen once) and re-fetch it after completion. That is a new, separately budgeted probe, not part of this bead.
