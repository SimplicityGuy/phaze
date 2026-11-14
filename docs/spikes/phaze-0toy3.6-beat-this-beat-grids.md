# phaze-0toy3.6 — Beat This! as the beat-grid source for phaze-7hcgn

- **Bead:** `phaze-0toy3.6` (spike), child of epic `phaze-0toy3`
- **Date:** 2026-11-14
- **Tree:** branch `wt/bead/issue/phaze-0toy3.6`, forked at `079660ce`
- **Status:** measurement and assessment only. **No product code changed**, no `pyproject.toml` change, no archive audio,
  no audio committed. Everything ran in throwaway `uv run --with` environments on a development Mac (arm64).

______________________________________________________________________

## Question

`phaze-7hcgn` plans to keep a per-file beat grid, which today is discarded from `RhythmExtractor2013`'s per-30 s fine
window. `docs/spikes/phaze-8ghbw-analysis-alternatives.md` (E4) named Beat This! (Foscarin, Schlüter, Widmer, ISMIR 2024,
arXiv 2407.21658) as the one credible alternative tracker. That survey also recorded what was unknown: its inference cost,
its tempo output (none) and how it behaves on windows. It is **not a CPU lever**: decode, not the beat tracker, dominates the fine tier.
`docs/spikes/phaze-zaf2l-where-phaze-spends-time.md` measured the fine tier at 97.7% decode, but on **one 10-hour file**;
`docs/spikes/phaze-fzubr-analysis-cpu-profile.md`'s six files put decode at about 81% of the fine tier (1.48 cpu-s of
1.48 + 0.32 + 0.017 = 1.817 per fine window, 0.81), and decode varies about tenfold by file (35 to 352 cpu-s per
audio-hour). So the question is:

1. Is Beat This!'s beat grid better than `RhythmExtractor2013`'s, measured on named data?
2. What does it cost (cpu-s per 30 s window, RSS across many windows)?
3. How would it plug into `phaze-7hcgn`, and what are the gaps (no tempo, windowing, export, packaging)?
4. For each gap: contribute upstream, fork, or work around, and at what effort?

> **Operator decisions on this bead.** (a) *Follow-up selected 2026-11-13*, AskUserQuestion label **"Beat This! for beat
> grids"**, adding, quoted: *"For any of the above, if theree are missing features, evaluate what the contribution
> guidelines are (or forking guidelines) and evaluate whether we can address the gaps"* — answered in §Evidence 5.
> (b) Question as put (dispatcher report, 2026-11-13): *"Beat This!: someone has to accept or reject the training-data
> caveat in its README."* Answer as given: *"accepted"*. Durable record: bead `phaze-0toy3.6`'s description.

______________________________________________________________________

## Method

**Trackers, configured as phaze would run them.**

| Name | What ran |
|---|---|
| `re2013` | `essentia.standard.RhythmExtractor2013(method="multifeature")`, essentia-tensorflow `2.1-beta6-dev` (the `pyproject.toml` pin's own family), fed 44.1 kHz mono float32, one **30 s window at a time**, ticks offset by the window start and concatenated — the way the fine tier calls it |
| `bt_full` | `beat_this` 1.1.0, checkpoint `final0` (~78 MB), `Audio2Beats(device="cpu", dbn=False)`, torch 2.15.0, fed the same 44.1 kHz audio (the package resamples to 22.05 kHz itself with `soxr`), also one 30 s window at a time; and, where stated, the whole clip in one call |
| `bt_small` | **Not measured.** See "What was not measured" |

**Data (named, with licence).**

- **GuitarSet** (Xi et al., ISMIR 2018; Zenodo record 3371780, licence **CC BY 4.0**). Beat annotations from the JAMS
  `beat_position` namespace; audio `audio_mono-mic`, 44.1 kHz. **39 of its 360 clips**, taken by a fixed stride over the
  sorted file list (the volume was nearly full, so the audio was range-fetched rather than downloaded whole; ~98 MB on
  disk). Clips are 30–45 s of solo or accompaniment acoustic guitar, so each is tracked as one 30 s window plus a remainder
  window when the remainder is at least 5 s. **GuitarSet turned out to be one of Beat This!'s training datasets**, which
  invalidates it as a fair comparison (§1).
- **Locally generated synthetic drum fixtures** (generated in the benchmark, never stored): kick on beats 1 and 3, snare on
  2 and 4, hi-hat on every eighth, a bass tone alternating every bar; 60 s each at 70, 90, 110, 120, 127.3, 140, 160 and
  180 BPM plus one slow ramp 120→140 BPM (9 cases), with exact beat times. A 300 s fixture at 120 and 127.3 BPM serves the
  window-boundary test. A bare impulse-train calibration at 100/120/140 BPM measured each tracker's constant latency.
- **Not used:** GTZAN, Ballroom, Beatles, Hainsworth, Harmonix. Either the audio licence is unclear or the audio needs a
  large or on-request download that did not fit the cap the dispatcher set (about 100 MB of downloaded data). The paper's
  GTZAN numbers are therefore **not reproduced here**.

**Metrics** (`mir_eval`): beat F-measure at the standard ±70 ms window, continuity **CMLt** and **AMLt**; tempo from
**median inter-beat interval**, scored **Acc1** (within 4% of the reference tempo, itself the median IBI of the reference
beats) and **Acc2** (also accepting ×2, ×3, ÷2, ÷3).

**Cost.** One synthetic 30 s window looped 100 times after a warm-up call; `time.process_time()` around each call (so it
includes Beat This!'s `soxr` resample, mel spectrogram and model, and for torch it counts every thread), RSS read every
10 windows with `psutil`. Each tracker ran in its own fresh process. **Caveat on the machine:** the 1-minute load average
was **30.8 on 10 cores** while this ran (other seats were gating), so wall times are inflated and cpu-s is the cleaner
figure; treat them as an upper bound, not a calibrated benchmark.

The benchmark scripts live in a gitignored scratch directory and are not committed; the fixture recipe and every
parameter above are enough to rebuild them.

______________________________________________________________________

## Evidence

### 1. Accuracy

**GuitarSet, 39 clips, 30 s windows tracked independently (the way phaze would):**

| Tracker | beat F1 | CMLt | AMLt | tempo Acc1 | tempo Acc2 |
|---|---|---|---|---|---|
| `re2013` | 0.591 | 0.464 | 0.673 | 0.718 | 0.795 |
| `bt_full` | **0.779** | **0.586** | **0.710** | 0.718 | **0.872** |
| `bt_full`, whole clip in one call | 0.789 | 0.602 | 0.727 | 0.718 | 0.872 |

**Synthetic drum fixtures, 9 cases, 60 s, 30 s windows:**

| Tracker | beat F1 | CMLt | AMLt | tempo Acc1 | tempo Acc2 |
|---|---|---|---|---|---|
| `re2013` | 0.644 | 0.497 | 0.929 | 0.778 | 1.000 |
| `bt_full` | **0.997** | **0.994** | **0.994** | **1.000** | 1.000 |

What the per-case rows say: `bt_full` scored F1 ≥ 0.99 on all nine. `re2013` scored **F1 0.0 on the 70 and 120 BPM cases while
its tempo was right** (AMLt 0.97), meaning its ticks were placed on the wrong phase, roughly half a beat off. It scored
0.66 on 160 and 180 BPM, where it reported **79.5 and 90.7 BPM** (half tempo), and 0.54 on 127.3 BPM. A bare impulse train
shows no such failure: `re2013` lands within +29 ms of truth at 100, 120 and 140 BPM, with F1 0.99–1.00, and `bt_full`
within 0–20 ms. So the misses are a property of tracking real drum texture, not of `re2013`'s latency.

**Reading the two tables with the right weight.** **The GuitarSet comparison is not fair evidence: GuitarSet is in
Beat This!'s training data.** Checked after this spike first reported (PR #805 review, bead `phaze-0toy3.7`,
2026-11-14): the authors' annotation repository `CPJKU/beat_this_annotations` (`gh api
repos/CPJKU/beat_this_annotations/contents`, read at commit `fd9fcc08`) has a `guitarset` directory among its 16 dataset
directories, and `gtzan` is the held-out test set. The `CPJKU/beat_this` README, line 105 at commit `b95c8ab0`, says
*"the results may be unfairly good if you run inference on any file from the training datasets"* and that `final*`
can only be evaluated fairly on GTZAN or datasets the paper did not use. `final0` is a `final*` checkpoint, so its
GuitarSet clips, these 39 among them, were training material. The GuitarSet gap (0.779 vs 0.591, +0.19 F1) is
therefore **contaminated in Beat This!'s favour** and is not evidence that it tracks unseen guitar better; how much of
the gap survives on unseen audio is unknown from this data. `re2013` is a hand-designed signal-processing extractor
with no learned training set to overlap.

That leaves the **synthetic fixtures as the only clean accuracy evidence** here. They are easy and unambiguous by
construction, which flatters any competent tracker, and what they show is narrow: `re2013` has phase and octave failures
on drum texture (above) that Beat This! does not. **No claim about the archive's music follows from either table.**
See Recommendation for the measurement that would settle it.

Also note the paper's own caveat: Beat This! has *lower* continuity than a DBN system. **This data neither supports nor
contradicts it.** CMLt below F1 does not discriminate between the trackers: on GuitarSet it holds for `bt_full` (0.586 vs
0.779) and equally for `re2013` (0.464 vs 0.591), and GuitarSet is contaminated in Beat This!'s favour anyway (above);
no DBN tracker was run. The caveat itself, from the paper, is the reason a stored grid should not assume it is
metrically continuous over a whole file.

### 2. Window boundaries (grid stitching)

300 s fixtures, ten 30 s windows tracked independently, beats offset by window start. "Bad boundary" = the gap between the
last beat of window *k* and the first of window *k*+1 differs from one beat interval by more than 25% (a missed or doubled
beat).

| Tracker @ tempo | bad boundaries | F1 of the stitched grid | phase offset to truth, per window |
|---|---|---|---|
| `bt_full` @ 120 | **0 / 9** | 0.999 | +10 ms in every window |
| `bt_full` @ 127.3 | **0 / 9** | 1.000 | +4.1 to +4.8 ms in every window |
| `re2013` @ 120 | 9 / 9 | 0.000 | −232 ms in every window |
| `re2013` @ 127.3 | 8 / 9 | 0.108 | −216 ms in nine windows, **−0.7 ms in one** |

Two findings. First, Beat This! stitched cleanly: independent windows agreed on the same phase and the gap across each
boundary was within ±2.4% of one beat interval (0.976–1.018), so tracking each 30 s window separately and concatenating
loses no beats on this fixture. The model discards its outer 6 frames (0.12 s) per chunk (its own `border_size`), so a beat
inside that margin of a window edge is the place a miss would occur, and the synthetic data did not provoke one.

Second, `re2013`'s bad boundaries are **not explained by phase flips**. At 120 BPM its offset was −232 ms in *every*
window, so its phase was the same on both sides of every boundary, yet all 9 boundaries were bad. At 127.3 BPM one window
flipped phase (−0.7 ms against −216 ms in the other nine), but a single window touches at most the 2 boundaries on its
own edges, so it cannot account for 8 bad ones. A constant phase cannot move the gap across a boundary; only the beat
count at the window edges can. So the bad boundaries come from **ticks dropped or added near window edges**: with the
phase held constant, a gap above 1.25 beat intervals means ticks are missing at the edge, and one below 0.75 an extra
tick. **The recorded data
does not say which**: this spike kept the count of bad boundaries per run, not the gap at each boundary or its sign, so
neither the direction nor how far from the edge the missing or extra ticks sit can be stated here. Separately, the one
flipped window at 127.3 BPM shows `re2013`'s phase is **not stable across independent windows** either, so a stitched
`re2013` grid can also carry a half-beat step at a boundary. Both matter for `phaze-7hcgn` whichever tracker it keeps.

Derived tempo from IBIs is coarse for Beat This!: frames are 20 ms, so 127.3 BPM (0.471 s) reads as 125.0 BPM (0.480 s)
from the median IBI. A tempo for storage should come from a robust fit over many beats (mean IBI over a window or the
slope of beat index against time), not a median of single intervals.

### 3. Cost

Per 30 s window, 100 windows after warm-up, `process_time`, machine under load (see Method):

| Tracker | cpu-s / window | wall-s / window | RSS across windows |
|---|---|---|---|
| `re2013` | **0.239** | 0.243 | 424 → 401 MiB, slope −0.13 MiB/window |
| `bt_full`, 1 torch thread | **2.338** | 2.374 | 438 → 310 MiB, slope −0.76 MiB/window |
| `bt_full`, 4 torch threads | 2.974 | 1.629 | 359 → 329 MiB, slope −0.05 MiB/window |

- **Beat This! costs about 9.8× `re2013` per window** (2.338 vs 0.239 cpu-s, one thread), i.e. 0.078 cpu-s per second of
  audio against 0.008. More torch threads buy wall time at the price of cpu-s (4 threads: +27% cpu-s for −31% wall).
- **The net depends on what supplies stored BPM**, and there are two cases:
  - **Keep `re2013` as the BPM source** and add Beat This! for the grid only: `re2013` still runs, so the net is the
    whole **+2.338 cpu-s per window**, and stored BPM keeps its current meaning.
  - **Swap the BPM source** to a tempo derived from Beat This!'s beats: Beat This! then replaces `re2013`, and the net is
    2.338 − 0.239 = **+2.099, about +2.1 cpu-s per window**. That is a **semantics change to stored BPM**, not only a
    cost line: the derived value is a different estimator (at 20 ms frames, 127.3 BPM reads as 125.0 from the median
    IBI, §2), so the bead that makes this swap owes a blast-radius statement (rule 4 of the verification-fidelity rules
    in `CLAUDE.md`) with a measured population of affected files, which this spike does not have.

  On the 4,761.835 s file `phaze-zaf2l` measured end to end (7,119.473 s at `cap = 4`), that is 4,761.835 / 30 = 158.7
  windows, so about **+371 cpu-s** keeping `re2013` (158.7 × 2.338) or **+333 cpu-s** swapping it (158.7 × 2.099): about
  **5.2%** (371 / 7,119.473) or **4.7%** (333 / 7,119.473) of that file's wall clock if the extra work ran serially on
  one core. That is a rough framing, not a measured end-to-end figure, and the cpu-s were taken on a loaded machine.
- **RSS does not grow with window count** (the D-09 shape): slopes are at or below zero across 100 windows for all three
  configurations, so neither tracker retains per-window state in this loop. This says nothing about a **single call on a
  multi-hour signal**: `Audio2Beats` takes the whole signal and builds the whole spectrogram, which at 50 frames/s ×
  128 bins × 4 bytes is about 25.6 KB per second of audio, so a 12-hour file would need about 1.1 GB for the spectrogram
  alone, plus the resampled signal. phaze would call it per window, as measured here.

### 4. Integration plan for phaze-7hcgn

- **What Beat This! returns.** Frame-wise beat and downbeat probabilities at 50 fps, reduced by a built-in peak-pick to
  beat and downbeat times in seconds (`dbn=False`; the optional `dbn=True` path imports `madmom` and is not wanted).
  phaze would store **the times, not the probabilities**: at about 2 beats per second a 12-hour file is on the order of
  86,000 beats, roughly 350 KB as float32 seconds or less as 10 ms integer ticks, against about 1.1 GB for the frames.
  Downbeats (also free) can be stored alongside if `phaze-7hcgn` wants bars.
- **Tempo.** Derived in phaze from the stored beats (robust fit, §2). Not a package feature; about 20 lines.
- **Windows.** Keep the existing 30 s fine windows and call the tracker once per window; §2 shows independent windows agree.
  A second option, one call per file, costs the RSS above and is not recommended past a few minutes of audio.
- **Resampling.** Beat This! wants 22.05 kHz; the fine tier decodes 44.1 kHz. The package resamples internally with `soxr`
  and that cost is *inside* the 2.338 cpu-s above, so no second decode is needed as long as the 44.1 kHz window buffer is
  passed in. Passing already-decimated audio would save only the `soxr` step.
- **Packaging, PyTorch.** `beat-this` 1.1.0 requires `numpy>=1.20`, `torch>=2`, `torchaudio`, `einops`,
  `rotary-embedding-torch`, `soxr`. It imported and ran a 30 s window under **CPython 3.14.7 and 3.13.13 on macOS arm64**
  (torch 2.15.0 in both), and torch 2.15.0 publishes a `cp313` `manylinux_2_28_aarch64` wheel on PyPI. **I did not run it
  on Linux arm64**, so the arm64 agent image on Python 3.13 is *indicated*, not verified. The cost is a second numeric
  framework next to essentia-tensorflow: the torch wheel alone downloaded as 132 MiB on this Mac, and the checkpoint is
  78 MB (`final*`) or 8.1 MB (`small*`, per the README, not measured here).
- **Packaging, ONNX.** `musetric/beat-this-onnx` on Hugging Face is a third-party conversion
  (`docs/spikes/phaze-8ghbw-analysis-alternatives.md`). **Not tested here**: it would remove torch from the image, but its
  numerical agreement with the reference model and the export of the rotary attention are unverified, and a third-party
  artefact is a supply-chain surface comparable to the litellm pin's lesson.
- **Model weights fetch.** The package downloads checkpoints on first use from `cloud.cp.jku.at`. During this spike that
  host returned HTTP 500 with a `MaxDelayReached` brute-force-throttle error against this IP, and never served `small0`.
  A deployed agent must **vendor the checkpoint into the image**, not fetch it at runtime.

### 5. Missing features and the contribution / fork assessment

Repository state read on 2026-11-14 (GitHub API and `raw.githubusercontent.com`): licence **MIT**, `pushed_at`
2026-05-28, 485 stars, 78 forks, **4 open issues**, **no `CONTRIBUTING` file** (both `CONTRIBUTING.md` and `CONTRIBUTING`
return 404). Of the seven most recent pull requests, **two merged** (a 2026-01 autocast guard, merged in nine days, and a
2024-08 one-line fix); the rest were closed unmerged or remain open, among them *"Making postprocessing more flexible by
using Dependency Injection"* (#10, 2025-05) and *"HuggingFace Integration & Pyproject"* (#8, open since 2025-01). With no
written contribution guide, the effective guideline is "small, focused fixes get merged; structural changes stall".

| Gap | Needed by phaze-7hcgn? | Contribute upstream | Fork | Verdict / effort |
|---|---|---|---|---|
| **No tempo output** | Yes, derived value | Unlikely to be wanted: the model is a beat tracker, and a tempo estimate is policy | Pointless | **Work around in phaze**, ~20 lines on the stored beats. **S** |
| **No windowed / streaming API**: `Audio2Beats` needs the whole signal and builds the whole spectrogram | Only if one call per multi-hour file is wanted; per-window calls already work (§2, §3) | A PR adding a chunk-iterator input is plausible but would join the "structural" PRs that stalled | Not needed | **Work around**: call per 30 s window. **S**. Revisit only if the edge margin (0.12 s per window) proves to drop beats on real material |
| **Per-window edge margin**: model discards 6 frames each side per chunk | Maybe: beats within 0.12 s of a window edge | Overlapping windows by ≥0.12 s is a caller-side fix | n/a | **Work around**: overlap the window feed by ~0.25 s and de-duplicate. **S**. Needs a check on real material |
| **No export / ONNX path in the package** | Only if torch in the agent image is unacceptable | The README links third-party conversions only; an export script PR is possible but unproven against rotary attention | A phaze-owned export plus a numerical-parity test against `final0` is the controllable route | **M**, and only if torch is rejected; **L** if parity on rotary embedding needs fixing |
| **Weights fetched from a university cloud host** | Yes: deployments must not depend on it | Not applicable | Not applicable | **Vendor the checkpoint** into the image, pinned by SHA-256, behind the licence acceptance recorded above. **S** |
| **`madmom` needed for `dbn=True`** | No | n/a | n/a | Do not enable `dbn`. Nothing to build |

**Bottom line on contribute-vs-fork:** nothing here justifies a fork. Every gap that phaze needs closed is closed by code
phaze already owns (a tempo derivation, a per-window loop, a vendored checkpoint). The only upstream-shaped item is an
ONNX export, which is optional and, given the repository's merge record, would be worth doing in phaze first and offering
upstream afterward rather than waiting on review. Forking would take on maintaining a 20 M-parameter model package for no
gap that a wrapper cannot close.

______________________________________________________________________

## Verdict

1. **Quality:** Beat This! (`final0`) produced a better beat grid than phaze's current `RhythmExtractor2013` call **on
   synthetic fixtures**: beat F1 **0.997 vs 0.644** on nine synthetic drum fixtures, where `re2013` showed half-beat
   phase errors, half-tempo errors, and bad window boundaries (8 or 9 of 9 on the 300 s fixtures, from ticks dropped or
   added at window edges, §2); Beat This! showed none. **The GuitarSet gap (F1 0.779 vs 0.591) is training-set
   contaminated** (GuitarSet is one of Beat This!'s training datasets, §1) and is not fair evidence of better tracking on
   unseen audio.
2. **Cost:** about **9.8× the cpu-s** of `re2013` per 30 s window (2.338 vs 0.239, one thread, loaded machine): a net of
   **+2.338 cpu-s per window if `re2013` stays the BPM source**, or **+2.099 (≈ +2.1) if Beat This!'s beats replace it**,
   the second being a change to what stored BPM means (§3). **No RSS growth across 100 windows.** It is not a CPU saving;
   it is a modest CPU price for a grid that is better on the fixtures and unproven on real music.
3. **Adoption cost:** a torch dependency in both images (arm64 agent on 3.13 indicated by wheel availability, untested),
   a vendored 78 MB checkpoint, and about 20 lines of tempo derivation. No upstream change and no fork is required.
4. **Not established:** any accuracy advantage on real, unseen music (no clean real-music comparison was run; the archive's
   full concert sets, electronic and band recordings are untested), why exactly `re2013` loses ticks at window edges, how
   `small0` compares, and whether the ONNX route is numerically faithful.

______________________________________________________________________

## Recommendation

1. **Take Beat This! (`final0`, torch, `dbn=False`) as the provisional working choice for `phaze-7hcgn`'s beat grid**,
   called once per 30 s fine window, storing beat (and optionally downbeat) times rather than frame probabilities, with
   tempo derived in phaze by a robust fit. **Provisional** because its accuracy edge is shown only on synthetic fixtures;
   the GuitarSet result is training-set contaminated (§1), so recommendation 2 is what confirms or overturns it.
   **Do not fork. Do not wait on upstream.**
2. **Before committing the grid format, run one operator-side check that this spike could not:** F1, phase agreement and
   window-boundary behaviour of `re2013` vs `final0` on a small, **operator-chosen and operator-annotated** sample of real
   archive music. This spike was barred from archive audio, its one real-music set (GuitarSet) turned out to be in Beat
   This!'s training data, and synthetic drums do not transfer to concert sets, so this check is the **only** clean
   real-music evidence the decision will have. Record the result as a comment on `phaze-7hcgn`.
3. **Treat the stitched grid as per-window, not metrically continuous**: store each window's beats with their window
   index so a later pass can reconcile each boundary, **chiefly ticks dropped or doubled at window edges** (the cause of
   `re2013`'s bad boundaries in §2) and secondarily a phase step between windows (seen once in `re2013`). Beat This!
   showed neither on the fixtures, but the paper reports lower continuity than a DBN system (§1; this spike's data
   neither supports nor contradicts that) and its 0.12 s edge margin is untested on real material. If `re2013`'s edge losses matter to the comparison, record the gap and its sign at each boundary next
   time; this spike kept only the count.
4. **File follow-ups, not part of this bead:** (a) re-measure `small0` (8.1 MB) once the checkpoint host serves it, or
   from a vendored copy, because a model one-tenth the size could change the adoption cost; (b) verify `beat-this` and
   torch on Linux arm64 / Python 3.13 in the real agent base image; (c) evaluate the ONNX route only if (b) shows torch is
   unacceptable.
5. **Lesson with a general form (rule 5 of the verification-fidelity rules in `CLAUDE.md`):** a tracker's tempo being
   right says nothing about its beat *phase* being right (`re2013` scored AMLt 0.97 with F1 0.0 on the same case), so a
   stored beat grid has to be validated on beat F1 or phase offset, never on BPM agreement alone. Written down here and
   in the table in §1; it generalises to any alternative beat source `phaze-7hcgn` evaluates later.

______________________________________________________________________

## What was not measured

- **`bt_small` (`small0`)**: the checkpoint host `cloud.cp.jku.at` returned HTTP 500 / `MaxDelayReached` against this IP
  for every request, including a retry after about ten minutes, so no number for the small model is reported and none is
  inferred from the paper's GTZAN figure (88.8 vs 89.1).
- **GTZAN, Ballroom, Beatles, Hainsworth, Harmonix**: not used (licence unclear or download size), so the paper's
  headline numbers are not reproduced.
- **Real archive music**, by instruction.
- **ONNX conversion** parity and speed.
- **Linux arm64 on Python 3.13**: only macOS arm64 was run (Python 3.13.13 and 3.14.7).
- **A single multi-hour call**: RSS arithmetic only, not measured.
- **Cost under an idle machine**: the cpu-s above were taken at a 1-minute load average of 30.8 on 10 cores and are an
  upper bound.

______________________________________________________________________

## Sources

- `docs/spikes/phaze-8ghbw-analysis-alternatives.md` (E4: Beat This!)
- `docs/spikes/phaze-zaf2l-where-phaze-spends-time.md` (fine tier 97.7% decode; the 4,761.835 s / 7,119.473 s file)
- Foscarin, Schlüter, Widmer, *Beat This! Accurate beat tracking without DBN postprocessing*, ISMIR 2024, arXiv 2407.21658
- `CPJKU/beat_this` README, LICENSE (MIT) and `beat_this/inference.py` (chunking, `border_size=6`, `soxr`), GitHub API
  repository, issue and pull-request listings; `beat-this` 1.1.0 and `torch` 2.15.0 on PyPI
- GuitarSet, Xi, Bittner, Pauwels, Ye, Bello, ISMIR 2018; Zenodo record 3371780 (CC BY 4.0)
- Training-set check (2026-11-14, bead `phaze-0toy3.7`): `gh api repos/CPJKU/beat_this_annotations/contents` at commit
  `fd9fcc08` (16 dataset directories, `guitarset` among them, `gtzan` the test set); `CPJKU/beat_this` `README.md`
  line 105 at commit `b95c8ab0` ("results may be unfairly good …")
