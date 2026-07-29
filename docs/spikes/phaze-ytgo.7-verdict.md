# D1 — Per-purpose verdict matrix: clean-room, sidecar, hybrid or not worth it

- **Bead:** `phaze-ytgo.7` (epic `phaze-ytgo` — AudioMuse-AI: clean-room vs sidecar, per purpose)
- **Date:** 2026-07-27
- **Tree:** branch `wt/bead/issue/phaze-ytgo.7`, forked off `wt/bead/epic/phaze-ytgo` at `6b25c20`
- **Inputs:** all six sibling spikes, read in full — `phaze-ytgo.1` (rubric), `.2` (embeddings),
  `.3` (CLAP/UMAP), `.4` (sidecar), `.5` (licence), `.6` (vector/ANN)
- **Status:** synthesis and decision only. **No measurement, no implementation, no product code.**
  The only artifact of this bead is this document.

> **Post-dated note (2026-07-29).** Epic `phaze-0jpe` removed audio fingerprinting — including
> AGPL-3.0 Panako — from phaze on 2026-07-28 ([ADR-0002](../design/0002-fingerprint-removal.md)).
> Any verdict below that assumes fingerprinting is available reads against an expired premise;
> see the post-decision note in [ADR-0001](../design/0001-audiomuse-ai-no-go.md).

> ## Clean-room disclosure
>
> **No AudioMuse-AI source code, prose, documentation, model binary or repository listing was
> read while producing this document.** Its only inputs are the six sibling spike documents in
> `docs/spikes/`, the `phaze-ytgo` and `phaze-vprd` bead texts, and phaze's own tree.
>
> Per the [corrected seal](#01--the-clean-room-seal-is-corrected-record-this-first) the binding
> constraint is on **output**. This document therefore reproduces **no AudioMuse expression**:
> no source, no identifier proposed for adoption in phaze, no parameter value transcribed
> without an independent citation, and no module decomposition of theirs proposed as phaze's.
> Where a sibling described AudioMuse behaviour, this document carries the **behaviour and its
> consequence for phaze**, never a specification a phaze implementation could build from.
>
> One propagation hazard is worth naming because this document sits at the exact choke point the
> seal exists to protect: `phaze-ytgo.4` read AudioMuse source, wrote a document, and this
> document reads it. That path is what hole **H1** identified. It is closed here by carrying
> only integration-seam and cost facts forward, and by the
> [Replan trigger](#replan-trigger) instructing the implementation molecule to treat `.4` as a
> **cost input, never a design source**.

______________________________________________________________________

## ⚠️ One acceptance criterion is deliberately deferred, not missed

The filed acceptance for this bead says: *"On any GO: `/bh:replan` run on this epic and the
implementation molecule filed."* **That step was consciously NOT performed.**

**Why.** The repository owner is reviewing this molecule's PR manually before it merges, and the
verdict below may change under that review. Filing an implementation molecule off an unreviewed
verdict produces orphan beads pointing at conclusions that no longer hold — which is the same
class of failure this whole molecule exists to prevent, only one level up.

**What replaces it.** The [Replan trigger](#replan-trigger) section states precisely what the
follow-on planning session must file: which cells, in what order, with what scope boundary, and
what must explicitly *not* be filed. It is written to be a mechanical read rather than a
re-derivation. Every other acceptance criterion is met in full.

______________________________________________________________________

## 0 — The licence filter, applied FIRST

`phaze-ytgo.5` runs before technical merit by design: **a shape that does not survive compliance
is unavailable for every purpose regardless of how it benchmarks.** Two things must be recorded
before any cell is scored.

### 0.1 — The clean-room seal is corrected. Record this first

**The epic's `S2/S3/S4`-sealed / `S5`-unsealed rule is superseded.** `phaze-ytgo.5` V1 found it
wrong in *both* directions and replaced it. The replacement, restated:

> **THE CLEAN-ROOM RULE.** The constraint is on **what you write**, not on **what you read**,
> and it applies to *documents*, not to *agents*.
>
> **Tier 1 — INPUT (unrestricted).** Anyone in this line may read anything AudioMuse publishes,
> including its source. Reading published material is expressly licensed, and under
> 17 U.S.C. § 102(b) ideas, procedures and methods of operation are outside copyright
> *"regardless of the form in which it is described"*. **The prohibition on reading prose is
> lifted.**
>
> **Tier 2 — OUTPUT (binding, and it is the whole seal).** Any document a future phaze
> implementation could read must contain **no AudioMuse expression**: no source in any quantity;
> no AudioMuse identifier proposed for adoption in phaze; no literal parameter value transcribed
> without an **independent citation** (a paper, a library's own docs, or a phaze measurement);
> no AudioMuse module decomposition proposed as phaze's.
>
> **Enforcement.** Every spike and design doc in this line carries a clean-room disclosure
> stating what it read and affirming it reproduces no AudioMuse expression. A contemporaneous,
> version-controlled, signed-commit record is worth considerably more than an unverifiable claim
> that nobody looked.

**Why the old rule was wrong.** It reasoned *reading source contaminates the reader, so
quarantine readers* — the trade-secret model, where the defendant's problem is access they were
not entitled to. AudioMuse's source is **published under a licence granting everyone the right
to read it**, so there is no unauthorised access to disprove. What a clean-room buys here is
**evidentiary**, not permissive. Naming the purpose correctly relocates the constraint from the
input side to the output side, which is what makes it enforceable.

### 0.2 — The `S`-number hazard, called out because it has already misled a reader

**The epic's design block is stale on the seal and carries a superseded `S`-number-to-bead-number
mapping.** `S4` is bead `.6`; `S5` is bead `.4`. An agent that reads the epic and matches the
digit unseals the **wrong** bead — it would seal the sidecar spike (which is the one bead
permitted to read source) and unseal the vector spike (which is sealed).

The dispatcher has since prepended a correction table to the epic's design block, but the
superseded seal text remains beneath it and the `S`-numbers persist in every spike's title line.

> **Rule for everything downstream of this document: refer to beads by bead id only
> (`phaze-ytgo.4`). Never by `S`-number.** The `S`-numbers are planning handles and they do not
> correspond to bead ids.

### 0.3 — Which shapes survive the filter

| Shape | Licence verdict | Condition |
| ----- | --------------- | --------- |
| **Clean-room native implementation in phaze** | **SURVIVES** | No DCLAP weights; permissive CLAP only if CLAP is used at all. **Licence-free by construction** — no `NOTICE`, no `LICENSE` change, no attribution obligation. This is a real and underrated advantage of the clean-room column. |
| **Sidecar — operator pulls the upstream image, phaze *references* it** | **SURVIVES** | Run **unmodified**; reference, never rebuild-and-publish; courtesy `NOTICE` + README line recommended. A compose file naming an upstream image is **not conveying** — the compose file can ship. |
| **Sidecar + a phaze-written Subsonic endpoint** | **SURVIVES** | Written from the **published Subsonic spec**, never adapted from AudioMuse's own adapters. The Subsonic API is a *third party's* published interface; § 102(b) and Directive 2009/24/EC Art. 1(2) both put interfaces outside protection. |
| **Sidecar — phaze builds & publishes its own AudioMuse-derived image** | SURVIVES ONLY WITH FULL AGPL COMPLIANCE | §4 notices + §6 Corresponding Source. Converts a zero-obligation shape into a permanent one. **Recommend against.** |
| **Vendoring / porting / translating AudioMuse code into `src/phaze/`** | **ELIMINATED** | AGPL §5(c): the whole work, all parts, however packaged. Irreconcilable with the binding MIT constraint. |
| **Importing AudioMuse as a Python library in phaze's process** | **ELIMINATED** | Shared address space. Same outcome as vendoring. |
| **Depending on `AudioMuse-AI-DCLAP` weights** | **ELIMINATED** | AGPL-3.0 weights, per the licensor's own repository record and AudioMuse's own `LICENSE` addendum. A permissive substitute (LAION Apache-2.0) exists at no capability cost. |
| **`microsoft/msclap` weights** | **EXCLUDED pending clarification** | Self-contradictory across the licensor's own mirrors — `ms-pl` on the model card, `cc-by-3.0-us` on the canonical Zenodo record the code repo itself links. Not resolvable by picking the friendlier answer. |

### 0.4 — What the filter does NOT do

**The licence filter eliminates no purpose.** It removes three shapes and attaches conditions to
two more. Every cell below is therefore free to be "not worth it" on technical merit — the filter
is a gate, not an endorsement.

Two further findings from `.5` that this document must carry, neither of which gates a cell:

- **`phaze` already conveys AGPL-3.0 software today, unremediated.**
  `ghcr.io/simplicityguy/phaze/panako` is anonymously pullable and contains a compiled AGPL-3.0
  Panako JAR bundling GPL-3.0 TarsosDSP, against a repository with no `NOTICE`, no third-party
  licence section, and a README that says "MIT" and nothing else. **This is the highest-priority
  action in the whole molecule's licence work and it is not about AudioMuse.** It must be filed
  as a separate P1 bead **outside** this molecule and **not blocked on this verdict**.
- **phaze's existing essentia weights are CC BY-NC-SA 4.0.** Redistribution is not triggered
  (they are runtime-downloaded per operator; no `COPY` bakes them in — that is the correct
  pattern and should be the template for every future model dependency). The **BY** condition is
  outstanding today; the **NC** condition is a ceiling on any commercial future, not a present
  breach.

______________________________________________________________________

## 1 — Two scale facts changed under the molecule's feet

`phaze-ytgo.6` was the first spike to reach the live production database. Two of the molecule's
working assumptions were estimates, and both were wrong in ways that move verdicts.

| | The molecule carried | Measured (`.6` E2, live production DB) |
| --- | --- | --- |
| Archive size | **200,000 files** (planner's figure from the epic text) | **11,428 files** today — the 200,000 is a **projection ~17.5× the current archive** |
| Multi-hour fraction | ~5 % multi-hour sets (S1's illustrative figure) | **85.9 % of files exceed 600 s; 39.4 % exceed one hour; mean duration 60 minutes** |
| `analysis_window` rows | ≤ 90/file code-derived ceiling | **71.5 rows/file** measured (54.1 fine + 17.3 coarse) over the analysed subset |

**The second row is the single most consequential fact in this document, and it cuts twice:**

1. **It is fatal to the sidecar.** AudioMuse truncates analysis at 600 s and *persists 600 as the
   track's duration*. Against a corpus that is 85.9 % over ten minutes, a sidecar would represent
   **9,808 files by their opening ten minutes** and report an identical 600 s duration for every
   one of them. This is not a tuning problem — it is what the deployment does, it was invisible
   in upstream documentation and only found by running the software, and it applies to the
   majority of what phaze exists to organise.
2. **It multiplies P3 by 7–70×.** A contiguous pass over a 60-minute-mean archive is **72.5 M
   vectors at a 10 s hop and 725 M at the native 1 Hz** at the 200,000-file target — against the
   ~1.1 × 10⁷ the molecule had been carrying.

______________________________________________________________________

## 2 — The matrix

Each purpose resolved to **exactly one** token.

| | **P1** dedup + rename | **P2** discovery / playlists | **P3** set/tracklist intelligence | **P4** archive QA |
| --- | --- | --- | --- | --- |
| **Verdict** | **insufficient evidence** | **clean-room** *(staged; stage 1 is a quality gate that can kill the cell)* | **insufficient evidence** *(one variant affirmatively closed)* | **not worth it** |
| **Shape, if it is ever built** | clean-room — the sidecar is eliminated for this purpose independently | **clean-room** — decisively, not marginally | clean-room, and only in the **boundary-detection** form | n/a — use the existing-features baseline phaze already stores |
| **What settles it** | S1's P1 bar on **≥ 50 operator-labelled** near-duplicate pairs from the real archive, with **same-genre** negatives | already committed: S1's **20-seed blind A/B with the mandatory EFB arm**, run as stage 1 before any surface ships | S1's P3 bar: **≥ 70 % of scraped 1001Tracklists tracks within ±30 s**, at ≤ 1 false segment per 10 true, **restricted to the sub-corpus fingerprinting could not identify** | nothing — the evidential burden was assigned to the embedding side by S1 and was not discharged |
| **Headline evidence** | `.2` E4: effnet is the only space with a positive hard margin (+0.0752) and the only one that expresses a remaster (0.064–0.083 vs different-work floor 0.1373) where EFB provably cannot (0.0023–0.0110 astride its floor 0.0028) — **but** R@P95 collapses **1.00 → 0.60** against same-genre negatives, and 44 of 45 positives are constructed. `.6` E7: no ANN index needed; exact all-pairs is ≈ 6 min. `.4` E7: the sidecar's only strength is the identity share phaze already owns. | `.6` E6/E10: **REAL** vectors, HNSW recall@10 **0.991 at p50 2.4 ms**; the index-free alternative measurably **fails** S1's 200 ms bar (seq scan p50 538 ms `vector`, p50 196 / p95 207 ms `halfvec`). `.2` E2: the embedding is **+1.6 %** marginal on a model phaze already runs. `.4` E5/E8: the sidecar answers this question for 14.1 % of the archive after a ~25-day ingest and a permanent shim. | `.6` verdict table: **725 M vectors ≈ 644 GB even at PCA-200 `halfvec`** against **47 GB free** — a *storage* verdict, so no alternative ANN engine rescues it. `.2` E6: the boundary signal is real (peak-to-median **8.74×** on a DJ set vs 3.12× within a track) and **never validated against ground truth**. `.4` E6: sidecar `BLOCKED` three ways. | S1: P4's value is largely reachable **today** over columns phaze already stores; even the 2-D map is a projection of a vector phaze already has. `.3` E6: PCA fits 200k × 200 in **0.11 s** vs UMAP's **189 s** plus a `numba`/`llvmlite` dependency class phaze has zero of. `.6`: P4 issues **no** interactive top-k query at all. `.4` E5: sidecar-derived outliers on phaze's most important files would be **artefacts of truncation**. |

### 2.1 — There are no `hybrid` cells, and that is a finding

The epic hypothesised a hybrid explicitly — *"sidecar as a fast proving ground for P2,
clean-room for P3"*. **Measurement refutes it in both halves.**

- **The sidecar is not a fast proving ground.** A production shim is 1–2 weeks plus *permanent,
  unbounded* maintenance (it tracks another project's internal client code, not a spec), the
  initial ingest is ~25 days of continuous running that also moves the entire archive
  byte-for-byte over HTTP, and it costs a 5.36 GB image and ~2.4 cores competing with phaze's own
  analysis workers. Against that, the clean-room path's production side is a **~10-line change at
  +1.6 % wall clock** on a model already loaded in the same process. **The "quick proof" is
  slower than the real thing.**
- **And it would prove the wrong thing.** The 600 s wall restricts a sidecar's honest scope to
  files ≤ 10 minutes — **14.1 % of this archive**. A proving ground that cannot speak to 85.9 %
  of the corpus does not de-risk the decision it exists to de-risk.

The staging discipline the criterion asks of a hybrid cell is therefore applied to **P2's
clean-room cell** instead, in [§3.2](#32--p2-discovery--playlists--clean-room-staged), where
it does real work.

______________________________________________________________________

## 3 — Cell by cell

### 3.1 — P1, dedup + rename — `insufficient evidence`

**First, split the purpose, per S1's binding rule 4.** P1 is two different problems wearing one
name:

- **The identity share** — same recording, different encode/bitrate/container/trim — is
  **`REDUNDANT`**. phaze already owns audfprint and Panako, and `combined_query` has exactly
  **one** consumer in the entire codebase (`tasks/scan.py`'s `scan_live_set`). phaze owns a
  same-recording matcher and does not use it for the same-recording problem. Every arm that
  measured the identity share confirmed it is easy: EFB matches effnet at R@P95 = 1.00 against
  diverse negatives, and the sidecar separates encodes at cosine 0.00007–0.001 vs 0.050–0.140.
  **None of that is an argument for embeddings** — two encodes of one track produce near-identical
  *anything*.
- **The resemblance share** — live vs studio rendition, differently-mastered rips, covers,
  remixes — is the only in-scope part, and it is where the evidence runs out.

**Why not `clean-room`.** There is one genuine, clearly-measured capability delta: effnet places
all four constructed remasters at 0.064–0.083, entirely below its different-work floor of 0.1373,
while EFB places them at 0.0023–0.0110, straddling its floor of 0.0028 — i.e. under EFB one
remaster is *closer to its original* than some genuinely different works are to each other. A
differently-mastered rip is named explicitly in S1's resemblance column, and **EFB cannot express
it while effnet can.** That is real. It is also the entire positive case, and it rests on four
constructed pairs.

**Why not `not worth it`.** Against that, three disqualifiers, none of which is a judgement call:

1. **The provenance bar is not met.** S1 requires ≥ 50 **operator-labelled** pairs. `.2` had 45,
   of which **44 are constructed** and exactly **one** (`W5`↔`W6`) is a naturally-occurring
   same-work-different-rendition pair. No operator labelled anything.
2. **The negative set that matters is the one it fails on.** Against class F — genuinely
   different tracks from the same set, sharing style, production era, master and encoder — recall
   at precision 0.95 falls from **1.00 to 0.60**. phaze's archive is heavily one genre family,
   i.e. far more F-like than diverse. The sidecar spike independently found the same shape:
   wholly unrelated electronic tracks at **0.86–0.95 cosine similarity**.
3. **The corpus cannot locate the truth.** Six distinct works. `.2` is explicit that its two
   confounds pull in opposite directions and that this corpus cannot resolve them.

**The measurement that settles it — name it precisely so it can be filed as one bead:**

> Assemble **≥ 50 operator-labelled near-duplicate pairs drawn from the real archive** — live vs
> studio renditions, differently-mastered rips, covers, remixes — explicitly **excluding**
> `sha256`-identical files and encode-only pairs, since both are degenerate positives that any
> method finds and both belong to the identity share. Draw the negative set from **same-genre,
> same-era** files, not across genres. Score effnet track vectors and the **EFB arm** against
> S1's bar: **recall ≥ 0.90 at precision ≥ 0.95**. Precision outranks recall because a false
> positive writes a `DedupResolution` marker that hides a real file; 0.95 rather than 0.99
> because `undo_resolve` makes it reversible.

**Storage and retrieval are not the obstacle and must not be cited as progress.** `.6` settled
that P1 **needs no ANN index at all** — its shape is offline all-pairs candidate generation over
200,000 track vectors, measured at **≈ 6 minutes** exact, against 1.39 GB of `halfvec` (3 % of the
production host's free disk). If an index exists for P2, P1 rides it for free; if it does not, P1
does not care. The cell is gated entirely on quality.

**The sidecar is eliminated for P1 independently of the above**: its only demonstrated strength is
the identity share phaze already owns; its own dedup does not collapse even a byte-identical file,
so it adds nothing there; and for any file over ten minutes the comparison runs over its first ten
minutes only.

**The largest available P1 win is not in this molecule.** Wiring the existing fingerprint engines
into the dedup surface needs no embeddings, no ANN index, no new dependency, no licence analysis
and no verdict from this bead. It should be filed separately and must not be blocked on this
molecule. See [§6](#6--boundary-against-phaze-vprd-and-the-fingerprint-services).

### 3.2 — P2, discovery / playlists — `clean-room` (staged)

**This is the one cell where the shape question is decisively answered, and it is answered in
clean-room's favour on every axis measured.**

| axis | clean-room | sidecar |
| ---- | ---------- | ------- |
| Production cost | **+1.6 % wall clock, +0.75 % RSS** on `discogs-effnet-bs64-1`, the model phaze already loads and already runs on every coarse window. O(1) per file by construction — it rides the coarse tier, already capped at 30 windows | **~20–26 s per track**, ≈ **25 days** of continuous running for a full initial ingest, plus moving the entire archive byte-for-byte over HTTP through a shim |
| New components | one column + one index. **No new dependency, no new model file, no new service** | Flask + worker containers (**5.36 GB image**), ~500 MiB idle / ~950 MiB peak RAM, **~2.4 cores** competing with phaze's own analysis workers, plus a permanently-maintained Subsonic shim |
| Corpus coverage | full — the embedding is computed over phaze's own strided coarse tier, whose measured coarse coverage is **97.5 %** | **14.1 %** — the 600 s wall makes every one of the 9,808 files over ten minutes a lie |
| Retrieval | measured on **REAL** production embeddings: recall@10 **0.991 at p50 2.4 ms / p95 3.2 ms**; 1.39 GB `halfvec` + HNSW; **95 s** build; incremental insert at 4.6 ms/row, so **phaze never rebuilds** | ranked neighbours demonstrated off their in-memory IVF, keyed on phaze UUIDs — mechanically fine, but computed over opening ten minutes for 85.9 % of the archive |
| Infrastructure | pgvector is a **3-line Dockerfile, +31 MB** (425 → 456 MB), staying Alpine and `postgres:18`. S1's blocker **B1** is *downgraded, not cleared* — see the cost note below | no second Postgres needed (verified on phaze's pinned `postgres:18-alpine`), Redis shareable on a dedicated logical DB. This is a genuine positive and the sidecar should not be penalised for a cost it does not have |

**P2 is also the only purpose that genuinely needs the ANN index**, and the index-free floor
*fails* the bar rather than merely underperforming: an exact Postgres sequential scan at
200,000 × 1280-d answers at **p50 538 ms** (`vector`) or **p50 196 / p95 207 ms** (`halfvec`)
against S1's p95 ≤ 200 ms search-as-you-type budget — versus **p50 4.3 ms** with the index. That
is the difference between a palette and a spinner.

#### Staging — and stage 1 is a gate that can kill this cell

**The quality question is open and must not be laundered.** S1's P2 bar is a 20-seed blind A/B
with a **mandatory** EFB arm, and EFB is a *real, usable* P2 baseline: "more like this" as
style + BPM window + key compatibility is a handful of SQL predicates over columns the search
layer already filters on, at zero cost. **Any embedding proposal must beat that, not merely
work.** No sibling ran that arm; `.2` and `.4` both scored P2 `UNMEASURED` and said so plainly.

| | **Stage 1 — the quality gate** | **Stage 2 — the browse surface** |
| --- | --- | --- |
| **What lands** | Embedding extraction and storage only: the second-pass shape `TensorflowPredictEffnetDiscogs(output="PartitionedCall:1")` on the buffer `_run_model_sets` already holds; two-level mean-pool (patches → window, windows → track); persisted as **`halfvec(1280)`** on the per-file row and on the **coarse** window rows. Plus the pgvector infrastructure change. **No user-visible surface.** | A new `STAGE_PARTIALS` rail node + workspace + router for similar-track browsing / clustering / playlists, following the existing scaffold. All 14 existing rail nodes are pipeline stages; none is a browse surface, so this is genuinely new. |
| **What it proves** | S1's 20-seed blind A/B: 20 seed tracks, top-10 from each method presented unlabelled, operator marks each "belongs in a playlist with the seed". Bar: **mean precision@10 ≥ 0.6 AND strictly better than the EFB arm.** Roughly a half-day of an operator's time against a populated archive. | nothing — stage 2 is delivery, not evidence |
| **Trigger for stage 2** | **The A/B clearing the bar.** If it does not clear, stage 2 is not filed and P2 flips to "not worth it — use EFB". | — |
| **Why stage 1 is worth landing even if the gate fails** | The same column is the *prerequisite* for closing **P1's** and **P4's** accuracy questions too. One bead unblocks three cells' measurements. It is also small: 1.39 GB, 3 % of free disk, a 95-second index build, and no new dependency. | — |

#### Free-text search is a separate, deferrable sub-capability — do not fold it into this cell

`.3` established that CLAP is **cleanly separable**, and that its *only* non-redundant
contribution is free-text/natural-language query — nothing else in phaze's stack, existing or
proposed, can do that, and EFB cannot fill the gap by any recombination of its columns. Its
mood/"other-features" role is **`REDUNDANT`**: AudioMuse derives those labels from CLAP text
similarity because its own embedding model does not ship purpose-built mood classifiers; phaze
already has six, integrated, in the `features` JSONB. **AudioMuse did not find CLAP superior for
mood — it built one out of CLAP because it lacked phaze's alternative.**

The cost is real and asymmetric: **~600 MB of new dependency** (`torch` + `transformers`; `torch`
alone is 476 MB installed), because phaze's existing TensorFlow footprint is a private,
essentia-only inference runtime that **cannot** host a CLAP checkpoint, and no production-ready
ONNX CLAP artifact exists to run on the lighter `onnxruntime` path (the five community exports
found all sat at zero downloads with no licence tag). Against that, the query-side cost is
trivial: **0.03–0.04 s** per text embed, comfortably inside S1's 200 ms budget.

**Verdict on free-text: file it as its own separately-scoped bead, and let it be deferred
indefinitely.** A GO or NO-GO on it must never gate P1, P3, P4, or the audio-similarity half of
P2. If it is ever built, the licence-clear path is `laion/clap-htsat-unfused` (Apache-2.0) —
which sits on **strictly cleaner terms than phaze's own existing CC BY-NC-SA 4.0 essentia
weights**.

#### Operational cost the implementation must not discover late

S1's blocker **B1** said pgvector requires a base-image change. `.6` confirmed the fact
(`vector` is unavailable in `postgres:18-alpine`, in the harness *and* in production) and
**corrected the conclusion**: Alpine ships the extension, it just installs to the wrong prefix,
and a 3-line Dockerfile fixes it for +31 MB. But B1 is **downgraded, not cleared** — it is still
four coordinated edits: the new Dockerfile, **nine** `postgres:18-alpine` pins across
`docker-compose.yml` (1) and `justfile` (8) that must move together or harness and production
diverge — **four of the justfile eight are `echo` strings**, so a partial bump prints the old tag
while running the new one — a
CI workflow change (**GitHub Actions service containers cannot be built, only referenced**, which
genuinely forces phaze to publish its own Postgres image), and an Alembic migration whose first
statement is `CREATE EXTENSION`.

And one operational note that `.6` calls its highest-value: **build memory is the binding
constraint, not query latency.** Every failure in that spike was a build-side resource failure,
and all of them are invisible at today's 11,428-file scale. `maintenance_work_mem` must exceed
`N × index-bytes-per-row`, and `/dev/shm` must be at least as large again — phaze's compose file
sets no `shm_size` today, so any HNSW build with a non-trivial `maintenance_work_mem` will fail
on a stock deployment until it does. Getting this wrong does not produce a slow index; it
produces a build that fails outright, dies on `/dev/shm`, or runs so long it looks hung.

### 3.3 — P3, set/tracklist intelligence — `insufficient evidence` (one variant affirmatively closed)

P3 is two proposals, and they must be scored separately because one is **closed** and the other
is **open**.

#### Closed, affirmatively: P3 as a stored contiguous vector tier

**This is a NO, not an absence of evidence, and it is a *storage* verdict.**

- **Sidecar: `BLOCKED`, three independent ways.** Patch embeddings are averaged into one
  per-track vector; the storage schema has **no time-bearing column anywhere in its 27 tables**;
  and the 600 s wall forecloses it regardless. The one workaround — phaze fanning a long set out
  into synthetic per-window "tracks" so the timestamp comes from phaze rather than from AudioMuse
  — is not structurally forbidden but is priced at ~1.1 × 10⁷ analyses ≈ **3.8 years** on the
  measured hardware. **P3-via-sidecar is closed.**
- **Clean-room, as a stored tier: unaffordable at the target scale.** Both of the objections the
  molecule carried died on measurement, and it did not help. S1's blocker **B2** (per-file O(1)
  cost) is priced at **0.193 s of inference per audio-minute**; and `.2`'s decode worry inverted
  outright — a single sequential decode of a 6-hour file took **19.9 s** against the **259 s**
  `.2` measured for *one* 600 s deep-seek excerpt from the same file, so a contiguous pass is
  **13× cheaper on the decode axis than what phaze already does**. What replaced them is bytes:
  at the 200,000-file target a native 1 Hz pass is **725 M vectors ≈ 644 GB even at PCA-200
  `halfvec`**, against **47 GB free**; a 10 s hop is still ~64 GB.

> **Say this explicitly because it is the finding that protects the epic's headline question:
> that failure is not a pgvector failure.** 725 M × 1280 float32 is 3.7 TB of raw vectors before
> any index exists. Swapping pgvector for AudioMuse's disk-paged IVF, or for any other engine,
> does not change that arithmetic. **The contiguous-tier problem is "how many vectors do we
> choose to keep", not "which index".** For every tier phaze can afford to store, pgvector is
> sufficient, and a separate ANN component buys nothing — so **nothing in this molecule erodes
> clean-room's "one stack" advantage.**

**A scale caveat the implementation molecule must not misuse.** The 644 GB figure is against the
**200,000-file projection**, which is ~17.5× today's archive. Multiplying `.6`'s measured, flat
per-row costs by today's 41.4 M audio-seconds (D1 arithmetic over `.6` E6's constants, not a new
measurement):

| tier, at **today's** 11,428 files | rows | representation | ≈ total | vs 47 GB free |
| --- | --- | --- | --- | --- |
| contiguous, 10 s hop | 4.14 M | 1280-d `halfvec` + IVFFlat | ~23 GB | 49 % |
| contiguous, 10 s hop | 4.14 M | PCA-200 `halfvec` + IVFFlat | ~3.7 GB | 8 % |
| contiguous, native 1 Hz | 41.6 M | PCA-200 `halfvec` + IVFFlat | ~37 GB | 78 % |

So a 10 s-hop tier fits **today**, even at full dimensionality with no PCA quality loss. **This
does not reopen the cell**, for two reasons: the quality bar is unmeasured and that is what gates
P3, not storage; and building a tier that fits at 11,428 files and is 17.5× over at the stated
target is exactly the trap `.6`'s build-memory finding warns about — every one of its failures
was invisible at small N. Record it as a sizing fact, not a permission.

#### Open: P3 as boundary detection only

This is a materially different and probably affordable proposition, and it is the form a P3 bead
should be planned around: **compute the 1 Hz novelty curve during analysis and persist only the
detected boundaries** — and perhaps one vector per detected segment — rather than a vector per
second. Storage collapses from O(duration) to O(tracks-in-set).

The signal exists and points the right way: a 30 s-lag novelty function over the native 1 Hz
patch series gave **peak-to-median 8.74×** on a real hardcore countdown with frequent track
changes, against **3.12×** within a single continuous track — a genuine 2.8× contrast in the
expected direction.

**It has never been validated against ground truth, and P3 is the only purpose in this molecule
that has real ground truth available.** Neither long set in `.2`'s corpus had a scraped
tracklist.

**The measurement that settles it:**

> Run the 1 Hz novelty curve over files that carry `Tracklist.source='1001tracklists'` rows and
> score the detected peaks against `TracklistTrack.timestamp`. Bar (S1): **≥ 70 % of scraped
> tracks located within ±30 s**, at **≤ 1 false segment per 10 true**. ±30 s is
> `analysis_fine_window_sec`, the finest temporal resolution anything else in phaze commits to.
>
> **Two constraints on how it is run, both load-bearing:**
>
> 1. **Restrict the corpus to the sub-set where fingerprinting returned nothing.** Otherwise the
>    measurement scores `phaze-vprd`'s identity win as this molecule's resemblance win — see
>    [§6](#6--boundary-against-phaze-vprd-and-the-fingerprint-services). S1's P3 resemblance
>    share is narrow and specific: segmenting sets whose constituent tracks are **not** in the
>    fingerprint DB, and corroborating a scraped ordering where fingerprinting found nothing.
> 2. **It stores nothing.** The curve is computed and discarded, so the measurement costs only
>    CPU (0.193 s per audio-minute) and carries none of the storage verdict above. It is
>    therefore cheap, and it is the only measurement in this molecule with objective ground
>    truth rather than an operator's judgement.

**And a standing caveat that survives every P3 variant:** the *existing* `analysis_window` rows
cannot serve P3 in any form. `.6` measured coarse coverage at a reassuring 97.5 %, so S1's
striding objection is much weaker than feared — but its **other** leg stands untouched: the
coarse tier is **180 seconds**, longer than most tracks in a DJ set, so a coarse window straddles
two to four tracks and its labels are a blend of them. Coverage was never the binding problem;
window length is.

### 3.4 — P4, archive QA — `not worth it`

**Read this as S1's rubric intends it: `REDUNDANT` means "do the clustering on what we already
store", never "do nothing".**

phaze already persists, per file *and* per coarse window: `bpm`, `musical_key`, `mood`, `style`,
`danceability` (window-only), and a `features` JSONB reducible to an **11-dimensional
interpretable score vector** plus a 10-of-400 genre distribution. The classic archive-QA finds —
a file tagged *Techno* whose classifier says *Blues*, a 40 BPM "House" track, a file whose
per-window style ribbons are incoherent — are all reachable with SQL or scikit-learn over
existing columns. Even the 2-D map is a projection of a vector phaze already stores.

**Why this is a resolvable NO and not another `insufficient evidence`.** The asymmetry with P1 is
principled and worth stating, because both cells are formally "unmeasured against EFB":

- **P1 has positive evidence of a specific gap.** EFB provably *fails* on a named sub-case
  (remasters, where it places a remaster closer to its original than some different works are to
  each other) and effnet provably *expresses* it. The question is only how far that generalises.
- **P4 has none.** No sibling produced any P4 sub-case where EFB fails. S1 assigned the
  evidential burden explicitly — *"treat P4 as `REDUNDANT` unless a sibling shows a measured
  delta over EFB"* — and instructed D1 to apply that default. Three siblings scored P4
  `UNMEASURED` or `REDUNDANT`; **none discharged the burden.**

Every other axis points the same way:

- **P4 needs no ANN index at all.** The map surface reads a precomputed 2-D projection — a
  two-column bounding-box read, not a vector search. Outlier detection is a batch job. **Nothing
  in P4 issues an interactive top-k query.**
- **UMAP is not warranted.** PCA fits 200,000 × 200 in **0.11 s** (sklearn) or 4.6 s (pure numpy,
  zero new dependencies) against UMAP's **189 s**, and PCA's out-of-sample update is exact and
  sub-millisecond against UMAP's ~8 s first-call JIT warm-up. `umap-learn` pulls five packages
  transitively including `numba` and `llvmlite` — a JIT-compiler dependency class phaze has zero
  of today. UMAP's real advantage (non-linear cluster separation) is unquantified and stacked on
  top of the molecule's weakest cell.
- **CLAP is `REDUNDANT`-to-`worse` for P4** — it would add ~600 MB of dependency to re-derive a
  mood signal phaze's own classifiers already deliver directly.
- **The sidecar is actively harmful here.** It serves 2-D map coordinates for free, but for
  85.9 % of the archive those coordinates derive from the file's first ten minutes — so the
  outliers it surfaces on phaze's most important files would be **artefacts of truncation**, not
  findings. An archive-QA tool whose flags are manufactured by its own truncation is worse than
  no tool.

**What "not worth it" does and does not license.** It licenses: *do not build an
embedding-backed, CLAP-backed, UMAP-backed or sidecar-backed P4.* It does **not** say archive QA
is worthless. If Robert wants EFB-based outlier detection and a PCA map, that is a **separate,
ordinary phaze feature** over columns that already exist — it is neither of this molecule's two
shapes, and it must be filed through normal planning, **not** off this epic. See
[Replan trigger](#replan-trigger).

One inexpensive door is left open by P2's stage 1: once the embedding column exists, "does an
embedding beat EFB for P4 outlier yield?" becomes a cheap follow-up question (sample 100 flagged
outliers from each arm, bar ≥ 30 % genuinely mis-tagged and better yield than EFB) rather than a
project. It is not filed here, and it must not be.

______________________________________________________________________

## 4 — Ranking by value-to-effort

### 4.1 — GO cells

Exactly one cell is GO, so the ranking is short — but the ordering *within* it matters.

| # | Item | Value | Effort | Why here |
| - | ---- | ----- | ------ | -------- |
| **1** | **P2 stage 1** — embedding extraction + `halfvec(1280)` storage + pgvector infrastructure, then S1's 20-seed blind A/B with the EFB arm | **Highest in the molecule.** It is the only thing that converts *three* cells (P1, P2, P4) from unmeasured to decided, and it is the gate on the one GO | **+1.6 %** analysis cost, ~10 lines against `_run_model_sets`, one column, one index, 1.39 GB, a 95 s build — plus the pgvector infrastructure change (four coordinated edits, `shm_size`, a published Postgres image for CI) | Cheapest possible path to the most decisions. **Nothing else in this molecule should be filed before it.** |
| **2** | **P2 stage 2** — the browse / similar-tracks / playlist surface | The purpose Robert actually asked for, and the only one that needs the ANN index | New rail node + workspace + router, following the existing scaffold | **Gated.** Not filed until stage 1's A/B clears mean precision@10 ≥ 0.6 *and* beats EFB. |

### 4.2 — Gated measurements — NOT GO cells, and not implementation

Ranked by value-to-effort. Each converts an `insufficient evidence` cell into a decision. **None
of these authorises building the capability it measures.**

| # | Measurement | Converts | Effort | Note |
| - | ----------- | -------- | ------ | ---- |
| **1** | S1's 20-seed blind A/B, EFB arm mandatory | P2 stage 1 → stage 2 | ~half a day of operator time, once stage 1 lands | Already inside item 1 above; listed for completeness |
| **2** | P3 boundary-detection accuracy vs scraped 1001Tracklists, ±30 s, restricted to the fingerprint-negative sub-corpus | P3 | CPU only (0.193 s/audio-minute), **stores nothing**, objective ground truth already in the database | The only bar in this molecule that does not need an operator's judgement. Cheapest honest decision available. |
| **3** | P1 accuracy on ≥ 50 operator-labelled archive pairs with same-genre negatives | P1 | Operator labelling is the cost; needs stage 1's column | The labelling, not the compute, is the expensive part |

### 4.3 — Adjacent work this molecule unblocks but does not own

Both outrank several items above on value-to-effort and **neither is blocked on this verdict**:

| Item | Why it is not this molecule's |
| ---- | ----------------------------- |
| **Wire the existing fingerprint engines into the dedup surface.** `combined_query` has exactly one consumer, `scan_live_set`. phaze owns a same-recording matcher and does not use it for the same-recording problem. Likely the **largest available P1 improvement**, needing no embeddings, no ANN index, no new dependency and no licence analysis | It is the identity share, which S1 scores `REDUNDANT` against this molecule. File separately; do not bundle it into an AudioMuse verdict and do not block it on one |
| **The Panako licence remediation** — `NOTICE` / `THIRD-PARTY-LICENSES.md`, a README third-party section, §6 Corresponding Source for the published image (publishing a source tarball per tag is the cheap, unambiguous option), and a CI guard that fails when a new image-matrix entry appears without a `NOTICE` entry | phaze is conveying AGPL-3.0 software today. This is a present-tense obligation with no dependency on any AudioMuse decision. **P1 priority, separate bead, not blocked** |

______________________________________________________________________

## 5 — Cells and capabilities that must NOT be built

Stated affirmatively so the implementation molecule cannot acquire them by drift.

| # | Do not build | Reason |
| - | ------------ | ------ |
| **1** | **Any AudioMuse sidecar, for any purpose** | The 600 s wall against an archive that is **85.9 % over ten minutes**: 9,808 files represented by their opening ten minutes, all reporting an identical 600 s duration — which also makes AudioMuse's own duration-agreement identity check degenerate across the entire set collection. Plus a ~25-day ingest, a 1–2 week shim with **permanent unbounded** maintenance tracking another project's internal client code, a 5.36 GB image and ~2.4 cores. P3 additionally `BLOCKED` three ways |
| **2** | **A Subsonic-compatible endpoint in phaze** | It exists only to feed a sidecar that is not being built. (If a sidecar is ever revisited: write it from the **published Subsonic spec**, never by reading AudioMuse's own adapters — that adapter-derived shim is the single highest-risk artifact the whole molecule identified) |
| **3** | **A stored contiguous vector tier for P3, at any dimensionality** | 725 M vectors ≈ 644 GB at PCA-200 `halfvec` against 47 GB free, at the stated target. A *storage* verdict — no alternative ANN engine changes it |
| **4** | **The per-window shim-fanout workaround for P3** | ~1.1 × 10⁷ analyses ≈ 3.8 years on measured hardware |
| **5** | **An embedding-, CLAP-, UMAP- or sidecar-backed P4** | `REDUNDANT` against EFB, burden not discharged; no ANN query in P4 at all; `numba`/`llvmlite` for an unquantified visual benefit on the molecule's weakest cell; sidecar-derived outliers would be truncation artefacts |
| **6** | **CLAP for mood / "other features"** | phaze already has six purpose-built binary classifiers integrated in the `features` JSONB. AudioMuse built mood out of CLAP because it lacked phaze's alternative — that is not a recommendation to copy |
| **7** | **`AudioMuse-AI-DCLAP` weights; `microsoft/msclap` weights** | AGPL-3.0 (blocking under the binding MIT constraint); and a self-contradictory licence across the licensor's own mirrors. LAION Apache-2.0 substitutes at no capability cost |
| **8** | **Vendoring, porting, translating, or in-process importing AudioMuse code** | AGPL §5(c). Irreconcilable with MIT |
| **9** | **Embeddings on the fine tier** | The fine tier decodes at 44.1 kHz and runs no neural model at all; embeddings there are a **new** cost line, not a marginal one. Put them on the coarse tier |
| **10** | **PCA at the track tier** | It costs 16 % of the true top-10 and 22 % of the true top-1 to save 1.2 GB on a host with 47 GB free. Store `halfvec(1280)`; reduce only where reduction is the difference between fitting and not fitting |
| **11** | **A periodic index rebuild-and-hot-swap pipeline** | pgvector maintains HNSW on `INSERT` in the same transaction as the row, at 4.6 ms/row on `halfvec` — which disappears behind an essentia analysis already costing tens of seconds per file. That machinery is work phaze does not have to do |
| **12** | **A near-duplicate group-key design (S1's blocker B4)** | Depends entirely on the P1 verdict, which is `insufficient evidence`. Designing the group key before the accuracy measurement is building on an undecided cell |
| **13** | **Any adoption of AudioMuse identifier spellings, or any parameter value citing "AudioMuse uses N"** | Tier-2 output rule. Zero capability gain, gratuitous similarity — and a constant with no engineering justification is bad practice as well as bad evidence |

______________________________________________________________________

## 6 — Boundary against `phaze-vprd` and the fingerprint services

**Two different questions. Conflating them is the main way an implementation molecule wastes
effort here.**

- **IDENTITY — *"is this the same recording/performance?"*** Owned by `services/audfprint/app.py`,
  `services/panako/app.py` and `services/fingerprint.py`. **Time-localized identity across sets is
  `phaze-vprd`**, which is building an additive Panako sidecar contract carrying query/match
  start-stop offsets, time factor and frequency factor; an **uncollapsed** `segment_query()` on
  the orchestrator (`combined_query` and dedup stay untouched); a `segment_matches` table with
  canonical pair ordering; an operator-triggered SAQ scan; and a shared-segments browse page.
- **RESEMBLANCE — *"does this sound like that?"*** Nothing owns this. It is this molecule's scope.

| Purpose | Identity share — **already owned, out of scope** | Resemblance share — **this molecule** |
| --- | --- | --- |
| **P1** | Same recording, different encode / bitrate / container / trim. audfprint + Panako do exactly this, and `combined_query` is wired to nothing but tracklists | Live vs studio rendition of the same work; differently-EQ'd or differently-mastered rips; covers and remixes — all of which degrade or defeat fingerprinting |
| **P2** | — none — | all of it |
| **P3** | **Most of it.** Locating a known track inside a set at an offset is precisely Panako and precisely `phaze-vprd`. The uncollapsed repeat-occurrence path is a known limitation `phaze-vprd` already owns | Narrow and specific: **(a)** segmenting a set when its constituent tracks are **not** in the fingerprint DB (unreleased IDs, live edits, mashups); **(b)** corroborating a scraped 1001Tracklists ordering where fingerprinting returned nothing |
| **P4** | — none — | all of it |

**Four non-duplication rules, binding on any implementation molecule filed off this epic:**

1. **Do not build a second uncollapsed matcher.** `phaze-vprd` owns `segment_query()`.
2. **Do not add time-offset columns that duplicate `segment_matches`.** A boundary-detection P3
   emits **boundaries** — segment starts within one file — not **identities** or cross-file pair
   offsets. Different table, different meaning.
3. **Consume `phaze-vprd`'s output as a prior; do not re-derive it.** Where fingerprinting already
   located a track inside a set, resemblance has nothing to add.
4. **Restrict P3's accuracy measurement to the fingerprint-negative sub-corpus.** Scoring
   boundary detection over tracks Panako already located measures identity and reports it as
   resemblance — the exact false positive S1 warned about. *"A P3 win that is really cross-set
   identity belongs to `phaze-vprd`."*

And symmetrically, for P1: **a P1 win that is really fingerprinting is a false positive for this
molecule.** The highest-value P1 action available today — wiring the existing engines into the
dedup surface — is a different bead, not an AudioMuse verdict, and must not be blocked on one.

______________________________________________________________________

## Replan trigger

**This section replaces the acceptance criterion's `/bh:replan` step**, which was deliberately
deferred (see the notice at the top). It is written so the follow-on planning session performs a
mechanical read rather than a re-derivation. **Do not re-litigate the matrix; file what is below.**

### Precondition

**Do not run this replan until Robert's manual review of this molecule's PR has landed and the
matrix is confirmed.** If review changes any cell, re-read this section — it is keyed to the
verdicts above, not to a fixed bead list.

### File — in this order, as one molecule off `phaze-ytgo`

| # | Bead | Scope | Depends on |
| - | ---- | ----- | ---------- |
| **1** | **pgvector infrastructure** | The 3-line `docker/postgres/Dockerfile` with a pinned `=0.8.1-r0` (or a source build per **S4-O5**); repoint `docker-compose.yml` and the **eight** `postgres:18-alpine` pins in `justfile` (lines 234, 241, 414, 420, 424, 430, 777, 780 — **four of them `echo` strings**, which is why a partial bump is invisible in the logs) together, and see `phaze-tcqq`, which fixes the scattered-pin hazard on its own; publish the image to GHCR so the CI service container can reference it; add `shm_size` to the postgres service; one Alembic migration whose first statement is `CREATE EXTENSION IF NOT EXISTS vector`. Carry `.6` E9's build-memory formula into the operational docs | — |
| **2** | **Embedding extraction + storage** | Second-pass `TensorflowPredictEffnetDiscogs(output="PartitionedCall:1")` on the buffer `_run_model_sets` already holds. Two-level mean-pool: patches → window, windows → track. Persist as **`halfvec(1280)`** on the per-file analysis row and on the **coarse** window rows. Store **raw, unstandardised** vectors — standardisation is corpus-dependent and computing it at query time keeps the stored vector stable as the archive grows. HNSW at `m=16, ef_construction=64`, start `hnsw.ef_search=100`. **No user-visible surface in this bead** | 1 |
| **3** | **P2 quality gate (measurement bead)** | S1's 20-seed blind A/B with the **mandatory** EFB arm. Bar: mean precision@10 ≥ 0.6 **and** strictly better than EFB. Record the result on the bead. **This bead's outcome decides whether bead 4 is ever filed** | 2 |
| **4** | **P2 browse surface** — *file this bead as blocked, or do not file it at all until bead 3 passes* | New `STAGE_PARTIALS` rail node + workspace + router for similar-track browsing / clustering / playlists. Bounded and paged — the house rule forbids rendering an unbounded row set inline, so no 200k-point inline scatter | 3, **and only on a pass** |

### File separately — NOT in this molecule, and not blocked on it

| Bead | Why separate |
| ---- | ------------ |
| **Wire the existing fingerprint engines into the dedup surface** | The identity share; needs no embeddings, ANN index, dependency or licence analysis. Likely the largest available P1 win. Filing it inside an AudioMuse molecule would make it hostage to a verdict it does not depend on |
| **Panako licence remediation** (P1 priority) | A present-tense conveying obligation in phaze's tree today. Zero dependency on any AudioMuse decision |
| **P3 boundary-detection accuracy measurement** | It is a **spike**, not implementation, and it gates a cell that is `insufficient evidence`. Filing it inside the implementation molecule would imply P3 has a GO. It does not. Scope: novelty curve over `Tracklist.source='1001tracklists'` files, restricted to the fingerprint-negative sub-corpus, scored at ±30 s against `TracklistTrack.timestamp`, bar ≥ 70 % at ≤ 1 false segment per 10 true. **Stores nothing** |
| **P1 accuracy measurement** | Same reasoning. Needs bead 2's column plus ≥ 50 operator-labelled archive pairs with same-genre negatives |
| **CLAP free-text search** | `.3`'s separability finding: it must never gate any other purpose. Deferrable indefinitely. If ever built: `laion/clap-htsat-unfused` (Apache-2.0) |

### Do NOT file

1. **Any bead for P4.** It is "not worth it". If Robert wants EFB-based archive QA + a PCA map, it
   is an ordinary phaze feature over existing columns and belongs in normal planning, **not** off
   this epic.
2. **Any bead for a sidecar, a Subsonic shim, or a media-server integration**, for any purpose.
3. **Any bead for a stored contiguous P3 vector tier**, at any dimensionality or hop.
4. **Any bead for UMAP, CLAP-for-mood, or a near-duplicate group-key design.**
5. **Any P2 surface bead that is not gated on bead 3's result.** This is the one that will be
   tempting, and it is the one the whole molecule exists to prevent.

### Scope boundary to restate in every filed bead

- The four non-duplication rules in [§6](#6--boundary-against-phaze-vprd-and-the-fingerprint-services).
- The Tier-2 output rule from [§0.1](#01--the-clean-room-seal-is-corrected-record-this-first):
  **`phaze-ytgo.4` is a cost input, never a design source.** No AudioMuse identifier, no
  transcribed constant, no imported module decomposition. Every numeric constant must cite a
  paper, a library's own documentation, or a phaze measurement.
- Refer to beads by **bead id only**, never by `S`-number.

### If the review flips a cell

- **P2 → not worth it:** file nothing. Record an ADR under `docs/design/` capturing why, so the
  question is not silently reopened later.
- **P1 or P3 → GO:** they still route through their measurement beads first. Neither has an
  accuracy bar that has been run, and a GO that skips the measurement is exactly the failure
  [§2](#2--the-matrix) is built to prevent.

______________________________________________________________________

## 7 — What remains unmeasured, carried forward

Every cell above rests on this list, so it is stated rather than buried. **The feasibility
questions closed; the quality questions did not.**

`phaze-ytgo.6` says it plainly and it is worth repeating verbatim in spirit: its per-purpose
`SERVES` tokens are verdicts on **storage-and-retrieval feasibility only**, and D1 must not read
them as quality evidence. **A capability that stores and retrieves perfectly and recommends badly
still fails S1's bar.**

| # | Open question | Gates | Who closes it |
| - | ------------- | ----- | ------------- |
| **Q1** | P2 quality: embedding vs EFB, 20-seed blind A/B | **P2 stage 2** — the one GO | Operator + replan bead 3 |
| **Q2** | P3 boundary accuracy vs scraped tracklists at ±30 s | **P3** entirely | The P3 measurement spike |
| **Q3** | P1 accuracy on operator-labelled archive pairs with same-genre negatives | **P1** entirely | Operator + the P1 measurement spike |
| **Q4** | Does effnet separability hold against homogeneous same-genre negatives at archive scale? | P1's realism — R@P95 fell 1.00 → 0.60 on the proxy, whose confounds pull both ways | Same as Q3 |
| **Q5** | **S4-O3** — is 47 GB free on a 98 %-full 1.7 TB volume the real production budget, or an artifact of a disk that will be cleaned? | Moves more of the P3 arithmetic than any other single fact. If the volume grows, tiers currently ❌ become reachable **without** PCA's 16 % quality loss | **Robert** |
| **Q6** | Is the 200,000-file figure a real target, or a planner's projection? | Every P3 storage number, and whether the 10 s-hop tier that fits today is a trap | **Robert** |
| **Q7** | **S4-O1** — does binary quantization (`bit` + Hamming + exact re-rank) rescue the window tier at full dimensionality? | Not on any critical path today; would matter only if a window tier is ever wanted. ~2 hours to measure | A follow-up spike, if ever needed |
| **Q8** | **S4-O2** — window-tier build time on production hardware (14 cores, 125 GB RAM) | Nothing on the GO path; the track tier builds in 95 s well inside budget | Deferred |
| **Q9** | Counsel questions **C1–C6** from `.5` — §6(d) Corresponding Source, copyrightability of weights, CC ShareAlike over model *outputs*, the §13 single-operator position, non-US jurisdictions, the fork/exec wrapper seam | **None block the recommended shapes.** Each is either a "which cheap remedy" question or future-facing | A lawyer, at leisure — except C1, which the Panako remediation should sidestep by taking the unambiguous option |

______________________________________________________________________

## 8 — Summary for the bead close

> **P1 `insufficient evidence`** — identity share `REDUNDANT` (phaze already owns audfprint +
> Panako and has them wired only to tracklists); resemblance share has one real measured win
> (remasters, which EFB provably cannot express) but 44 of 45 positives were constructed and
> recall at precision 0.95 collapses 1.00 → 0.60 against the same-genre negatives this archive
> actually contains. Settled by ≥ 50 operator-labelled archive pairs with same-genre negatives.
>
> **P2 `clean-room`, staged** — the only GO. Clean-room beats sidecar on every measured axis:
> +1.6 % marginal cost on a model already running vs a ~25-day ingest and a permanently
> maintained shim; full corpus coverage vs 14.1 %; recall 0.991 at p50 2.4 ms on real production
> vectors, where the index-free alternative measurably fails the latency bar. **Stage 1 is the
> embedding column plus S1's 20-seed blind A/B with the mandatory EFB arm — a gate that can kill
> the cell. Stage 2, the browse surface, is not filed until that gate passes.** Free-text CLAP
> search is separable and deferrable indefinitely.
>
> **P3 `insufficient evidence`** — the stored contiguous vector tier is affirmatively **closed**
> (725 M vectors ≈ 644 GB at PCA-200 against 47 GB free — a storage verdict, so no alternative
> ANN engine rescues it; sidecar `BLOCKED` three ways and priced at 3.8 years). The
> boundary-detection form is open: the novelty signal is real (8.74× vs 3.12×) and never
> validated against the only objective ground truth in this molecule. Settled by scoring peaks
> against scraped 1001Tracklists timestamps at ±30 s, restricted to the fingerprint-negative
> sub-corpus so it cannot score `phaze-vprd`'s identity win as its own.
>
> **P4 `not worth it`** — `REDUNDANT` against the existing-features baseline. S1 assigned the
> burden of showing a measured EFB delta to the embedding side and no sibling discharged it; P4
> issues no ANN query at all; PCA suffices and UMAP is not warranted; the sidecar's outliers
> would be artefacts of its own 600 s truncation on 85.9 % of the archive. *Not* "do nothing" —
> "do it on what we already store", as an ordinary feature outside this molecule.
>
> **Licence filter:** eliminates no purpose; eliminates three shapes (vendoring, in-process
> linking, DCLAP AGPL-3.0 weights) and conditions two more. The molecule's clean-room seal is
> **corrected** — input unrestricted, output constrained — and the epic's `S`-number mapping is a
> live hazard: `S4` is bead `.6`, `S5` is bead `.4`.
>
> **Deviation:** the `/bh:replan` acceptance step was deliberately deferred pending manual review;
> the **Replan trigger** section specifies exactly what the follow-on planner must file.

______________________________________________________________________

## Inputs

| Document | Carried into |
| -------- | ------------ |
| [`phaze-ytgo.1`](phaze-ytgo.1-purpose-rubric.md) — purpose rubric | the bar, the EFB baseline, blockers B1–B4, the identity/resemblance boundary, the six scoring rules |
| [`phaze-ytgo.2`](phaze-ytgo.2-essentia-embeddings.md) — essentia embeddings | §3.1 (remaster delta, class F collapse), §3.2 (+1.6 %, mean-pool, coarse tier), §3.3 (novelty signal) |
| [`phaze-ytgo.3`](phaze-ytgo.3-clap-umap-deps.md) — CLAP / UMAP | §3.2 (free-text separability, torch cost), §3.4 (PCA vs UMAP), §0.3 (msclap exclusion) |
| [`phaze-ytgo.4`](phaze-ytgo.4-sidecar-seam.md) — sidecar seam | §1 (600 s wall), §2.1, §3.1–§3.4 (every sidecar column), §5 items 1–2, 4 |
| [`phaze-ytgo.5`](phaze-ytgo.5-agpl-mit-compliance.md) — AGPL/MIT compliance | §0 in its entirety, §4.3 (Panako remediation), §7 Q9 |
| [`phaze-ytgo.6`](phaze-ytgo.6-vector-index.md) — vector storage and ANN | §1 (corrected scale facts), §3.1 (no ANN needed), §3.2 (recall, latency, pgvector cost, build memory), §3.3 (the storage verdict) |
| `phaze-ytgo` epic text | the four purposes, the verdict shape, the stale seal and `S`-number mapping |
| `phaze-vprd` epic text | §6, the non-duplication rules |
