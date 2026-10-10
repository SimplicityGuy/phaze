# Provider-neutral set-to-recording matching policy

Date: 2026-10-08. Bead: phaze-rxhn6. Epic: phaze-uquqk. Status: proposed specification; runtime implementation remains gated.

This specifies core-owned matching of a provider's concert/set tracklist to a local recording. It absorbs the intended matching scope of paused phaze-o338a without implementing a scorer, parser, association writer or tests. The operator approved the public contract specification; that approval does not establish measured matching quality or authorize this implementation.

## Baseline and scope

Assigned baseline is `ae0a3fcc6d2683412730d8337e26d406ece2b140`, the epic integration of phaze-drv39. The inherited [provider contract](tracklist-provider-contract.md) describes proposed API behavior and cites its earlier source baseline. This matching section cites source at the assigned baseline. Local main and its remote-tracking ref diverged when the claim refused an automatic epic refresh; no manual merge was performed. Inherited specification text is not evidence that a runtime registry or matching policy exists.

| Source and symbols | Current behavior / design implication |
|---|---|
| [companion_linking.link_companion, resolve_references, own_folder_stem, is_collection_folder, close_name](../../src/phaze/services/companion_linking.py) | Existing companion association is an ordered chain over agent-local stored content features and media. Explicit references and own-folder stems precede broad folder/name rules; collection tracklists cannot use later broad fallbacks. This relates a sidecar file to media, not a provider tracklist snapshot to one concert recording. |
| [constants.companion_match_key](../../src/phaze/constants.py) | Existing own-folder stem key removes copy markers, leading scene indices and nonalphanumerics after casefold/NFC. This deliberately lossy key is useful association evidence, not a public set identity or certain artist/event parser. |
| [companion_linking.name_tokens, name_similarity, full_dates, dates_agree](../../src/phaze/services/companion_linking.py) | Current close-name similarity combines token/sequence evidence. Its dates treat day/month as an unordered pair; that existing permissiveness must not become a certain date in the provider contract. The constants 0.9 and 0.05 govern the existing companion chain, not this new matching policy. |
| [companion.associate_companions, _replace_links](../../src/phaze/services/companion.py) | Existing association can rederive and replace sidecar links; stale content features are deferred. Its mutation semantics cannot be reused to overwrite approved provider associations or manual selections. |
| [FileCompanion](../../src/phaze/models/file_companion.py) | Stored links are many-to-many and do not record whether a human, reference, stem or folder rule created them. Existing presence alone cannot prove explicit human intent or uniquely targeted content. |
| [Tracklist, TracklistVersion, Tracklist.is_canonical / is_propagated](../../src/phaze/models/tracklist.py) | Stored file links, confidence, auto_linked, source and approved status are existing data; canonical and inherited projections differ. The model does not supply a complete durable history of association decisions. Preserving human decisions needs explicit provenance in the later persistence/integration design. |
| [get_file_tracklist_review](../../src/phaze/services/tracklist_review.py) | Baseline reads stored latest tracks without external acquisition. Matching must preserve stored review data and cannot recreate retired acquisition triggers. |
| [discogs_matcher.compute_discogs_confidence / match_track_to_discogs](../../src/phaze/services/discogs_matcher.py) | Discogs matches an individual track's artist/title to releases, using a 0.6/0.4 similarity/relevance blend and top-three results. It answers a different identity question; neither its blend nor result confidence establishes concert-set association. |

Repowise context was unavailable during the preceding contract work. This section uses directly read repository sources; no indexed claim, archive inspection or remote research is assumed. All examples below are invented.

## Decision: evidence, ranking and authorization are separate

A pure core matching port consumes bounded normalized recording context, provider-scoped candidates/snapshots and existing association decisions. It returns evidence and relative ordering without reads, storage writes, provider activation or task scheduling. Providers supply parsing and source evidence through the public contract. Core determines whether to offer a candidate for human review, preserve an association, block a conflict or authorize a narrowly established local-origin association under explicit policy.

```text
match_set_to_recording(recording, candidate_or_snapshot, local_origin_evidence,
                       existing_decisions, candidate_scope, policy_version)
    -> MatchAssessment
rank_assessments(bounded_assessments, candidate_scope) -> RankedAssessments
```

These illustrative signatures are documentation, not implemented DTOs or tests. A proposal to associate a recording is distinct from approval of imported tracks, CUE export, tags or file moves. None may be inferred from the candidate being ranked first.

## Inputs and normalization

| Input | Proposed normalization and evidence rules |
|---|---|
| Recording reference | Stable internal file identity plus agent ownership and observed metadata revision. Moves change display paths without changing established file identity where that identity survives. Never match by UUID string similarity or a content digest copied into a title. |
| Filename / parent display name | Core supplies a bounded basename and, only when needed, bounded folder-name evidence. Preserve original text alongside Unicode NFC/casefold, separator normalization and whitespace normalization. Explicitly identify removed source/quality tokens or copy markers. Lossy aliases are separate features and cannot replace exact identity. |
| Artist | Preserve raw spelling and uncertainty; compare normalized names and explicit evidenced aliases separately. Do not drop collaborator, stage, remix or guest distinctions merely to increase similarity. A shared artist without event/date corroboration is a review suggestion, not an automatic match. |
| Event | Separate event/series, edition/year, venue/stage and broadcast/program where source makes the distinction. Missing edition/stage data is unknown, not equality. “Live”, “radio” or a quality suffix alone supplies no event corroboration. |
| Date | Preserve kind (performance, broadcast, publication or unknown), precision (day/month/year) and certainty. Unambiguous valid ISO dates may support exact comparison; locale-ambiguous forms retain alternatives. A broadcast/upload date cannot be substituted for performance date without evidenced mapping. No year/month/day inferred from unrelated numeric tokens. |
| Provider candidate / snapshot | Provider-native identity is opaque and scoped by provider. Compare documented set metadata and source fragments, with their certainty/revision. Search rank or provider-native similarity is only retrieval evidence. Load confirmation must not silently inherit a discovery display-name guess as known metadata. |
| Local origin | Carry embedded-recording origin or fresh sidecar reference resolution, reference target(s), agent, content revision, reference method and completeness. Bare FileCompanion existence or shared-folder membership is insufficient to identify an explicit direct link. |
| Existing decisions | Preserve approved/manual selections, explicit human rejection and prior auto-association provenance as distinct states. Missing provenance is unknown and defaults to review rather than assuming an association is disposable. Decision persistence is delegated to phaze-vn41h / phaze-9ahjc. |
| Candidate scope | Bounded retrieval/assessment scope, whether complete, truncation reasons and observed alternatives. “Complete” only concerns stated enumeration; it never guarantees alias recall or semantic absence. |

Filename parsing must support conservative hypotheses for `Example Artist - Example Event - 2024-06-15.mp3` and separator variants, while recording which fields came from which tokens. Names such as `Example Artist - Example Event - 03-04-2024.mp3`, a series with multiple dates or a title containing a year yield ambiguous/partial hypotheses. A field inferred from a filename remains inferred unless independent evidence establishes its meaning. Duplicate-copy or source-quality normalization can help retrieve alternatives, but cannot prove that the recordings contain the same performance.

Limits must bound characters, hypothesis count, candidate count and work per comparison. On truncation, preserve the reason and assessment scope; do not report that an excluded rival cannot exist. No archive-wide unbounded scans or filesystem reads occur inside the matcher. Exact default bounds and retrieval index choice remain implementation questions.

## Assessment and ordering

| Output | Meaning |
|---|---|
| `identity` / `recording_id` | Exact provider-scoped object being assessed against one recording; never a new conflated cross-provider identity. |
| `evidence` | Per-field exact, normalized, fuzzy, missing, uncertain or contradictory comparison, origin fragments/references, freshness and parsing policy version. Mark copied/derived evidence: artist/event/date extracted from one filename are not three independent confirmations. |
| `confidence` | Proposed qualitative band: corroborated, plausible, insufficient or conflicting, with uncertainty and reasons. It is an explainable judgment, not a calibrated probability or invented numeric accuracy. If implementation adds a numeric ranking score, identify it as an uncalibrated ordering statistic. |
| `rank_key` / `alternatives` | Core ordering over explicit evidence, with bounded alternatives and ties. Deterministic identity tiebreaks provide stable display only; they never resolve semantic ambiguity. |
| `eligibility` | `preserve_existing`, `review_required`, `explicit_local_origin_eligible`, or `blocked`, accompanied by authorization blockers and the exact policy scope. No write is performed by this output. |
| `completeness` | Assessment/discovery completeness and reason, separate from track parse completeness. Retry/unavailable/incomplete cannot become proof that no match exists. |

Proposed ordering is lexicographic and explainable: preserve authoritative existing decision; identify fresh explicit local origin; prefer corroborated artist/event/date over plausible partial matches; separate contradictory candidates; retain equally supported alternatives. Within a band, exact field agreement can precede normalization/alias/fuzzy evidence. Do not let a high average score cancel a certain wrong performance date, wrong artist or incompatible event edition. Provider popularity or earlier registry order is not identity evidence. Cross-provider candidates with similar content remain distinct alternatives unless core later establishes their equivalence with recorded evidence.

No numeric acceptance threshold or rival margin is calibrated for provider set matching. Existing companion-chain values are grounded in that chain's own evidence and must stay scoped there. The Discogs blend is similarly scoped to individual releases. Initially, all similarity-only or metadata-only matches require human review, including exact artist/event/day agreement. A later automatic metadata-matching policy would require a separate approved evaluation and conservative thresholds, not an adapter's score or an arbitrary reuse of 0.9.

## Precedence and association authorization

1. Preserve the current human selection or approved association. Explicit rejection blocks re-proposal of the same scoped candidate/revision under the recorded decision policy. A fresh conflicting candidate creates an explanation/review item; it never overwrites the established link.
2. An embedded snapshot tied by an authorized read to this recording, or a fresh sidecar with an explicit unambiguous reference resolving to this recording, has direct local-origin evidence. Mark `explicit_local_origin_eligible` only when parsing/identity are valid, origin is fresh, no unresolved target or certain metadata conflict remains and no manual/approved decision conflicts. Actual association still requires an explicitly enabled core policy; this specification does not enable it. Track approval/export remains separate.
3. Own-folder stem matches, folder/twin membership and close-name associations are weaker association evidence. They may aid ranking, but are not upgraded to explicit local origin because a FileCompanion row exists. A collection sidecar mentioning several episodes retains target-specific evidence; no automatic fanout into every recording is justified.
4. All provider metadata/name similarity and ambiguous local references require review. Certain contradictory performance dates or target mismatch block automatic association; incomplete/retry/stale origin blocks authorization while preserving prior data.

A human can explicitly choose a candidate despite imperfect metadata after reviewing conflicts; record that override and its evidence. The matcher itself cannot reinterpret a human action as having occurred. Conversely, unknown/missing metadata does not invalidate an already explicit manual decision. Re-assessment on changed recording metadata or provider revision produces a new assessment and review explanation, not an unapproved relink or mutation of historical snapshots.

This precedence is a proposed core provider-import policy, not a rewrite of the existing sidecar association chain. FileCompanion links can be rederived today; preserving manual provider association requires a separate durable decision representation. Canonical/projection distinction must survive: a propagated stored tracklist is not evidence that the receiving recording was independently matched to the source object.

## Written compatibility scenarios

Each row describes expected design behavior. No scorer, synthetic fixtures or runnable tests were added or executed. “First” means display rank among the stated bounded alternatives, not automatic association.

| Synthetic case | Evidence / confidence | Ranking | Eligibility / action |
|---|---|---|---|
| Recording and set independently name Example Artist, Example Event and known performance date 2024-06-15; no rival | Exact corroborated fields, no contradiction | First corroborated candidate | Review required for metadata-only match; no automatic link from exactness alone. |
| Filename uses `Example_Artist-Example_Event-2024_06_15-WEB.mp3`; source agrees | Normalized separators/quality marker with retained parsing evidence; date certainty independently checked | Ahead of artist-only candidate | Review required; normalization does not create authorization. |
| Artist “Example Artst” versus “Example Artist”, event/date exact | Fuzzy artist and exact contextual evidence; plausible pending typo/alias confirmation | Ahead of missing-event candidate, behind exact corroborated one | Review required; no asserted measured confidence. |
| Source exact artist/event but known performance date 2024-06-22; recording known 2024-06-15 | Certain contradictory performance date, conflicting | Below compatible candidate; visibly retain reason | Blocked for automatic association; human override may be requested, never assumed. |
| Source broadcast date 2024-06-22 and recording performance date 2024-06-15 | Different date kinds, not proved contradiction or agreement | Plausible if other fields agree; no exact-date bonus | Review required until an evidenced broadcast/performance relation exists. |
| Filename `03-04-2024`, source `2024-04-03` | Recording date has multiple locale interpretations; uncertain | Retain alternatives; not equal to independently certain date match | Review required; unordered day/month equality cannot establish certainty. |
| Artist exact, event/date missing | Insufficient identity corroboration | May appear as a low-evidence suggestion within bounds | Review required; artist-only cannot bypass core authorization. |
| Artist/event/date all absent, search hit similarity high | Retrieval score without identity evidence | Unranked/insufficient explanation, no fabricated confident result | Review required or blocked for authorization; never automatic. |
| Two candidates agree on artist/event/date, differing stage or edition unknown | Both plausible/corroborated within limits; unresolved distinguishing field | Semantic tie retained; deterministic provider/native ID sorts display only | Review required; tie cannot be “won” by registry order. |
| Same provider/native ID appears twice in one batch | Duplicate candidate identity, not two independent confirmations | Deduplicate with evidence; conflicting revisions flagged | No multiplied confidence; revision mismatch requires fresh load/review. |
| Fresh CUE explicitly references `Example Recording.mp3`, uniquely resolves to this recording; parsed set valid | Direct local origin with exact target and revision; metadata can be unknown | Ahead of similarity-only candidates | Explicit local origin eligible under an explicitly enabled core policy; tracks/export still need their own approval. |
| Existing sidecar link came from own-folder broad matching; collection has several episodes | Link exists, creation method unknown or non-explicit | Local availability evidence only | Review required; do not convert broad many-to-many links into automatic provider fanout. |
| Multi-file CUE references two parts, queried recording is only one part | Explicit targets exist but set/part relationship and timestamp origins unresolved | Keep target-specific alternatives | Review required until part mapping is evidenced; never flatten to one recording by filename similarity. |
| Human selected candidate A; candidate B later ranks higher | Manual A remains authoritative; B's new evidence recorded | A preserved, B shown only as alternative/conflict | Preserve existing; no relink or overwrite. |
| Human rejected A; repeated discovery returns A unchanged | Explicit rejection with scoped identity/revision | Suppress/review according to recorded decision scope | Blocked; no retry-driven resurrection or score override. |
| Discovery truncated with only one very good candidate | Candidate plausible; rival search incomplete | Display candidate with incomplete scope | Review required; absence of observed rivals does not prove uniqueness. |
| Sidecar changed after origin resolution or provider load returns retry/unavailable | Stale/transient evidence | No newly authorized ranking conclusion | Preserve approved data; re-resolve/review through core effects, no negative cache. |

Timestamp closeness can provide ancillary evidence only when relative meaning, origin and precision are qualified by the public contract. A first cue later than ten minutes does not establish clock time; minute/clock/unknown cues cannot create exact recording-offset evidence. Duration mismatch needs coverage evidence: a partial listing or recording excerpt is not automatically the wrong set.

## Validation and later implementation decisions

Planned validation includes independent fake/local provider substitution; normalization with Unicode/separator/quality-marker variants; invalid/ambiguous dates and date-kind distinctions; incomplete candidate sets; semantic ties; approved/manual/rejected decision preservation; explicit and broad local association origins; multi-file origins; revision freshness; canonical/projection behavior; deterministic display without semantic tiebreak authorization; and no import/network/storage effects in pure matching. These are proposed future checks, not coverage evidence from this documentation work.

A future evaluation should use an approved synthetic corpus initially and separately authorized labeled data if needed. Split exact-reference association, metadata-only set matching and fuzzy retrieval strata; report precision, recall, false-link counts, uncertainty and abstention by stratum and source. Include repeated artist/event/date performances, aliases, broadcast dates, collection episodes and near ties. Keep threshold selection separate from held-out evaluation; measure retrieval misses as well as ranking errors. Do not claim archive accuracy from a few hand-picked examples or transfer a companion-chain measurement to this policy.

Remaining review decisions are the eventual metadata-only automatic-association policy, calibrated acceptance/margin thresholds, alias authority, bounded hypothesis/index defaults, representation of decision scope/history and target-specific multi-part mapping. Until separately approved, similarity-only matches remain review-required. phaze-vn41h and phaze-9ahjc must preserve these distinctions in storage/orchestration; phaze-f5iyc checks consistency across all specification sections before any implementation is authorized.
