# MixesDB investigation recommendation

Date: 2026-10-08. Bead: phaze-x6zxl. Epic: phaze-ypcx6. Decision status: recommendation ready; operator GO/NO-GO pending.

## Question

Should MixesDB proceed to a separately approved adapter plan, given historical yield, current access uncertainty and the proposed tracklist provider interface?

## Method

Synthesize the [baseline](phaze-py7ls-mixesdb-investigation-baseline.md), [access/pacing evidence](phaze-6ehg6-mixesdb-rest-access.md), [search semantics](phaze-s19tp-mixesdb-search-semantics.md) and [parsing/provenance assessment](phaze-vwcsv-mixesdb-parsing-provenance.md). Preserve the difference between measured evidence, specification and operator decisions.

## Evidence

Historical archive yield was 25/148 matching titles, with nonempty content on 21/24 fetched distinct pages. Ground-truth precision was not measured. Sixty search misses were classified as absent historically but do not demonstrate provider-wide absence. Sixty-three capped misses were undetermined. No sampled page supplied a second-precise relative cue judged usable by that historical run.

Fresh authorization allowed up to 100 requests, but the conservative mandatory stop occurred at **2/100**: robots HTTP 200 and a readable legal page HTTP 200 containing Cloudflare challenge-platform JavaScript. There were zero fresh REST requests. Request spacing was 49.555183887 seconds. No script was executed or fetched. The stop persists across runner restarts. Current REST availability, slash-key handling and revision/pagination schema remain unknown.

Offline transport checks passed for explicit retry charging, before-send durable reservation, restart/worker pacing, shared budget and persistent stop. Written synthetic design walkthroughs cover ambiguous matching, capped/uncertain enumeration, dual markup, unresolved fields, mixed timing interpretation, midnight clocks and provenance. They are not production implementation tests or live search/parser measurements.

## Proposed ADR: separate content value from activation permission

**Context.** Historical evidence indicates a possible additional content source. Search, cue quality and access do not support promises of exhaustive discovery, accurate automatic association or precise cue export.

**Proposed decision.** Retain MixesDB as a candidate content provider and recommend **conditional GO for a later adapter planning decision**, limited to ordered track text and source provenance. Product implementation and activation remain gated. Do not rely on historical GO language as operator approval.

**Conditions before an adapter plan is authorized.** The operator must review this report and decide whether to proceed, including the challenge-platform stop and need for further access clarification. The provider interface/compatibility specification must be reviewed. Any renewed live experiment needs a recorded scope and remaining/fresh budget decision; this report does not restart transport automatically.

**Conditions a later plan must address.** Verify allowed REST access, native slash keys and revision/schema behavior; preserve incomplete search and uncertain interpretation; define bounded parser limits and immutable rights/provenance; separate native identity from recording association; protect stored/manual approved data; specify qualified timestamp consumers without inventing precise cues. Keep every provider effect subject to core authorization and persistent pacing.

**Consequences.** A modest historical content yield may justify the source if the operator values those tracklists. It adds access/policy maintenance, parsing and provenance costs. No credible current coverage or match-precision guarantee follows from this batch. A NO-GO is appropriate if live access cannot remain within allowed rules, source content is not worth those costs, or precise relative cues are a requirement.

**Alternatives.** Continue local companion/embedded providers alone; defer MixesDB until current access is clarified; authorize a later narrowly scoped public REST investigation before making a planning decision. None requires reopening archive access.

## Verdict

**Conditional GO recommendation for content-only planning consideration; no operator GO recorded.** Fresh access is unresolved after a mandatory stop. The investigation is complete within its permitted scope, including explicit unknowns; the product decision remains pending.

## Recommendation and operator review prompt

Review the evidence and choose whether to defer/drop MixesDB or authorize a later adapter planning step after access clarification. If continuing, decide how the challenge-platform marker should be treated under a new or clarified access scope. No answer is inferred from operator unavailability, historical recommendations or remaining request budget.

No adapter molecule, production code, migrations, queue activation, merge or deployment is created by this batch. The documentation branch is submitted for human review. The subsequent operator decision must be recorded before adapter planning or implementation proceeds.
