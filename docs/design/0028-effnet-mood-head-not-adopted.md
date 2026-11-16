# ADR-0028: The EffNet mood head is not adopted; the musicnn mood graphs stay

Date: 2026-11-16. Status: accepted. Bead: phaze-m3t4d. Epic: phaze-t04h6. Evidence:
`docs/spikes/phaze-m3t4d-effnet-mood-head-fit.md`. Durable operator records: the comments on bead phaze-m3t4d
(thresholds and pilot 2026-11-13; extra rounds 2026-11-14; stopping round 2 2026-11-15), and on bead phaze-uv8s3
for the epic's mood decision.

## Context

Mood is phaze's most expensive analysis step. The 11 `musicnn_msd` graphs are 84.5% of analysis CPU, about
96.8 cpu-s per 180 s coarse window (`docs/spikes/phaze-fzubr-analysis-cpu-profile.md`). Epic phaze-t04h6 carries the
operator's decision of 2026-11-13 (question as put and answer as given recorded on phaze-uv8s3): "Fit a head to
musicnn (Recommended)". That is a small head on the Discogs-EffNet embedding, trained to reproduce the stored musicnn
scores, **adopted only if it passes operator-approved thresholds**. The thresholds were set before any held-out data
was read.

- **Question as put** (2026-11-13, AskUserQuestion via the dispatcher): "headfit (phaze-m3t4d) proposes these pass
  thresholds, all on 350 held-out test files and ALL required. (1) Pearson r >= 0.90 ... (2) Mean absolute error <=
  0.3 x each score's spread ... (3) Dominant mood matches on >= 85% of windows, >= 95% where the stored top two moods
  differ by >= 0.10, and macro recall >= 0.70 ... (4) Energy ... MAE <= 0.03. Approve these?"
- **Answer as given** (selected option label): "Approve as proposed (Recommended)". As the dispatcher stated it,
  this means: decide on the point estimate, and flag any threshold inside its confidence interval.

That makes 25 gates in all; the full list is in the spike doc.

## Outcome

The head (a NumPy MLP, 1280 → 1024 → 11, 5-seed ensemble) was trained on 1,651 files / 11,712 coarse windows. It was
evaluated once on 351 held-out files / 2,455 windows, archive-weighted, with a stratified file-level bootstrap. It
passes **21 of 25** gates. It fails four, each with its whole 95% interval past the threshold:

| Gate | Measured [95% CI] | Threshold |
|---|---|---|
| MAE mood_happy | 0.0514 [0.0486, 0.0543] | ≤ 0.046 |
| MAE gender | 0.0562 [0.0532, 0.0593] | ≤ 0.042 |
| MAE voice_instrumental | 0.0661 [0.0620, 0.0704] | ≤ 0.053 |
| Energy MAE | 0.0328 [0.0308, 0.0354] | ≤ 0.03 |

Every correlation gate passes (r 0.872 to 0.950). The failures are level errors, not ranking errors.

## Decision

Under the approved rule, **the head is not adopted.** Nothing in the analysis pass changes:

- Mood and the characteristic scores keep coming from the 11 deployed `musicnn_msd` graphs, with today's provenance.
- No integration bead is filed. The bead's PASS branch (head inference in the analysis pass, versioned score
  provenance, tests on real essentia) does not apply.
- The one combined re-analysis planned in phaze-dqwcv carries the per-window embeddings, the sub-window mood and the
  beat grids, **without** the head. Its CPU budget keeps the musicnn sweep, about 65,000 cpu-h per archive pass
  (arithmetic on measured per-window costs). With the head it would have been about 3,000.
- The embedding stays. phaze-l5mzm, phaze-g79ea and phaze-qijoh store it whatever this verdict is, so a later head
  can be fitted and tested without re-decoding the archive.

The thresholds are not relaxed here. Changing them, or running another measurement round, is a new question for the
operator, not a corollary of this record.

## What would reopen it

Each item below is an option to put to the operator, not a decision taken here:

1. **Finish the stratified data.** Round 2 stopped at 450 of 600 files under production load. The 150 that never ran
   were the party, relaxed and sad files. The VAL curve was still rising at 1,651 files (19 of 25 gates on VAL). Beyond
   1,200 files, the failing MAE gaps narrowed by 0.01 SD or less per 300 files, so how much data it would take to
   pass is unknown.
2. **A different head on the same embedding** (per-score calibration, multi-window context, another embedding
   layer). Only ridge and a one-hidden-layer MLP were tried.
3. **The clean-room route** that the operator's licence statement points to (quoted in the spike doc). It is a
   different objective: it cannot use the stored scores as labels, so it would be judged against them, not fitted to
   them. Its cost and accuracy are unmeasured. `docs/spikes/phaze-0toy3.5-clean-room-mood.md` ranks the candidates.

## Licence note

The measured route inherits the Essentia model terms twice: through its input embedding (an Essentia model) and
through its training targets (outputs of the NC musicnn models). Not adopting it changes nothing about phaze's
licence position. Mood still depends on the Essentia musicnn models, as it did before. The spike doc's "The two
licence routes" section sets out what each route implies for licence, cost and accuracy. The judgement is the
operator's.

## The general form

**A replacement that must reproduce an incumbent's scores is judged on the incumbent's levels, not just its
ranking.** This head clears every correlation gate and still fails on mean absolute error, and energy is a
weighted sum that inherits every level error. A future fitted replacement for any stored score (tempo, key,
embeddings-derived features) should gate on both correlation and absolute error, with the thresholds set before the
held-out look, as these were.
