# Genre level (max over styles) on real `effnet_discogs` output

- **Bead:** `phaze-kq5hv.1` (epic `phaze-kq5hv` — hierarchical genre/style from `Genre---Style` labels)
- **Date:** 2026-10-05 (bead-comment timestamps are UTC; the operator exchange that cited this spike was 2026-10-04 US Pacific)
- **Tree:** branch `wt/bead/issue/phaze-kq5hv.1`, forked at `110b7f98`
- **Status:** investigation only. No product code, test code or build config changed. Every measurement
  script lives in the session scratchpad and is untracked. Archive identifiers (paths, filenames, file ids)
  are scrubbed; every quantity is exact.
- **Idea source:** the TypeSafe hierarchical-classification cookbook,
  <https://docs.typesafe.ai/cookbooks/hierarchical_classification.md> — tree-structured scoring with a
  separation/ambiguity signal. Only the idea is borrowed; no TypeSafe API is involved.

## Verdict

**NO-GO** on persisting a *separate genre level with a genre-separation signal* alongside the flat top
style. **GO, narrowly,** on the two cheap things that fall out of the measurement (see Recommendation):
derive the genre of the dominant style at read time from a static label map, and, only if a downstream
consumer asks for a genre runner-up, persist 15 genre maxima per coarse window rather than anything
derived from the stored top-10.

Measured basis, in one paragraph: the activation is confirmed sigmoid on the real deployed graph (row sums
1.571–2.695 over 210 real windows, never 1); under the *max over styles* rule the top genre is
the genre of the top style by construction, and that identity held in 210 of 210 real windows, so the
genre **label** adds nothing the flat top style does not already imply. The only new information is the
runner-up genre score, and it cannot be recovered from what is stored: the analysis keeps each coarse
window's **top 10** of 400, which contains two or more genres in only 37 of 210 (17.6 %) real windows and
3,067 of 17,117 (17.9 %) windows in a 1,000-file stored-data sample. Where the full 400 *is* available, the
top/runner-up genre **ratio** is an unstable signal (median 38.6, p95 436.6, unbounded as the runner-up
tends to zero) and is almost uninformative in this archive, which is overwhelmingly Electronic: genre
separation is at least 2 in 202 of 210 windows. It also does not flag the degenerate inputs from
`phaze-nkucu`: a 3-second real clip scores a genre separation of 2,651.3 and a 3-second 1 kHz tone 7.5.

## Question

On the real archive, does a genre level (genre = **max** over its styles, per the operator decision of 2026-09-22 on bead `phaze-kq5hv`) plus a separation ratio add useful, stable information over the flat top style, and does it
improve the degenerate cases (`phaze-nkucu`)? Four sub-questions, from the bead's acceptance:

1. Is the `discogs-effnet-bs64-1` output really sigmoid in *this* environment, and which output node does
   phaze read?
2. What are the distributions of genre separation and style separation; how often does the top style's
   genre differ from the max-over-styles top genre; how often is genre confident while style is ambiguous?
3. What does the rule do on the `phaze-nkucu` degenerate inputs (short clip, silence)?
4. Does stored analysis hold all 400 activations, or only the top N?

## Method

**Where.** Read-only against production. The control-plane Postgres (`host-prod`) was queried inside a
`SET default_transaction_read_only = on` session. Inference ran in the already-running analyze container on
the agent host (`host-store`) — image `ghcr.io/simplicityguy/phaze:2026.10.2`, essentia `2.1-beta6-dev`,
deployed graph `/models/discogs-effnet-bs64-1.pb` — via `docker exec` at `nice -n 15`. The model and archive
mounts are read-only there; the only writes were scratch files under the container's `/dev/shm`, removed
afterwards. No restart, no schema change, no queue interaction. It did consume one core of the agent host
for roughly forty minutes, at load average 2.3–4.6 on 8 cores.

**Real consumer (CLAUDE.md, rule 3 of "Acceptance criteria, attribution, and verification fidelity").**
Inference called phaze's own `phaze.services.analysis._predict_single` with `GENRE_MODEL`, i.e.
`es.TensorflowPredictEffnetDiscogs(graphFilename=…, batchSize=64)` and the `np.mean(np.atleast_2d(…), axis=0)`
reduction phaze applies. No mock, no upstream metadata standing in for a measurement. Audio was decoded with
`es.MonoLoader(sampleRate=16000)` and sliced on the window boundaries stored in `analysis_window`.

**Parity check on the harness.** For all 210 sampled windows the re-computed top style equals the stored
one (210 of 210), the re-computed top 10 label set equals the stored top 10 (overlap 10 of 10 in every
window), and the largest absolute confidence difference is 0.00047 (the harness slices one full decode, the
product decodes per chunk). So the 400-wide vectors below are the vectors the product saw.

**Sample A — real inference, full 400 activations.** 60 files drawn uniformly at random
(`setseed(0.4242)`, `ORDER BY random() LIMIT 60`) from the analysis rows that have a completed analysis and
at least one stored coarse window. 58 mp3 + 2 flac; duration min 0.04 h, quartiles 0.20 / 0.88 / 1.00 h,
max 2.81 h; 933 stored coarse windows in total. Per file, up to four coarse windows at evenly spaced
indices (first, one third, two thirds, last) were run: **211 windows, of which 1 was a 0.12 s trailing window
that yields an empty vector, leaving 210 analysed windows.** Window-level, not file-level: a file's other
windows were not run.

**Sample B — stored data, top-10 only.** Every completed analysis (6,966 rows with a stored `style` array)
for the file-level view, and every coarse window of 1,000 random completed files (`setseed(0.77)`;
17,128 windows, 17,117 with predictions) for the window-level view.

**Definitions** (the epic's design, made explicit):

- genre score = max activation among that genre's styles. Operator decision, 2026-09-22 (bead `phaze-kq5hv`, durable record: the epic description). Question as put: "The genre model outputs independent sigmoid scores (upstream metadata: op=Sigmoid, 400 classes), so summing styles into a genre isn't a probability. How should (2) derive the genre?" Answer as given (selected option label, verbatim): "Max over its styles (Recommended)". No sum, no noisy-OR;
- genre separation = top genre score / runner-up genre score;
- style separation = top style / runner-up style, both over all 400 (*flat*) and within the winning genre
  (*in-genre*). Both are undefined when there is no runner-up, which is what truncation to a top 10 causes.

**Label hazard found on the way.** Stored labels have `---` rewritten to `/` (at load time in `_get_labels`, `src/phaze/services/analysis.py`),
and one genre is itself named `Funk / Soul`, so a stored label cannot be split on `/` to recover its genre.
All genre assignments here come from the model's 400-entry `classes` list (15 genres; Electronic 106
styles, Rock 91, Latin 35, Folk, World, & Country 27, Hip Hop 26, Jazz 25, Pop 16, Funk / Soul 15,
Classical 13, Non-Music 13, Blues 12, Reggae 11, Stage & Screen 4, Brass & Military 3, Children's 3).

## Evidence

### 1. Activation type and output node — sigmoid, `PartitionedCall:0`

- Upstream metadata in the deployed `discogs-effnet-bs64-1.json`: output `PartitionedCall:0`, shape
  `[64, 400]`, `op: "Sigmoid"`, purpose `predictions`; output `PartitionedCall:1`, shape `[64, 1280]`,
  `op: "Flatten"`, purpose `embeddings`. This is the claim being checked, not the evidence.
- Measured, real essentia, deployed graph, default `output` (what phaze passes — it sets none): a
  10 s buffer of synthetic noise (a graph-plumbing check, not a genre claim) returns shape `(9, 400)`; per-patch row sums 2.296, 2.234, 2.300; min 6.2e-8, max 0.395.
  Requesting `output="PartitionedCall:1"` returns `(9, 1280)`, i.e. the embeddings. So phaze reads the
  400-wide predictions node.
- Measured on real audio, 210 windows (phaze's mean-over-patches vector): row sums
  **min 1.571 / p25 2.081 / median 2.202 / p75 2.376 / max 2.695** — never 1. Independent per-label
  scores, as the metadata says. Direct corroboration: the per-window count of styles above 0.5 is
  0 / 0 / 0 / 1 / 2 at p5 / p25 / p50 / p75 / p95, so at least 5 % of windows hold two labels above 0.5
  at once, which a softmax cannot produce.

**The sigmoid premise behind the max-over-styles rule holds.** Summing styles into a genre would not be a probability;
max over styles is the defensible rule.

### 2. Separation distributions (Sample A, full 400, 210 windows)

Quantiles are p5 / p25 / p50 / p75 / p95.

| Quantity | p5 | p25 | p50 | p75 | p95 |
| --- | --- | --- | --- | --- | --- |
| Top style score | 0.148 | 0.294 | 0.409 | 0.595 | 0.779 |
| Flat style separation (top / runner-up style) | 1.033 | 1.142 | 1.375 | 1.681 | 2.421 |
| In-genre style separation | 1.039 | 1.147 | 1.388 | 1.693 | 2.475 |
| Genre separation (top / runner-up genre) | 2.716 | 12.353 | 38.584 | 127.555 | 436.580 |
| Runner-up genre score | 0.001 | 0.004 | 0.010 | 0.026 | 0.075 |
| Margin (top genre − runner-up genre) | 0.084 | 0.282 | 0.396 | 0.563 | 0.773 |

- The genre ratio spans nearly three orders of magnitude because the runner-up score is typically ~0.01
  (p99 0.143). It is a quotient of a stable numerator by a noise-floor denominator, so it is not a
  stable signal. The difference (margin) is bounded in [0, 1] and tracks the top score; if a genre
  separation is ever persisted, a margin or the runner-up score itself is the safer form.
- A runner-up genre at or above 0.1 occurs in 5 of 210 windows, at or above 0.2 in 1, at or above 0.3 in 0.
  In this archive the second genre is almost never a real contender.
- Top genre in the sample: Electronic 199, Rock 8, Non-Music 1, Hip Hop 1, Folk, World, & Country 1.
  Over all 6,966 stored file-level style arrays the top genre is Electronic in 6,182 (88.7 %), Rock 484,
  Hip Hop 78, Folk, World, & Country 76, Latin 64, Pop 36, Funk / Soul 20, Reggae 11, Jazz 5, Non-Music 4,
  Stage & Screen 2, Classical 2, Brass & Military 1, Blues 1.

### 3. Top-style genre vs. max-over-styles top genre

- **Window level: 0 disagreements in 210 of 210** (Sample A) and **0 in 17,117 of 17,117** (Sample B,
  top-10). This is not an empirical finding but an identity: the global maximum over 400 styles is the
  maximum of its own genre, so the max-over-styles top genre is *always* the genre of the top style. The
  genre label is a pure function of the flat top style.
- **File level** (where the label is the duration-modal window style `dominant_style`, and the score is the
  duration-weighted mean): genre of `dominant_style` equals the max-aggregated top genre in **6,926 of
  6,966** files; 40 (0.57 %) differ. `dominant_style` equals the highest mean-score style in 6,409 of 6,966
  (557 differ, 8.0 %) — the modal-vs-mean gap that exists today regardless of a genre level.

### 4. Confident genre, ambiguous style

Defined as genre separation at or above G and in-genre style separation below S (Sample A, 210 windows,
full 400):

| G | S | windows |
| --- | --- | --- |
| 2 | 1.2 | 63 |
| 2 | 1.5 | 124 |
| 3 | 1.5 | 122 |
| 5 | 1.5 | 115 |

Cross-tab at (genre separation at least 2, in-genre separation at least 1.5): (yes, no) 124, (yes, yes) 78,
(no, yes) 4, (no, no) 4. **Confident genre is the norm (202 of 210) and so carries almost no information
here**; the discriminating half is the in-genre style separation, which is numerically the flat style
separation (the two quantiles above agree to within 0.06 in the table; they are identical whenever the
top two styles share a genre) and is already computable from stored data.

Also: of the 136 windows with a top style score below 0.5, 128 still have genre separation at least 2; of the
31 below 0.2, 23 do. A low top score does not make the genre ratio look ambiguous.

### 5. Degenerate inputs from `phaze-nkucu`

Real `analyze_file` on the deployed image, plus the genre rule applied to the same vector from
`_predict_single`. The 3/10/30 s clips are cut from one real archive track; silence and tone are synthetic.

| Input | `analyze_file` style | Window genre predictions | Genre / separation under max-over-styles |
| --- | --- | --- | --- |
| Real clip, 3 s | `Electronic/Hardstyle` | 10 entries, top 0.843 | Electronic, genre separation 2,651.3, flat 1.347 |
| Real clip, 10 s | `Electronic/Hardstyle` | 10 entries, top 0.880 | Electronic, genre separation 4,658.6, flat 1.419 |
| Real clip, 30 s | `Electronic/Hardstyle` | 10 entries, top 0.413 | Electronic, genre separation 21.3, flat 1.638 |
| Tone, 3 s, 1 kHz | `Electronic/Minimal` | 10 entries, top 0.292 | Electronic, genre separation 7.5, flat 1.372 |
| Digital silence, 2 s | `None` | `[]` | no genre (nothing to take a max over) |
| Digital silence, 200 s | `None` | `[]` | no genre |

- The current code already returns `None` for silence (the `-100 dBFS` gate in `_has_analyzable_signal`
  yields an empty prediction list), so a genre rule has nothing to fix there: max over an empty set is
  undefined and must stay undefined.
- A short clip or a pure tone gets a *confident* genre (Electronic, separation 7.5 to 4,658.6). The
  genre-separation signal therefore does **not** flag the degenerate-but-audible cases; it is highest where
  the input is shortest. The reliable signal for those is window duration or absolute top score, which the
  flat path already has.
- The legacy defect is still present in stored data: **125** coarse windows carry `style = 'unknown'`
  (129 coarse windows have an empty prediction list in this query; section 6 counts 128 from a different query, and the
  difference was not re-measured, so the two are reported as found), though **0** analysis rows have
  `dominant_style = 'unknown'`. In Sample A the one trailing window (0.12 s) is such a case. A genre level
  would inherit these as "no genre", which is correct.

### 6. What is stored — top 10 per window, not 400

- `analysis_window.features->'genre'->'predictions'` for coarse windows is cut to **10 entries**
  (`genre_pairs[:10]` in `_run_model_sets_over_windows`): 116,770 coarse windows hold exactly 10 and 128 hold
  0, of 116,898 coarse windows. Total `analysis_window`: 674,883 rows
  over 6,972 files.
- `analysis.style` is the duration-weighted mean of those top-10 lists with missing labels counted as
  zero (`aggregate_style_scores`): arrays of **10 to 122** entries, never 400.
- **So the genre level cannot be backfilled without re-analysis.** What the top 10 can show is the winning
  genre and its in-genre structure; the runner-up genre is missing in the majority of windows. Measured
  top-10 truncation effect on Sample A: genre separation is defined (two or more genres present) in **37 of
  210** windows; number of distinct genres in a window's top 10: 1 genre 173, 2 genres 30, 3 genres 6,
  5 genres 1. In Sample B: 14,050 of 17,117 windows have exactly one genre in the top 10, 2,279 two, 515
  three, 198 four, 54 five, 19 six, 2 seven. At file level the stored aggregate has a single genre in
  2,892 of 6,966 files, so a stored-data file genre separation is undefined for 41.5 % and, where defined
  (median 48.0), is a ratio against a zero-filled mean and not a measurement. The top genre is still right: it
  matched the full-400 top genre in 210 of 210 windows.

## Recommendation

1. **Do not add a persisted genre column, a genre score, or a genre-separation field.** The genre label is
   a function of the top style (identity in section 3), the one genuinely new quantity is unrecoverable from
   stored data (section 6), and the ratio form is unstable and uninformative in an archive that is 88.7 %
   Electronic (sections 2 and 4). It also does not improve `phaze-nkucu` (section 5).
2. **If a genre facet is wanted, derive it at read time** from `dominant_style` through a static map built
   from the model's `classes` list (400 labels to 15 genres). Do not split on `/`: `Funk / Soul` is a genre
   name. Cost: zero storage, zero re-analysis, no backfill. It is consistent with the max-over-styles rule cited under Definitions, because at the top-style level it is the same answer.
3. **If a genre runner-up is wanted later, persist 15 genre maxima per coarse window** at analysis time, not
   the 400 activations (400 floats per window is the cost this top-10 cut was avoiding) and not a derived
   ratio. That needs re-analysis of the existing 6,966 completed files; the stored top 10 cannot supply it.
   If a scalar is stored from it, prefer the margin (top − runner-up) or the runner-up score over the ratio.
4. **The signal worth surfacing is the style separation**, which needs no new data: p50 1.375 and
   p5–p95 1.033–2.421 on full-400 windows, and Sample B's top-10 window-level flat separation is
   1.029 / 1.166 / 1.387 / 1.796 / 3.199 at the same quantiles, matching. That is a decision for
   `phaze-kq5hv.2` and is independent of any genre level.

## What was not measured

- File-level separation from full 400 activations: Sample A ran at most four windows per file, so no
  duration-weighted file-level genre vector exists from real inference; file-level numbers above come from
  the stored top-10 aggregates and are labelled as such.
- Archive composition beyond the two samples. 88.7 % Electronic is the stored file-level top genre; whether a
  genre level would add more on a mixed-genre collection is not answered here, and could change item 1 of
  the Recommendation.
- Other model variants: only `discogs-effnet-bs64-1` was measured.
- Blast radius: nothing here changes a production path; no change is proposed to the analysis path.
