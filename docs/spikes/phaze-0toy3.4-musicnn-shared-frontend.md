# phaze-0toy3.4: do the eleven deployed musicnn mood graphs share a front end?

Part of the spike molecule `phaze-0toy3` (where analysis CPU goes and how to cut it). The lever under test
is shortlist rank 1, "A2", from the cloud version of `docs/spikes/phaze-8ghbw-analysis-alternatives.md`
(commit `816b2ff4`). This spike reads public model files only. It uses no archive audio, no production
access and no stored values.

## Question

Each coarse (180 s) window runs eleven separate `musicnn_msd` graphs, one per mood characteristic. Per
`docs/spikes/phaze-fzubr-analysis-cpu-profile.md` they cost **96.8 cpu-s per coarse window** (about 9.0
per graph), which is 84.5% of analysis CPU. A2 proposed that if the eleven graphs are byte-identical up to
the classifier split (`model/dense/BiasAdd`), one front-end pass could feed eleven small heads: one backbone
pass per window instead of eleven, with zero label drift.

So: are the weights identical up to the split? If yes, does a split-graph sweep hold to the
`phaze-mqq5` tolerance, and what does it cost? If no, which layer is the first to differ in each graph,
and what does that rule out? Where Essentia lacks a capability the lever needs, what would an upstream
contribution, a fork or direct TensorFlow cost? And what is the licence question for the operator?

## Method

1. **Which graphs are deployed.** `src/phaze/services/analysis_models.py`: `_select_model_sets()` defaults to
   `msd` (`PHAZE_ANALYSIS_MODEL_VARIANTS` unset, confirmed on the deployed agent by `phaze-fzubr`), which keeps
   the `musicnn_msd` model of each of the 11 sets in `FULL_MODEL_SETS`. That gives ten `*-musicnn-msd-2` graphs
   plus `voice_instrumental-musicnn-msd-1`. All are built by `es.TensorflowPredictMusiCNN`
   (`_construct_classifier` in `src/phaze/services/analysis.py`).
2. **Files.** Downloaded on 2026-11-13 from `https://essentia.upf.edu/models/classifiers/<set>/` into the
   worktree's gitignored `models/` directory, along with the MSD MusiCNN backbone `msd-musicnn-1.pb` from
   `models/feature-extractors/musicnn/` and the hosted `LICENSE`. Nothing was committed. The server timed out
   on many requests (the TLS flakiness `src/phaze/scripts/download_models.py` already records). Ten of the
   eleven graphs downloaded before the dispatcher paused downloads because the host disk was full.
   For the comparison, `mood_party-musicnn-msd-2.pb` was read from the operator's local model cache
   (`~/essentia-models`, the set `tests/conftest.py` uses for the `phaze-28883` real-model pin). After the
   hold lifted (2026-11-14) it was re-downloaded from the server into the worktree's gitignored
   `.cache/0toy3/`. The server copy matches the cache copy byte for byte: `cmp` reports no difference, the
   SHA-256 values are equal, and the size equals the server's `Content-Length` (3,239,548, which is
   `_PB_MUSICNN_COMMON` in `download_models.py`). For the other ten graphs, the fresh download and the cache
   copy also have identical SHA-256. So every file analysed below is the server's bytes.
3. **Static comparison, no inference.** A throwaway TensorFlow 2.21.0 environment (`uv run --no-project
   --python 3.13 --with tensorflow`, no `pyproject.toml` change) parsed each frozen `GraphDef`. For every node
   it recorded name, op and inputs. For every `Const` node it recorded dtype, shape and the SHA-256 of the
   value bytes (`tf.make_ndarray(...).tobytes()`). Each graph was compared with every other graph (all 55
   pairs) and with `msd-musicnn-1`. The scripts are kept in per-bead scratch outside the repository, not
   committed (see Cleanup).
4. **Classification of constants.** A `Const` whose name ends in `kernel`, `bias`, `gamma` or `beta` is
   **trainable** (44 per graph). Everything else is **frozen**: batch-norm `moving_mean` / `moving_variance`
   (22), plus 40 structural constants (axes, paddings, reshape shapes, epsilons). That makes 62 frozen
   constants per graph.
5. **Timing the shared input stage (2026-11-14).** One 180 s synthetic window (16 kHz, two tones plus
   fixed-seed noise, generated in-process, no archive audio), on the development Mac, using the repo venv's
   `essentia-tensorflow` with `TF_NUM_INTRAOP_THREADS=1`, `TF_NUM_INTEROP_THREADS=1` and `OMP_NUM_THREADS=1`,
   as deployed. The script times `FrameGenerator(frameSize=512, hopSize=256, startFromZero=True)` plus
   `TensorflowInputMusiCNN` over the whole window with `process_time`: one warm-up, then 7 timed repetitions.
   (A second run then compared framings; centred framing yields 2 more frames, 11,251 against 11,249, which
   does not move a per-window cost of this size.) For scale, it also times
   one deployed-style graph call, `TensorflowPredictMusiCNN(batchSize=32)` on the `mood_party` graph, on the
   same window: 3 repetitions after a warm-up call. Finally, it feeds the precomputed mel frames, cut into
   187-frame patches at a 93-frame hop, through the generic `TensorflowPredict` and compares the result with
   `TensorflowPredictMusiCNN` patch by patch.

## Evidence

### Files

| graph | bytes | SHA-256 (file) | nodes | consts | graph structure vs `mood_acoustic` |
|---|---|---|---|---|---|
| `mood_acoustic-musicnn-msd-2` | 3,239,548 | `052c4caf…654a8856` | 268 | 106 | (reference) |
| `mood_electronic-musicnn-msd-2` | 3,239,548 | `e4b35ea4…5bf79f66` | 268 | 106 | identical |
| `mood_aggressive-musicnn-msd-2` | 3,239,548 | `954c6eeb…76d4a6f2` | 268 | 106 | identical |
| `mood_relaxed-musicnn-msd-2` | 3,239,548 | `2ba595cf…5004c4bc` | 268 | 106 | identical |
| `mood_happy-musicnn-msd-2` | 3,239,548 | `db888fca…4a6a17fa` | 268 | 106 | identical |
| `mood_sad-musicnn-msd-2` | 3,239,548 | `20e2a3e0…44ad50aa` | 268 | 106 | identical |
| `mood_party-musicnn-msd-2` (verified against the server, method step 2) | 3,239,548 | `0961cd73…4ffa0154` | 268 | 106 | identical |
| `danceability-musicnn-msd-2` | 3,239,548 | `08e9a9f7…9ac2f0b7` | 268 | 106 | identical |
| `gender-musicnn-msd-2` | 3,239,548 | `374a9d34…39bee1c7` | 268 | 106 | identical |
| `tonal_atonal-musicnn-msd-2` | 3,239,548 | `17132b3f…cd89510e` | 268 | 106 | identical |
| `voice_instrumental-musicnn-msd-1` | 3,239,625 | `e513e748…7dcf4244` | 271 | 106 | same layers, older export (below) |
| `msd-musicnn-1` (backbone, not deployed) | 3,197,999 | `cdea0722…38d04f3e` | 264 | 104 | same backbone, 50-tag head, no `dense_2` |

The ten `-msd-2` graphs have identical topology, node for node. `voice_instrumental-musicnn-msd-1` has the
same layers and shapes, but it was exported by an older TensorFlow (`FusedBatchNorm` instead of `FusedBatchNormV3`,
`Add` instead of `AddV2`, `dropout` `Identity` nodes, two extra `Placeholder`s). It also has a **different head**:
`dense_1` feeds `dense_2` directly, with no ReLU between them, where the `-msd-2` graphs apply `model/dense_1/Relu`.
Every graph takes `model/Placeholder` `[-1, 187, 96]` and emits `model/Sigmoid` (2 classes).

### Identity, all 55 pairs

| quantity | result |
|---|---|
| trainable tensors identical between any two deployed graphs (max over 55 pairs) | **0 of 44** |
| frozen constants identical between any two deployed graphs (min over 55 pairs) | 62 of 62 |
| frozen constants identical to `msd-musicnn-1` (min over the 11) | 62 of 62 |
| trainable tensors identical to `msd-musicnn-1` (max over the 11) | **0** |
| smallest max\|Δ\| of any trainable tensor between any two graphs | 2.79 × 10⁻⁴ |

The frozen batch-norm statistics are identical in all twelve files, including the backbone, and are aligned
channel for channel. So all eleven heads were fine-tuned from `msd-musicnn-1` with batch-norm statistics
frozen, and **every trainable weight in the network was updated**, from the first layer to the last.
Aligned statistics also rule out the differences being a channel permutation. The smallest difference
anywhere (2.79 × 10⁻⁴ max\|Δ\| on one tensor pair) is more than three orders of magnitude above float32
re-export noise, and it is the closest case. Typical layers differ by tens of percent.

### Per-layer comparison, all eleven graphs

Each cell is the relative L2 distance, ‖W_graph − W_backbone‖ / ‖W_backbone‖, of that layer's main tensor
(`kernel`, or `gamma` for batch norm) from `msd-musicnn-1`. Any non-zero value means the layer differs.
Layers are listed in graph order. The first row, `batch_normalization`, is the input normalisation and the
first op with weights, applied directly to the mel patch. `dense_1` and `dense_2` have no backbone
counterpart of the same shape (the backbone's `dense_1` is `[200, 50]`, the MSD tag layer), so they are
marked n/a. They differ pairwise between all graphs (0 of 44 above).

| layer | shape | acoustic | electronic | aggressive | relaxed | happy | sad | party | danceability | gender | tonal_atonal | voice_instrumental |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `batch_normalization` | [1] | 0.020 | 0.016 | 0.068 | 0.057 | 0.032 | 0.039 | 0.041 | 0.054 | 0.307 | 0.004 | 0.050 |
| `conv2d` | [7, 38, 1, 204] | 0.420 | 0.394 | 0.542 | 0.632 | 0.375 | 0.533 | 0.456 | 0.672 | 1.525 | 0.102 | 0.521 |
| `batch_normalization_1` | [204] | 0.034 | 0.027 | 0.056 | 0.062 | 0.031 | 0.041 | 0.035 | 0.073 | 0.131 | 0.011 | 0.076 |
| `conv2d_1` | [7, 67, 1, 204] | 0.688 | 0.633 | 0.809 | 1.013 | 0.591 | 0.790 | 0.692 | 1.154 | 2.793 | 0.109 | 0.866 |
| `batch_normalization_2` | [204] | 0.042 | 0.033 | 0.070 | 0.077 | 0.039 | 0.051 | 0.045 | 0.094 | 0.166 | 0.013 | 0.098 |
| `conv2d_2` | [128, 1, 1, 51] | 0.793 | 0.826 | 0.705 | 0.979 | 0.814 | 0.837 | 0.605 | 0.721 | 2.270 | 0.214 | 0.701 |
| `batch_normalization_3` | [51] | 0.062 | 0.068 | 0.059 | 0.056 | 0.070 | 0.048 | 0.053 | 0.113 | 0.140 | 0.031 | 0.073 |
| `conv2d_3` | [64, 1, 1, 51] | 0.283 | 0.421 | 0.307 | 0.458 | 0.336 | 0.448 | 0.298 | 0.300 | 1.063 | 0.143 | 0.296 |
| `batch_normalization_4` | [51] | 0.058 | 0.042 | 0.111 | 0.123 | 0.054 | 0.077 | 0.065 | 0.152 | 0.241 | 0.013 | 0.160 |
| `conv2d_4` | [32, 1, 1, 51] | 0.073 | 0.100 | 0.081 | 0.161 | 0.088 | 0.112 | 0.076 | 0.106 | 0.360 | 0.055 | 0.088 |
| `batch_normalization_5` | [51] | 0.048 | 0.032 | 0.095 | 0.107 | 0.043 | 0.063 | 0.052 | 0.138 | 0.361 | 0.007 | 0.148 |
| `conv2d_5` | [7, 561, 1, 64] | 0.563 | 0.547 | 0.848 | 0.907 | 0.608 | 0.702 | 0.618 | 1.021 | 2.031 | 0.232 | 0.877 |
| `batch_normalization_6` | [64] | 0.079 | 0.063 | 0.107 | 0.115 | 0.077 | 0.097 | 0.081 | 0.147 | 0.232 | 0.019 | 0.129 |
| `conv2d_6` | [7, 64, 1, 64] | 0.267 | 0.229 | 0.336 | 0.313 | 0.281 | 0.278 | 0.295 | 0.391 | 1.053 | 0.181 | 0.420 |
| `batch_normalization_7` | [64] | 0.009 | 0.007 | 0.012 | 0.012 | 0.009 | 0.011 | 0.010 | 0.013 | 0.061 | 0.002 | 0.013 |
| `conv2d_7` | [7, 64, 1, 64] | 0.274 | 0.210 | 0.350 | 0.306 | 0.273 | 0.274 | 0.297 | 0.355 | 1.216 | 0.134 | 0.371 |
| `batch_normalization_8` | [64] | 0.004 | 0.002 | 0.006 | 0.005 | 0.003 | 0.003 | 0.004 | 0.008 | 0.136 | 0.001 | 0.008 |
| `batch_normalization_9` | [1506] | 0.028 | 0.016 | 0.040 | 0.032 | 0.020 | 0.024 | 0.026 | 0.063 | 0.242 | 0.006 | 0.060 |
| `dense` (the A2 split, `model/dense/BiasAdd`) | [1506, 200] | 1.001 | 0.731 | 1.131 | 1.239 | 0.827 | 1.016 | 1.111 | 1.457 | 1.895 | 0.502 | 1.100 |
| `batch_normalization_10` | [200] | 0.017 | 0.012 | 0.019 | 0.022 | 0.014 | 0.016 | 0.020 | 0.029 | 0.157 | 0.006 | 0.023 |
| `dense_1` | [200, 100] | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| `dense_2` | [100, 2] | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

**First differing layer, per graph:** `batch_normalization` (its `gamma` and `beta`), the first layer with
weights, in **all eleven** graphs, compared both with each other and with the backbone. No prefix of any
graph that does more than a fixed reshape and pad is shared.

### What is shared: the non-learned input stage

`TensorflowPredictMusiCNN` computes its mel-band input (`TensorflowInputMusiCNN`: 16 kHz, 96 bands, cut into
187-frame patches at a 93-frame hop) inside every call. The parameters are identical for all eleven graphs:
every graph declares the same `[-1, 187, 96]` placeholder and the same `patchSize` / `patchHopSize` defaults,
and phaze passes only `graphFilename` and `batchSize`. So that stage is computed eleven times per window with
identical results. Computing it once is the only exactly equivalent sharing left. `phaze-fzubr` timed each
graph call as a whole and does not separate it out, so it was measured here (method step 5):

| quantity, one 180 s window, one TF thread, development Mac | result |
|---|---|
| mel input (`FrameGenerator` + `TensorflowInputMusiCNN`), cpu-s, median of 7 | **0.089** (range 0.088 to 0.091; wall 0.090 s) |
| mel frames / patches | 11,249 frames in the timed run (`startFromZero=True`), 11,251 with centred framing / 119 patches either way |
| one full graph call (`TensorflowPredictMusiCNN`, `batchSize=32`), cpu-s, median of 3 | **8.72** (range 8.52 to 8.80) |
| mel input as a share of one graph call | 1.0% |
| compute-once path vs `TensorflowPredictMusiCNN`, max \|Δ\| over 119 patches × 2 classes, centred framing (`FrameCutter` default `startFromZero=False`) | **0.0** (bit-identical) |
| the same with `startFromZero=True` (11,249 frames) | 3.9 × 10⁻³ per patch (not equivalent) |

The full graph call here (8.72 cpu-s) is close to `phaze-fzubr`'s production figure of about 9.0 cpu-s
per graph, so the share should carry over. Computing the input once instead of eleven times saves
10 × 0.089 ≈ **0.89 cpu-s per coarse window**, about **0.9%** of the 96.8 cpu-s mood sweep. The
compute-once path is exact, but only with Essentia's default centred framing. The obvious
`startFromZero=True` choice shifts every patch by half a frame and is not equivalent. The exact result
covers one graph and one synthetic window. It was not repeated over all eleven graphs or held to the
`phaze-28883` real-model pin.

### Essentia capabilities (installed `essentia-tensorflow` 2.1-beta6-dev, read from the algorithm docs)

| capability the follow-ups would need | present? | where |
|---|---|---|
| run a graph only up to an inner node | yes | `TensorflowPredictMusiCNN(output=...)`, `TensorflowPredict(outputs=[...])`; `docs/spikes/phaze-ytgo.2-essentia-embeddings.md` already read `model/dense/BiasAdd` this way |
| feed a precomputed tensor (an embedding or mel patches) into a graph | yes | `TensorflowPredict(inputs=[...])` reads named input tensors from a `Pool`; `TensorflowPredict2D` runs heads on embeddings (already used for the EffNet heads in this epic) |
| compute the mel input once and feed several graphs | yes, with patching done in phaze | `FrameGenerator` (default centred framing) + `TensorflowInputMusiCNN` + `TensorflowPredict`, with the 187/93 patching done in Python; measured bit-identical to `TensorflowPredictMusiCNN` on one graph and window (above) |
| one network pass that serves eleven different backbones | not possible in any tool | the weights differ (above); no runtime can share computation over different weights |

## Verdict

**No. The eleven deployed graphs share no learned layer, so A2 is ruled out.** Every trainable tensor
(0 of 44, over all 55 pairs) differs from the input batch norm onward, in all eleven graphs. They are eleven
independent full fine-tunes of `msd-musicnn-1`. Only the frozen batch-norm statistics and the graph topology
are common. That rules out:

1. **One front-end pass at the survey's split (`model/dense/BiasAdd`) or at any earlier node.** Exact
   equivalence would need identical weights up to the split, and they already differ at the first layer.
   The `phaze-mqq5` prototype (aggregated |Δ| ≤ 10⁻³, zero top-1 flips) was therefore not run: it was
   conditional on identity.
2. **Feeding the `msd-musicnn-1` embedding into the deployed graphs' own `dense_1`/`dense_2` heads.** Each
   head was trained on its own fine-tuned features, not the backbone's, so this is a different model
   (label drift), not an optimisation.
3. **Weight deduplication, pruning shared tensors, or merging the eleven graphs to save backbone FLOPs.** No
   tensor is shared. A merged graph would still compute eleven backbones; it could save only per-call
   overhead and the duplicated mel input.

The zero-drift cost floor of the mood sweep therefore stays at **eleven full backbone passes per coarse
window** (96.8 cpu-s at one TF thread, `phaze-fzubr`). The only exactly equivalent sharing left is the
non-learned mel input. It measured **0.089 cpu-s per 180 s window**, 1.0% of one graph call, so computing it
once saves about **0.89 cpu-s per coarse window (0.9% of the sweep)**. Done with Essentia's default centred
framing, that path is bit-identical to `TensorflowPredictMusiCNN` on the window tested.

No Essentia capability is missing for any of this. The lever fails because the weights differ, not because
of a tooling gap. Sharing the mel input, partial graphs and embedding inputs all work with the installed
algorithms. A fork or upstream change cannot make eleven different networks cost one.

## Recommendation

1. **Drop A2 from the plan** (`phaze-uv8s3`). It cannot deliver an 11-to-1 cut at zero drift for these
   models.
2. **Every remaining large saving on the mood arm carries label drift and needs the head-fit measurement.**
   The survey's rank-1 path (a mood head on the already-deployed Discogs-EffNet embedding, fitted to
   reproduce the stored musicnn scores) is unchanged by this result. A second candidate surfaced here and
   is worth fitting **in the same measurement**: heads on the **`msd-musicnn-1` embedding**. It is the
   backbone all eleven deployed graphs were fine-tuned from (identical batch-norm statistics, smallest
   per-layer distance for `tonal_atonal`), so its features are plausibly closer to the deployed models'
   than EffNet's. It costs one musicnn pass (about 9 cpu-s) per window instead of 11 (about 97). Essentia
   also hosts its own heads on that embedding: `classification-heads/danceability/danceability-msd-musicnn-1.pb`
   answered `200` with 82,458 bytes to a HEAD request. Requests for two other characteristics timed out, so
   coverage of all eleven is unverified. Like the EffNet heads, these are different models and would shift
   levels. Their agreement with the stored scores is unmeasured.
3. **Do not build mel-input sharing for its CPU.** It saves 0.89 cpu-s of the 96.8 per coarse window (0.9%).
   In return phaze would own the framing and patching that `TensorflowPredictMusiCNN` now does internally,
   and the measurement shows how easily that goes wrong: `startFromZero=True` is off by 3.9 × 10⁻³ per
   patch, and only the default centred framing is exact. If it is ever built for another reason, it needs
   its own equivalence check over all eleven graphs against the `phaze-28883` real-model pin.
4. **Threads remain the zero-drift wall-time lever.** `docs/spikes/phaze-phjwi-tf-threads.md` covers it.

### Contribution and fork assessment (requested by the operator)

The operator's instruction (2026-11-13, on selecting this follow-up), quoted from the bead: *"For any of the
above, if theree are missing features, evaluate what the contribution guidelines are (or forking
guidelines) and evaluate whether we can address the gaps"*. No capability gap blocks this lever: the blocker
is the weights. For completeness, here is how each route would work if one were needed later (for example,
an Essentia algorithm that takes precomputed mel patches, or one that runs several graphs in one call):

- **(a) Upstream contribution.** Essentia is AGPL-3.0 (GitHub licence API, `MTG/essentia`). Its policy
  (`doc/sphinxdoc/contribute.rst`) is pull requests on GitHub under a Developer's Certificate of Origin, plus a
  **signed Individual Contributor Licence Agreement for any contribution over 20 lines of code**. Feature
  ideas go to GitHub issues first. The project is active: an ONNX predictor was merged on 2026-10-22 and
  the latest master commit is from 2026-11-05. Effort for a "precomputed mel input" option to
  `TensorflowPredictMusiCNN`: small C++ change plus tests, but more than 20 lines, so the CLA applies. phaze
  would gain it only after a release and an `essentia-tensorflow` wheel that includes it. Phaze's pin is
  `dev1438+` with cp314 wheels, and the arm64 agent compiles a pinned commit.
- **(b) Fork.** It means building and hosting `essentia-tensorflow` wheels for every platform phaze runs
  (macOS arm64/x86_64, linux x86_64) and rebasing the arm64 agent's pinned build, indefinitely. Under AGPL-3.0
  §13, a modified Essentia whose functionality users reach over a network must offer its source to those
  users. For a single-operator deployment that is trivially met, but it is an obligation the operator should
  know about. This is high maintenance cost for, at best, the small mel-sharing saving: not worth it.
- **(c) TensorFlow directly.** Running the frozen graphs through the TensorFlow Python API needs no Essentia
  change. phaze would have to reproduce the mel front end and patching itself, and add a TensorFlow
  dependency next to the TensorFlow that `essentia-tensorflow` already bundles. Whether a cp314 TensorFlow
  wheel exists for every phaze platform was **not checked** (this spike used 3.13). It saves nothing over
  (a) for these models, because there is nothing to share.

### Licence question for the operator (not decided here)

The hosted `https://essentia.upf.edu/models/LICENSE` (fetched 2026-11-13, SHA-256 `f0c07609…174f3ea4`)
contradicts itself in three places. Its header says *"Creative Commons Attribution-NonCommercial-NoDerivatives
4.0"*. Its summary then lists *"Adapt — remix, transform, and build upon the material"*, a freedom NoDerivatives
does not grant. It links to `http://creativecommons.org/licenses/by-nc-sa/4.0/legalcode` (ShareAlike). The
full legal text appended below it is BY-NC-ND 4.0. None of the graphs' `.json` metadata carries a licence field.

What bears on phaze if the appended BY-NC-ND 4.0 text governs:

- §1(a) defines Adapted Material as the Licensed Material *"translated, altered, arranged, transformed, or
  otherwise modified"*. Truncating a graph at an inner node, merging several graphs into one, or re-exporting
  a graph would plausibly be altering it. Whether trained weights attract copyright at all is itself
  unsettled.
- §2(a)(1)(B) nonetheless grants the right to *"produce and reproduce, but not Share, Adapted Material for
  NonCommercial purposes only"*, and §3(a) restates that there is no permission *"to Share Adapted Material"*.
  So under ND, a derived graph made and used privately on the operator's own host would appear to be
  permitted, but committing it to the repository, baking it into a published image or offering it for
  download would not.
- **Under this spike's verdict, A2 needs no derived graph at all**, so the question no longer gates A2. It
  still gates two things: (i) any merged or re-exported graph (for example, to share the mel input inside
  one graph rather than through Essentia's `Pool` API, which needs no derivative), and (ii) whether a head
  **fitted on the outputs or embeddings** of these models is Adapted Material. Item (ii) bears directly on
  recommendation 2.

**For the operator, who is asking UPF:** which licence governs the hosted model files, BY-NC-SA 4.0 or
BY-NC-ND 4.0? And under that licence, is (i) a privately produced, never-distributed truncated or merged
graph, or (ii) a new head trained on the models' embeddings or scores, a permitted use for a single-operator,
non-commercial deployment? This spike does not decide either.

## What was not measured

- **The mel-input timing on the production host.** It was timed on the development Mac. The full graph call
  there (8.72 cpu-s) is within 4% of `phaze-fzubr`'s production figure (about 9.0), so the 1.0% share is
  expected to hold, but the production host was not used.
- **Equivalence of the compute-once path beyond one graph and one synthetic window**, and against the
  `phaze-28883` real-model pin.
- **cpu-s per window, both ways, and RSS across chunks for a split-graph sweep.** The acceptance criteria
  make these conditional on identity, and identity was refuted, so there is no split-graph sweep to measure.
- **Whether every characteristic has an Essentia `msd-musicnn-1` classification head.** Only `danceability`
  answered the HEAD request.

## Cleanup

Downloaded models are in the worktree's gitignored `models/` and `.cache/0toy3/` directories and go away with
the worktree. The comparison and timing scripts and their outputs are in the session's per-bead scratch
directory (`<scratch>/phaze-0toy3.4/`), not committed. They started in the worktree's gitignored
`test-results/`, but `just check-fast`'s mypy step type-checks gitignored `.py` files there (ruff does not),
and a scratch `compare.py` collided with a tracked module of the same name. So **a spike's scratch `.py`
files must live outside the worktree, or under a path mypy excludes.**
