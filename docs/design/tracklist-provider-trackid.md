# TrackId.net provider: measured evidence, constraints and open questions

Date: 2026-10-12. Bead: phaze-j5sv2. Epic: phaze-oprdu. Status: evidence record; the adapter itself is built by other beads of the epic.

This document keeps the TrackId.net spike verdict in a tracked file. Until now it lived only in the epic description and an untracked
scratchpad. It records what was measured, the rules that follow, the operator constraints that bind the adapter, and what is still
unproven. It adds no code and makes no request. All quantities are exact; no stream slug, title, artist, track name, client identifier or
credential appears here, and none may be added.

The contract extension that lets a provider read a remote source is `docs/design/0025-remote-provider-sources.md` (ADR-0025, remote
provider sources). The provider contract itself is `docs/design/tracklist-provider-contract.md`. The pure interpreter for the detail
document is `src/phaze/tracklist_providers/trackid_interpreter.py`. The retirement boundary this provider sits behind is in
`docs/tracklist-retirement.md` and `docs/design/0024-tracklist-source-retirement.md` (ADR-0024, tracklist source retirement).

## Question

Can the official TrackId.net Integration API supply ordered, timestamped tracklists that an operator can review against a local
long-form recording, and under what limits?

## Method

Five read-only GET requests were made on 2026-10-11 during the planning session for phaze-5lovg, covering the list endpoint, the detail
endpoint for several streams, and the `url=` filter. The verdict was recorded as a conditional GO. The credential was held outside every
tracked file and outside the session transcript.

## Measured facts

| # | Fact | Measured value |
|---|---|---|
| 1 | Requests made, and their outcome | 5 GETs, all HTTP 200 (the first spike). The later revision-signal probe, `docs/spikes/phaze-rjsdy-trackid-revision-signal.md`, made 45 more, all HTTP 200 |
| 2 | Where ordered tracks live | `GET /audiostreams/{slug}`, at `detectionProcesses[].detectionProcessMusicTracks[]`, each row carrying artist, title, label, track id, slug, `startTime`, `endTime` |
| 3 | List endpoint track arrays | Returned empty, so a detail read is needed per stream |
| 4 | Offset format | Stream-relative TimeSpan strings of **mixed precision**, for example `00:02:43` beside `00:00:00.2700000` (two formats in one API) |
| 5 | Detail `trackCount` | Read **0** on two streams that carried **11** and **18** rows; the list endpoint reported **11** and **18** for the same streams |
| 6 | `url=` filter | A known stream looked up by its exact canonical URL returned `rowCount` **1** |
| 7 | Rate-limit signalling | No rate-limit, quota or retry headers; Cloudflare headers only |
| 8 | Status codes | Status **3** means Ready |
| 9 | Revision signal | **20** Ready streams sampled at LIST level, all with empty amendments and one detection process. That was a list-level artefact: the list endpoint shows only the original detection process. At DETAIL level the probe in `docs/spikes/phaze-rjsdy-trackid-revision-signal.md` found 2 or 3 detection processes on every stream it fetched, later ones being reprocesses |
| 10 | Catalog size | **391,795** Ready streams in the list `rowCount` |

### Rules that follow

* **Count the rows read, never the reported count.** Fact 5 shows the detail `trackCount` can be 0 beside populated rows while the list
  endpoint is right. A provider that trusted the detail field would report an empty tracklist for a stream that has one, which is a
  silent wrong answer rather than an error. The interpreter counts the rows it actually parses, and a stream with rows it could not
  read is incomplete, not empty.
* **Merge the rows of every detection process.** A reprocessed stream carries several detection processes whose rows are disjoint
  parts of one tracklist, and the list `trackCount` is their sum (`docs/spikes/phaze-rjsdy-trackid-revision-signal.md`). Taking only
  the highest-id process returned 0 rows for a stream shaped 10 + 0 (the reprocess still in flight) and 3 rows for one shaped
  17 + 1 + 3. The interpreter therefore merges rows from all processes ordered by start time (ties by process id, then row order),
  drops only an exact repeat found in two processes (same track id, start and end; never by track id alone, since a track can be
  played twice), records each row's process id, and marks the snapshot incomplete while any process has a null `endDate`. It
  assumes rows within a process arrive in the API's list order. The highest process id and its `endDate` are recorded as revision
  evidence only; `Snapshot.revision` stays unset.
* **Parse both TimeSpan precisions.** Fact 4 means a fixed-width parser would reject half the rows. Whole-second text is second
  precision; text with a fractional part is recorded as unknown precision with its exact rational value preserved.
* **Offsets are approximate and never qualified.** The origin of every offset is the platform stream, not the local recording. The two
  can differ by intros, trims, re-encodes and ad breaks, so an offset is evidence for an operator to review and is never promoted to a
  qualified position in the local file. The interpreter stamps each offset `approximate` and carries the origin note.
* **Only Ready streams are read.** Status 3 is the only status measured as usable (fact 8).
* **Absence of rate-limit headers is not permission to go fast.** Fact 7 means the server gave no signal to pace against, so pacing is
  decided by the operator record below, not inferred from the wire.

## Operator records that bind this provider

Each record is restated from the epic description of phaze-oprdu (the durable record until the ADR
`docs/design/0025-remote-provider-sources.md` restated it). All were given on 2026-10-11 in the planning session for phaze-5lovg.

**Pacing.** On 2026-10-11 (bead phaze-oprdu) the operator was asked: "Terms of service and caching rights can't be determined from the
API. How should that gate the plan?" The answer, verbatim: "no rate limit currently, but please be responsible, let's not innundate
their servers". Durable record: the description of phaze-oprdu and `docs/design/0025-remote-provider-sources.md`. The answer extends
only to request pacing. It is **not** an answer on storing snapshots, and nothing in this document may be read as one. The adapter
therefore makes serial requests, with a per-run budget, a delay between requests and no retry storms.

**No upload of local content.** On 2026-10-11 (bead phaze-oprdu) the operator was asked: "What should link a local recording to a
TrackId stream: a stored source URL, artist/event/date keywords, or something else?" The answer, verbatim: "i don't know. for sure
we're not uploading the local recording up to them." Durable record: the description of phaze-oprdu. The binding constraint is that no
local audio or file content is ever sent to the provider; only recording-context facts (artist, event, date, duration) may appear in a
query. The answer did not choose between a URL and keywords, so that choice stays with the design of the matching-evidence bead.

**Measurement.** On 2026-10-11 (bead phaze-fi8rs) the operator was asked whether a read-only count query could be run against the
archive database to see how many recordings carry a platform URL, with counts reported only. The answer, verbatim: "yes". Durable
record: comments on phaze-fi8rs. The numbers below are counts only.

## How many recordings the `url=` filter can serve

Measured on 2026-10-11 (bead phaze-fi8rs) by a read-only, counts-only query over the metadata table of the archive database:

| Population | Count |
|---|---|
| Metadata rows | 104,445 (104,402 with extraction not failed) |
| Long-form (duration at least 1,200 s) | 93,744 |
| Long-form with an artist | 61,061 (65.1%) |
| Long-form with a year | 54,767 (58.4%) |
| Long-form carrying a TrackId-supported platform URL in tags | 1,305 (1.39% of 93,744) |

Across all durations the platform URLs in raw tags were 1,025 SoundCloud, 294 Mixcloud, 96 YouTube and 3 hearthis.at; 9,340 rows had
any http(s) URL in tags. The `url=` lookup (fact 6) therefore serves only about 1.4% of long-form recordings. **Keyword discovery from
artist and year is the main path** for the rest, which is why the no-upload constraint matters: keywords are the only link available for
roughly 98.6% of the long-form archive. These counts are the evidence recorded on phaze-fi8rs; re-run the query rather than trusting
them if the archive has changed materially.

## What is still open

* **Revision signal: unproven.** The list endpoint shows only the original detection process (fact 9), so it cannot reveal a
  reprocess; `docs/spikes/phaze-rjsdy-trackid-revision-signal.md` found several processes only at detail level. No stream with
  amendments has been observed, and the adapter cannot yet say how a changed upstream tracklist is detected, so any revision
  handling is a hypothesis.
* **Terms of service and snapshot-caching rights.** Neither is answerable from the API. The pacing record covers request pacing only.
  Whether fetched snapshots may be stored is a question for the operator that has not been put or answered.
* **Rights in the data itself.** The API says nothing about reuse of the returned track data.
* **Pagination and quota limits at scale.** Only 50 requests in total (5 in the first spike, 45 in the revision-signal probe) were made against a catalog of 391,795 Ready streams; behaviour under
  sustained use is unmeasured, which is another reason the budget is conservative.

## Constraints on the adapter

Lookups are operator-triggered only, through the provider framework. Nothing here authorizes scheduled, background or automatic
acquisition, scraping of any website, or submission of data to TrackId. The credential is never committed, logged, placed in a repr or
error detail, or pasted into a session. Fixtures are synthetic copies of response shapes, with no captured production data.
