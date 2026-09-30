# UpCloud paired analysis evaluation: prepared handoff

The first and repeat incumbent passes finished on the same UpCloud host, each with eight complete files and zero contract failures. Their measured playback speeds were 3.156× and 2.950×. The one-time local completion monitor has been removed. No candidate analysis has run.

## Frozen inputs and baseline evidence

- Host: `datum@209.50.61.190`, UUID `0068802d-f350-4a44-9ca9-b79c960b749d`, 4 vCPU / 8 GiB plan. The Docker analysis limit is 4 CPUs and 7 GiB.
- Evaluation root: `/home/datum/phaze-iq64v`; SHA-named audio under `samples`, incumbent models under `models`, source and launchers under `code`, private reports under `results`.
- Baselines: `results/baseline-1` and `results/baseline-2`; validated `results/baseline-report.json` and `.md`.
- Frozen homelab DB reference: `results/homelab-reference.json`, SHA256 `19b19661afcf50b068841c203f9519c3d714dc56d016e03313c0b4d5d9f75ae4`. The export script is `scripts/analysis_eval_export_reference.py`. It used one read-only repeatable-read transaction and contains only manifest IDs/SHA, current analysis values/windows, and source genre tags. It has eight files and 1,382 windows; no filename, archive path, artist, title, or album.
- Incumbent agreement reports: `results/baseline-1-reference-comparison.json` and `baseline-2-reference-comparison.json`; both align every stored reference window. These describe agreement with existing Phaze results, not independently verified accuracy.
- Source-tag map: `code/docs/analysis-evaluation-tag-map.json`. `results/baseline-1-tag-score.json` and `baseline-2-tag-score.json` each score four of five nonempty genre tags at top-1/top-5 = 1.0. The ambiguous `Hardcore` tag is excluded explicitly. These are broad family matches against embedded tags, not independent listening labels.

## Stage a candidate

1. Put the candidate program under `/home/datum/phaze-iq64v/candidate/`. It must emit the harness's JSONL progress/heartbeat/result protocol and a neutral result satisfying the semantic and window contract in `docs/analysis-evaluation.md`. The candidate directory is private and mounted read-only at `/candidate` during the run.
2. Use a digest-pinned image with `/app/.venv/bin/python`, Phaze harness dependencies, and the candidate's additional dependencies. Deriving it from the pinned incumbent image keeps the harness runtime consistent. Record its exact digest; a mutable image tag is rejected. Stage any candidate models under `/home/datum/phaze-iq64v/candidate-models/`. That directory is already present and may stay empty for a model-free candidate; the harness records the empty-tree digest and zero files. It is mounted read-only at `/models` and fingerprinted before timing.
3. Copy `code/docs/analysis-evaluation-candidate-command.example.json` to `code/docs/candidate-command.json` and replace the argv with the real candidate entrypoint. The JSON array must include `{audio}`; `{models}` is optional. Copy `code/docs/analysis-evaluation-candidate-context.example.json` to `code/docs/candidate-run-context.json` and fill in the image digest and candidate source commit. Keep the host, CPU, and memory values unchanged.
4. Predeclare the candidate's acceptable disagreement tolerances and its source-tag taxonomy behavior before running it. The current DB values are an incumbent reference, and equality is not required. Source-tag scoring is exploratory because only four tags have unambiguous broad mappings.

## Smoke check, then run once the candidate is staged

Run the shortest SHA-selected file first. This uses the same image, limits, protocol, and model mount but writes only `results/candidate-smoke-1`. It does not produce a verdict:

```bash
ssh datum@209.50.61.190
/home/datum/phaze-iq64v/code/run-candidate-v2.sh \
  candidate-smoke-1 candidate-command.json \
  'ghcr.io/OWNER/CANDIDATE@sha256:REPLACE_WITH_DIGEST' \
  /home/datum/phaze-iq64v/candidate-models \
  candidate-run-context.json smoke
python3 -c 'import json; p="/home/datum/phaze-iq64v/results/candidate-smoke-1/summary.json"; d=json.load(open(p)); print(d["runs"], d["failed_runs"], d["protocol"])'
```

Proceed only if this prints `1 0 neutral`. The smoke result checks the output protocol and semantic contract on one file; it does not predict full-corpus performance.

Then run the complete paired candidate batch:

```bash
/home/datum/phaze-iq64v/code/run-candidate-v2.sh \
  candidate-1 candidate-command.json \
  'ghcr.io/OWNER/CANDIDATE@sha256:REPLACE_WITH_DIGEST' \
  /home/datum/phaze-iq64v/candidate-models \
  candidate-run-context.json
```

The launcher refuses an existing run ID, obtains the evaluation lock, checks the candidate image/context and matched host limits, then runs one worker against the same eight SHA-verified audio copies. It writes `results/candidate-1/summary.json`, `candidate-1-reference-comparison.json`, `candidate-1-tag-score.json`, and `candidate-1-report.json`. The final report checks protocol and corpus coverage, zero child/contract failures, and speedup against the **faster** of the two incumbent batch times. The performance gate is 2×. It also reports per-file speedups and whether each file finished faster than playback. All commands run with no network inside the containers and no Phaze callback or database write.

The existing `finish-evaluation.py` produced the baseline report and assumes an incumbent image/model tree; use `run-candidate-v2.sh` for a candidate with its own image or models. The baseline output remains untouched. A failed candidate run keeps its partial directory for diagnosis; use a new run ID for a retry.

## Verdict boundary

The staged pipeline can establish runtime, output/window contract, agreement with stored Phaze results, and broad agreement with four source genre tags. It cannot establish that BPM, key, all 11 characteristic scores, style transitions, or mood transitions are at least as accurate as the incumbent: the current eight files have no source labels for those families. Mark them `accuracy unverified` in the verdict until independent labels are collected. The DB's analysis rows remain the authoritative record of current Phaze behavior, not independent truth about the audio.
