# phaze-5rhjq — unique-set collapse ratio, zero-signal share, and what looser merging would buy

- **Bead:** `phaze-5rhjq` (spike, epic `phaze-5d0wg`)
- **Date:** 2026-10-07
- **Status:** measurement only. **No product code changed.** Counts and ratios only; no filename, directory, digest, UUID or host name from the archive appears below.
- **Data:** the real archive database, read-only session, one pass. The 145,057-row `files` table is the archive as scanned on the measurement date.

## Question

What is the real number of unique-set lookups the archive needs, and how many set-like files does the drain skip today for having no query signal? Specifically: the collapse ratio (`docs/design/0014-tracklist-candidate-sets.md` assumes 2.0), the zero-signal population under the current `derive_query`, how many of those a tag or folder fallback would rescue, and whether looser event+date merging of duration-split sets is worth building given its false-merge exposure.

## Method

1. **Rows.** One `COPY ... TO STDOUT` of exactly the columns `candidate_signals_query` selects (same media extension set, same `COALESCE(original_filename_repaired, original_filename)`, same `has_cue` and `has_tracklist` EXISTS predicates), plus `raw_tags` so embedded-tracklist detection could run in Python as production does. Data was staged outside the repository and deleted afterwards.
2. **Production code, not a re-implementation.** `derive_query` was applied to every filename exactly as `tracklist_drain` does; the signals were then handed to `admitted_signals`, `group_unique_sets` and `build_queue_from_signals` (an all-MISS cache, so the stats are the full funnel width). `classify`, `detect_embedded_tracklist`, `normalize_query` and `query_has_date` are the production functions. Only the aggregation (counters, the folder/tag fallback probes, the looser-merge grouping) is spike code.
3. **Cache.** The 1,872 existing `tracklist_lookup_cache` rows were read separately and matched against the freshly computed set keys, to say how much of the queue is already answered.
4. **Zero signal** is defined the way the drain defines it: `derive_query` returns neither an artist nor an event. That is the exact guard in `tracklist_drain` (phaze-97uw8) that spends zero host requests on such a file. Weaker tiers are reported separately.
5. **Tag yield** is `derive_query` over `"<artist tag> - <album or title tag>"`; counted when it returns an artist or an event. **Folder yield** is `derive_query` over the parent directory name, and separately over parent or grandparent. Neither is checked for precision (see Verdict).
6. **Looser merging** is measured on the production unique sets (canonical file's derived fields), three ways: same normalized query across duration splits; same normalized event and same full date, ignoring artist and duration; same normalized event only.

Assumptions are labelled `A1..A5` where they enter the arithmetic.

## Evidence

### Funnel (production grouping, all-MISS cache)

| Stage | Count |
|---|---|
| Files scanned (`files` rows) | **145,057** |
| Media files (audio and video, the funnel's entry) | **104,247** (71.9% of scanned) |
| Set-like files, `classify` = `LIVE_SET`, before the tracklisted filter | **94,553** (65.2% of scanned, 90.7% of media) |
| ...of which already tracklisted | 53 (cue 31, scraped 15, embedded 7) |
| Already tracklisted, all media (funnel step) | **103** = cue 71, embedded 17, scraped 15 |
| Classified track / unknown (not queued) | 6,867 / 2,744 |
| Candidate files (media, untracklisted, `LIVE_SET`) | **94,533** |
| **Unique sets after `group_unique_sets`** | **85,964** |
| **Collapse ratio** | 94,533 / 85,964 = **1.0997** |
| Sets already answered by the cache (found 9 + not_found 1,103) | 1,112 |
| Queue still to look up | 84,852 (also in the cache but retryable: search_failed 178 and low_confidence 569, with 13 of the 1,872 rows no longer matching any current key) |

Set-like duration, over the 94,553 set-like files: 93,515 are at or above 20 minutes (the duration-decisive rule) and 1,038 are admitted on other evidence (706 of the 94,553 have no duration at all). Median 60.2 min, 120,429.1 hours of audio in total (files with a duration).

Shape of the collapse: 80,067 of 85,964 sets (93.1%) are singletons; 5,897 sets hold 2 or more files and together 14,466 files. Set-size histogram tail: 4,853 pairs, 574 triples, 230 quads, then a thin tail topping out at 41 files (2 sets).

Composition of the 8,569 collapsed links (94,533 − 85,964), by `DuplicateConfidence`:

| Tier | Links | Reason |
|---|---|---|
| EXACT | 1,867 | identical sha256 |
| MEDIUM | 6,516 | same query without a full date and duration in tolerance: 5,604; multi-part markers: 912 |
| LOW | 177 | same query with duration missing or out of band: 144; transitive: 33 |
| HIGH | 9 | same dated query, duration in tolerance |

Only 9 links ever reach HIGH: the query text `derive_query` produces carries a year, never a full date, so `query_has_date` is almost never true although 25,313 of the 85,964 sets (29.4%) have a full date in their filename (a further 34,949 have a year only, 25,702 neither).

`DEFAULT_PROPAGATION_MIN_CONFIDENCE` in `tracklist_drain` is `EXACT`. Only **1,867 of the 8,569 links (21.8%)** therefore actually propagate a result today; the other 6,702 members are grouped (one lookup) but not given the answer.

### Zero query signal

| Measure | Files | Unique sets |
|---|---|---|
| Neither artist nor event (the drain's zero-request guard) | **70** | **8** |
| Same, and also no date and no year | 0 | 0 |
| Share of the 94,533 candidate files | 0.074% | |

All 70 are media files that reach the candidate stage (69 mp3, 1 wav), and none has a digits-only filename stem. The fallback inside `derive_query` (cleaned raw filename when parsing yields nothing) means the date and year tier is empty by construction.

Of those 70 files:

| Fallback | Files that would gain an artist or event |
|---|---|
| Tags (artist, title or album tag present) | 18 present, **18** gain a derived artist or event (25.7%) |
| Parent folder name alone | **1** |
| Parent or grandparent folder name | **70** (see caveat: this is an upper bound, not a yield) |
| Tags or folder (parent or grandparent) | 70 |
| Neither | 0 |
| Tags and folder both | 18 |

Weaker signal, reported because it is where query quality actually sits: 8,136 sets (9,914 files) derive an artist but no event; 4,816 of those also have no year; 570 sets have a single-token query; 20,886 sets have an event but no year; 12,836 sets carry an ambiguous day/month date.

### Duration-bucket splits and looser merging

Sets sharing one normalized query but kept apart by the duration band (45 s absolute, 2% relative):

| Measure | Value |
|---|---|
| Distinct normalized queries among the 85,964 sets | 82,570 |
| Queries split across 2 or more sets | **1,936**, holding 5,330 sets, **3,394 extra sets** (3.95% of all sets) |
| Of those, queries carrying a full date | 0 (all 1,936 are undated, so a merge here is bounded by the year and nothing else) |
| Split-size histogram | 2: 1,354; 3: 289; 4: 118; 5: 66; 6: 30; 7: 27; 8: 13; 9: 6; 10: 10; 11: 7; 12: 4; 13: 2; 14: 2; 15: 3; 17, 18, 21: 1 each; 23: 2 |
| Longest-to-shortest duration ratio inside a split, quartiles | 1.058 / 1.190 / 1.532 |
| Ratio below 1.1 / below 1.5 / 1.5 or more / 2 or more | 716 / 1,401 / 535 / 253 |

Merging on **event + full date**, ignoring artist and duration (sets with a derived event and a full date: 23,955):

| Measure | Value |
|---|---|
| Groups holding 2 or more sets | **1,718**, holding 5,982 sets, **4,264 extra sets** |
| Groups with differing artist text | **1,571** (91.4%), **4,103 extra sets** (96.2%) |
| Groups with a single artist text | 147, 161 extra sets |
| ...of those already one query (i.e. plain duration splits) / different query text | 144 / 3 |

Merging on **event only** (the loosest form):

| Measure | Value |
|---|---|
| Groups holding 2 or more sets | 6,868, holding 34,486 sets, 27,618 extra sets |
| **Groups spanning more than one date** (same event name, different dates: the false-merge shape) | **1,342 groups, 11,629 sets** (13.5% of all sets) |

Reading the false-merge counts: the event+date merge removes the "same event, different dates" shape by construction, but 1,571 of its 1,718 groups join sets whose artist text differs, which is the festival-day shape (several artists, one event, one day). Differing artist text can be a spelling variant of one artist, so 4,103 is an **upper bound** on true false merges, not a count of them; no ground truth exists in the database to separate the two without spending lookups. What the data does bound from below is the safe yield: the single-artist groups save 161 sets.

### Drain-time arithmetic

Assumptions: **A1** 8 to 12 s paced per host request, mean 10 s, applied to the whole host (so adding workers changes nothing). **A2** about 2.5 host requests per lookup (search, detail render, fractional challenge reload, per `docs/design/0014-tracklist-candidate-sets.md`). **A3** continuous crawling (no downtime, no retries beyond the 2.5 average). **A4** every queued set needs exactly one lookup and a set with no match costs the same as a hit. **A5** non-propagated members need no lookup of their own (generous; the last row relaxes it).

Per lookup: 2.5 × 10 s = 25 s; at 8 s: 20 s; at 12 s: 30 s. Lookups per day: 86,400 / 25 = **3,456** (mean), 4,320 (8 s), 2,880 (12 s).

| Scenario | Sets to look up | Collapse ratio | Days at 8 s | Days at 10 s | Days at 12 s |
|---|---|---|---|---|---|
| No dedup | 94,533 | 1.0000 | 21.88 | 27.35 | 32.82 |
| **Measured grouping** | **85,964** | **1.0997** | **19.90** | **24.87** | **29.85** |
| Measured, minus 1,112 cache-answered | 84,852 | 1.1141 | 19.64 | 24.55 | 29.46 |
| Design assumption 2.0 applied to 94,533 | 47,266 | 2.0000 | 10.94 | 13.68 | 16.41 |
| Fold every same-query duration split | 82,570 | 1.1449 | 19.11 | 23.89 | 28.67 |
| Fold every event+date group (upper bound) | 81,700 | 1.1571 | 18.91 | 23.64 | 28.37 |
| Propagate only EXACT links (today's drain default, A5 relaxed) | 92,666 | 1.0201 | 21.45 | 26.81 | 32.18 |

Worked row: 85,964 sets × 2.5 = 214,910 requests × 10 s = 2,149,100 s ÷ 86,400 = 24.87 days.

## Verdict

1. **The 2.0 assumption does not hold. The measured ratio is 1.0997 on 94,533 candidate files.** The design's own table priced 2.0 at 17.5 days at 4,320 lookups a day for an assumed 151,000 set-like files; the real archive is smaller (145,057 files scanned, 94,553 set-like, not 250,000 and 151,000) but collapses far less, so the real queue of 85,964 unique sets is larger than the 75,593 the 2.0 row implied. At the mean 10 s pacing the full queue is about **24.9 days** continuous, not the roughly 13.7 days that 2.0 would give for this file count.
2. **The premise "no one has measured it" is partly out of date.** `docs/design/0014-tracklist-candidate-sets.md` already records a drain-time measurement (phaze-fq9h.11) of 1.0307 on 9,708 lookupable sets. This spike's 1.0997 is the first figure over the whole 94,533-file candidate population and is consistent in direction: the archive is overwhelmingly singletons (93.1% of sets), and 5,604 of the 8,569 collapsed links are duration-tolerance matches on undated queries rather than byte-identical copies.
3. **The collapse is weaker than the headline suggests because propagation is gated at EXACT.** Only 1,867 byte-identical links (21.8% of the collapsed links) propagate today; at that gate the effective ratio is 1.0201 and the drain is about 26.8 days, not 24.9. That choice is a separate decision and is not changed here.
4. **The zero-signal premise is contradicted.** Under the current `derive_query` only **70 candidate files (0.074%), forming 8 unique sets**, have neither artist nor event, and the drain already skips those at zero host cost. The value of a query-derivation bead resting on "many set-like files are skipped for no signal" is therefore **negligible**: even a perfect rescue of all 70 saves or adds at most 8 lookups. Tag fallback would rescue 18 of the 70 (25.7%). The folder fallback figure of 70 of 70 is an upper bound only: `derive_query` accepts any residual text as an artist, so it will "find" a signal in nearly any folder name; the parent folder alone yields 1. Whether either rescued query would match anything is unmeasured and cannot be measured without spending host requests. The real query-quality question is the weak tier (8,136 sets artist-only, 25,702 with neither date nor year), which this spike measures but does not size.
5. **Looser event+date merging: NO-GO.** The most it can save is 4,264 sets (4.96% of the queue, about 1.2 days of 24.9) and 4,103 of those (96.2%) are groups of differing artist text, a festival-day shape that would propagate one set's tracklist onto another's files. The safe part, groups with one artist text, is 161 sets (0.19% of the queue, about 0.05 days), and 144 of its 147 groups are already plain same-query duration splits. Event-only merging is worse: 1,342 groups (11,629 sets) are the same event on different dates. The risk is not paid for by the saving.
6. **What the duration splits suggest instead (not a recommendation to ship).** The 1,936 same-query splits would save 3,394 sets (about 0.98 days at 10 s) if folded. 716 of them are within a 1.1 duration ratio, 253 differ by 2 times or more (almost certainly distinct recordings or parts). A tolerance change is a different, narrower lever than event+date merging, but the evidence here only counts the splits; it does not show the folded pairs are the same recording.

## Recommendation

- **Do not build event+date (or event-only) merging.** NO-GO, per verdict item 5.
- **Re-plan the drain schedule on 85,964 unique sets, about 24.9 days at 10 s pacing (19.9 to 29.9 days across 8 to 12 s), and plan on 26.8 days if EXACT-only propagation stays.** The 2.0 row in `docs/design/0014-tracklist-candidate-sets.md` should be replaced by the measured 1.0997; that edit is prose and goes through its own docs PR.
- **Deprioritize any bead whose value is rescuing zero-signal files.** The population is 70 files and 8 sets. If a query-derivation bead is kept, justify it on the weak tier (artist-only and undated queries), and size that first with a held-out sample of real lookup outcomes rather than from filenames.
- **Consider one narrow follow-up** if the 3,394 splits matter: a bead to evaluate a wider duration tolerance for same-query sets, with a measured false-merge check against sets that already have a confirmed tracklist. Not filed here.
- **Observation for the owner of `query_has_date`:** the HIGH confidence tier is reachable for only 9 links because the derived query carries a year, never a full date. That is a latent mismatch between the design's confidence tiers and the query text, not a change this spike proposes.

## Scrub

This document contains counts and ratios only. A scrub check was run over the file for path separators outside repository paths, filename extensions, hex digests, UUIDs and host or account names before submit.
