# 💡 Decision-Model Opportunities (Jev / shipwithjev.com review)

**Status: idea backlog. Nothing here is a decision, a commitment, or a dependency.** Each idea
needs its own bead, and the adoption question (an external decision API at all) is the operator's
to answer. Bead: `phaze-kvkcs`.

## Source and scope

Reviewed 2026-09-25: every entry in [shipwithjev.com](https://www.shipwithjev.com/), an independent
directory of **551** builds made with **Jev**, TypeSafe AI's "System One" decision model (official
docs: [docs.typesafe.ai](https://docs.typesafe.ai)). The directory is not affiliated with TypeSafe,
and **every cost and latency figure quoted below is author-reported, not measured here.**

Jev writes no text. It takes application state plus closed questions — yes/no, choose one of N, or a
score — and returns typed answers with probabilities, reportedly in ~100–700 ms for fractions of a
cent. So the question this review asks of phaze is narrow:

> Where does phaze make **closed-set judgments** — especially ones it currently asks an LLM to make
> as part of generated prose?

Roughly 70% of the directory does not apply: games, trading bots, browser/desktop computer-use
agents, social-feed filters, and client SDKs in other languages.

## The pattern worth copying: a cascade

The strongest builds share one architecture — **deterministic rules → learned history → cheap
decision model → an LLM only for what genuinely needs writing → a human for anything uncertain.**

| Build | What it shows |
|---|---|
| Spliit Cloud expense categorization | Local dictionary and group history first; Jev only for the residue; uncertain suggestions go to the user |
| Fraud detection with Jev and Kimi K3 | Classifier first, uncertain cases routed to an LLM; 96/100 correct for ~$0.07 |
| Alan AI application review console | 3,518 applications checked in 4:54; 20 surfaced for human review |
| A Downloads folder that sorts itself | phaze's problem at toy scale: decide what a file is, then rename and move it by rules |

phaze already has the first layer —
[`filename_convention_learner.py`](../../src/phaze/services/filename_convention_learner.py) learns
per-release-group date order from the corpus. The ideas below add the middle layers.

## Product ideas, ranked

### 1. Split proposal generation into *writing* and *deciding*

[`FileProposalResponse`](../../src/phaze/services/proposal_parsing.py) asks one LLM completion for
the filename **and** for `source_type`, `stage`, `proposed_path`, and a self-reported `confidence`
(which already needs `clamp_confidence`). Keep the LLM for `proposed_filename`; move the closed-set
fields to typed choices with real probabilities: source type, stage, and which **existing**
destination folder. A folder tree larger than one question's option limit (the directory lists a
255-option "Choice ceiling" — unverified) is what `jev-tree` addresses: recursive choice down a
taxonomy.

### 2. Calibrate against phaze's own approval history

This is the asset no directory entry has. Every approve / edit / reject in the review UI is a
labelled example. `jevcal` (fit and drift-check thresholds against labelled data) and `jev-align`
(human labels → calibrated classifiers) are the shape. Use the calibrated confidence to **order and
group the review queue** — high-confidence batches versus "look at these 20" — and **never to
auto-approve**: nothing moves without review.

### 3. Rerank matches instead of trusting fuzzy scores alone

[`discogs_matcher.py`](../../src/phaze/services/discogs_matcher.py) matches with `rapidfuzz`, and
[`routers/tracklists.py`](../../src/phaze/routers/tracklists.py) carries the longest bug-fix history
in the repo. Keep fuzzy matching for candidate generation; have a decision model rerank the top few
("is this release the same set?") — the Oko / `jev-reranker` shape. The same applies to borderline
[dedup](../../src/phaze/services/dedup.py) pairs: "same recording, different encode?"

### 4. Natural-language search in the admin UI

JevQL, `duckdb-jev`, `pg-jev`, and the Zillow listing demo all share one shape: SQL narrows the
rows, then the model scores what is left. For phaze: "sets with a b2b", "recordings where the
stream drops out", "sets tagged techno that are really house". Would sit beside
[`search_queries.py`](../../src/phaze/services/search_queries.py).

### 5. Companion-file judgment

[Companions](../../src/phaze/services/companion.py) are linked by directory. When a directory holds
several media files, "is this `.txt`/`.nfo` a tracklist, and which file does it describe?" is a
closed choice.

### 6. Genre and tag reconciliation

The closest domain analog in the directory is "Music intelligence audit" (3,274 songs, 114 genres).
Combine essentia style output, filename, and tracklist into a genre *choice* for the tag-write
review ([`review_tagwrite.py`](../../src/phaze/services/review_tagwrite.py)).

### Lower value

- **Concert-video segment scoring** (`jev-skip`, `jevmeter`, health-video answer clips) presumes a
  transcript phaze does not produce.
- **Log triage** (`jevlogs`, Tocsin, `jev-logtriage`) for SAQ/agent logs — plausible, not pressing.

## Dev-workflow ideas

- **`jev-lint` (mizchi)** checks "does a test verify what it claims". That is exactly the gap in
  `CLAUDE.md`'s section *"A test's name can carry a lesson its assertions do not check"*, where a
  keyword guard was measured and rejected. A semantic judge is a different mechanism, but owes the
  same evidence: the pre-fix version of the seed test (parent of `a149fdf1`) is a ready-made
  negative control — does the judge flag it? (Rule 3 of
  [`0012-verification-fidelity-and-operator-attribution.md`](../design/0012-verification-fidelity-and-operator-attribution.md):
  an independent check is not automatically a discriminating one.)
- **`jevcache`** — deterministic replay keyed on (model, schema, state). If a decision model ever
  reaches production, replaying **recorded real responses** keeps tests hermetic without a mock.
- **Done-claim and rule-compliance gates** (`jev-belay`, Canny, `jev-gates`, Abide) are less
  compelling: phaze's gate-reading rules are already deterministic, and code beats a judge there.
- **Finding triage** ("Code-review finding triage") could pre-sort adversarial-verifier output from
  [`0011-bug-hunt-cadence.md`](../design/0011-bug-hunt-cadence.md) by severity — low priority.

## Caveats

- **Unverified claims.** No Jev API behaviour, limit, price, or latency in this document was run in
  this environment. Per `CLAUDE.md` ("a belief that is true in a neighbouring system is a claim"),
  any one that becomes load-bearing gets measured first.
- **Dependency pins.** `litellm` is pinned to `>=1.100.0,<1.101.0` for supply-chain reasons; whether
  that line ships a TypeSafe provider is unchecked. The Pydantic AI provider (derives questions from
  Pydantic output types) fits phaze's schemas but is a new dependency. Several integrations are
  LangChain-based, which `CLAUDE.md` rules out.
- **Data boundary.** An external API receives the same class of data the proposal LLM already
  receives (filenames, tags, companion text), but it is a second vendor.

## Proposed first step

A **spike** bead: replay a sample of past proposals and measure typed answers for `source_type`,
`stage`, and destination folder **against what the operator actually approved or edited** — the real
consumer of a proposal, not the tool that produced it. Its verdict decides whether ideas 1 and 2 are
worth building. Scope, and whether an external decision API is acceptable at all, are operator
questions to settle before filing.
