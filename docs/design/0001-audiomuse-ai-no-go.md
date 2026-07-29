# ADR-0001 — Do not incorporate AudioMuse-AI capability into phaze

| | |
| --- | --- |
| **Status** | Accepted — **NO-GO** |
| **Date** | 2026-07-27 |
| **Decider** | Repository owner |
| **Investigation** | `phaze-ytgo` (epic, closed 9/9) |
| **Supersedes** | — |

> **Post-decision note (2026-07-29) — a premise expired, the decision stands.** Epic
> `phaze-0jpe` (2026-07-28) removed audio fingerprinting from phaze entirely
> ([ADR-0002](0002-fingerprint-removal.md)). The P1 rationale below rests on a premise that is
> no longer true — "Dedup and rename continue to rest on the existing fingerprinting engines"
> (Consequences → Accepted) — because there are no longer any fingerprinting engines to rest
> on. Per operator decision 2026-07-29 the **NO-GO stands as written**: nothing in this ADR's
> cost, licence or feasibility analysis changed. What *is* re-opened is the narrower question
> of how phaze should detect near-duplicates now that fingerprinting is gone; that is tracked
> as its own decision bead and is not decided here.

______________________________________________________________________

## Context

[AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) is a self-hosted sonic-similarity and
playlist-generation project (AGPL-3.0). In July 2026 we asked what it would take to bring its
capability into phaze, scoped deliberately to **two shapes compared on evidence** — a clean-room
native implementation, and AudioMuse deployed as a sidecar.

The question was decomposed into four independent **purposes**, because the right answer plausibly
differed per purpose:

| | Purpose |
| --- | --- |
| **P1** | Better dedup + rename proposals — catch near-duplicates fingerprinting misses; give the naming LLM sonic context |
| **P2** | Discovery / playlists — similar-track browsing, clustering, playlist generation |
| **P3** | Set/tracklist intelligence — window-level similarity inside long concert sets |
| **P4** | Archive exploration / QA — music map + clustering to eyeball the archive |

The gap that made this a real question: phaze already persists BPM, key, mood, style and
danceability from essentia-tensorflow across a windowed analysis, but stores **no embedding vector
and has no ANN index**. Every distinctive AudioMuse capability hangs off those two missing pieces.

Six spikes ran: a requirements rubric, essentia embedding capability, CLAP/UMAP dependency pricing,
the sidecar integration seam, an AGPL-vs-MIT compliance assessment, and vector storage / ANN search
at archive scale. A seventh bead cross-tabulated them into a per-purpose verdict matrix.

## Decision

**Do not build it — in any of the four purposes, in either shape.**

The investigation was completed, not abandoned. The decision was taken with the matrix in hand and
declines the implementation as not justified by what the research found.

### The matrix as actually reached, before the decision

The honest record is that this was **not** a unanimous technical NO. One cell was a technical GO
and was declined anyway; the reason that is coherent rather than arbitrary is set out below.

| | **P1** dedup + rename | **P2** discovery / playlists | **P3** set/tracklist | **P4** archive QA |
| --- | --- | --- | --- | --- |
| **Verdict reached** | insufficient evidence | **clean-room** *(staged)* | insufficient evidence *(one variant affirmatively closed)* | not worth it |
| **Shape, if ever built** | clean-room; sidecar independently eliminated | clean-room, decisively | clean-room, boundary-detection form only | n/a — use features already stored |

**No `hybrid` cell survived**, which was itself a finding — the investigation was explicitly
commissioned to test a hybrid hypothesis, and measurement refuted it (see *Sidecar*, below).

## Rationale

### 1. The capability is technically reachable, and cheaply

This is the part that makes the decision a genuine trade-off rather than a foregone conclusion.

- **Embeddings are nearly free.** The `effnet_discogs` classifier phaze already runs emits a
  **1280-dimension** embedding at a native **~1.005 Hz** patch rate, for **+1.6 % marginal
  wall-clock** on a forward pass phaze already computes. Not a new model stack — one output node.
- **The track tier is comfortable.** 200,000 track-level rows occupy **1.39 GB** as `halfvec` with
  an HNSW index, build in **95 s**, and answer at **recall@10 = 0.991, p50 2.4 ms** — measured on
  **real** embeddings, not synthetic vectors.
- **`halfvec` is free.** Recall tracked the float32 arm to within **0.008 at all eight operating
  points** tested, while halving storage and index size and cutting query latency ~1.8×.
- **pgvector is a 3-line Dockerfile**, **+31 MB**, staying on the pinned Alpine Postgres base — not
  the base-image change it was assumed to be.

Nothing found erodes the clean-room option's "one stack" advantage.

### 2. But nothing measured *quality* — and that is why the GO was declined

**Every accuracy bar the rubric defined went unmeasured.** The spikes settled **feasibility**:
storage, retrieval fidelity, and cost. They never demonstrated that the capability would
*recommend well*.

| Purpose | Bar the rubric set | Status |
| --- | --- | --- |
| P1 | ≥ 50 operator-labelled near-duplicate pairs, same-genre negatives; recall ≥ 0.90 at precision ≥ 0.95 | **never run** |
| P2 | 20-seed blind A/B against the existing-features baseline; mean precision@10 ≥ 0.6 *and* strictly better than baseline | **never run** |
| P3 | ≥ 70 % of scraped tracklist entries within ±30 s, ≤ 1 false segment per 10 true | **never run** |
| P4 | Outlier yield against the existing-features baseline | **never run** |

The vector spike's per-purpose table records "SERVES" on **storage-and-retrieval grounds only** and
warns explicitly against reading those tokens as quality evidence. A capability that stores and
retrieves perfectly and recommends badly still fails the bar.

The P2 GO was therefore **staged so that stage 1 could kill it**: land the embedding column and the
index, then run the blind A/B, and file no user-facing surface unless it passed. Declining to start
that sequence — rather than spending the infrastructure work to reach a gate that might close — is
consistent with the evidence, not contrary to it.

The one genuinely positive quality signal found was narrow: the embedding space places a remaster
pair at cosine **0.064–0.083**, below its different-work floor of **0.1373**, where the
existing-features baseline puts the same pair at **0.0023–0.0110** astride its own floor of
**0.0028** — i.e. the baseline provably cannot express that distinction. But **44 of 45 positive
pairs were constructed rather than found**, and recall at 95 % precision collapsed **1.00 → 0.60**
against same-genre negatives — which is what this archive actually consists of.

### 3. The sidecar shape was refuted by measurement

The investigation was commissioned partly to test "sidecar as a fast proving ground for P2,
clean-room for P3". Both halves failed:

- **A sidecar cannot see most of this archive.** AudioMuse analyses and stores only the **first 600
  seconds** of any file, recording that truncated value as the track duration. Measured against the
  archive's real duration distribution, **85.9 % of files exceed 600 s** and **39.4 % exceed an
  hour**. A sidecar would represent the majority of the collection by its opening ten minutes, and
  would report an identical 600 s duration for **9,808 files** — which also degenerates its own
  duration-agreement identity check across most of the corpus.
- **It is slower than the thing it would de-risk.** Ingest measured at ~20–26 s/track, extrapolating
  to **~25 days** of continuous running for an initial 200,000-file load that also moves the entire
  archive byte-for-byte over HTTP. It requires phaze to permanently impersonate a media-server API
  (no filesystem ingest path exists), a shim tracking another project's internal client code rather
  than a spec — 1–2 weeks to production plus unbounded maintenance. Runtime cost: **+5.36 GB image,
  ~950 MiB peak RAM, ~2.4 cores** competing with phaze's own analysis workers.

Against that, the clean-room path's production side is roughly a ten-line change at +1.6 % wall
clock on a model already loaded in the same process. **The "quick proof" was slower than the real
thing, and could not speak to 85.9 % of the corpus.**

### 4. What is affirmatively closed, and must not be re-litigated

**P3's stored contiguous vector tier is closed on storage, permanently.** A contiguous 1 Hz pass
over a 200,000-file archive at this corpus's real duration distribution is **~725 million vectors**
— **10.3 TB** at native dimensionality, and still **~644 GB** after reduction to 200 dimensions,
against **47 GB free** on the production host. Even a 10-second hop is ~64 GB.

This is a **storage** verdict, not an index verdict. Substituting a different ANN engine does not
change the arithmetic — the best case for a bespoke quantized store is still ~145 GB of raw vectors
before any index exists. **A future proposal to revisit P3 by changing the index technology is
answering a question that was not the problem.**

Notably, the compute objections to P3 *died* during the investigation — contiguous decode measured
**13× cheaper** than an earlier estimate, and inference at ~1,340 CPU-hours is tractable. Only
storage stands, and it stands by one to three orders of magnitude.

For contrast, the intermediate window tier (~10.3 M rows) **does** fit: 9.14 GB at 200 dimensions,
13.6-minute index build, recall@10 0.862. The reduction from native dimensionality to 200 costs
**16 % of the exact top-10 and 22 % of the exact top-1** — a real quality trade, not a free win.

### 5. What remains open rather than settled

The two `insufficient evidence` cells are **open questions, not settled NOs**. Should the question
be reopened, each is settled by one specific measurement:

- **P1** — score ≥ 50 operator-labelled near-duplicate pairs drawn from the real archive
  (excluding byte-identical and encode-only pairs, which belong to the identity share that
  fingerprinting already owns), with same-genre negatives and a baseline arm.
- **P3** — score the 1 Hz novelty curve against scraped tracklist timestamps at ±30 s, restricted
  to the sub-corpus fingerprinting could not identify. Costs CPU only and **stores nothing**, so it
  does not run into the storage verdict above. The signal is real but unvalidated: peak-to-median
  **8.74×** on a DJ set versus 3.12× within a single track.

P4 is a resolvable **no** rather than a third open cell: the rubric assigned the burden of a
measured improvement over existing features to the embedding side, and no spike discharged it —
whereas P1 at least produced positive evidence of a specific baseline failure. P4's value is largely
reachable today over columns phaze already stores; even the 2-D map is a projection of a vector we
already have. PCA fits 200,000 × 200 in **0.11 s** against UMAP's 189 s plus a `numba`/`llvmlite`
dependency class phaze currently has none of.

## Consequences

### Accepted

- phaze gains no sonic-similarity, playlist-generation, music-map or embedding-based dedup
  capability. Dedup and rename continue to rest on the existing fingerprinting engines.
- No embedding column, no pgvector, no ANN index, and no new model stack.
- If discovery/playlists become desirable later, the investigation is largely reusable: the
  feasibility numbers stand and the quality gate is the only unrun step.

### Findings that outlive this decision

These were surfaced by the investigation but are **independent of it**, and are tracked separately:

| Bead | Finding |
| --- | --- |
| `phaze-knwk` | `docker-compose.yml` sets no `shm_size` on postgres, leaving parallel dynamic-shared-memory allocations against Docker's 64 MB default. Proven for index builds; whether today's parallel query workload is already affected is that bead's first task. |
| `phaze-tcqq` | The pinned Postgres image is hardcoded in 10 places with no single source of truth — 4 of them `echo` strings, so a partial version bump prints the old tag while running the new one. |
| `phaze-gfdx` | Worktree provisioning invokes a `just setup` recipe that does not exist, so no development seat gets working pre-commit hooks. |
| `phaze-dnso` | **Pre-existing P1 — conveyance stopped 2026-07-28.** phaze published container images conveying AGPL-3.0 Panako from an MIT repository with no NOTICE and no source offer. `phaze-0jpe` removed Panako from the images on 2026-07-28, so no newly built tag conveys it; the remaining exposure is the set of previously published GHCR tags, whose deletion is tracked as a separate ops bead. Its supporting analysis is `docs/spikes/phaze-ytgo.5-agpl-mit-compliance.md`, landing with this ADR. |

### Clean-room policy correction — retained

The investigation's licence spike corrected the clean-room rule the epic started with, and **that
correction is retained as policy** regardless of this NO-GO:

- Reading a published project's **prose** (README, architecture and algorithm docs) does **not**
  taint an independent implementation. 17 U.S.C. § 102(b) excludes ideas, procedures and methods of
  operation *"regardless of the form in which it is described."* Published prose is safe input.
- **Configuration variable names are the exception** — names are literal expression, and
  systematically adopting another project's naming scheme donates a gratuitous similarity for zero
  capability gain.
- The correct model constrains **output, not input**, enforced by a per-document disclosure. A
  clean-room here is evidentiary hygiene — a cheap, strong rebuttal if derivation is ever alleged —
  **not a legal prerequisite**, since the upstream licence grants everyone the right to read.

### Where the evidence lives

The seven spike documents (~5,000 lines of measured evidence) land alongside this ADR under
`docs/spikes/`:

| Document | What it settles |
| --- | --- |
| `phaze-ytgo.1-purpose-rubric.md` | The P1–P4 requirements rubric and accuracy bars every other spike scores against, plus an audit of what phaze already covers |
| `phaze-ytgo.2-essentia-embeddings.md` | Whether the shipped essentia models can emit embeddings, at what dimension, cost and quality |
| `phaze-ytgo.3-clap-umap-deps.md` | The price of CLAP text-search and UMAP/PCA as new dependencies, with licence screening |
| `phaze-ytgo.4-sidecar-seam.md` | The sidecar integration seam and capability envelope, measured against a running deployment |
| `phaze-ytgo.5-agpl-mit-compliance.md` | AGPL-3.0 vs phaze's MIT licence, and the clean-room correction retained above |
| `phaze-ytgo.6-vector-index.md` | Vector storage and ANN search at archive scale — the benchmark matrices behind the storage verdicts |
| `phaze-ytgo.7-verdict.md` | The per-purpose cross-tabulation this ADR summarizes |

This ADR is written to be self-contained: the decision and its reasoning stand without opening any
of them. They carry the underlying measurement detail — full benchmark matrices, sample provenance,
the licence analysis and the per-purpose scoring — for anyone who needs to re-examine a conclusion
rather than accept it.

Read them with one caveat: they were written during the investigation and each records the state of
knowledge at its own point in the sequence. Where a spike's framing conflicts with this ADR, **this
ADR is the operative record.** Two known instances: the original clean-room seal was superseded
mid-molecule by the licence spike, and the `S`-number labels used in early planning do not
correspond to the filed bead numbers.

## Notes on this record

Consistent with repository practice for investigation records, **host names, filesystem paths,
account names, media filenames, content digests and file UUIDs have been replaced with stable
placeholders** throughout this ADR and every accompanying spike document. Identifiers only: every
measured quantity is reproduced exactly as recorded, and document line counts are unchanged.
Obfuscation removed identifiers, never precision.
