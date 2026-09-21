# Jev versus Essentia: investigation archive

This directory is a de-identified, offline case study. It lives on the
long-lived `jev-investigation` branch so it can be studied without changing
Phaze's production classifier path or merging into `main`.

## What was tested

- A 12-track pilot and a separate 500-track benchmark drawn from already
  completed Phaze analyses.
- Two Jev arms, each repeated three times: `jev_only` received display
  metadata plus timing/key evidence; `hybrid` additionally received the seven
  Essentia mood probabilities. Essentia outputs were the designated reference,
  not independent human ground truth.
- The 500-track run completed 3,000 successful requests. Jev-only reached
  macro average precision 0.726, macro F1 0.672, MSE 0.04715, and dominant
  mood agreement 179/500. Hybrid reached 0.989, 0.920, 0.00721, and 325/500.
  Estimated public-rate Jev cost was $0.139677 for the 3,000 requests.

The [decision record](../../design/0019-jev-mood-fidelity-no-go.md) is NO-GO
for replacing or augmenting Essentia mood outputs. The hybrid arm's stronger
score depends on having Essentia's scores as input; it does not remove the
existing classifier. Read the [pilot](phaze-eswzd.2-jev-mood-runs.md),
[evaluation](phaze-eswzd.3-jev-evaluation.md), and
[verdict](phaze-eswzd.4-jev-verdict.md) for the full limitations.

## Privacy and reproducibility boundary

Production artist, track, album, style-summary, file ID, and leakage-group
values were replaced with stable local pseudonyms. Exact durations, BPMs, and
keys were removed from the machine-readable corpus to reduce
re-identification risk. `P01`–`P12` and `B001`–`B500` are subject codes, not
production identifiers. Captured Jev answers, Essentia probabilities, token
counts, and latency measurements remain available for *offline evaluation*.
The published manifests cannot reproduce the original Jev prompts or API
responses, and the benchmark runner rejects them as live checkpoints. The
SQL is illustrative: its pilot exclusions are redacted placeholders.

No audio, production filesystem paths, database credentials, or TypeSafe API
key are in this branch. The bridge prototype in
[`essentia_bridge/`](essentia_bridge/) uses synthetic input and reads any
API key only from `TYPESAFE_API_KEY` in the environment. Do not put a secret
in code or commit a new live run capture without a separate privacy review.

To recompute offline scores, from this directory run:

```sh
pytest -q test_evaluate_jev_pilot.py test_run_jev_pilot.py test_run_jev_benchmark.py
python3 evaluate_jev_pilot.py --help
(cd essentia_bridge && python3 -m unittest discover -s . -p 'test_*.py')
```

The study code was copied from the local bead worktrees into this clean-rooted
branch. The raw worktree commits were deliberately not made ancestors of this
branch, because those commits contained production metadata.
