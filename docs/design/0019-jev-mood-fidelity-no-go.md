# ADR 0019: Retain Essentia mood outputs after Jev fidelity benchmark

**Status:** Accepted for the evaluated seven-mood replacement/hybrid proposal (2026-09-20)

## Context

Phaze stores seven Essentia mood probabilities. An operator-authorized local spike tested whether TypeSafe Jev could reproduce them from catalog metadata, BPM/key/duration alone, or from that state plus the incumbent probability vector. The 12-track pilot established the request/evaluation path; a distinct 500-track, artist/release-grouped sample supplied the decision evidence. The operator designated stored Essentia probabilities as operational ground truth for this spike, not as independent perceptual truth. This public branch retains [aggregate findings](../evaluations/jev-phaze/README.md) and the [per-mood verdict](../evaluations/jev-phaze/phaze-eswzd.4-jev-verdict.md), not per-track evidence.

## Decision

Retain Essentia unchanged for acoustic, electronic, aggressive, relaxed, happy, sad, and party. Do not replace these outputs with the tested Jev-only probabilities and do not add the tested hybrid layer in production. This does not ban Jev from a separately defined future semantic feature.

## Evidence and tradeoffs

Across 500 held-out tracks and 3,000 successful calls, Jev-only achieved macro AP/F1 0.726/0.672, pooled soft-target MSE 0.04715, and 179/500 (35.8%) dominant-label agreement against Essentia. Hybrid achieved 0.989/0.920, MSE 0.00721, and 325/500 (65.0%), but it received the incumbent vector; its remaining 35% dominant changes and service/disclosure cost lack a measured benefit. Happy, sad, and relaxed were especially poor Jev-only fidelity slices. Three-repeat noise was low; this is systematic disagreement, not a flaky call artifact. Calls were locally reliable and inexpensive ($0.139677048 public-rate for both arms × three repeats); cost alone is not a reason to adopt. A previous sandbox DNS failure is retained in checkpoint history, not counted as an API success or charge.

This decision preserves current Phaze behavior and avoids an added external service dependency. It does **not** establish that Essentia matches listener judgments, nor that a different Jev task cannot add value. If reconsidered, specify that task and independent reference, freeze a new grouped validation split, pre-register quantitative/operational gates, review external-data retention and credential handling, and re-enter planning before product work. No product integration follows from this ADR.
