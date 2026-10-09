# ADR-0023: Tracklist acquisition phase 2 verdicts (parser fixes, ajax JSON search, MixesDB, captcha re-queue)

Date: 2026-10-07. Bead: phaze-rs4x6 (decision bead, epic phaze-4mdi7). Status: superseded for external acquisition by `docs/design/0024-tracklist-source-retirement.md`.

This follows `docs/design/0022-tracklist-acquisition-spike-verdicts.md`, which selected four paths to carry forward. The phase 2 spikes
have now run. This ADR records one verdict per path, each citing its spike doc by filename and the measured quantity that decided it.
Nothing here is product code. All acquisition decisions below are historical; the 2026-10-08 cancellation takes precedence.

## Decision

| # | Path | Verdict | Authority (phaze-rs4x6, 2026-10-07) | Spike doc |
|---|---|---|---|---|
| 1 | Parser fixes (mark unresolved `ID - ID` rows; row-count canary against `numTracks`) | **DONE** | shipped work, no decision needed | `docs/spikes/phaze-gakqn-parser-gap-audit.md` |
| 2 | ajax JSON search (`/ajax/search_tracklist.php`) | **CANCELED**; historically deferred, nothing built | the operator (see "Operator decisions") | `docs/spikes/phaze-movg3-ajax-plain-httpx.md` |
| 3 | MixesDB as a second source | **BACKLOG** for a deeper investigation (bead phaze-cwyuq); neither GO nor NO-GO | the operator (see "Operator decisions") | `docs/spikes/phaze-q2v7w-mixesdb-archive-coverage.md` |
| 4 | Captcha re-queue (script merged in PR #678, bead phaze-c9go7) | **CANCELED**; earlier release deferral superseded | the operator (see "Operator decisions") | `docs/spikes/phaze-gnbct-ajax-endpoints.md` |

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

### 2. ajax JSON search: CANCELED

The operator canceled this path on 2026-10-08; the original words are preserved in phaze-1soo9
and phaze-tqoty. See `docs/design/0024-tracklist-source-retirement.md`. Nothing was built and no
revival condition remains.

Historical evidence from `docs/spikes/phaze-movg3-ajax-plain-httpx.md`: 2 of 2 requests returned
`success: true`, 20 entries, 4851 bytes each, byte-identical; 0 of 2 met a challenge. Requests were
about 10 s apart, from one IP in one minute. HTTP status and content type were lost by a wrapper.
Those measurements establish only that session's behavior, not permission to acquire data.

The planner's historical correction remains: search already used plain `httpx`; JSON would not
reduce the one-search-plus-one-detail request count. This is the planner's finding (phaze-rs4x6, 2026-10-07), not an attributed selection.

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

### 4. Captcha re-queue: CANCELED

The operator canceled this acquisition-only support on 2026-10-08. The earlier release timing is
superseded and gives no permission to run a re-queue. The script, originally merged in PR #678, and runtime support are removed.
Authority: phaze-1soo9 and phaze-tqoty; see `docs/design/0024-tracklist-source-retirement.md`.

Historical deployment observation: on 2026-10-07 the containers were at image revision 110b7f98,
built 2026-10-05, without the then-proposed captcha classifier. This is an observation about that
image, not a current deployment check or a remaining action.

## Historical operator decisions (phaze-rs4x6, 2026-10-07, session crossed midnight UTC)

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
- **A cancellation replaces a deferral.** The canceled acquisition paths have no remaining revival conditions.
- **A small clean sample is a claim about one session.** 2 of 2 plain `httpx` requests served says nothing about a rate. See
  `docs/design/0016-transferred-model-verification.md` and the matching lesson in `docs/design/0022-tracklist-acquisition-spike-verdicts.md`.
