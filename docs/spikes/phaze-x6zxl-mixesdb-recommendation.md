# MixesDB investigation recommendation

Date: 2026-10-08. Bead: phaze-x6zxl. Epic: phaze-ypcx6. Decision status: recommendation ready; operator GO/NO-GO pending.

## Question

Should MixesDB proceed to a separately approved adapter plan, given historical yield, current access uncertainty and the proposed tracklist provider interface?

## Method

Synthesize the [baseline](phaze-py7ls-mixesdb-investigation-baseline.md), [access/pacing evidence](phaze-6ehg6-mixesdb-rest-access.md), [search semantics](phaze-s19tp-mixesdb-search-semantics.md) and [parsing/provenance assessment](phaze-vwcsv-mixesdb-parsing-provenance.md). Preserve the difference between measured evidence, specification and pending review.

## Evidence

Historical archive yield was 25/148 matching titles, with nonempty content on 21/24 fetched distinct pages. Ground-truth precision was not measured. Sixty search misses were classified as absent historically but do not demonstrate provider-wide absence. Sixty-three capped misses were undetermined. No sampled page supplied a second-precise relative cue judged usable by that historical run.

Initial authorization allowed up to 100 requests, with a conservative stop at **2/100** after readable legal HTML included background challenge-platform JavaScript. The 2026-10-08 scope amendment on phaze-ypcx6 then authorized both API families; its policy clarification was **“Respect those robots restrictions; test REST and the bare Action API endpoint only”**. The prior stop remains historical evidence; the new API phase was explicitly authorized and retained the shared counter and clock.

Final count is **13/100**, with 87 unspent: 10 HTTP 200 and 3 HTTP 404 across both phases. Minimum persisted gap is **5.140897035598755 seconds**. Bare Action API help identifies MediaWiki 1.46.1. REST search and two ordinary native page fetches succeed; the root has no handler. Successful page JSON supplies current revision ID/time, wikitext and an empty license object. Slash-containing keys fail in ordinary encoded and literal forms; failures are routing evidence, not proof of absence. Search returns body-match candidates and non-tracklist namespaces, and the 100-record response carries no total/cursor. Action query modules and exhaustive REST pagination remain unverified. No 403, 429, actual challenge, script execution, Action query or archive access occurred.

Offline transport checks passed for explicit retry charging, before-send durable reservation, restart/worker pacing, shared budget and persistent stop. Written synthetic design walkthroughs cover ambiguous matching, capped/uncertain enumeration, dual markup, unresolved fields, mixed timing interpretation, midnight clocks and provenance. They are not production implementation tests or live search/parser measurements.

## Proposed ADR: separate content value from activation permission

**Context.** Historical evidence indicates a possible additional content source. Search, cue quality and access do not support promises of exhaustive discovery, accurate automatic association or precise cue export.

**Proposed decision.** Retain MixesDB as a candidate content provider and recommend **conditional GO for a later adapter planning decision**, limited to ordered track text and source provenance. Product implementation and activation remain gated. Do not rely on historical GO language as operator approval.

**Conditions before an adapter plan is authorized.** The operator must review this report and decide whether to proceed; API investigation permission is not an adapter planning GO. The provider interface/compatibility specification must be reviewed. Access scope is now clarified for REST and bare Action help, while query-bearing Action calls remain excluded by the explicit robots-respecting answer. Future experiments remain bounded by the recorded scope and remaining shared budget; this report grants no broader authorization.

**Conditions a later plan must address.** Carry forward verified REST search/page revision schema; resolve or explicitly classify unsupported slash-key routes and uncertain pagination; preserve incomplete search and uncertain interpretation; define bounded parser limits and immutable rights/provenance; separate native identity from recording association; protect stored/manual approved data; specify qualified timestamp consumers without inventing precise cues. Keep every provider effect subject to core authorization and persistent pacing. Do not depend on Action queries as a fallback under the present scope.

**Consequences.** A modest historical content yield may justify the source if the operator values those tracklists. It adds access/policy maintenance, parsing and provenance costs. No credible current coverage or match-precision guarantee follows from this batch. A NO-GO is appropriate if live access cannot remain within allowed rules, source content is not worth those costs, or precise relative cues are a requirement.

**Alternatives.** Continue local companion/embedded providers alone; defer MixesDB because content value may not justify its remaining routing/parser/policy costs; perform a later bounded public REST investigation of an unresolved issue before making a planning decision. None requires reopening archive access.

## Verdict

**Conditional GO recommendation for content-only planning consideration; no operator GO recorded.** Ordinary REST search/page access and revision schema are verified. Slash-key routing, exhaustive discovery, ground-truth match accuracy and production parser behavior remain unresolved. The investigation is complete within its clarified scope; the product decision remains pending.

## Recommendation and operator review prompt

Review the evidence and choose whether to defer/drop MixesDB or authorize a later adapter planning step. API availability is one input to that choice, not a promise of useful association, exhaustive coverage or precise cues. No answer is inferred from operator unavailability, historical recommendations or remaining request budget.

No adapter molecule, production code, migrations, queue activation, merge or deployment is created by this batch. The documentation branch is submitted for human review. Adapter planning or implementation requires a later recorded approval.
