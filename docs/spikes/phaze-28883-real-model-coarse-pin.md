# phaze-28883 — the coarse tier's model sweep, pinned against the REAL model set

- **Bead:** `phaze-28883` (task — the coarse-tier TensorFlow model sweep was unpinned: no model set
  in the repo or CI, so `phaze-bk9el.22`'s analysis output-equivalence pin had a hole)
- **Date:** 2026-10-13
- **Tree:** branch `wt/bead/issue/phaze-28883`, forked off `main` at `a0348a3f`
- **Code under test:** this tree's `src/phaze`, unmodified. The bead changes tests and this record
  only
- **Status:** a real-model pin now exists and passes on a host with the models; **CI still does
  not run it** (§4). Choosing how to close that is left to the operator; §5 sets out the options

______________________________________________________________________

## Verdict in one paragraph

**The gap is closed on any host that holds the model set, and stays open in CI.**
`tests/analyze/services/pipeline/test_analysis_output_equivalence.py` now runs `analyze_file`
against the **real** weight files: no mocked `TensorflowPredict*` and no fake `models_dir`. Every
entry of its new recording, `analysis_output_equivalence_real_models_golden.json`, carries
`coarse_windows_analyzed == coarse_windows_total > 0`: **1/1** for each of the four existing
fixtures under the 12-graph default, **31/31** for a new case that crosses a D-07 coarse chunk
boundary, and **1/1** for the 34-graph rollback set. The recording is **byte-identical across
separate processes** on the recording host. A deliberate label/score misattribution turns the
comparison red. CI cannot run this pin today: no tests workflow provisions the weights, so the
tests **skip with a reason that says so**, and the skip is not a pass. The skipped tests can be
made to run in CI more cheaply than the bead assumed. The 12-graph production default is
**54.0 MB**, not 3.26 GB. **97.3%** of the bytes are the eleven vggish graphs, and only rollback
mode loads them.

______________________________________________________________________

## 1. What it takes to run the model set in a test (acceptance 1)

### 1a. Where the models live, and their size

| | |
| --- | --- |
| **Pinned set** | `phaze.scripts.download_models.MANIFEST`: **68 files** (34 `.pb` graphs + 34 `.json` sidecars), **3,264,667,851 bytes (3.265 GB)** |
| **12-graph default** (`PHAZE_ANALYSIS_MODEL_VARIANTS` unset / `msd`: 11 `musicnn_msd` heads + `discogs-effnet-bs64-1`) | **54,048,897 bytes (54.0 MB)** |
| **Vggish graphs** (11 × 288,629,030 bytes, rollback mode only) | **3,174,951,729 bytes, 97.3% of the set** |
| **Developer host** | `~/essentia-models`: the complete set, verified file-by-file against `MANIFEST` by `tests/_real_models.holds_complete_model_set`, plus `msd-musicnn-1.pb` and an `mtg-essentia-models/` subdirectory that the manifest does not name. 3.1 GB on disk (`du -sh`). Treated read-only throughout |
| **Production** | the operator-provisioned `phaze-models` claim, mounted read-only at `/models` (`PHAZE_MODELS_DIR`) |

### 1b. Can CI fetch them? Measured, not assumed

- **Upstream is live.** `HEAD` against `essentia.upf.edu` on 2026-10-13 returned `200` with
  `Content-Length` equal to the manifest's size for one graph of each size class:
  `mood_happy-musicnn-msd-2.pb` 3,239,548; `mood_happy-vggish-audioset-1.pb` 288,629,030; and
  `discogs-effnet-bs64-1.pb` 18,366,619.
- **CI already downloads and caches the full set, outside the tests workflow.**
  `.github/workflows/docker-publish.yml` restores `actions/cache` key
  `essentia-models-${{ hashFiles('src/phaze/scripts/download_models.py') }}` and, on non-PR events
  only, runs `scripts/download-models.sh models` on a miss. It uses the set for the x86/arm64
  parity golden. `gh cache list` on 2026-10-13 showed that entry at **2.82 GiB**, last accessed the
  same day.
- **The tests workflow provisions nothing.** `.github/workflows/tests.yml` has no model step and no
  `PHAZE_TEST_MODELS_DIR`. The test lives in the `ingest-pipeline` shard (`tests/ci_shards.json`
  maps `tests/analyze/services/pipeline` there). Each shard job runs on `ubuntu-latest` with
  `timeout-minutes: 15`.
- **Cache pressure is real.** `GET /repos/SimplicityGuy/phaze/actions/cache/usage` on 2026-10-13
  reported **10,445,019,888 bytes across 278 active caches**. That is at GitHub's documented 10 GB
  per-repository default, above which GitHub evicts older entries. A second 3 GB entry would
  compete with every other cache. A 54 MB entry would not.

### 1c. Does a subset give a meaningful signal?

**Yes. The meaningful subset is the production default itself.** Since `phaze-0sec0` the coarse
tier runs 12 graphs by default. The 22 `musicnn_mtt` and vggish graphs only load under
`PHAZE_ANALYSIS_MODEL_VARIANTS=all`, which is the rollback mode. So a CI pin over the 54 MB default
covers exactly the code and weights production runs. It does not cover rollback mode.

## 2. The pin (acceptance 2)

The new section of the equivalence module, and what each part asserts:

| test | needs models | asserts |
| --- | --- | --- |
| `test_real_models_recording_covers_the_coarse_tier` | **no**, so it runs in CI | the recording itself has `coarse_windows_analyzed == coarse_windows_total > 0`, genre predictions and non-null mood/style/danceability for every entry, and the multi-chunk entry exceeds `_COARSE_CHUNK_WINDOWS`. This guards against a recording taken without the weights, which would silently re-pin the empty-models path under a real-model name |
| `test_real_model_analysis_matches_the_recorded_baseline[<entry>]` × 6 | yes | the whole returned dict against the recording. Floats are compared with `abs_tol=1e-4`, and everything else exactly |
| `test_real_model_features_follow_the_registry_order[msd-12 / all-34]` | yes | feature-dict **insertion** order (registry order, `genre` last), and that each of the 12 or 34 graphs produced predictions for every coarse window |

The six recorded entries (12-graph default unless noted). Fixtures are generated in-process from
pure Python, so there are no archive files:

| entry | fine | coarse | bpm | key | mood | style | danceability |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `exact_windows_90s_120bpm_a220` | 3/3 | **1/1** | 120.0 | A minor | sad | Electronic/Minimal | 0.9505 |
| `short_trailing_dropped_100s_90bpm_c262` | 3/3 | **1/1** | 90.0 | C minor | sad | Electronic/Minimal | 0.6213 |
| `kept_trailing_105s_150bpm_e330` | 4/4 | **1/1** | 150.0 | E minor | sad | Electronic/Glitch | 0.9118 |
| `single_window_25s_120bpm_a220` | 1/1 | **1/1** | 120.0 | A minor | sad | Electronic/Glitch | 0.9538 |
| `multi_chunk_124s_128bpm_g196` (coarse window 4 s) | 4/4 | **31/31** | 128.0 | G major | sad | Electronic/Glitch | 0.9702 |
| `rollback_all:single_window_25s_120bpm_a220` (34 graphs) | 1/1 | **1/1** | 120.0 | A minor | relaxed | Electronic/Glitch | 0.6966 |

**Why the multi-chunk entry exists.** Every original fixture is at most 180 s, so each has exactly
one coarse window. With one window, a run never sweeps a graph across several buffers in
`_infer_live_windows`, and never builds and releases a graph per D-07 chunk. A 4 s coarse window
over 124 s gives 31 windows, one more than `_COARSE_CHUNK_WINDOWS = 30`. Each graph is therefore
built, swept over 30 buffers, released, and built again for the last window. The 4 s window is a
smaller value of a real parameter, not a different code path, and it still holds a whole musicnn
patch (~3 s) and a whole effnet patch (~2 s). Reaching a second chunk at the production 180 s would
need a 93-minute file. Inference cost follows audio length, not window count. A first version at
310 s with 10 s windows recorded the same 31/31 but took 200.8 s alone, so the file was cut to
124 s (104.9 s alone, 1.293 GiB peak).

**How rollback is selected.** `_select_model_sets("all")` is the function production calls at import
time. The test swaps its result into `phaze.services.analysis.MODEL_SETS`, which is the global the
pipeline reads. The recording made that way is **identical** to a separate process started with
`PHAZE_ANALYSIS_MODEL_VARIANTS=all`. The default entries are likewise identical to a process
started with `=msd`.

### 2a. Receipts

**Host:** the developer host. macOS 26.6.2 arm64 (Apple M1 Pro, 32 GiB), Python 3.14.7,
`essentia-tensorflow 2.1b6.dev1438` (essentia `2.1-beta6-dev`). Other seats' gates shared the host,
so **wall times below come from a loaded machine and are not benchmark figures.** Peak RSS is the
process `ru_maxrss` (bytes on macOS), read by `/usr/bin/time -l` and `resource.getrusage`.

| run | what | wall | peak RSS |
| --- | --- | --- | --- |
| probe 1 | 12-graph default, the four fixtures, fresh process | 62.14 s | 1.574 GiB |
| probe 2 | same, second process | 105.87 s | 1.405 GiB |
| probe 3 | 34-graph rollback, the four fixtures | 357.25 s | 4.351 GiB |
| recorder | default × 4 (141.8 s) then rollback × 1 (87.3 s), one process | 234.55 s | 4.045 GiB |
| recorder | multi-chunk entry alone (124 s / 4 s windows, as committed) | 104.9 s | 1.293 GiB |
| pytest | the whole module with the models present (as committed): `28 passed in 280.86s`, of which 268.98 s is the real-model fixture's setup | 331.09 s | 4.681 GiB |
| pytest | the whole module with `PHAZE_TEST_MODELS_DIR=` (opt-out): `20 passed, 8 skipped in 23.15s` | — | — |

- **Determinism:** probe 1 and probe 2 results are **byte-identical** (`cmp`). The recorder's
  default entries are equal to probe 1, and its rollback entry is equal to probe 3's.
- **Discrimination:** the test's comparator was run with `_get_labels` monkeypatched to reverse the
  label list. That attaches each score to the wrong class without touching TensorFlow. The
  comparison failed at once, on the 25 s fixture:
  `danceability: 0.054745156317949295 != 0.9538005590438843`. Without the patch it passed.
- **Skip path:** with `PHAZE_TEST_MODELS_DIR=` the module reports **20 passed, 8 skipped**, and each
  skip carries the reason `REAL-MODEL COARSE PIN NOT RUN (a skip, not a pass): …`.
- **Rollback mode on macOS peaks at ~4 GiB, not ~1.5.** This is an observation, not a finding about
  the deployed path. `phaze-u1n7j` measured 1.50–1.67 GiB for the 34-graph sweep under Debian 13 /
  glibc 2.41. On this macOS host, one rollback window lifted a process already at ~1.5 GiB to
  **4.045 GiB**. Nothing here says whether macOS's allocator holds pages from released vggish
  graphs, or whether graphs are co-resident. The D-09 guards and the `phaze-15sw` one-graph
  invariant were not re-measured, and this bead does not claim either way.

## 3. Where it CAN run, and the exact command (acceptance 3)

On any host whose model directory holds the complete `MANIFEST` set (the developer host, or a
compute node with the `phaze-models` claim mounted), from a checkout with `uv sync` done:

```bash
PHAZE_TEST_MODELS_DIR=<models-dir> \
  uv run pytest tests/analyze/services/pipeline/test_analysis_output_equivalence.py -rs \
  > real-model-pin.log 2>&1; echo "GATE_EXIT=$?" >> real-model-pin.log
```

On the developer host the variable can be omitted. `tests/conftest.py` supplies it from
`~/essentia-models` when that directory holds the complete set. **It ran only if the summary shows
no `SKIPPED` line naming `REAL-MODEL COARSE PIN NOT RUN`.** `28 passed` with no skips is the
expected shape (6 recordings + 2 order checks + 1 recording guard + the 19 existing tests). The
same `PHAZE_TEST_MODELS_DIR=` (empty) is the opt-out for a host that has the models but cannot
spare the time.

An explicitly exported directory that is **incomplete** fails the run and names the missing files.
It does not skip, because an export is a claim that the models are there.

**Cross-architecture portability is unmeasured.** The recording was taken on macOS arm64. The
`1e-4` tolerance is `scripts/parity/compare_analysis.py`'s cross-architecture default, chosen so
that the deployed linux/amd64 and linux/arm64 nodes can run the same recording. No linux run of this
pin was taken. If the first one goes red, treat that as a finding to investigate. Do not re-record
or widen the tolerance to clear it.

## 4. What CI does NOT cover (acceptance 3, the explicit statement)

**CI does not run the real-model pin.** The real-model tests skip there. A green CI run therefore
covers the coarse tier's window enumeration, its per-window failure isolation, and the pure
reductions over hand-written inputs. It does **not** cover the TensorFlow model sweep. Until the
operator chooses an option in §5, the only check of the sweep is the operator-run command in §3.
`phaze-bk9el.15`'s seat, and any later refactor of `services/analysis.py`, must run that command
rather than read a green CI run as covering the model sweep.

## 5. Options for closing it in CI (none chosen; this needs the operator)

None is implemented. Each one changes CI infrastructure.

1. **Status quo.** The pin runs on hosts with models, and CI skips it loudly. Costs nothing, and
   the CI gap remains.
2. **Default set in the tests workflow (54 MB).** Add a cached step that fetches only the 12
   default graphs into the `ingest-pipeline` shard, and export `PHAZE_TEST_MODELS_DIR`. This needs
   two changes: a subset mode for `download_models` (today it fetches all 68 files), and a split of
   the module's gate so that the default entries require only the default files. As written, the
   fixture requires the full `MANIFEST` and fails on an incomplete explicit export. The cost to
   the 15-minute shard budget is unmeasured on a runner. The linux portability of the recording
   (§3) would be tested for the first time.
3. **The full set in the tests workflow (3.26 GB).** Reuse the docker-publish cache key. This
   covers rollback mode too. It costs a ~3 GB restore per run and holds a large slice of a cache
   budget that is already at its limit.
