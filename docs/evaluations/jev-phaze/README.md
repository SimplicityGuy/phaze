# Jev versus Essentia: investigation archive

This long-lived branch holds the prototype code and aggregate conclusions for
study. It makes no change to Phaze's production classifier path and is not
intended to merge into `main`.

The operator designated stored Essentia mood probabilities as the reference
for this fidelity test—not as independent human ground truth. A 12-track pilot
established the request and evaluation path. A distinct, artist/release-grouped
500-track benchmark compared two Jev arms, each with three repeats:

| Measure versus Essentia | Jev-only | Hybrid (given Essentia scores) |
| --- | ---: | ---: |
| Macro average precision / F1 | 0.726 / 0.672 | 0.989 / 0.920 |
| Mean squared error | 0.04715 | 0.00721 |
| Dominant mood agreement | 179/500 (35.8%) | 325/500 (65.0%) |

All 3,000 benchmark requests succeeded. Estimated public-rate Jev cost was
$0.139677048, with combined p50/p95 latency 167.355/235.570 ms. The
[verdict](phaze-eswzd.4-jev-verdict.md) and [ADR 0019](../../design/0019-jev-mood-fidelity-no-go.md)
record the NO-GO decision for replacing or augmenting these seven Essentia
mood outputs. Hybrid's better fidelity depends on being given the incumbent
scores and does not eliminate Essentia.

## Publication boundary

This public branch intentionally omits production-derived per-track manifests,
Jev response captures, classifier vectors, identifiers, and disagreement rows.
The original detailed evidence remains **local-only** on
`jev-investigation-local-evidence`; it was not made an ancestor of this branch
and must not be pushed to a public remote without a separate data review.
The aggregate report documents what was measured, but the public branch alone
cannot independently recompute the published scores.

[`run_jev_pilot.py`](run_jev_pilot.py),
[`run_jev_benchmark.py`](run_jev_benchmark.py), and
[`evaluate_jev_pilot.py`](evaluate_jev_pilot.py) are study utilities for a
separately supplied, appropriately authorized corpus. The
[`essentia_bridge/`](essentia_bridge/) proof of concept uses synthetic input.
Live calls read `TYPESAFE_API_KEY` only from the process environment; never
put a key in code or a committed fixture. No new Jev API calls are needed to
inspect this branch.

The synthetic/unit tests can be run with:

```sh
pytest -q test_run_jev_pilot.py
(cd essentia_bridge && python3 -m unittest discover -s . -p 'test_*.py')
```
