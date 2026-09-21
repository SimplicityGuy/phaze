# phaze-eswzd.3 — Jev fidelity evaluation

## Question

How closely do the Jev-only and hybrid mood probabilities reproduce Phaze's stored Essentia outputs, and what are the repeatability, latency, failure, and cost characteristics on the frozen 12-track pilot and held-out 500-track benchmark?

## Method

[evaluate_jev_pilot.py](evaluate_jev_pilot.py) reads only the frozen corpus and captured Jev response JSON. For phaze-eswzd.3 on 2026-09-20, the question put to the operator was whether to wait for blind listener ratings or score the completed pilot against stored Essentia mood probabilities; the operator chose the latter as the **operational ground truth for this spike**. This measures fidelity to current Phaze classifier behavior, not independent human perception, semantic truth, or audio understanding. Jev never receives audio; the hybrid arm sees the Essentia vector, so its agreement is evidence-following, not independent validation.

The preregistered positive rule is **Essentia probability >= 0.5** and Jev predicts positive at **Jev probability >= 0.5**. Seven independent labels can each be positive. The evaluator averages three Jev repeats per subject/arm before comparing them with Essentia. Per-label non-interpolated average precision (PR-AUC) groups exact score ties; F1 uses the fixed 0.5 decisions. Soft-target Brier is the mean squared error against Essentia probability (the same numeric MSE, not a Brier score against observed binary outcomes). MAE, RMSE, and Pearson correlation describe probability distance; zero-variance correlation is undefined. Dominant mood uses canonical mood order to break exact ties, and top-two uses the same ordering. Confidence-distance coverage keeps decisions with `2 * abs(Jev probability - 0.5) >= floor` for floors 0, 0.2, 0.4, 0.6, and 0.8; conditional accuracy uses the Essentia >= 0.5 target. These are descriptive candidate floors, not thresholds calibrated on the held-out benchmark.

The public price retrieved from [TypeSafe's site](https://typesafe.ai/) on 2026-09-20 was $42 per billion input tokens, equivalent to $0.042 per million; output tokens were presented as free. The numbers here are public-rate calculations, not claims about account-specific credits or contract terms.

## Evidence

### Frozen 12-track pilot

The evaluator now runs without ratings: `python docs/evaluations/jev-phaze/evaluate_jev_pilot.py docs/evaluations/jev-phaze/phaze-eswzd.1-corpus.json docs/evaluations/jev-phaze/jev-runs.json OUTPUT.json`. There are 12 subjects, 84 subject-label comparisons, and no calibration split. Per-label results below are against the stored Essentia operational target; the target-positive count is the number of Essentia probabilities >= 0.5.

| Mood | Positives | Jev-only AP | Jev-only F1 | Jev-only soft MSE | Jev-only MAE | Jev-only r | Hybrid AP | Hybrid F1 | Hybrid soft MSE | Hybrid MAE | Hybrid r |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| acoustic | 3 | 1.000 | 1.000 | 0.05833 | 0.21208 | 0.854 | 1.000 | 1.000 | 0.00360 | 0.04408 | 0.991 |
| electronic | 7 | 0.968 | 0.800 | 0.06074 | 0.20679 | 0.706 | 1.000 | 1.000 | 0.00664 | 0.06215 | 0.983 |
| aggressive | 5 | 1.000 | 1.000 | 0.02872 | 0.13968 | 0.875 | 1.000 | 0.769 | 0.00569 | 0.06649 | 0.979 |
| relaxed | 3 | 0.778 | 0.800 | 0.07301 | 0.23423 | 0.513 | 1.000 | 1.000 | 0.00168 | 0.02994 | 0.996 |
| happy | 4 | 0.555 | 0.333 | 0.08910 | 0.24261 | 0.117 | 1.000 | 0.800 | 0.00843 | 0.07789 | 0.944 |
| sad | 3 | 0.767 | 0.400 | 0.13736 | 0.32752 | 0.280 | 1.000 | 1.000 | 0.00246 | 0.04174 | 0.991 |
| party | 9 | 0.928 | 0.889 | 0.08797 | 0.24774 | 0.584 | 1.000 | 1.000 | 0.00473 | 0.05534 | 0.983 |

Macro AP/F1 are 0.8565/0.7460 for Jev-only and 1.0000/0.9385 for hybrid. Across all 84 probabilities, Jev-only soft MSE/MAE/Pearson are 0.07646/0.23009/0.60446; hybrid 0.00475/0.05395/0.98197.

| Subject | Essentia dominant | Jev-only dominant | Hybrid dominant |
| --- | --- | --- | --- |
| P01 | acoustic | party | acoustic |
| P02 | aggressive | aggressive | aggressive |
| P03 | electronic | electronic | electronic |
| P04 | happy | sad | party |
| P05 | party | electronic | party |
| P06 | relaxed | sad | relaxed |
| P07 | sad | sad | sad |
| P08 | aggressive | party | electronic |
| P09 | aggressive | aggressive | aggressive |
| P10 | electronic | electronic | electronic |
| P11 | party | electronic | electronic |
| P12 | party | electronic | electronic |

Jev-only matched the Essentia dominant on 5/12 and placed its dominant in Essentia's top two on 10/12; hybrid did so on 8/12 and 11/12. Mean top-two-set overlap is 0.625 versus 0.917, respectively.

| Certainty floor | Jev-only coverage | Jev-only accuracy | Hybrid coverage | Hybrid accuracy |
| ---: | ---: | ---: | ---: | ---: |
| 0.0 | 84/84 (100%) | 80.95% | 84/84 (100%) | 94.05% |
| 0.2 | 62/84 (73.81%) | 87.10% | 77/84 (91.67%) | 98.70% |
| 0.4 | 37/84 (44.05%) | 89.19% | 71/84 (84.52%) | 100% |
| 0.6 | 19/84 (22.62%) | 100% | 61/84 (72.62%) | 100% |
| 0.8 | 0/84 (0%) | undefined | 24/84 (28.57%) | 100% |

Mean three-repeat population standard deviation is 0.00560 for Jev-only and 0.00337 for hybrid. All 72 requests succeeded, with zero retries or terminal failures, 79,911 input tokens, 8,856 output tokens, and $0.003356262 at the public rate. With linearly interpolated p50/p95 over successful request latencies, Jev-only is 171.963/230.138 ms, hybrid 195.600/280.872 ms, and combined 179.686/256.404 ms. One 1,539.898 ms hybrid tail request is above p95 and should not be concealed by the percentile summary.

At the observed pilot request shapes (997 Jev-only and 1,222.75 hybrid input tokens per request), public-rate projections for **both arms** are:

| Subjects | One run each | Three repeats each | Interpretation |
| ---: | ---: | ---: | --- |
| 12 | $0.001118754 | $0.003356262 | completed pilot |
| 500 | $0.046614750 | $0.139844250 | pilot-shape projection; actual benchmark below |
| 5,583 | $0.520500299 | $1.561500896 | completed production files |
| 93,444 | $8.711737398 | $26.135212194 | stored coarse windows, if each is a separate request |

Window-level evidence would change the token count; this is not a billed invoice.

### Held-out 500-track benchmark

The [frozen benchmark manifest](phaze-eswzd.1-benchmark.json.gz) has 500 distinct subjects from 437 artist/release leakage groups (374 singleton and 63 two-track groups; maximum two), no pilot UUID/group overlap, and 3,500 subject-label comparisons. The [captured 3,000 responses](jev-benchmark-runs.json.gz) and [machine-readable evaluation](jev-benchmark-evaluation.json) retain every repeat and the stratified breakdown. The frozen state/question/model/threshold are identical to the pilot; the benchmark is the primary decision evidence. The sample deliberately balances strata rather than estimating the prevalence of all production files.

| Mood | Essentia positives | Jev-only AP | Jev-only F1 | Jev-only soft MSE | Hybrid AP | Hybrid F1 | Hybrid soft MSE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| acoustic | 87 | 0.837 | 0.789 | 0.04034 | 0.994 | 0.888 | 0.00629 |
| electronic | 319 | 0.936 | 0.897 | 0.02994 | 0.999 | 0.944 | 0.01571 |
| aggressive | 251 | 0.876 | 0.745 | 0.05184 | 1.000 | 0.942 | 0.00440 |
| relaxed | 167 | 0.653 | 0.627 | 0.04456 | 0.998 | 0.920 | 0.00729 |
| happy | 120 | 0.358 | 0.376 | 0.05400 | 0.934 | 0.813 | 0.00775 |
| sad | 156 | 0.542 | 0.463 | 0.06517 | 0.999 | 0.984 | 0.00267 |
| party | 289 | 0.881 | 0.806 | 0.04419 | 0.999 | 0.949 | 0.00634 |
| **Macro / pooled** | — | **0.726** | **0.672** | **0.04715** | **0.989** | **0.920** | **0.00721** |

The last row averages AP and F1 equally across seven labels, but pools all 3,500 pairs for soft MSE. Pooled MAE/RMSE/Pearson are **0.17361/0.21714/0.63740** for Jev-only and **0.06335/0.08490/0.96594** for hybrid. These values are distance/correlation to incumbent probabilities, not audio or human correctness. Hybrid was given the exact seven incumbent scores, explaining much of its fidelity. Of 500 Essentia dominant labels, Jev-only agrees on **179 (35.8%)**, its dominant falls in the Essentia top two on **317 (63.4%)**, and its mean top-two-set overlap is **0.585**. Hybrid yields **325 (65.0%)**, **449 (89.8%)**, and **0.836**. The 12-track pilot's better AP and worse hybrid dominant agreement were not reliable estimates of this held-out behavior.

| Certainty floor | Jev-only coverage | Jev-only conditional accuracy | Hybrid coverage | Hybrid conditional accuracy |
| ---: | ---: | ---: | ---: | ---: |
| 0.0 | 3,500/3,500 (100%) | 75.66% | 3,500/3,500 (100%) | 94.17% |
| 0.2 | 2,451/3,500 (70.03%) | 83.76% | 3,217/3,500 (91.91%) | 97.30% |
| 0.4 | 1,572/3,500 (44.91%) | 91.28% | 2,805/3,500 (80.14%) | 99.57% |
| 0.6 | 669/3,500 (19.11%) | 95.37% | 1,862/3,500 (53.20%) | 100% |
| 0.8 | 48/3,500 (1.37%) | 95.83% | 510/3,500 (14.57%) | 100% |

Higher certainty improves conditional agreement while sharply reducing Jev-only coverage. The floors were descriptive and must not be selected after seeing held-out outcomes for deployment. Mean within-track, per-label three-repeat population standard deviation is **0.00569** Jev-only and **0.00391** hybrid, so disagreement is mainly systematic rather than run-to-run noise.

The stratified figures below are dominant-label agreement percentages against Essentia, with per-subject pooled probability MAE in parentheses. They are descriptive of the balanced selection, not causal effects of missing metadata or duration.

| Slice | n | Jev-only agreement (MAE) | Hybrid agreement (MAE) |
| --- | ---: | ---: | ---: |
| Essentia acoustic dominant | 27 | 88.9% (.1395) | 100.0% (.0477) |
| electronic dominant | 102 | 76.5% (.1604) | 100.0% (.0508) |
| aggressive dominant | 130 | 31.5% (.1876) | 66.9% (.0609) |
| relaxed dominant | 70 | 10.0% (.1846) | 71.4% (.0638) |
| happy dominant | 50 | 10.0% (.1715) | 38.0% (.0971) |
| sad dominant | 55 | 20.0% (.1625) | 40.0% (.0765) |
| party dominant | 66 | 19.7% (.1796) | 27.3% (.0570) |
| ambiguous top-two margin | 199 | 28.1% (.1719) | 36.7% (.0597) |
| close top-two margin | 195 | 39.0% (.1725) | 74.9% (.0647) |
| clear top-two margin | 106 | 44.3% (.1789) | 100.0% (.0677) |
| short, <180 s | 87 | 40.2% (.1821) | 70.1% (.0621) |
| medium, 180–360 s | 163 | 32.5% (.1886) | 61.3% (.0643) |
| long, >360 s | 249 | 36.5% (.1607) | 65.5% (.0632) |
| unknown duration | 1 | 0% (.2160) | 100% (.0581) |
| complete metadata | 400 | 33.0% (.1771) | 62.0% (.0646) |
| incomplete metadata | 100 | 47.0% (.1598) | 77.0% (.0583) |

The unknown-duration cell is a single observation. The apparently higher agreement for incomplete metadata is a selection/case-mix association and does not show that withholding metadata improves Jev. Representative discordant IDs (full 500-subject table is in JSON): B006 aggressive versus electronic/electronic (Jev-only/hybrid); B029 happy versus electronic/relaxed; B038 party versus electronic/electronic; B047 relaxed versus electronic/electronic; B057 sad versus electronic/relaxed. No names or audio are needed to review these differences.

All **3,000 final records succeeded**, 1,500 per arm, with zero active failures or internal retries. The first sandboxed DNS-blocked run's three terminal failure events and six internal retries are preserved separately as history (nine transport attempts, no response usage); the explicit DNS-only resume reissued those same keys, making **3,003 request events and 3,009 transport attempts** across both sessions. Successful-request p50/p95 latencies are 167.260/232.206 ms Jev-only, 167.730/237.771 ms hybrid, and **167.355/235.570 ms combined**. The local sequential result does not prove a production concurrent SLA.

Captured usage: Jev-only **1,493,634 input / 184,500 output tokens, $0.062732628**; hybrid **1,832,010 / 184,500, $0.076944420**; total **3,325,644 / 369,000, $0.139677048** at the published input-only rate. This is observed public-rate arithmetic, not an account invoice. At the benchmark's observed prompt shapes, both arms cost **$0.046559016** for one pass over 500 and the actual **$0.139677048** for three; projection to 5,583 completed production files is **$0.519877973 / $1.559633918**, and to 93,444 coarse windows individually **$8.701321382 / $26.103964147**. These are request-shape extrapolations, not a proposal to call Jev per window or a measured production bill. A one-arm Jev-only, one-pass 500-track run would have cost **$0.020910876**; hybrid **$0.025648140**. Catalog changes, concurrency, model/pricing changes, and credits can alter real cost.

## Verdict

The held-out result demonstrates a reliable and inexpensive request path, but Jev-only cannot substitute for the seven incumbent mood probabilities at the frozen threshold: the 35.8% dominant agreement and particularly weak happy/sad/relaxed fidelity are large operational changes. Hybrid follows incumbent scores much more closely, but still changes 35% of dominant outcomes while adding no independent audio evidence. This is a **NO-GO for replacement or a production hybrid layer under the present fidelity objective**, not a claim about which system better describes music to listeners.

## Recommendation

Retain Essentia unchanged for these seven moods. A future Jev semantic/metadata feature needs its own user-value question and independent evaluation; do not tune thresholds on this held-out set and then reuse it as unbiased confirmation. The [adoption decision](phaze-eswzd.4-jev-verdict.md) resolves each mood and records operational safeguards if a new proposal emerges.
