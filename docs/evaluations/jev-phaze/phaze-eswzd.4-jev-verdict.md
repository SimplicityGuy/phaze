# phaze-eswzd.4 — Jev adoption verdict

## Question

Should Phaze replace its seven Essentia mood classifiers with Jev-only, add a hybrid Jev layer, retain Essentia unchanged, expand the benchmark, or reject Jev outright?

## Method and scope

This decision uses a 12-track pilot and a separate, leakage-grouped 500-track benchmark, with 72 + 3,000 captured live responses. The per-track evidence is retained only on a local branch, not published here; see the [archive boundary](README.md). The public [evaluator code](evaluate_jev_pilot.py) remains inspectable but cannot recompute these measurements without the omitted evidence. The operator designated **stored Essentia mood probabilities as operational ground truth for this spike**. Agreement means fidelity to today's Phaze output, not human or semantic correctness. No blind human labels or audio listening are asserted. Jev-only sees metadata, duration, BPM, and key but not classifier output or audio; hybrid additionally sees the exact seven Essentia probabilities. Hybrid fidelity is therefore evidence-following, not independent validation.

The benchmark is held out from the pilot by UUID and normalized artist/release group and caps each group at two tracks. Its 500 subjects deliberately cover dominant mood, top-two margin, duration, and metadata-completeness strata. It is not a prevalence-weighted estimate of the full catalog. Both arms used the same frozen `jev-latest` seven-Noul request, three repeats, fixed 0.5 target/decision threshold, and no threshold tuning on benchmark outcomes.

## Evidence

| Measure versus Essentia | Jev-only | Hybrid |
| --- | ---: | ---: |
| Macro PR-AUC / F1 across seven moods | 0.726 / 0.672 | 0.989 / 0.920 |
| Soft-target Brier (= MSE), MAE, Pearson r | 0.04715 / 0.17361 / 0.63740 | 0.00721 / 0.06335 / 0.96594 |
| Dominant agreement | 179/500 (35.8%) | 325/500 (65.0%) |
| Jev dominant within Essentia top two | 317/500 (63.4%) | 449/500 (89.8%) |
| Mean top-two-set overlap | 0.585 | 0.836 |
| Three-repeat mean population standard deviation | 0.00569 | 0.00391 |
| 1,500 successful-call input tokens / public-rate cost | 1,493,634 / $0.062732628 | 1,832,010 / $0.076944420 |
| Successful-call p50 / p95 latency | 167.260 / 232.206 ms | 167.730 / 237.771 ms |

All 3,000 final benchmark requests succeeded at a **combined $0.139677048** public-rate calculated cost; combined p50/p95 were 167.355/235.570 ms. A sandbox DNS failure before successful access left three historical terminal events/six internal retries in the checkpoint, not active failed records or charged responses. The local sequential latency is not a concurrent production SLA. Mean repeat noise is small compared with the cross-arm disagreement.

The most concerning per-label Jev-only F1s are happy **0.376**, sad **0.463**, relaxed **0.627**; happy AP is **0.358**. Jev-only dominant agreement by incumbent-dominant slice is 10.0% for happy and relaxed, 20.0% sad, and 19.7% party (n=50, 70, 55, 66). Hybrid's high pooled probability correlation still leaves 175/500 dominant labels changed; in the ambiguous-margin slice it matches only 36.7% (n=199). Even when its predictions are more confident, certainty-based coverage drops and no floor has been calibrated independently. The full per-label, margin, duration, metadata, coverage, repeat, and cost evidence is in the evaluation JSON/report.

The approved external request disclosed artist, title, album, duration, BPM, key, and (hybrid only) seven classifier scores. It disclosed no audio, filesystem path, database credential, or persisted API key. Future production integration would add an external metadata disclosure and service dependency; it would require retention/privacy review, server-side secret handling, bounded timeouts/retries, caching, observed concurrency limits, and a fail-open path preserving incumbent results. The benchmark establishes neither semantic superiority nor a user-facing reason to introduce that layer.

## Verdict

**NO-GO for replacing or augmenting the current seven mood classifiers with this Jev-only or hybrid contract. Retain Essentia unchanged.** Each evaluated mood has exactly one outcome:

| Mood | Decision | Material evidence |
| --- | --- | --- |
| acoustic | Retain Essentia unchanged | Jev-only F1 .789; hybrid still changes incumbent probabilities; no demonstrated independent benefit. |
| electronic | Retain Essentia unchanged | Jev-only F1 .897, but replacement is not operationally equivalent across the seven-label vector. |
| aggressive | Retain Essentia unchanged | Jev-only F1 .745; aggressive-dominant agreement 31.5%. |
| relaxed | Retain Essentia unchanged | Jev-only F1 .627; relaxed-dominant agreement 10.0%. |
| happy | Retain Essentia unchanged | Jev-only AP .358/F1 .376; happy-dominant agreement 10.0%. |
| sad | Retain Essentia unchanged | Jev-only AP .542/F1 .463; sad-dominant agreement 20.0%. |
| party | Retain Essentia unchanged | Jev-only F1 .806 but party-dominant agreement 19.7%. |

This rejects the **current replacement/hybrid proposal**, not every potential use of Jev. Keeping Essentia is a decision relative to its designated operational target, not an assertion that Essentia is perceptually correct. A close hybrid number is unsurprising given its access to the incumbent vector; its additional cost, disclosure, and failure mode have no measured payoff here. The 12-track pilot was a plumbing/cost probe; the grouped 500-track result is the decision evidence. No production code or production data was changed.

The following surfaces were **out of scope** and retain their existing behavior without an inferred Jev verdict: Discogs genre/style, danceability, gender, tonality, and voice/instrumental. Raw-audio feature extraction, BPM, and key estimation were not replacement targets. The decision is recorded in [ADR 0019](../../design/0019-jev-mood-fidelity-no-go.md).

## Reconsideration boundary

A new proposal should specify user value that the incumbent does not provide (for example, a separately named metadata/semantic feature) and an independent reference appropriate to that goal. Freeze a new artist/release-grouped calibration/held-out split, pre-register decision thresholds and acceptable per-label AP/F1/probability calibration, dominant/top-two change budget, risk/coverage, cost and p95/error limits, then evaluate once. Do not tune on these 500 held-out outcomes and describe a repeat score as unbiased. If the new scope supports GO, re-enter planning with `/bh:replan phaze-eswzd` before implementation. No such work is authorized or implemented by this spike.
