# ADR-0021: No persisted genre level or genre-separation signal

Date: 2026-10-05. Bead: phaze-kq5hv.2 (spike epic phaze-kq5hv). Status: accepted.

## Decision

Phaze does **not** persist a separate genre level, a genre score, or a genre-separation signal
alongside the flat top style. No column, no `analysis_window` feature field, no backfill.

A genre facet, if surfaced, is **derived at read time** from `dominant_style` (see "Follow-up").

## Operator decision (phaze-kq5hv.2, 2026-10-04)

Question as put, summarised in the bead comment: the phaze-kq5hv.1 evidence (sigmoid confirmed, row
sums 1.57-2.70; top genre equals top style genre in 210 of 210 windows; only the top 10 stored, so no
backfill is possible; separation ratio unstable, median 38.6, p95 437; does not flag the
`phaze-nkucu` clips) and the spike recommendation, followed by "Your call?".

Answer as given (selected option label, verbatim):

> NO-GO + read-time genre facet

Date of the exchange: 2026-10-04 (dispatch session, `AskUserQuestion`). Durable record: the comment on
bead `phaze-kq5hv.2`. This satisfies [ADR-0012](0012-verification-fidelity-and-operator-attribution.md)
rule 2. The reasons and implementation notes below are the implementer's, not further operator
statements. The genre-as-max-over-styles rule referenced here is the earlier operator decision of
2026-09-22 recorded in the `phaze-kq5hv` epic description.

## Measured reasons

All figures are from `docs/spikes/phaze-kq5hv.1-hierarchical-genre-style.md` (real
`discogs-effnet-bs64-1` inference through phaze's own `_predict_single`, 210 analysed windows from 60
files; stored-data samples of 6,966 analyses and 1,000 files).

1. **The genre label is a function of the top style.** Under max over styles, the top genre is the genre
   of the top style by identity: 0 disagreements in 210 of 210 real windows and 17,117 of 17,117
   stored windows. At file level the genre of `dominant_style` equals the max-aggregated top genre in
   6,926 of 6,966 files (40, or 0.57 %, differ).
2. **The only new quantity is unrecoverable.** Stored coarse windows keep the top 10 of 400 activations
   (116,770 of 116,898 coarse windows hold exactly 10, 128 hold 0). Two or more genres appear in only
   37 of 210 (17.6 %) real windows and 3,067 of 17,117 (17.9 %) windows of the stored sample, so the
   runner-up genre cannot be backfilled without re-analysis.
3. **The ratio is unstable and uninformative.** Genre separation over full 400 activations: p5 / p25 /
   p50 / p75 / p95 = 2.716 / 12.353 / 38.584 / 127.555 / 436.580, unbounded as the runner-up tends to
   zero (typical runner-up score 0.010). It is at least 2 in 202 of 210 windows, because 6,182 of 6,966
   files (88.7 %) have Electronic as top genre.
4. **It does not help the degenerate inputs of `phaze-nkucu`.** A 3 s real clip scores a genre
   separation of 2,651.3, a 10 s clip 4,658.6, a 30 s clip 21.3, and a 3 s 1 kHz tone 7.5: confident
   exactly where the input is least trustworthy. Silence already yields no predictions and no genre.
5. **The sigmoid premise holds** (row sums min 1.571, median 2.202, max 2.695 over 210 windows), so
   max over styles remains the right rule if a genre is ever computed; summing would not be a
   probability.

## Follow-up: read-time genre facet (phaze-kq5hv.2, 2026-10-04)

Derive the genre of a file from `dominant_style` at read time, through a static map built from the
model's `classes` list (400 labels to 15 genres). Zero storage, zero re-analysis, no backfill; at the
top-style level it gives the same answer as max over styles.

**Never split the stored label on `/`.** `derive_style` rewrites `---` to `/` in stored labels, and one
genre is itself named `Funk / Soul`, so a split mis-assigns its 15 styles. Look the label up in the
`classes`-derived map. A small implementation bead is filed separately by the dispatcher; it is not
specified here.

## What would reopen this decision

A consumer that needs the **runner-up genre** (or any genre-separation scalar). That means persisting
**15 genre maxima per coarse window** at analysis time (not the 400 activations, and not the ratio),
plus re-analysis of the 6,966 completed files, since the stored top 10 cannot supply it. If a scalar is
stored from it, prefer the margin or the runner-up score over the ratio. A mixed-genre collection might
also change the picture: the spike measured an archive that is 88.7 % Electronic and did not answer
otherwise.

## What this does not decide

The style separation (p50 1.375 on full-400 windows) needs no new data and is independent of any genre
level; whether to surface it is outside this decision.
