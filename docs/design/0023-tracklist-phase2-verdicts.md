# ADR-0023: Tracklist acquisition phase 2 verdicts (parser fixes, ajax JSON search, MixesDB, captcha re-queue)

Date: 2026-10-07. Bead: phaze-rs4x6 (decision bead, epic phaze-4mdi7). Status: accepted.

This follows `docs/design/0022-tracklist-acquisition-spike-verdicts.md`, which selected four paths to carry forward. The phase 2 spikes
have now run. This ADR records one verdict per path, each citing its spike doc by filename and the measured quantity that decided it.
Nothing here is product code.

## Decision

| # | Path | Verdict | Authority (phaze-rs4x6, 2026-10-07) | Spike doc |
|---|---|---|---|---|
| 1 | Parser fixes (mark unresolved `ID - ID` rows; row-count canary against `numTracks`) | **DONE** | shipped work, no decision needed | `docs/spikes/phaze-gakqn-parser-gap-audit.md` |
| 2 | ajax JSON search (`/ajax/search_tracklist.php`) | **DEFERRED**, nothing built | the operator (see "Operator decisions") | `docs/spikes/phaze-movg3-ajax-plain-httpx.md` |
| 3 | MixesDB as a second source | **BACKLOG** for a deeper investigation (bead phaze-cwyuq); neither GO nor NO-GO | the operator (see "Operator decisions") | `docs/spikes/phaze-q2v7w-mixesdb-archive-coverage.md` |
| 4 | Captcha re-queue (script merged in PR #678, bead phaze-c9go7) | **Running it is deferred** until after the next release | the operator (see "Operator decisions") | `docs/spikes/phaze-gnbct-ajax-endpoints.md` |

The operator's authority (phaze-rs4x6, 2026-10-07) is limited to the three timing and scope statements quoted below. The evidence summaries, the correction in
section 2 and the standing conditions in section 4 are the planner's and the spikes', not the operator's.

### 1. Parser fixes: DONE

Shipped in beads phaze-8yvb0 and phaze-xtwjg, merged into the phase 2 epic branch (they reach `main` in the phase 2 PR).

- **phaze-8yvb0** stores unresolved `ID - ID` rows with NULL artist and title instead of the literal `ID`. The spike
  `docs/spikes/phaze-gakqn-parser-gap-audit.md` measured 12 such rows in the two recorded captures (10 of 52 and 2 of 12), exactly the
  rows lacking `data-isided="true"`. The bead's tests pin the parser output for the resolved rows and exercise the downstream consumers
  of an unresolved row.
- **phaze-xtwjg** fails the parse through the existing `PARSE_FAILED` transient path (backoff, parked after the cap) when a page
  declares `itemprop=numTracks` and the rendered track-container count differs. A page with no `numTracks` is not checked. A false
  mismatch therefore delays a tracklist; it does not lose one and is not cached as a negative.

**What remains unmeasured:** declared-versus-rendered count agreement is proven on exactly two real captures, 52 rows and 12 rows.
Its rate over the other detail pages the drain stores is unmeasured. The unresolved-row rate over the full archive is likewise unmeasured
(12 rows in 64, from two public captures).

### 2. ajax JSON search: DEFERRED, nothing built

Evidence, from `docs/spikes/phaze-movg3-ajax-plain-httpx.md`: plain `httpx` with the honest User-Agent, no browser, no cookies and no
prior navigation was served the JSON in **2 of 2** requests (`success: true`, 20 entries, 4851 bytes each, byte-identical), 0 of 2
met a challenge. The sample is tiny (2 requests, about 10 s apart, one IP, one minute), and the HTTP status line and `content-type` value
were lost from the record by a `tail` in the wrapper. The spike's own limits say it cannot bound the rate at which a challenge would appear.

**Planner's correction (a finding, not the operator's).** The spike and the planner described the benefit as "takes the browser out of
the search step". That premise does not hold against the code. The drain's search leg is already plain `httpx`: it POSTs to
`/search/result.php` (`SEARCH_URL`) in `src/phaze/services/tracklist_scraper.py`, and only the detail page uses the browser. The JSON
search would also not reduce the number of host requests per set, which stays one search plus one detail fetch. Its only effect would be to
swap scraped HTML for a smaller structured response. With the claimed benefit gone, the case for building it is a marginal one.

What would revive it: the HTML search markup breaking (the parser no longer matching `/search/result.php`), or the HTML search starting to
be challenged while the JSON endpoint still answers plain `httpx`. Either needs a fresh rate-bounded probe first, because 2 requests cannot
say how many the endpoint tolerates. Nothing was built.

### 3. MixesDB as a second source: BACKLOG, neither GO nor NO-GO

The operator moved this to the backlog for a deeper investigation (bead phaze-cwyuq). The measured figures from
`docs/spikes/phaze-q2v7w-mixesdb-archive-coverage.md`, recorded exactly:

- Sample: **148** random unique sets from **85,969**. **25 matched = 16.9%** (95% Wilson interval 11.7% to 23.7%); **60** absent from a
  complete search listing (40.5%); **63** undetermined because the listing is capped at 100 (42.6%).
- **21 of 25** matched pages have a non-empty tracklist, **14.2%** of the sample (9.5% to 20.7%).
- No second-granular offset cue: 0 of 24 fetched pages (the two `mm:ss` pages were wall-clock times). **10 of 148 = 6.8%** carry
  minute-granular offsets (3.7% to 12.0%).
- Cost: about **1.96 requests per set**; **298 of 300** requests used.
- **One spacing breach, disclosed in the spike:** requests 176 and 177 were **0.52 s apart**, under both the 5 s term and the 4 s
  `Crawl-delay`. No throttling followed. Every other gap was at least 5.3 s.

The spike's own verdict was "GO, narrowly" for tracklist content but not cues, and it calls that the implementer's judgement. This ADR does
not adopt it. The "MixesDB source" selection in `docs/design/0022-tracklist-acquisition-spike-verdicts.md` (its section 3 and decision-table
row 3) is **superseded for now** by this deferral. The coverage figures are a floor and a sample estimate, not a decision.

### 4. Captcha re-queue: running it is deferred

The re-queue script merged in PR #678 (bead phaze-c9go7). The operator deferred running it until after the next release. Standing
conditions (the planner's, derived from the bug that motivates the script, not the operator's wording):

1. The captcha fix must be deployed to the running drain. As of 2026-10-07 the containers were at image revision 110b7f98, built
   2026-10-05, which lacks `looks_like_captcha`. Running the re-queue against that image would re-fetch pages and classify the captcha
   again as "no tracklist".
2. The operator must give an explicit go at run time. The deferral above is not a standing authorisation to run.

## Operator decisions (phaze-rs4x6, 2026-10-07, session crossed midnight UTC)

Durable record: this section, per `docs/design/0012-verification-fidelity-and-operator-attribution.md` rule 2. Only the operator's own words
are quoted as answers. The attribution extends no further than the words.

**ajax JSON search.** Question as put (the planner's words):

> The ajax decision. Record the JSON search as deferred (my recommendation), or build it for a named purpose?

Answer as given (operator, verbatim): "defer JSON search on ajax". This was part of one message that also said "yes, merge #679" and "we
can do the re-queue after the next release."

**MixesDB.** There was no question; the operator volunteered a decision. Operator statement, verbatim: "for mixesdb, let's move it to the
backlog. i want to do a deeper investigation there".

**Re-queue timing.** In answer to the planner's open question about when the captcha re-queue write should run. Operator statement,
verbatim: "we can do the re-queue after the next release."

What is **not** operator-decided (phaze-rs4x6, 2026-10-07): the planner's correction in section 2, the revival conditions in section 2, the evidence summaries, the
statement that section 3 supersedes the earlier selection (the operator moved the item; the supersession is the implementer's reading of
what that does to the earlier record), the standing conditions in section 4, and the whole of the findings below.

## Findings (not decisions)

**Archive scale** (`docs/spikes/phaze-5rhjq-collapse-ratio.md`): 85,964 unique sets, collapse ratio 1.0997, about 24.9 days of drain at
10 s per host request. These figures are carried in `docs/design/0022-tracklist-acquisition-spike-verdicts.md` and not re-derived here.
The later MixesDB sample measured 85,976 `LIVE_SET` sets and 85,969 in the artist-or-event population on a table two rows larger, which is
consistent with the earlier count and is not a disagreement.

**Drain activity** (planner's check, 2026-10-07): the planner found no writes to `tracklist_lookup_cache` for over 24 hours. The cause is
not established in this ADR; it is recorded so the next reader does not assume the drain was progressing.

## General forms of the lessons recorded

- **A benefit claimed for a change must be checked against what the code does today before the change is chosen.** The ajax premise
  ("takes the browser out of the search step") was repeated by a spike and by the planner without reading that the search leg was already
  plain `httpx`. It was caught while recording the verdict. General form: state the benefit as a delta against the current code path,
  with the file that shows the current path.
- **A limit stated to a developer must be enforced inside the request loop, not trusted to the caller.** The MixesDB 0.52 s breach came from
  restarting a process whose last-request time was lost; a 5 s spacing held only in the runner's memory does not survive a restart.
  General form: a persisted per-host throttle that a restart must wait out, which the spike already recommends for the build.
- **A deferral records its revival condition.** Section 2 names what would revive the ajax search so that "deferred" is not read as
  "rejected" or as "forgotten". General form: a deferred path states the evidence that reopens it, or it is an unrecorded NO-GO.
- **A small clean sample is a claim about one session.** 2 of 2 plain `httpx` requests served says nothing about a rate. See
  `docs/design/0016-transferred-model-verification.md` and the matching lesson in `docs/design/0022-tracklist-acquisition-spike-verdicts.md`.
