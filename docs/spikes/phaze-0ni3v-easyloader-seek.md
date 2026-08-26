# phaze-0ni3v — extending the essentia seek patch to EasyLoader, and testing it

- **Bead:** `phaze-0ni3v` (close the two gaps operator review found in the prepared `#771` PR:
  `EasyLoader` was not covered, and there were no tests)
- **Date:** 2026-08-12
- **Predecessor:** [`phaze-han03`](phaze-han03-essentia-seek.md) — read its §3a, §4 and §4b first;
  this document does not repeat them, it applies them.
- **Upstream base:** `MTG/essentia` at `b9fa6cb674ca43dfb94d28d293aeda441c6745db`. **Upstream master
  has not moved since the spike**, so no rebase was needed; verified by fetch, not assumed.
- **Branch:** `feat/audioloader-seeking` @ `febfee0a` on the fork `SimplicityGuy/essentia`, three
  DCO-signed commits. **Pushed, NOT submitted** — still held pending the operator's CLA.
- **Status:** **No phaze product code changed.** Nothing here installs a patched essentia into the
  phaze image; that remains `phaze-a51jo`'s decision.
- **Audio:** 100% synthetic ([`make-corpus.sh`](phaze-0ni3v/make-corpus.sh)) for the measurements,
  and upstream's own committed `test/audio` fixtures for the unit tests. No corpus file was
  opened, named, hashed or timed. These artifacts are destined for a **public** tracker.

______________________________________________________________________

## Verdict in one paragraph

**`EasyLoader` and `EqloudLoader` now seek, their output is bit-identical to the pre-patch build,
and the change is covered by 24 new tests that are mutation-checked rather than merely green.**
Reading one 30 s window 1800 s into a 1-hour MP3 through `EasyLoader` falls from **2.07 s to
0.085 s (24.4×)**, and reading the second half of a file now costs **53%** of reading all of it
instead of 92% — the `O(span)` property, delivered through the algorithm #771 actually names.
Bit-identity was established the honest way, by building essentia **twice** — once at
`b9fa6cb` and once with the patch — and diffing 59 outputs byte for byte across both binaries:
**51 of 59 rows are unchanged**, and every one of the remaining 8 is a known, already-explained
residual (four AAC/PNS rows, one Opus ULP, three rows where only the *text* of an exception
changed). The AAC residual was re-proved on this build by the §4b method, and it is now proved
twice over: **flat across a 200× preroll sweep** (0.05 s → 10 s) and **exactly zero at all four
offsets** on the same audio re-encoded with `-aac_pns 0`. Two things changed versus the prepared
patch as a direct consequence of taking `EasyLoader` seriously. First, the loader now converts
seconds to samples by **truncation in double**, not rounding, because truncation is `Trimmer`'s
rule and matching it is the only way the composites can switch to seeking without moving a sample
— rounding shifts every non-integral offset by one sample, and the mutation test proves the suite
catches it. Second, an **inverted** range still raises but an **empty** one no longer does, again
because that is what `Trimmer` has always done. The one deliberate limitation, and it is forced by
measurement rather than chosen: `EasyLoader`/`EqloudLoader` **do not seek when they also resample**,
because `phaze-han03` §4c established that libsamplerate's state makes a resampled slice
reproducible only by decoding from zero — so seeking there would silently move the output of every
existing caller. That case keeps the old path, is asserted to keep it, and is the follow-up to
raise with the maintainers. `EqloudLoader` was not in scope and was done anyway: it is the same
composite with the same two parameters, and leaving it inconsistent would have been worse than the
six lines it cost.

______________________________________________________________________

## 1. What changed versus the prepared patch

The prepared PR was a single commit, `0b674bb1`, covering `AudioLoader` + `MonoLoader`. It is now
three commits, `e468720d` → `f8b9a47c` → `febfee0a`.

| | prepared (`0b674bb1`) | submitted-ready (`febfee0a`) |
| --- | --- | --- |
| commits | 1 | **3** — feature, composites, tests |
| `AudioLoader` / `MonoLoader` | seek | unchanged in shape; two **semantic corrections**, below |
| `EasyLoader` | untouched, explicitly out of scope | **seeks** (except when resampling) |
| `EqloudLoader` | untouched, not mentioned | **seeks** (except when resampling) |
| tests | none | **24 new tests** across the four existing `test/src/unittests/io/` files |
| lines | +153 / −8 | **+573 / −16** (of which +323 is tests) |

### 1a. Correction: truncation, not rounding

`0b674bb1` computed `(int64_t)(startTime * sampleRate + 0.5)`. `Trimmer` — the algorithm that has
implemented `EasyLoader`'s `startTime`/`endTime` for a decade — computes
`(long long)(startTime * sampleRate)`. Those differ by one sample at every offset that is not a
whole number of samples, so the prepared patch would have shifted `EasyLoader`'s output by a sample
the moment it was wired through. The loader now truncates.

It truncates **in `double`** rather than in `Real`, which is a deliberate divergence and the one
place the patch is knowingly *not* bit-compatible with the old path. `Trimmer` multiplies in
float32, whose 24-bit mantissa cannot represent every sample index past 2²⁴ samples — **380.4 s at
44.1 kHz** — so its cut points quantise onto a coarsening grid (`phaze-han03` §6b). The benchmark
caught this in the wild: a 30 s window at 3540 s comes back as **1,322,992 samples on the pre-patch
build and 1,323,000 on the patched one**. The patched number is the correct one. Anyone slicing
past ~6 minutes gets a slightly different — better — cut, and the PR body says so.

### 1b. Correction: an empty range is not an error

`0b674bb1` threw on `endTime <= startTime`. `Trimmer` throws on `startIndex > endIndex` only, so
`startTime == endTime` yields an empty slice today and must keep doing so. The rule is now
`endTime < startTime` → throw. Upstream's existing
`assertConfigureFails(EasyLoader(), {'startTime': 10, 'endTime': 1})` still passes.

The **text** of the exception necessarily changed (`Trimmer: startTime cannot be larger than
endTime.` → `AudioLoader: 'startTime' cannot be larger than 'endTime'`), because the check now
fires earlier in the chain. It is still a configure-time `RuntimeError`; only the message differs.
That is the entirety of rows 1–3 of the eight non-identical rows in §3.

### 1c. `EqloudLoader` — out of scope, done anyway

`EqloudLoader` is not `EasyLoader` internally (it builds its own `MonoLoader` + `Trimmer` + `Scale`
+ `EqualLoudness` rather than wrapping `EasyLoader`), but it declares the same `startTime`/`endTime`
with the same defaults and implements them the same expensive way. Two algorithms with identical
parameters behaving differently is a worse outcome than six extra lines, so it got the same
treatment, in the same commit, and can be dropped independently if the maintainers prefer.

______________________________________________________________________

## 2. Method — two builds, not one model

The correctness claim is "bit-identical to the pre-patch decode-and-trim path". `phaze-han03` §4
established that against a numpy model of that path. That is a fair model, but it is a model. Here
it is the real thing: essentia was built **twice from the same tree**, into two venvs.

| | `base` | `seek` |
| --- | --- | --- |
| source | `b9fa6cb` + the `distutils`→`sysconfig` build fix (needed to compile at all on 3.14) | `base` + `pkt_timebase` + the three seek commits |
| build | `waf configure --build-static --with-python --fft=ACCELERATE`, no TensorFlow / Gaia / Chromaprint | same |
| full build | **1 m 10 s** | **1 m 16 s** |
| incremental rebuild after editing one `.cpp` | — | **10.9 s** |

Host: Apple M1 Pro (8P + 2E), 32 GiB, macOS 26.5.2, Apple clang 21.0.0, Python 3.14.5, numpy 2.5.2,
Homebrew ffmpeg 8.1.2 (libavcodec 62.28.102). Load average during the benchmark was 2.7–3.4, so
absolute wall clock is an upper bound; every A/B pair runs **adjacently in time**, one fresh process
per data point, and the whole matrix twice, so the ratios are what the claims rest on.

The `distutils` blocker and the empty Command Line Tools `c++/v1` directory documented in
`phaze-han03` §2b both recurred exactly as described. The second one needed the same two-line
compiler wrapper injecting `-isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1"`.

Reproduce with [`make-corpus.sh`](phaze-0ni3v/make-corpus.sh),
[`verify_easyloader.py`](phaze-0ni3v/verify_easyloader.py),
[`compare.py`](phaze-0ni3v/compare.py) and [`run_bench.sh`](phaze-0ni3v/run_bench.sh).

______________________________________________________________________

## 3. Correctness — 51 of 59 rows byte-identical across two binaries

`verify_easyloader.py` runs the same 59-cell matrix under each venv and `compare.py` diffs the
dumps. Rows are `EasyLoader` unless marked.

### 3a. Slices, output rate equal to the file's rate

Four offsets each — 1.0 s, 37.5 s, 123.456 s (deliberately not a whole number of samples), 300.0 s.

| container / codec | verdict | worst `max|diff|` |
| --- | --- | ---: |
| WAV / pcm_s16le | **bit-identical ×4** | 0 |
| MP3 / mp3float | **bit-identical ×4** | 0 |
| FLAC | **bit-identical ×4** | 0 |
| Ogg / Opus (48 kHz, read at 48000) | **bit-identical at 3 of 4**; one offset differs by a single ULP | 5.96 × 10⁻⁸ (= 2⁻²⁴) |
| MP4 / AAC-LC | **no** — PNS, see §3c | 3.02 × 10⁻² |
| MP4 / AAC-LC, `-aac_pns 0` | **bit-identical ×4** | 0 |
| `EqloudLoader`, WAV | **bit-identical ×2** | 0 |
| `EasyLoader` with `replayGain=-12` | **bit-identical** | 0 |

The Opus row reproduces `phaze-han03` §4a exactly, single ULP and all.

### 3b. Slices where the loader also resamples — unchanged, by construction

| row | verdict |
| --- | --- |
| MP3 44.1 kHz read at 16000, ×4 offsets | **bit-identical** |
| Opus 48 kHz read at 44100, ×4 offsets | **bit-identical** |

These are bit-identical because they do **not** seek — see §4. Before that restriction was added
they were not: the same matrix measured **−34.8 dB to −66.3 dB** relative RMS and ±1 sample of
length drift, which is the `phaze-han03` §4c resampler residual arriving in an algorithm that has
existing callers.

### 3c. The AAC residual is the codec's — re-proved on this build

Both halves of the `phaze-han03` §4b proof, re-run through `EasyLoader` rather than `AudioLoader`:

1. **It does not respond to preroll.** Rebuilt at four preroll lengths spanning 200×:

   | `SEEK_PREROLL_SEC` | 1.0 s | 37.5 s | 123.456 s | 300.0 s |
   | ---: | ---: | ---: | ---: | ---: |
   | 0.05 s | 1.851e-02 | 3.193e-04 | 3.015e-02 | 7.977e-05 |
   | 0.5 s | 2.046e-02 | 3.193e-04 | 3.015e-02 | 7.977e-05 |
   | 2.0 s | 2.118e-02 | 3.193e-04 | 3.015e-02 | 7.977e-05 |
   | 10.0 s | 2.118e-02 | 3.685e-04 | 2.594e-02 | 7.019e-05 |

   Flat. A decoder-warm-up effect would fall off with preroll; this does not, so the difference
   depends on state that no bounded history reconstructs.

1. **It disappears when the encoder stops using PNS.** The same audio re-encoded with
   `ffmpeg -c:a aac -aac_pns 0` is **bit-identical at all four offsets, at every one of the four
   preroll lengths** — 16 zeros.

### 3d. Boundary cases — identical behaviour, pre- and post-patch

Eight cases × three files (600 s WAV, 600 s MP3, 3 s MP3), all agreeing between builds:

| case | behaviour, both builds |
| --- | --- |
| `startTime` 0, `endTime` 1e6 (the defaults) | whole file |
| `endTime` past EOF | stops at EOF |
| `endTime` exactly at EOF | whole file |
| `startTime` past EOF | **empty, not an error** |
| `startTime == endTime` (at 0, and at 1 s) | **empty, not an error** |
| `startTime > endTime` | raises at configure time (message text differs, §1b) |

______________________________________________________________________

## 4. The one deliberate limitation, and why it is not a cop-out

**`EasyLoader` and `EqloudLoader` do not seek when `sampleRate` differs from the file's own rate.**
The condition is exact `Real` equality of the two rates, which is precisely `Resample`'s own
`src_ratio == 1.0` short circuit — i.e. the patch seeks exactly when the converter is a `fastcopy`
carrying no state.

This is forced by `phaze-han03` §4c, which fed the resampler progressively more history and
measured what came out:

| preroll fed to the resampler | relative RMS |
| ---: | ---: |
| 0 s | −49.0 dB |
| 2 s | −49.3 dB |
| 10 s | −50.6 dB |
| **60 s (= full history, `startTime` 0)** | **−78.5 dB**, head exactly zero |

Bit-exactness arrives only with the *full* history, which is to say only by not seeking. Any
bounded preroll leaves a residual around −49 dB spread uniformly across the slice. For
`MonoLoader`, whose `startTime`/`endTime` are **new** parameters with no existing callers, that is a
documented property of new API. For `EasyLoader`, whose parameters have shipped for years, it would
be a silent change to everybody's output — so it is not taken.

An attempt was made to do better and abandoned on evidence. Feeding the resampler a 0.1 s
**lookahead past `endTime`** does fix the tail (the last ~4 output samples at 48 kHz, which were
being computed from a zero-padded flush and which broke upstream's existing
`test_eqloudloader_streaming.testResample` by 7.14e-3 against a 5e-3 tolerance). But the head
residual survives, and cutting the surplus back to the *exact* pre-patch sample count cannot be
expressed through `Trimmer`, whose bounds are float32 seconds: the round-trip
`(count + 0.5) / sampleRate → count` only survives `Trimmer`'s own float32 multiply for counts
below ~4.2 × 10⁶ samples, i.e. slices shorter than ~95 s at 44.1 kHz. Half a fix that breaks on
long slices is worse than a clean restriction.

Lifting the restriction properly means prerolling *through* the resampler and discarding after it —
a change to the composites' shape, not a parameter. That is candidate #9 in `phaze-han03` §7, and
the PR offers it as a follow-up rather than guessing at it.

______________________________________________________________________

## 5. Tests — 24 new, and mutation-checked

Added to the four existing files, in their existing idiom, using only fixtures already committed to
upstream's `test/audio` submodule. No new test dependencies.

| file | new tests | covers |
| --- | ---: | --- |
| `io/test_audioloader_streaming.py` | 8 | per-container accuracy (wav/flac/mp3/ogg), AAC as a documented expectation, boundaries, short file, seeked MD5 |
| `io/test_monoloader.py` | 3 | accuracy at ratio 1.0 on four files, the resampled residual bounded, boundaries |
| `io/test_easyloader_streaming.py` | 9 | accuracy per container, resampled-is-unchanged, replayGain, boundaries, short file, the standard (non-streaming) variant |
| `io/test_eqloudloader_streaming.py` | 4 | accuracy, resampled-is-unchanged, boundaries |

Every accuracy test uses the same reference — read the whole file, cut the slice out afterwards —
because that is what a caller had to do before seeking existed, so agreeing with it *is* the
definition of correct.

Two of them were wrong on the first attempt and the reasons are worth recording:

- The `EqloudLoader` reference cannot be a slice of a full read. `EqualLoudness` is an IIR whose
  state depends on everything it has seen, so a slice of a whole-file read has **never** equalled a
  read of that slice — confirmed by running the test against the **unpatched** build, where it
  fails identically. The reference is now composed as `EqualLoudness(EasyLoader(slice))`, which is
  exactly what `EqloudLoader` is, and it passes on both builds.
- A `replayGain` test that asserted the ratio between two gains failed because `Scale` **clips** at
  ±1 by default, so the louder arm is not a scalar multiple of the quieter one. Replaced with the
  invariant actually wanted: a seeked slice at a given gain equals the same slice of a full read at
  that gain.

### 5a. Mutation results — the tests fail when the code is wrong

Three mutations, each rebuilt and run:

| mutation | failing tests |
| --- | ---: |
| `SEEK_PREROLL_SEC = 0.0` (seek straight to the packet, no preroll) | **8** across 3 files |
| round instead of truncate in seconds → samples | **4** across 2 files |
| ignore `endTime` entirely | **21** across 4 files |
| *(restored)* | **0** |

The rounding mutation is caught only because one test offset — `12.345 s` — is not a whole number
of samples. That was added on purpose after the first draft used only offsets like `12.5 s`, where
rounding and truncation agree and the mutation survived.

### 5b. The rest of upstream's suite is unaffected

Every unit test file except `machinelearning/**` (this build has no TensorFlow) was run on **both**
builds. The failing set is **identical** — 64 files, all pre-existing failures of a no-TensorFlow /
no-Chromaprint / Accelerate-FFT build, failing the same way at `b9fa6cb`. The one apparent extra
failure on the patched build (`synthesis/test_sprmodel_streaming.py`) passes on re-run on both
builds and was an artefact of running the two suites concurrently.

### 5c. What the tests do NOT cover

- **Opus.** Upstream's `test/audio` has no Opus fixture — every `.ogg` in it is Vorbis. This
  matters because Opus is the format that needs `pkt_timebase` (#1524): without that fix a seeked
  Opus read is **grossly** wrong, measured here at **+2.4 dB relative RMS** (i.e. essentially
  uncorrelated, the 312-sample pre-skip shift), and **bit-identical** with it. Adding a fixture
  means committing a binary to a separate submodule repository, so this is stated in the PR body
  instead. **The suite is green with or without #1524**, which is exactly why it needs saying.
- **An unseekable input asserting the fallback rather than an exception.** `phaze-han03` §8c asks
  for it; no fixture in `test/audio` is unseekable, and manufacturing one is out of scope.

______________________________________________________________________

## 6. The measured win, through `EasyLoader`

`run_bench.sh`, pre-patch venv against post-patch venv, adjacent in time, fresh process per point,
whole matrix twice. Both runs given.

| case | pre-patch | post-patch | speedup |
| --- | ---: | ---: | ---: |
| 3600 s MP3, full `[0, 3600)` | 2.331 / 2.338 s | 2.327 / 2.320 s | 1.0× |
| 3600 s MP3, second half `[1800, 3600)` | 2.140 / 2.132 s | **1.230 / 1.223 s** | **1.7×** |
| 3600 s MP3, one 30 s window at 1800 s | 2.070 / 2.016 s | **0.085 / 0.089 s** | **24.4× / 22.7×** |
| 3600 s MP3, one 30 s window at 3540 s | 1.942 / 1.878 s | **0.146 / 0.146 s** | **13.3× / 12.8×** |
| 600 s MP3, one 5 s window at 300 s | 0.680 / 0.690 s | **0.017 / 0.017 s** | **41.0× / 41.6×** |
| 3600 s MP3 (48 kHz source, read at 48000), 30 s window at 1800 s | 2.096 / 2.082 s | **0.088 / 0.088 s** | **23.9× / 23.7×** |
| 3600 s MP3 read at 16000 (resamples) | 29.117 / 29.069 s | 29.086 / 29.070 s | 1.0× |
| 48 kHz source read at 44100 (resamples) | 53.678 / 53.466 s | 53.487 / 53.336 s | 1.0× |

Read three things off it.

1. **`seek [1800, 3600)` is 52.8% of `full [0, 3600)`**, against 91.8% before. That is the
   `O(span)` property, now demonstrated through the algorithm #771 names.
1. **The last two rows are flat on purpose.** Where the composite resamples it does not seek (§4),
   so it costs exactly what it always did. Reporting them is the point: the win is real where it is
   claimed and absent where it is not.
1. **The 3540 s row is slower than the 1800 s row** (0.146 s vs 0.085 s) *and* returns 8 more
   samples than the pre-patch build. Both are the same fact: the seek lands where the parameters
   say, and `av_seek_frame` on a VBR-indexed MP3 near EOF costs a little more.

______________________________________________________________________

## 7. Corrections to `phaze-han03`

- **§8b.5 is superseded.** "`EasyLoader` is deliberately out of scope of the current patch …
  Propose it as a follow-up once `AudioLoader` lands" — operator review overruled that, and it was
  right to: #771's own title names `EasyLoader`, and the PR would not have answered the issue it
  cites. It is now in scope, done, and tested.
- **§8c's "endTime <= startTime (must raise)" is wrong** in the second half. `endTime == startTime`
  must *not* raise; `Trimmer` has always allowed it and returns an empty slice. See §1b.
- **§3a understates the sample-index rule.** The seek is not merely "sample-accurate"; to be usable
  by the composites it has to be sample-accurate *the same way `Trimmer` is*. See §1a.

______________________________________________________________________

## 8. State, and what happens next

- Branch `feat/audioloader-seeking` @ `febfee0a`, three commits, DCO-signed, authored as
  SimplicityGuy, **pushed to the fork and NOT submitted**. It remains held pending the operator's
  signed CLA — the change is now +573/−16, so far past the 20-line threshold that this is not
  close.
- The PR body is re-recorded on `phaze-09skl` and covers `EasyLoader`, the resampling restriction,
  the refreshed numbers and the #1524 dependency.
- The patch series is re-exported to [`phaze-han03/patches/`](phaze-han03/patches/) as `0003`,
  `0004`, `0005`; `0001` (`sysconfig`) and `0002` (`pkt_timebase`) are unchanged and already
  submitted as #1523 and #1524.
- **phaze adoption is still `phaze-a51jo`'s decision and none of its prerequisites moved here** —
  no TensorFlow-enabled linux build, no real-corpus seek-accuracy pass, no explanation of the
  `phaze-han03` §5c fine-tier regression, and `phaze-u1n7j` (P0, memory) is still open. Nothing in
  this bead is a reason to bring the patch into the image sooner.
