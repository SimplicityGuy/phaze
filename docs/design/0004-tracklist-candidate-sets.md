# 0004 — Candidate sets for the 1001Tracklists drain: classify, dedup, cache

**Bead:** phaze-fq9h.3 (epic phaze-fq9h) · **Status:** implemented · **Date:** 2026-08-02

## The constraint, restated because it drives every decision below

The 1001Tracklists scrape is not CPU-bound and cannot be parallelized out of existence. Its
robots.txt asks for an **8-second crawl-delay applied to the whole host**, so the entire system —
every worker, every process, every restart — shares one budget:

| Quantity | Value | Source |
|---|---|---|
| Crawl-delay | 8 s / request / host | 1001TL robots.txt (phaze-hu8v) |
| Host requests per day | **10,800** | 86,400 / 8 |
| Host requests per lookup | ~2.5 | search + detail render + fractional Turnstile reload (spike phaze-dmvs) |
| **Lookups per day** | **~4,320** | 10,800 / 2.5 |

Adding workers raises nothing but rudeness. The only lever is to **look up fewer things**, which is
what this bead builds. `DAILY_LOOKUP_CEILING` in `services/tracklist_candidate_queue.py` is derived
from the crawl-delay in code, not hardcoded, and pinned by a test — so the arithmetic in this table
cannot drift away from the arithmetic the drain actually uses.

## Corpus ratios, and which are measured

Measured on the scanned corpus (the numbers carried in the bead, from the real archive):

| Ratio | Measured value |
|---|---|
| Files scanned | 11,428 |
| Set-like files | 6,911 (**60.5%**) |
| Files with a populated `metadata.duration` | 11,412 (99.9%) |
| Median duration of set-like files | 58.9 min |

Projected onto the ~250,000-file target: **~151,000 set-like files**. What that costs depends
entirely on the collapse ratio (candidate files per unique set):

| Collapse ratio | Unique sets | Days at 4,320/day |
|---|---|---|
| 1.0 (no dedup — the naive pass) | 151,186 | **35.0** |
| 1.5 | 100,791 | 23.3 |
| 2.0 | 75,593 | 17.5 |
| 2.5 | 60,474 | 14.0 |
| 3.0 | 50,395 | 11.7 |

The 35.0-day figure reproduces the epic's "35+ days" estimate from independent arithmetic, which is
the check that these numbers are the same numbers.

**The collapse ratio on the real archive is NOT yet measured, and this document does not pretend
otherwise.** It cannot be: the dedup needs a scanned corpus with populated durations, and the
measurement is one function call away from the operator but not from CI. On the synthetic corpus in
`tests/identify/services/test_tracklist_candidates.py` — built to the shape the bead describes
(scene re-encodes plus byte-identical copies plus singletons) — the measured collapse is **80 files
→ 40 unique sets, ratio 2.00, with zero false merges across 40 distinct sets**. Treat 2.0 as the
design assumption, not as a measurement of the archive.

To get the real number:

```python
from phaze.services.tracklist_candidate_queue import build_candidate_queue, format_corpus_report

queue = await build_candidate_queue(session)
print(format_corpus_report(queue.stats))
```

`format_corpus_report` is the single renderer the admin UI (phaze-fq9h.8) also uses, so the
operator's live numbers and this document can never disagree about what "unique sets" means.

## The funnel

```
every file
  -> media only                 (a .nfo or .cue is not a set)
  -> not already tracklisted    (embedded tags / .cue companion / prior scrape)
  -> classified LIVE_SET        (never spend a lookup on an individual track)
  -> collapsed to unique sets   (one lookup, propagated to every duplicate)
  -> not answered by the cache  (positives forever, negatives for the TTL)
  = the queue
```

Each stage's width is reported in `CandidateQueueStats`. A reduction the operator cannot see is a
reduction nobody notices going wrong — and "we skipped 25,000 files as already tracklisted" is the
single most load-bearing claim the module makes.

### Classification

Duration decides where it is decisive, because it is the one signal a scene release cannot corrupt:
≥20 min is a set whatever the filename says, ≤15 min is a track *even if the filename says
"live @"* (that file is one track lifted out of a set, and looking it up spends a request to attach
a two-hour tracklist to six minutes of audio). Between the two, and when duration is missing, generic
filename markers vote. `UNKNOWN` is a real third answer, excluded from the queue by default and
counted separately, so the undecided tail is visible rather than silently folded into either side.

### Dedup without fingerprinting — what was lost

This bead originally deduped through phaze-vprd's cross-set **audio** similarity graph. Audio
fingerprinting was removed from the product entirely (epic phaze-0jpe, `0002-fingerprint-removal.md`)
and phaze-vprd is closed. Fingerprinting would have recognized two encodings of one set from the
audio, immune to anything a scene release did to the filename. The replacement is text and duration
heuristics, and it is worse in **both** directions:

* it **misses** duplicates whose filenames disagree — cost: a wasted lookup, a slower drain;
* it can **merge** two genuinely different sets sharing artist, event and length — two nights of one
  residency being the obvious case — cost: the wrong tracklist propagated.

Those are traded against each other explicitly. Grouping is tuned for **recall**, because a missed
duplicate costs the scarce resource; every link then carries a `DuplicateConfidence` so the drain
gates **propagation** — the operation a false merge actually corrupts — at whatever tier it chooses.
Collapsing is cheap to be wrong about; propagating is not, so only propagation is gated.

Three linking passes feed one union-find (so transitivity is the data structure's problem):

| Pass | Signal | Confidence earned |
|---|---|---|
| 1 | Equal sha256, across query buckets | `EXACT` — not a heuristic; same bytes |
| 2 | Same normalized query + duration within tolerance | `HIGH` if the query carries a date, else `MEDIUM` |
| 3 | Multi-part markers within a query bucket, all indices distinct | `MEDIUM` |
| — | Same query, duration missing or out of band | `LOW` |

Confidence is recomputed **pairwise against the canonical file**, never inherited from whichever
pass happened to union a member — a file pulled in transitively gets the weaker, more honest grade.

Two deliberate refusals, both tested:

* **Duration-less files** join a query bucket only when it holds exactly one cluster. With two,
  there is no signal saying which, and picking one would be a coin flip on the operator's behalf.
* **Repeated part indices** in one query bucket mean two parted sets share a query. They are not
  merged.

Normalization has two failure modes that destroy dates, both found in development and both now
regression-tested, because deleting a date is the single worst thing this code can do (it is the
strongest disambiguator there is): a scene-group-suffix pattern that ate `-2024`, and an extension
pattern that ate `.12` off a trailing `2024.04.12`. Ambiguous `NN-NN-YYYY` dates are left in their
original component order rather than guessed — same-convention duplicates still merge, cross-
convention ones do not, and no date is ever invented.

### Cache: positives, negatives, and the distinction that matters

`tracklist_lookup_cache` (migration 049) holds one row per unique set. `outcome` is a **string, not
a boolean**, and that is the whole design. Four different things produce "no tracklist":

| Outcome | Meaning | Cache effect |
|---|---|---|
| `found` | Located and persisted | Never re-queried (a past event's tracklist does not change) |
| `not_found` | The search ran cleanly; 1001TL has nothing | Suppressed for the **negative TTL** (180 days), then re-checked |
| `blocked` | Turnstile interstitial survived the retry loop | Short exponential backoff, then back in the queue |
| `render_failed` / `search_failed` / `parse_failed` | Our browser or our selectors | Short exponential backoff, then back in the queue |

Only `not_found` is a statement about the world. The rest are statements about *us*. Caching a
Turnstile block as `not_found` would remove real sets from the queue for six months because of a
flaky browser — data loss with no error and no way to notice, in a system that already takes weeks
to run. Turnstile failed ~2 of 8 attempts in spike phaze-dmvs, so this is not a hypothetical.

A set that keeps failing transiently is **parked** (`TRANSIENT_EXHAUSTED`) after 5 attempts rather
than retried forever — and pointedly *not* rewritten as a negative, because we still do not know
whether it is on 1001TL. Unknown outcome strings (a row from a newer version) fall through to
"re-query": a wasted request is recoverable, a silently dropped set is not.

The negative TTL is 180 days: long enough not to turn the drain into a treadmill re-asking answered
questions while an untouched tail waits, short enough that community-added tracklists are eventually
picked up. At the projected drain duration that is roughly one re-check per corpus pass.

## Seams left open

* **Query derivation** (phaze-fq9h.2) — `CandidateSignals.derived_query` is optional and defaults to
  a filename fallback. Both sources are passed through the same `normalize_query`, so an upstream
  change cannot split a cluster on punctuation. This module never imports fq9h.2.
* **Result scoring** (phaze-fq9h.6) — stored as `result_confidence`. It means "did we pick the right
  search result", **never** that the tracklist matches the audio; with fingerprinting gone there is
  no way to check that and none is planned.
* **The drain** (phaze-fq9h.7) — consumes `CandidateQueue.entries` in `priority` order (widest
  propagation first) and writes back through `record_outcome`. Propagation should gate on
  `UniqueSet.members_at_least(...)`.
* **Admin UI** (phaze-fq9h.8) — `CandidateQueue.cached` exists so a set missing from the queue can be
  *explained* rather than merely absent, and `format_corpus_report` renders the funnel.
