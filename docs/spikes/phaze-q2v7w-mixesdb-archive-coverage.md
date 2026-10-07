# phaze-q2v7w: how much of the archive does MixesDB cover, and are its cues usable?

Spike under epic phaze-4mdi7. Measured 2026-10-07. No product code was written or committed. Counts and ratios only: no
filename, directory, digest, UUID, set name or host or account name from the archive or from MixesDB appears below.

## Question

What share of **this archive's** unique sets does MixesDB cover, and how many of the matching pages carry usable cue
timestamps? This is Condition 1 of the GO in `docs/spikes/phaze-2ov24-mixesdb-viability.md`, whose 33% to 56% came from a
hand-picked public mainstage sample of 36 sets and explicitly was not the archive's rate.

## Method

Limits stated in the assignment of bead phaze-q2v7w on 2026-10-07, recorded here as the terms the run kept to, not as a quoted exchange: read-only sampling of the archive database; at most 300
requests to `mixesdb.com` counting every request, retries and `robots.txt`; REST API only; at least 5 s between requests;
honest User-Agent; counts only; scratch data outside the repository and deleted afterwards.

### Drawing the sample (re-derivable)

1. **Rows.** One read-only `COPY ... TO STDOUT` of exactly the columns `candidate_signals_query` selects (the statement was
   compiled from that function, not re-typed): 104,249 media rows out of 145,059 `files` rows on the measurement date.
2. **Production code, not a re-implementation.** `derive_query` was applied to every row exactly as `tracklist_drain` does;
   the signals then went through `admitted_signals`, `group_unique_sets` and `build_queue_from_signals` with an empty cache
   (so every set is queued). That yields **85,976 unique sets** classified `LIVE_SET` (the earlier collapse-ratio spike,
   `docs/spikes/phaze-5rhjq-collapse-ratio.md`, measured 85,964 on a table two rows smaller).
3. **Population.** The sets whose canonical file derives an artist or an event, which is the population the drain spends host
   requests on: **85,969** sets (7 have neither). Strata: artist and event 77,833 (90.5%); artist only 8,136 (9.5%).
4. **Randomisation.** Sets sorted ascending by their set key, shuffled with `random.Random(20261007)`, and processed in that
   order until the request budget ran out. The sample is the first **148** sets of that permutation, so the same seed over the
   same rows gives the same sample.

### Looking each set up (REST only)

Request method and heuristics follow `docs/spikes/phaze-2ov24-mixesdb-viability.md`, with one forced change (below).

- `robots.txt` was fetched first (request 1). Every later URL was checked against its `*` group `Disallow` patterns before it was
  sent; none was refused. Only `/w/rest.php/v1/search/page` and `/w/rest.php/v1/page/<key>` were requested.
- User-Agent: `phaze/2026.10.2 (+https://github.com/SimplicityGuy/phaze)`, which is what `honest_user_agent_token()` returned
  when run. No rotation, no proxy, no challenge handling.
- **Step 1**: `search/page?q=<derived query>&limit=5`. The derived query is artist, event and year as `derive_query` builds it,
  the same shape as the earlier spike's "artist event year".
- **Step 2, only on a step 1 miss**: `search/page?q=<artist>&limit=100`.
- **Match rule**: a result title matches when it contains every token of the derived artist and of the derived event
  (case- and diacritic-insensitive; a short stop list of `live set full at the of in and a an hd mix sets b2b vs feat ft with`
  removed from the event tokens), and, when a year was derived, starts with that year.
- **Outcome classes.** *matched*: a title matched. *Absent by complete listing*: no title matched and step 2 returned fewer than
  100 results, so the search listing for that artist was exhausted. *Undetermined*: no title matched and step 2 returned the
  full 100, so the listing is capped and a miss proves nothing.
- **Forced change from the earlier spike.** That spike used the HTML `Category:<Artist>` page, which shows `(N out of N)`, to
  prove an absence. The approved terms here are REST only, and a category page is not a REST endpoint, so the completeness test
  is the REST result count against the 100 cap. It is a weaker proof (a page filed under another artist credit or spelling is
  missed by both) and "absent" below should be read as "absent from the search listing".
- **For each match**, one `page/<key>` fetch, parsing `source`. A track line is a `#` line, or a line starting with `[` inside a
  `<list>` block. A cue is the leading `[...]` mark. Marks are classed `mm:ss` form (`\d+:\d\d`, optionally with hours), minute
  only (`\d+`), or placeholder (`?`). A mark counts as a **usable offset** only if at least half of the tracks carry one, the
  first is at most 10 minutes, the sequence never decreases, and the last does not exceed the set's own file duration by more than
  5 minutes.

### Pacing and budget

A throwaway runner enforced a 5.3 s minimum gap and a hard cap of 300 requests, logging every attempt before it was sent.

## Evidence

### Requests spent: 298 of 300

| Purpose | Requests |
|---|---|
| `robots.txt` | 1 |
| Step 1 searches | 150 |
| Step 2 searches | 118 |
| Page fetches for matches | 26 |
| Page re-fetches (see deviations) | 3 |
| **Total** | **298** |

290 of these were spent inside the 148 completed sets (1.96 per set), 3 were re-fetches and 1 was `robots.txt`; the other 4
were attempts that were interrupted and redone. The run took 39.1 minutes. Every response was HTTP 200 except two, both mine to
explain and neither a block: one 30 s client timeout on a step 2 search (request 144; the set was redone from the start), and
one HTTP 404 on a page fetch for a matched page whose title contains a `/` (the REST page endpoint did not resolve the encoded
key; the set counts as matched, its page content is unavailable). No 403, no 429, no challenge page, no non-JSON body.

### Deviations from the approved terms

- **One spacing breach.** Requests 176 and 177 were **0.52 s apart**, under both the 5 s term and the 4 s `Crawl-delay`. I killed
  the runner to add page saving, ran a 3-page re-fetch as a separate process, and the restarted crawl began its first request
  without waiting out the gap. Every other gap was at least 5.3 s (minimum of the remaining 296 gaps: 5.3 s). No throttling
  followed.
- **Interrupted attempts redone** (two runner restarts, one timeout, one 404 handled by a code fix). All of them are counted above.
- **Pages saved late.** The first three matched pages were parsed before I began saving page bodies, so they were fetched a
  second time (the 3 re-fetches). Nothing else was skipped.

### Coverage (sample n = 148 of 85,969)

| Outcome | Sets | Share of 148 | 95% Wilson interval |
|---|---|---|---|
| Matched | 25 | **16.9%** | 11.7% to 23.7% |
| Absent from a complete search listing | 60 | 40.5% | 33.0% to 48.6% |
| Undetermined (listing capped at 100) | 63 | 42.6% | 34.9% to 50.6% |

- Matched is a **floor**. If every undetermined set existed, the ceiling would be 88 of 148 = 59.5%, but nothing here supports
  that, and 63 of 63 undetermined sets hit the cap, which a build can narrow by adding the year to the search.
- By stratum: artist and event, 21 matched of 133 (15.8%, interval 10.6% to 22.9%), 49 absent, 63 undetermined. Artist only, 4
  matched of 15, 11 absent, 0 undetermined (the small n makes this stratum anecdotal).
- **Step 2 never produced a match.** All 25 matches came from the first, narrow search. The 118 step 2 requests (40% of the
  budget) bought only the split between absent and undetermined.
- Scaling the interval to the 85,969-set population gives about **14,500 sets** (10,100 to 20,400). That is an extrapolation
  from a sample of 148, labelled as one.
- **Precision is not measured.** I read all 25 matched titles against their derived queries: 22 are consistent on artist plus
  event or year. Three rest on a bare artist string with neither event nor year, so they may be a different set by the same
  act. No ground truth exists to separate those without more requests.
- Two matched sets resolve to the same page (24 distinct pages for 25 sets).

### Cue quality on the matched pages (n = 24 pages fetched; the 25th returned 404)

| Property | Pages | Share of 24 | Share of 148 sampled sets |
|---|---|---|---|
| Non-empty tracklist | 21 | 87.5% | 14.2% (9.5% to 20.7%) |
| Any digit-bearing `[..]` mark on tracks | 14 | 58.3% | 9.5% |
| `mm:ss`-form mark | 2 | 8.3% | 1.4% |
| ...of which usable as an offset from the start | **0** | 0% | 0% (upper bound 2.5%) |
| Minute-only mark | 12 | 50.0% | 8.1% |
| ...of which usable as an offset from the start | **10** | 41.7% | 6.8% (3.7% to 12.0%) |
| Placeholder (`?`) marks | 2 | 8.3% | 1.4% |
| Non-empty tracklist, no mark at all | 7 | 29.2% | 4.7% |

- **Both `mm:ss` pages are wall-clock times, not offsets.** Their marks read like a time of day (the first track at 21:00 or
  23:00, rising over a two-hour show) and fail the first-mark-within-10-minutes test. A reader that treated `[21:00]` as 21 minutes
  would be wrong by hours. So **zero** of 24 pages carry second-granular offset cues.
- Ten pages carry minute-granular offsets. Track counts on the 21 non-empty pages ran 3 to 47.
- Two markup shapes were present: `#` numbered lines (16 pages) and a `<list>` block of `[mm] Artist - Title` lines (8 pages). The
  earlier spike parsed only the first, so its "tracklist non-empty" and cue figures undercount `<list>` pages. This is a
  correction to a parser assumption, not a contradiction of its other findings.

## Verdict

**GO, narrowly: build MixesDB as a second source of tracklist content, not as a source of cue timestamps.**

This verdict is the implementer's judgement from the figures above. The earlier spike said to drop the source if
the matched share on real archive sets was low, but set no threshold, so "low" is a judgement; the numbers that drive it are:

- **Coverage is modest but not empty.** At least 16.9% of the archive's unique sets (11.7% to 23.7%) have a MixesDB page, and
  14.2% (9.5% to 20.7%) have one with a non-empty tracklist. That is the archive's rate, about half the 33% the public
  mainstage sample suggested, which is consistent with that sample not being representative.
- **Timestamp quality is poor.** No sampled set had a second-granular offset cue. 6.8% of sampled sets (10 of 148) had
  minute-granular offsets. MixesDB cannot be the cue source; anything it supplies is track order and track text.
- **Cost is bounded and the access question is settled.** About 1.96 requests per set measured; sweeping all 85,969 sets at
  5.3 s spacing is about 10.3 days continuous, and dropping the step 2 search (which found nothing) would cut that to roughly
  6 days. No request was blocked or throttled.
- **What would flip this to NO-GO:** a requirement that the source supply cues, or that precision on the three weak
  artist-only matches matter more than yield. Neither is a premise of this bead.

**Premise corrections.** (1) The earlier spike's "absent by complete listing" used HTML category pages; under REST only, the
completeness proof is weaker (above). (2) That spike's "mm:ss cues" figure of 27% may include wall-clock marks; here none of the
`mm:ss` marks were usable offsets. (3) Its parser missed the `<list>` markup, 8 of 24 pages here. (4) Spacing was breached once
by this run (above).

## Recommendation

1. **File the build bead**, scoped to tracklist content. Expect about 14,500 sets (10,100 to 20,400) to gain a page and about 14%
   a non-empty tracklist; size the work against that, not against the 33% to 56% public sample.
2. **Do not promise cues.** Model the cue as `(value, granularity)` with `second | minute | clock | unknown` and treat a first
   mark above 10 minutes, or a sequence that wraps past midnight, as a wall-clock time. Expect no second-granular offsets, and
   minute offsets on roughly 7% of sets.
3. **Parse both markups**: `#` lines and `<list>` blocks. Tolerate a page with no tracklist (3 of 24) and `?` placeholders.
4. **Matching**: require the year and every event token, as here. Do not accept an artist-only match with no event and no year
   without a second signal (3 of 25 matches were of that shape). Do not store a bare-artist hit as FOUND.
5. **Drop the capped `limit=100` artist search as the default second step.** It produced no match. If a miss needs resolving,
   search artist plus year, which narrows the listing under the 100 cap. Do not read a miss as an absence unless the listing
   was exhausted.
6. **Handle the `/` key.** One matched page whose title contains `/` returned 404 from `page/<key>`. A build needs to find the
   encoding the endpoint accepts, or fall back to the `key` the search returned verbatim, before relying on page fetches.
7. **Pace in the runner, not in the caller.** The one spacing breach came from restarting a process whose last-request time was
   lost. Keep the last-request time in a persisted per-host throttle, at least 5 s, and make a restart wait it out.
8. **Carry forward the attribution and licence obligations from `docs/spikes/phaze-2ov24-mixesdb-viability.md`**, unchanged:
   - Store with every imported tracklist the page URL, revision id, retrieval date and the licence string "CC BY-SA 3.0 US, as
     stated on MixesDB:Legal_stuff".
   - Show the source link in the admin UI wherever imported data appears.
   - Do not republish or export imported tracklists unless the recipient gets them under the same or a similar licence
     (ShareAlike).
   - Do not copy images, comments or the Help, MixesDB and User namespaces; they carry other terms.
   - Do not use the site to fetch audio or links to audio; it says it is "not for downloading sets".
   - Read the CC legal code for 3.0 US before any public redistribution (not read in either spike).
   - Stay on the REST API; never use `api.php`, `?action=` or `?title=`; never touch a `Disallow`ed path; treat a `robots.txt`
     change as a stop; stop on any 403, 429 or challenge page and report it; never evade.
9. **Edit `docs/design/0022-tracklist-acquisition-spike-verdicts.md` section 3** to record Condition 1 as measured. That is prose
   and goes through its own docs PR.

## Scrub

Counts and ratios only. A scrub check was run over this file before submit for path separators outside repository paths, filename
extensions, hex digests, UUIDs and host or account names, and the real-archive scratch data (rows, derived queries, responses,
logs) lived outside the repository and was deleted afterwards.
