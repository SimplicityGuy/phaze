# MixesDB matching ambiguity and search completeness

Date: 2026-10-08. Bead: phaze-s19tp. Specification and offline investigation only.

## Question

How should a future MixesDB provider expose capped search, fuzzy/body matches and ambiguous credits without falsely linking a set or negative-caching a recording?

## Method

Review historical search evidence and walk synthetic examples through the proposed provider contract. No archive queries were used. The initial [access investigation stopped](phaze-6ehg6-mixesdb-rest-access.md) after two policy requests. After the recorded scope amendment, four public REST search requests were completed: three with limit 5 and one with limit 100. The synthetic cases below remain authored design walkthroughs, not executed adapter tests.

## Evidence

Historical artist-only fallback consumed 118 requests and produced zero new matches. The 63 capped results among 148 sampled sets establish incomplete discovery, not absence. Historical title review left three of 25 matches based on artist alone; there was no measured precision ground truth. Full-text search can rank body matches above the intended set.

Fresh public evidence confirms that distinction: one narrow artist/event/year search returned 3 results for limit 5, of which one title names the intended public event and two titles name other events whose excerpts contain the searched terms. This is title consistency evidence, not ground-truth precision measurement. Two broader limit-5 searches each returned 5 results; one of those responses consisted of one category and four file-namespace entries. Their referenced images were never requested. The limit-100 artist response returned 100 records, with only `pages` at the top level and no total/continuation field. No unsupported pagination parameter was probed.

| Synthetic scenario | Evidence preserved | Required outcome |
|---|---|---|
| Query “Example Artist Example Festival 2024”; returned title is another artist, whose body mentions the query | Query and title/body distinction | Candidate may exist as evidence; core must reject automatic identity association. |
| Two recordings on the same festival day, different stages; source titles omit stage | Known artist/date/event, unknown stage | Ambiguous alternatives; no automatic link by date alone. |
| Query uses “Example Artist”, title uses a diacritic or documented alias | Explicit alias evidence, normalized comparison version | Core may rank the candidate; provider cannot manufacture certainty or silently expand to unbounded aliases. |
| Source credits “Example Artist b2b Other Artist”; recording hypothesis names only one | Full ordered/source credit and joint-act evidence | Ambiguous unless independent signals establish the joint performance. Token containment is insufficient. |
| Artist matches, year/event unknown | Missing context and low specificity | Candidate discovery only; no FOUND association to the recording. |
| Search returns exactly requested cap 100 | Count/cap/query and unknown exhaustion | Incomplete; optional continuation only if demonstrably supported. |
| Search returns 3 with cap 100, without total/exhaustion evidence | Query-scoped results, unknown source search semantics | Incomplete enumeration evidence; never provider-wide absence. |
| Demonstrated source enumeration exhausted for one exact query | Exhaustion evidence and exact query scope | Complete query batch; zero candidates still does not assert semantic absence across aliases. |
| Artist-only search capped; artist+year query uncapped-looking but no continuation contract | Both bounded queries and their constraints | Scoped observations remain incomplete; narrowing is not a proof of exhaustion. |
| Load of a discovered slash-containing key returns 404 | Original key, route encoding and HTTP response | Missing native object only if correct routing is established; otherwise unavailable/incomplete, never recording-wide absence. |
| Revision changes between discovery and load | Observed and loaded revisions | Explicit changed-source/retry evidence; approval cannot silently switch snapshots. |

## Proposed discovery policy

Provider discovery returns provider/native identity, bounded source metadata, origin and explicit completeness/reason/evidence. Core owns candidate ranking and association. No numeric matching threshold is proposed as measured accuracy. A date prefix can be useful evidence but should not overwrite uncertain event dates, rebroadcast dates or title dates.

Use a finite artist/event/year search when those hypotheses are supplied. Stop when the authorized request/deadline/candidate budget is exhausted and return partial evidence. Avoid the historical artist-only fallback as a default: it consumed 40% of that run's budget without finding another match. A later experiment may compare artist+year narrowing, but this report establishes no recall improvement.

No undocumented offset, continuation token or pagination parameter may be invented. A cursor must bind provider, query and source snapshot where available. A source that cannot demonstrate enumeration exhaustion returns incomplete without a cursor. Never infer completeness solely from count below a cap. Search-level nonmatch is not a negative-cache key for the recording.

## Verdict

The proposed candidate batch and core-owned association boundary handle the observed risks, including now-observed body-match false candidates and non-tracklist namespaces. Limited live ranking behavior was observed; exhaustive discovery, documented pagination and corrected slash routes remain unverified. Matching precision and recall are unknown.

## Recommendation

Require implementation acceptance cases for every table row, with an independent fake provider/transport. Keep partial results, ambiguous candidates and scoped absence distinct. Any future evaluation of live recall or match accuracy needs separately authorized samples and ground truth; this documentation does not reopen archive access or file an adapter plan.
