# Phaze analysis replacement evaluation, version 1

Status: **prepared, baseline not run**. `phaze-iq64v` is kept on a local bead branch. This document fixes the test contract before a replacement is measured. The eight-file production sample is in `docs/evaluation-phaze-iq64v-corpus.json`. It was selected in one read-only query against Phaze on lux, filtered to completed analyses owned by nox, using one deterministic SHA-seeded selection per format and duration band. The manifest records file IDs, SHA256, duration, codec, and source sample rate; it contains no archive pathname or filename. All eight files were reachable through nox's analysis container when their headers were probed. The sample totals 10.27 audio hours and has MP3, AAC/M4A, FLAC, 44.1 kHz and 48 kHz, and short, medium, long, and very long bands. This is a **performance and contract** sample; it has no independent labels yet and cannot establish quality on its own.

## Decision rule

A candidate passes only if **all** of these hold:

1. It produces every Phaze semantic result below. More attributes are welcome. The current 33 classifier-variant arrays are implementation detail, not a required output. A candidate supplies an adapter to Phaze's stored schema and downstream consumers; a JSON blob that merely validates at the HTTP boundary is insufficient.
2. It covers the complete audio timeline at least as finely as today's 30-second fine tier and 180-second coarse tier. Finer or overlapping windows are allowed. The only tolerated fine-tier omission is a trailing segment shorter than 15 seconds, matching today's rule. Every absent or failed window is counted explicitly.
3. It is no worse on independently annotated data under the predeclared metrics below. Current Phaze output is a comparator, never ground truth. Missing annotations remain **unscored**, not assumed correct.
4. On the same isolated vox, pinned input copies, image/model versions, resource allocation, and concurrency, it uses at most **half the current analyzer's end-to-end wall time** on the paired corpus, with no worse failure rate, no coverage regression, and no larger production-limit breach. Report the paired per-file ratios as well as the total; one very long file must not hide regressions elsewhere. The 2× threshold is the initial performance gate; change it only by versioning this contract before seeing candidate results.

The old 0.56–0.79×-duration measurements in `docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md` are historical context, **not** the baseline for this decision. The baseline must use the version that is actually deployed when the isolated run occurs.

## Result inventory and consumers

| Level | Required semantic output | Current source and consumers |
| --- | --- | --- |
| File | BPM and musical key (`<note> <major/minor>`) | `analysis` columns; file views, set/record facts, Camelot lookup |
| File | Seven named mood scores: acoustic, electronic, aggressive, relaxed, happy, sad, party | callback `mood` dict from `analysis_wire`; stored as a bounded string summary |
| File | Danceability; ranked genre/style scores; dominant style | `analysis` columns; genre grouping, similarity, proposals |
| File | Representative gender, tonal/atonal, voice/instrumental characteristic scores | the analysis child returns these in its representative `features`; the present callback does **not** forward that top-level object |
| Fine window | Index, start/end, BPM, musical key, read-time Camelot | `analysis_window`; timeline and harmonic/set projection |
| Coarse window | Index, start/end, mood, style, danceability, seven mood scores plus danceability/gender/tonality/voice (11 fixed scores), energy | `analysis_window` and `set_projection`; mood river, glyph, energy arc, set profiles |
| Both tiers | Natural total/analyzed counts and explicit failure gaps | `analysis` progress/completion, operator timeline |

The contract is grounded in `analysis_models.py`, `analysis_windows.py`, `analysis_wire.py`, `models/analysis.py`, `schemas/agent_analysis.py`, `routers/agent_analysis.py`, `set_projection.py`, `set_projection_writer.py`, and `proposal_context.py`. The child returns file-level `features` copied from one representative coarse window. The callback's `AnalysisWritePayload` omits that field; on current writes, the aggregate database `features` is primarily the wire overflow (danceability and energy), while the full characteristic predictions persist per coarse window. The persisted `style` list is aggregated over that timeline. `proposal_context` forwards **the stored aggregate** `features`, not the child's representative feature object. A replacement's adapter must preserve the meaning of these actual stored consumers. `set_projection_writer.positive_class_vector` currently reads Essentia-shaped variant predictions from coarse windows, so it needs a deliberate neutral-score adapter or schema migration before rollout. Do not manufacture three fake variant arrays to satisfy it.

The benchmark runner accepts the current child protocol (`--protocol current`) and a replacement-neutral terminal result (`--protocol neutral`) with the same file/window semantic fields plus `mood_scores`, `energy`, and `camelot`. It checks the wire shape, 11-score completeness, window geometry, and coverage. Neutral predictions need ranked `{name, score}` style rows. It does not assert equality of numbers to Essentia.

## Independent quality evidence

The annotation file is JSON with `schema_version: 1` and an `annotations` array; see `docs/analysis-evaluation-labels.example.json`. Every record uses `sha256` and may provide `bpm`, `key`, `binary` (a mapping from an `MOOD_ORDER` name to 0 or 1), and `genre` (one or more accepted labels in the **same** taxonomy as Phaze's ranked style output). Each record requires an independent annotation source, annotator count, and ambiguity/confidence note. Keep a separate held-out split from any candidate training or threshold tuning. Music with a disputed perceptual tempo or key remains explicitly ambiguous; do not force it into a single false reference.

The supplied scorer reports strict ±2% BPM accuracy, octave-aware ±2% accuracy, absolute BPM error, exact and [MIREX-weighted key](https://music-ir.org/mirex/wiki/2020%3AAudio_Key_Detection) scores, per-attribute F1 and Brier score at a declared 0.5 threshold, and genre top-1/top-5. Evaluate each of the 11 characteristics separately. For gender, tonality, and voice/instrumental, first document which physical class a score represents; the present `positive_class_vector` selects a class by its label convention, so a numeric value without that mapping is **not** a valid human-label comparison. Public [MTG-Jamendo classification annotations](https://github.com/MTG/mtg-jamendo-dataset/blob/master/derived/music-classification-annotations/README.md) cover most named characteristics but do not directly establish correctness of Phaze's Discogs400 hierarchy or changing moods within a multi-hour set. Those need independently annotated Phaze-representative excerpts.

For transition quality, collect time-stamped tempo, key, mood, and style changes in multi-hour sets, then report event precision/recall with matching tolerance no larger than one corresponding existing window. Also report window-aligned attribute scores and worst-case false-change rate. The initial scorer covers file-level labels; transition scoring remains an explicit evidence gap until such labels are collected. A candidate decision is **blocked** for any result family lacking an independent quality reference. Thresholds: no more than two percentage points below baseline for accuracy/F1/top-k/transition recall, no more than 0.02 above baseline Brier, and no lower exact coverage. Report paired confidence intervals and every per-stratum result; a gap or material regression in any family fails the gate. These tolerances are practical noninferiority margins, not claims that two classifiers are numerically equivalent.

## Offline run and evidence

`scripts/analysis_eval_stage.py` is the **later execution** command for staging. It queries lux with `BEGIN READ ONLY`, creates only the named private `/scratch` directories using datum's noninteractive sudo (the parent mount is root-owned), copies the selected sources from nox's analysis container using SHA names, streams them to vox, and verifies SHA256 on vox. Its command output contains IDs and counts only. Run it only for the isolated measurement window:

```bash
UV_CACHE_DIR=/private/tmp/phaze-iq64v-uv-cache uv run python -m scripts.analysis_eval_stage \
  --manifest docs/evaluation-phaze-iq64v-corpus.json \
  --nox-dir /scratch/phaze-iq64v-inputs \
  --vox-dir /scratch/phaze-iq64v-inputs
```

Run the harness inside a pinned production-equivalent job image on vox with the branch's source overlaid and the real model volume mounted read-only. Point `--audio-dir` at the SHA-named staged copies and `--output-dir` at a fresh scratch path. The harness verifies every SHA before timing; it spawns the actual `phaze.analysis_child`, consumes its JSONL progress/heartbeat/result protocol, uses the production wire adapter, and writes only scratch `metrics.json`, `prediction.json`, and `summary.json`. There is no production API callback, database write, or archive write. Use separate fresh output directories for the first pass and later repeat passes, first with `--workers 1`, then `--workers 4`. The phase label records run order; it does **not** prove an empty OS page cache. Each child always starts a fresh process and loads its models. Do not label a repeat pass as a true cold-cache experiment without an independently verified cache reset.

```bash
uv run python scripts/analysis_eval.py --manifest docs/evaluation-phaze-iq64v-corpus.json \
  --audio-dir /scratch/phaze-iq64v-inputs --models-dir /models \
  --output-dir /scratch/phaze-iq64v-baseline-single-first \
  --workers 1 --phase cold --protocol current \
  --run-context-json /scratch/phaze-iq64v-run-context.json
```

Start from `docs/analysis-evaluation-context.example.json`, replace the placeholders with the observed image digest and resource limits, and keep the filled file in scratch. Repeat with `--workers 4` and a new output directory. Run another pass with `--phase warm` if measuring repeat behavior. For a candidate, pass a JSON argv template through `--command-json` with `{audio}` and optionally `{models}`, and use `--protocol neutral`. Save the exact branch SHA, image reference/digest, model inventory/digests, CPU/thread settings, cgroup requests/limits, and manifest SHA with the report. The runner records code SHA, exact model-tree digest, Python/platform, thread environment, wall and CPU time, peak RSS, stage timestamps, p50/p95, per-run failures, and batch audio-hours/hour. Throughput at concurrency four is **batch audio duration divided by actual batch elapsed time**, not the sum of individual latencies. Report full file-to-serialized-result timing; database persistence is outside the isolated boundary and must be measured separately before a production rollout.

The scorer reads independent annotations and normalized predictions:

```bash
uv run python scripts/analysis_eval_score.py --annotations /scratch/phaze-iq64v-annotations.json \
  --run-dir /scratch/phaze-iq64v-baseline-single-first \
  --output /scratch/phaze-iq64v-baseline-quality.json
```

No annotations have been invented for the eight production samples. The quality baseline is pending independent labels and public labeled data. A nonzero score-command exit means a requested annotation lacked a prediction. A report with zero scored fields is **not** a quality pass.

Use `docs/analysis-evaluation-report-template.md` for the later operator record and verdict. It starts in a pending state and separates measured baseline, independent quality, and restoration evidence.

## Isolate and restore vox for the later run

Follow the measured operational sequence in `docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md` §8, checking it against the deployment as it exists on the run date. Record UTC times and before/after state, without archive identifiers:

1. Capture the deployed backend registry file and its checksum. Put Kueue's `host-compute-cluster-queue` in `Hold`, **never `HoldAndDrain`**. Let already running analysis pods finish. Confirm the node is quiescent.
2. Remove the `host-compute` backend from the deployed Phaze registry. The current control plane hot-reloads `backends.toml` (see `docs/k8s-burst.md` § Runtime config hot-reload); use its watched config path or HUP and verify the lane snapshot no longer offers vox to live analysis. Restart API/worker only if the deployed version still requires it. Keep local Phaze service available.
3. Run staging and measurement in an isolated pod on vox without a Kueue queue label or production callback credentials. Use read-only model and input mounts, scratch output, the same production CPU/thread policy, and recorded resource limits. Do not overcommit four concurrent runs beyond the node's physical resources.
4. Remove the measurement pod and scratch files after preserving anonymized evidence. Restore the captured registry **byte-for-byte**, verify its checksum, release the queue to `None`, and verify lane availability plus admission of new real work. If any step fails, restore live service before continuing analysis of the benchmark.

No vox isolation, staging transfer, benchmark, or queue/configuration change was performed while preparing this branch. The operational and measured baseline acceptance criteria for `phaze-iq64v` remain open until that execution window.
