# phaze-b2qs9 — exhaustive analysis, measured on real hardware and real audio

- **Bead:** `phaze-b2qs9` (spike — the real peak-RSS / wall-clock measurement ADR-0007 §8 marked
  "still owed", plus the chunk-gate question it left open)
- **Date:** 2026-08-11
- **Tree:** branch `wt/bead/issue/phaze-b2qs9`, forked off `main` at `75e7575d` (the
  `phaze-w55w1` merge)
- **Code under test:** **`main@75e7575d`** — the `phaze-w55w1` merge commit, whose `src/phaze`
  tree is byte-identical to the bead tip `1db98ca6` — overlaid onto the deployed analyze image
  `ghcr.io/simplicityguy/phaze/job:2026.8.1` for its wheels. Identity proven four ways, and the
  reason it is not an image build is that CI failed at that commit so none was published (§1c)
- **Status:** measurement only. **No product code changed.**

______________________________________________________________________

## Verdict in one paragraph

**Exhaustive coverage works and is nearly free in wall clock; bounded memory does not, and it
breaks ADR-0005 at four hours.** Every one of the six files that ran end to end analyzed **every**
natural window of both tiers — `analyzed == total`, zero skips, from 1:00 to 12:04 — for
**0.56–0.79× the file's own duration** solo on the burst node, and against the capped code on the
*same* one-hour file the whole exhaustive rework costs **+2.75% wall** for **2× the fine windows**.
That is the good news and it is the half lane scheduling needed. The other half fails: whole-process
peak RSS is **linear in duration**, `≈ 0.7634 + 0.3108 × n_fine_chunks` GiB (R² 0.99959) — **+0.31
GiB per 30 minutes of audio** — measured at **2.1107 / 2.9287 / 4.1854 / 5.5211 / 10.2768 GiB** for
1:00 / 2:00 / 4:00 / 5:38 / 12:04. **The 4-hour and every longer file exceed the deployed
`memory_limit: 4Gi`; the longest exceeds it by 2.57×.** D-07's live-PCM arithmetic is correct and
does not describe the process, and the synthetic long-file test proves the claim only of a mocked
essentia. A model fitted below 6 hours predicted the 12-hour peak to **0.5%**, and running the
image's own **capped** module against the same 4-hour file peaks at **1.7681 GiB** — +1.3% over its
own 1-hour peak — so the growth is attributable to `phaze-w55w1` and to nothing about the corpus,
the node or the image. On the second question: the chunk gate **is** taken on the deployed essentia
(zero fallbacks, full window sets) and **is** worth **18.5–36.0%** of a non-final chunk's decode,
but per-chunk decode time is **not proportional to the chunk boundary** — on one file it *falls*
with chunk index — so `duration × (K+1)/2` does not describe wall clock. Finally, and loudly:
the longest healthy heartbeat gap rises with duration and with source sample rate, reaching
**930.719 s** in a real 12-hour run and **1 422.391 s — 79.0% of `analysis_stall_timeout_sec`** for
one chunk decode of a 10-hour 48 kHz file that is in the archive today.

______________________________________________________________________

## 1. Method

| | |
| --- | --- |
| **Host** | `vox` — Debian 13 (trixie), kernel 6.12.100, glibc 2.41, Xeon E3-1271 v3, **4 physical cores / 8 logical (SMT)**, 31.31 GiB total, k0s burst node (k0s v1.36.2), **taken out of the phaze backend registry for the measurement window** and otherwise idle |
| **Runtime** | the deployed job image `job:2026.8.1` **for its wheels only** — Python 3.14.7, `essentia-tensorflow` 2.1b6.dev1438, numpy 2.5.1 — with **`main@75e7575d`'s `src/phaze`** (tree `93c846bc…`) overlaid at `/scratch/src` and put ahead of the image's own `/app/src` on `PYTHONPATH`. No image exists at that commit; see §1c |
| **Models** | the deployed `phaze-models` PVC, mounted **read-only** (68 files, the 34 graphs + labels) |
| **Thread sizing** | `derive_sizing()` on this node returns `physical_cores=4, intra_op=4, inter_op=1, omp=4, concurrency=1` (`source='sysfs:thread_siblings_list'`) — the production derivation, applied by `services/analysis.py` at import |
| **Pod** | a bare `sleep infinity` pod on `vox`, **no Kueue queue label** (consumes no quota) and **no memory limit** — the limit is deliberately absent so a peak above ADR-0005's 4Gi tier would be *observed* rather than OOMKilled |
| **Process model** | **one exec'd child per file** (`python -m phaze.analysis_child <file> --models-dir /models`), exactly as production; the harness is the parent that `analysis_exec` is in production |
| **Peak RSS** | `wait4()`'s `ru_maxrss` for that one child — a kernel high-water mark, **not** a sampled curve, so it is immune to the `phaze-7i0k` §9 GIL trap. Cross-checked against a host-side 1 s sampler of the child's `/proc/<pid>/status:VmHWM` (a separate process, never GIL-starved). On Linux `ru_maxrss` is KiB |
| **Wall clock / per-tier split** | every protocol line the child emits is timestamped by the harness against a monotonic clock. Tier boundaries come from the heartbeat stage names (`fine_decode`/`fine` → `coarse_decode`/`coarse_model`/`coarse`), so the split is read off the shipped liveness channel rather than from added instrumentation |
| **Heartbeat gaps** | the same timestamped stream: the gap between consecutive heartbeat lines is what `analysis_exec`'s stall watchdog measures. The watchdog also resets on child stderr, so in principle these are an upper bound — in practice the child emitted exactly ONE stderr line per run (§4a), so they are the watchdog's own view |
| **Chunk gate** | two independent probes — (a) a direct decode-only benchmark that calls the shipped `_decode_windows_streaming` with and without `stop_at_sec` (§4), and (b) the per-chunk decode intervals read out of the heartbeat trace of every full run |

**No operator media is named anywhere in this document.** Files are referred to as `<set-01>` …
`<set-07>` and characterized only by container, codec, sample rate, bit rate and duration. No
filename, path, digest, file UUID or per-file metadata value appears here. Originals were never
opened for write: each measured file is a read-only copy taken to `vox`'s local scratch, and the
scratch tree is removed at the end.

### 1a. Sample — seven real corpus files, duration-stratified

Picked from the live corpus by **duration band only**, then characterized by container/codec/rate.
Four were already staged for cloud analysis and were copied out of the staging bucket; three
(`<set-02>`, `<set-04>`, `<set-07>`) were copied read-only from the fileserver. Nothing was
written back, and no original was opened for write.

| file | duration (s) | duration (h:mm) | container / codec | source rate | bit rate | size (B) | fine windows | coarse windows | fine chunks | coarse chunks |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `<set-01>` — 60 min control | 3 600.091429 | 1:00 | mp3 | 44 100 Hz | 192 006 | 86 405 202 | 120 | 20 | 2 | 1 |
| `<set-02>` — codec variant | 3 588.922630 | 1:00 | m4a / AAC-LC | 44 100 Hz | 269 193 | 120 764 534 | 120 | 20 | 2 | 1 |
| `<set-03>` | 7 200.078367 | 2:00 | mp3 | 44 100 Hz | 192 003 | 172 804 890 | 240 | 40 | 4 | 2 |
| `<set-04>` | 14 400.888000 | 4:00 | mp3 | 48 000 Hz | 231 455 | 416 644 887 | 480 | 80 | 8 | 3 |
| `<set-05>` | 20 279.872000 | 5:38 | mp3 | 44 100 Hz | 128 000 | 324 477 952 | 676 | 113 | 12 | 4 |
| `<set-06>` *(decode probe only — see below)* | 36 182.359175 | 10:03 | mp3 | 48 000 Hz | 320 000 | 1 447 298 463 | 1 206 | 201 | 21 | 7 |
| `<set-07>` — longest in corpus | 43 466.893061 | 12:04 | mp3 | 44 100 Hz | 165 354 | 898 429 776 | 1 449 | 242 | 25 | 9 |

Window and chunk counts are the shipped geometry: fine = 30 s windows with a 15 s trailing floor
in chunks of 60; coarse = 180 s windows with no floor in chunks of 30. They are the counts the
runs actually reported (`analyze_file`'s `*_windows_total`), which is why they are computed from
`_probe_duration_sec`'s truncated duration rather than from `ffprobe`'s fractional one;
`<set-06>`'s are the same arithmetic applied to a file that did not get a full run.

`<set-07>` is the **longest file in the corpus** — the whole library holds exactly one file above
36 182 s. Both 48 kHz sources are in the sample deliberately: the fine tier resamples 48 000 →
44 100 Hz, which the 44.1 kHz sources do not pay.

**`<set-06>` was measured partially, on purpose.** Six files ran end to end. `<set-06>` — the
second-longest, and the only 48 kHz file in the 10-hour class — was projected from the completed
bands at **≈ 12.5 hours** of exhaustive analysis, and by the time `<set-07>` finished, everything
`<set-06>` could still decide had already been decided by files that had run: the longest band was
covered by `<set-07>`, the memory model had been validated against a pre-registered prediction, and
the 48 kHz penalty had been isolated by `<set-04>`. Rather than spend another half-day of a node
that is out of the production registry, `<set-06>` contributes **targeted decode measurements**
(§4e) instead of a full run — which is also what makes §5's worst number a measurement rather than
an extrapolation. The results table therefore records **six** complete runs. This is a deviation
from the sample design in the bead and is called out here rather than papered over.

### 1d. Two deviations to record

- **The `kubectl exec` streaming session for `<set-07>` was severed at exactly 4 h 00 m** (an API
  server stream limit), which made the *driver script* believe the run had ended and start the next
  one. The analysis child was unaffected — it is a process in the pod, not a child of the exec —
  and ran to completion, writing its own `wall_sec` from its own monotonic clock. The wall clock
  and peak in the table are the child's, not the driver's.
- **Because of that, ~7 minutes of `<set-07>`'s 9 h 24 m run overlapped a second analysis** (the
  §8 capped-code control, started early by the same mistake and killed as soon as it was noticed:
  contention window 13:47:47Z–13:54:47Z). That is **0.02%** of the run, on the wall-clock axis
  only; `phaze-8r6t4` measured per-process peak flat across a 1 → 12 concurrency sweep, so the peak
  is unaffected. No other run overlapped anything.

### 1b. Why the runs are serial

Every run had the node to itself. Production admits up to `cap = 4` analyze pods at once, and
`phaze-8r6t4` §10 measured what that costs per file (+113.2 s of wall per added worker at that
spike's file sizes, +83.6% per-file wall at W=4 against W=2), so a concurrent measurement would
have produced a number that is neither the per-file cost nor the throughput ceiling. The
solo-run wall clock reported here is the input to that correction, not a substitute for it. Peak
RSS is unaffected either way: `phaze-8r6t4` found per-process peak flat at 1.282–1.332 GiB across
a 1 → 12 concurrency sweep.

The node was verified idle before the matrix started (load average 0.68 / 0.94 / 1.09, zero
`phaze` analyze pods, `analysis_child` process count 0).

### 1c. Code identity — what was measured, proven, and why not an image

**Everything in this document was measured against `main@75e7575d`'s `src/phaze`** — the
`phaze-w55w1` merge commit — carrying the post-review fixes (heartbeat pinned on replays,
per-window beats in the fallback decode loop, the derived 2× outer net, both-tasks cancellation).
Verified four ways rather than assumed:

| check | evidence |
| --- | --- |
| the merge commit and the bead tip are the same code | `75e7575d:src/phaze` and `1db98ca6:src/phaze` resolve to the **same git tree object**, `93c846bcf0410f05a7b44fbf97fb74925ebc5681` |
| the file that produced the numbers | `git show 75e7575d:src/phaze/services/analysis.py` hashes to **`38be362d…`** — the digest the container reported and every child re-reported |
| the four post-review fixes are present | `_beat()` per window in the fallback loop (`analysis.py:989`), `_ANALYSIS_OUTER_HEARTBEAT_MULTIPLIER = 2` (`config.py:88`), `await _settle(drive, watch)` on the cancellation path (`analysis_exec.py:321`), and the pinned `timeout=0` + `heartbeat` on the replay path (`reenqueue.py:1200`) |
| what the deployed image would have given instead | its own `services/analysis.py` is **`a8c30496…`** — the pre-`w55w1` capped implementation. Measuring the image as shipped would have answered the wrong question |

**There is no published image at or after `75e7575d`, and none can be waited for.** The `main` push
at `75e7575d` ran CI **run 31544941830**, which **failed** on `test / Tests (shared-rest)`;
`ci.yml`'s `docker-publish` job is `needs: [detect-changes, aggregate-results]`, so it never ran and
no `job:` tag was produced for that commit. (That red run on `main` is a real finding in its own
right and is reported upstream; it is not caused by this spike, which changes no code.)

The overlay is therefore not a second-best substitute — it is the *only* way to run the merged sha,
and on the axis that matters it is stronger than an image would be: the source is the merge commit's
own bytes, re-hashed inside the container **and inside the process that produced every number**,
while the runtime underneath (Python 3.14.7, `essentia-tensorflow` 2.1b6.dev1438, numpy 2.5.1, the
`phaze-models` PVC) is exactly what production runs.

### 1c-i. The overlay, and the proof it took

The deployed image predates `phaze-w55w1`, so measuring it directly would answer the wrong
question — and a *silent* fallback to the image's own module would answer the wrong question while
looking right. Three checks close that:

| check | where | result |
| --- | --- | --- |
| the image's own `services/analysis.py` | inside the container | `a8c30496686dab41145af0d1095aaaea9ea82cb84825efb52b6b760a46838032` — the pre-`w55w1`, capped implementation |
| the overlaid `services/analysis.py` | inside the container | **`38be362d2329c2045f6fa41072ac04552367ac57c95a21d5ce19a946ada52c00`** — byte-identical to `main@75e7575d:src/phaze/services/analysis.py` |
| the overlaid `analysis_child.py` | inside the container | **`2337ff4d95905fbb1a404a6242dbce43797e7fa5134e593d2ff91e006c3cb9f6`** — byte-identical to `main@75e7575d:src/phaze/analysis_child.py` |
| the module object actually imported | in the harness's own probe, same env as the child | `phaze.services.analysis.__file__` resolves to `/scratch/src/phaze/services/analysis.py` and re-hashes to `38be362d…`; the value is recorded in every run's summary |
| the post-`w55w1` constants | inside the container | `_FINE_CHUNK_WINDOWS = 60`, `_COARSE_CHUNK_WINDOWS = 30` — present, i.e. the chunked implementation, and `_stride_to_cap` is absent |

______________________________________________________________________

## 2. Results — peak RSS and wall clock per band

*(this table is filled in as each band completes; runs are strictly serial, shortest first)*

| file | duration (s) | **peak RSS (GiB)** | **wall (s)** | wall ÷ duration | startup (s) | fine tier (s) | coarse tier (s) | fine windows | coarse windows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `<set-01>` | 3 600.09 | **2.1107** | **2 016.508** | 0.560 | 2.265 | 159.557 | 1 854.649 | 120 / 120 | 20 / 20 |
| `<set-02>` (m4a) | 3 588.92 | **2.1166** | **2 006.893** | 0.559 | 2.259 | 160.389 | 1 844.185 | 120 / 120 | 20 / 20 |
| `<set-03>` | 7 200.08 | **2.9287** | **4 171.154** | 0.579 | 2.260 | 337.351 | 3 831.477 | 240 / 240 | 40 / 40 |
| `<set-04>` (48 kHz) | 14 400.89 | **4.1854** | **11 352.656** | 0.788 | 2.266 | 3 461.933 | 7 888.402 | 480 / 480 | 80 / 80 |
| `<set-05>` | 20 279.87 | **5.5211** | **12 920.196** | 0.637 | 2.264 | 1 521.660 | 11 396.163 | 676 / 676 | 113 / 113 |
| `<set-07>` — longest | 43 466.89 | **10.2768** | **33 843.202** | 0.779 | 2.257 | 5 791.331 | 28 049.416 | 1 449 / 1 449 | 242 / 242 |

Peak RSS is `wait4()`'s `ru_maxrss` for the analysis child. The host-side `VmHWM` sampler agreed
**to the kibibyte** on every run (`2 213 220 KiB` on `<set-01>`), which is the harness self-test
`phaze-8r6t4` §1d recommends.

Every run analyzed **every** natural window of both tiers — `analyzed == total` in all four
counters, with zero per-window skips. Exhaustive coverage is confirmed on real audio, not just in
the unit tests.

### 2a. The one-variable A/B: the same file, the capped code and the exhaustive code

The image ships its own pre-`phaze-w55w1` `services/analysis.py` (`a8c30496…`, §1c). Running it
against `<set-01>` — same node, same file, same pod, the module digest recorded in the run summary
— isolates the change from everything else:

| `<set-01>`, 3 600.09 s | code | fine windows | coarse windows | **wall (s)** | **peak RSS (GiB)** |
| --- | --- | ---: | ---: | ---: | ---: |
| capped (`a8c30496…`, the deployed image's own) | pre-`w55w1` | 60 / 120 *(strided)* | 20 / 20 | **1 962.620** | **1.7450** |
| exhaustive (`38be362d…`, `main@75e7575d`) | post-`w55w1` | **120 / 120** | 20 / 20 | **2 016.508** | **2.1107** |
| **delta** | | **2× the fine work** | — | **+53.888 s (+2.75%)** | **+0.3657 GiB (+21.0%)** |

Two things fall out of this table.

**The capped arm reproduces the number ADR-0005's tier was derived from.** `phaze-5lop` measured
**1.7383 GiB** for this exact shape on synthetic audio; the capped arm here measures **1.7450 GiB**
on real audio — **+0.39%**. The baseline is sound and the corpus is not exotic, which is what
licenses reading the rest of this document as a change measurement rather than a content one.

**Exhaustive coverage is nearly free in wall clock and expensive in memory.** Doubling the fine
tier's analyzed windows costs **2.75%** of the run — the coarse tier's 34 model sweeps dominate,
exactly as `docs/essentia-analysis.md` says post-`phaze-5lop`. The memory cost of the same change
is **21.0%** on a one-hour file, and §3b is why that number keeps growing with duration.

### 2b. The control that pins the attribution: the capped code on a LONG file

The 1-hour A/B above cannot separate "the change" from "the file". So the same capped module was
run against `<set-04>`, the 4-hour file, under the caps it was written for:

| code | file | duration | fine | coarse | **wall (s)** | **peak RSS (GiB)** |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| capped `a8c30496…` | `<set-01>` | 1:00 | 60 / 120 | 20 / 20 | 1 962.620 | **1.7450** |
| capped `a8c30496…` | `<set-04>` | 4:00 | 60 / 480 | 30 / 80 | 3 370.597 | **1.7681** |
| exhaustive `38be362d…` | `<set-04>` | 4:00 | **480 / 480** | **80 / 80** | 11 352.656 | **4.1854** |

**The capped code's peak moves +1.3% across a 4× duration span.** That is ADR-0005's premise,
reproduced on this node, on this corpus, on this image, today: *peak is uncorrelated with
duration* — for the code the ADR was written about. On the identical file the exhaustive code
peaks **2.37× higher (+2.4173 GiB)**.

The growth in §3b is therefore attributable to `phaze-w55w1` and to nothing else in the
environment — not the corpus, not the node, not the image, not real-versus-synthetic audio.

______________________________________________________________________

## 3. Bounded memory — the shape is right, the claim as written is not

### 3a. Where the high-water forms

Instantaneous RSS is **not** monotone (`phaze-7i0k` §2 again): on `<set-01>` it oscillates between
**1.2613 GiB** and **1.9285 GiB** while the high-water sits at **2.1107 GiB**. Only the high-water
is quoted below.

The high-water is built in two places, and both matter:

| `<set-01>` (2 fine chunks, 1 coarse chunk) | high-water after |
| --- | ---: |
| import essentia + TF, probe duration | 0.2460 GiB |
| fine chunk 1 of 2 | 1.0586 GiB |
| fine chunk 2 of 2 (end of FINE tier) | **1.3596 GiB** |
| ~4 coarse model sweeps in | 2.1072 GiB |
| remaining 30 coarse sweeps (1 600 s of run) | **2.1107 GiB** (+0.17%) |

That second half reproduces `phaze-esut` / `phaze-7i0k` exactly: the coarse sweep sets the peak
within the first handful of models and then saturates. The **first** half is the new thing.

### 3b. The fine tier's high-water grows with the CHUNK COUNT

D-07 states that "peak PCM residency is a function of the CHUNK SIZE and never of the total
duration". Per **chunk**, that is true — each chunk decodes the same 60 windows. Per **process**,
it is not what the kernel high-water does:

| fine chunk completed | `<set-01>` (2) | `<set-03>` (4) | `<set-04>` (8) | `<set-05>` (12) |
| ---: | ---: | ---: | ---: | ---: |
| after import / probe | 0.2460 | 0.2462 | 0.2461 | 0.2463 |
| 1 | 1.0586 | 1.0564 | 1.0894 | 1.0792 |
| 2 | **1.3596** | 1.3583 | 1.3922 | 1.3806 |
| 3 | — | 1.7411 | 1.6856 | 1.7178 |
| 4 | — | **2.0188** | 2.0028 | 2.0356 |
| 5 | — | — | 2.3215 | 2.2870 |
| 6 | — | — | 2.6425 | 2.6105 |
| 7 | — | — | 2.9603 | 2.9282 |
| 8 | — | — | **3.2435** | 3.2483 |
| 9 | — | — | — | 3.5670 |
| 10 | — | — | — | 3.8850 |
| 11 | — | — | — | **4.1605** |
| 12 | — | — | — | 4.1605 |

Four files agree to within 3% at **every** shared point — this is reproducible, not noise — and
each additional fine chunk adds a near-constant **+0.25 to +0.34 GiB** of high-water, on a tier
whose live PCM is a flat ~317 MB per chunk. `<set-05>`'s eleven increments are +0.833, +0.301,
+0.337, +0.318, +0.251, +0.324, +0.318, +0.320, +0.319, +0.318, +0.276 — **linear, with no sign of
saturation over a 12× span.** The `_malloc_trim()` at the end of every `_decode_windows` is
already being called on every one of these chunks; what grows is what a trim does not return.

Least squares over the 25 loaded chunk-boundary points above (excluding the post-import baseline
and `<set-05>`'s final chunk, which is short and adds nothing):

```
peak_after_fine_tier_GiB  ≈  0.7634  +  0.3108 × n_fine_chunks       (n = 25, R² 0.99959)
n_fine_chunks             =  ceil(duration_sec / 1800)
```

**That is linear in duration.** It is precisely the property D-07 says the chunking eliminated.

The coarse tier then adds a large one-off plus a small per-chunk term — `<set-05>`'s four coarse
chunks take the high-water 4.1605 → 5.2346 → 5.3816 → 5.5038 (final 5.5211), i.e. **+1.074** for
the first chunk's TF working set and **+0.13 ± 0.02** for each one after it. Combining:

```
peak_GiB  ≈  0.7634  +  0.3108 × n_loaded_fine_chunks  +  1.07  +  0.13 × (n_coarse_chunks − 1)
```

which predicts `<set-07>` (24 loaded fine chunks, 9 coarse chunks) at **≈ 10.33 GiB**. That
prediction was written down **before** the `<set-07>` run — a pre-registered test, in the style of
`phaze-esut` §3d — and `<set-07>` then measured:

| | predicted | **measured** | error |
| --- | ---: | ---: | ---: |
| high-water after the fine tier (24 loaded chunks) | 8.2226 GiB | **8.2406 GiB** | +0.22% |
| whole-process peak | 10.33 GiB | **10.2768 GiB** | −0.51% |

`<set-07>`'s own fine series is a straight line across **24 chunks**: 1.0596, 1.3952, 1.7100,
1.9980, 2.3163, 2.6342, 2.9252, 3.2513, 3.5701, 3.8924, 4.2112, 4.5293, 4.8063, 5.1245, 5.4439,
5.7615, 6.0554, 6.3672, 6.6718, 6.9850, 7.3051, 7.6253, 7.9130, 8.2406 GiB. Its nine coarse chunks
then add 8.2406 → 9.2458 → 9.3466 → 9.5010 → 9.7112 → 9.8360 → 9.9745 → 10.1222 → **10.2768**
(+1.005 for the first, +0.101…+0.210 for each after).

**A model fitted on files up to 5h38m predicts a 12h04m file to within half a percent. The growth
is not an artifact of any one file, and it does not saturate.**

The consequences are direct and they are not small:

- `<set-03>`'s **whole-process** peak is **2.9287 GiB** against `<set-01>`'s **2.1107 GiB** —
  **+38.8% for 2× the duration**.
- **`<set-04>`'s FINE TIER ALONE ends at 3.2435 GiB** — already past ADR-0005's `memory_request:
  3Gi`, before a single TensorFlow graph is constructed, on a 4-hour file. `<set-05>`'s fine tier
  ends at **4.1605 GiB**, past the `memory_limit: 4Gi`, for the same reason.
- The claim "peak PCM residency is a function of the CHUNK SIZE and never of the total duration"
  is true of the **live PCM** and false of the **process**. Bounding the live working set did not
  bound the high-water, and the high-water is what the cgroup kills on.

______________________________________________________________________

## 4. The chunk gate — it runs, it is correct, and it saves far less than D-07's arithmetic implies

### 4a. Which path actually runs

`_decode_windows` has three rungs: gated streaming, ungated streaming, per-window `EasyLoader`.
Rungs 2 and 3 are only reached from an `except`, and each logs a warning first. Across every run
in this spike the child's **entire** stderr was one line —

```
[   INFO   ] MusicExtractorSVM: no classifier models were configured by default
```

— with **zero** `gated streaming decode failed …; retrying ungated`, zero `streaming decode pass
failed …; falling back to per-window EasyLoader`, and zero per-window skip warnings. Every
non-final chunk of every tier of every file took the **gated** rung on the deployed essentia, and
returned a full window set (`n_decoded == n_windows`, 60/60 in the direct probe). **The gate is
not falling back.**

That single stderr line has a second consequence worth recording: because essentially nothing but
protocol lines reaches the parent, the heartbeat gaps in §5 are not merely an upper bound on what
the stall watchdog sees — they *are* what it sees.

### 4b. Does a non-final chunk's decode scale with its boundary? No.

The direct probe (§1, "Chunk gate" (a)) calls the shipped `_decode_windows_streaming` on
`<set-05>` — 20 279 s, 676 fine windows, **12 fine chunks** — one **fresh process per
measurement**, so no run inherits another's allocator or page-cache state. `stop_at_sec` for chunk
*k* is `1800 × (k+1)`; the last chunk is ungated by design.

| chunk | gate `endTime` (s) | audio the gate should admit (s) | **decode wall (s)** | implied decode rate (× realtime) |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 1 800 | 1 801 | **111.415** / **114.754** *(run twice, first and last in the matrix)* | 15.7–16.2 |
| 1 | 3 600 | 3 601 | **100.057** | 36.0 |
| 2 | 5 400 | 5 401 | **90.720** | 59.5 |
| 3 | 7 200 | 7 201 | **91.616** | 78.6 |
| 6 | 12 600 | 12 601 | **108.587** | 116.0 |
| 9 | 18 000 | 18 001 | **124.400** | 144.7 |
| 10 | 19 800 | 19 801 | **129.202** | 153.3 |
| 9 | *(ungated)* | 20 279 (EOF) | **152.722** | 132.8 |

**Read the last column.** If the gate stopped the decode at the boundary, the implied rate would
be roughly **constant** — the same decoder doing proportionally less work. It is not: it climbs
**9.8×** across the sweep, which is another way of saying the wall clock is **nearly independent
of the boundary**. Chunk 10 admits **11.0×** the audio chunk 0 does and costs **1.16×** the time
(1.13× against the pos-0 repeat). A boundary-proportional decode would have cost 11×.

The same shape appears **in situ**, in the heartbeat trace of the real `<set-01>` run, where the
comparison is even starker because that file has exactly two fine chunks — one gated, one not:

| `<set-01>` fine chunk | gate | audio admitted (s) | decode wall (s) |
| --- | --- | ---: | ---: |
| 0 | `stop_at_sec = 1800` | 1 801 | **62.185** |
| 1 (last) | *ungated* | 3 600 (EOF) | **49.927** |

The gated chunk, admitting **half** the audio, took **25% longer** than the ungated one.

### 4c. What the gate is actually worth

It is worth something — just not what D-07 claims. Against the only clean like-for-like pair
(`<set-05>` chunk 9, same 60 windows, same process shape, gate the only difference):

- gated `124.400 s` vs ungated `152.722 s` → the gate saves **28.322 s, 18.5%**, on a chunk whose
  boundary is 88.8% of the file. On chunk 0 (boundary 8.9% of the file) the gated cost is
  **111.415 / 114.754 s** against the same ~152.7 s ungated full-file decode → **~25–27%**.

So the saving is real, roughly **19–27%** per non-final chunk, and — critically — **it does not
grow as the boundary shrinks.** D-07's model says total decode falls from `K × duration` to
`duration × (K+1)/2`, i.e. a **46% saving at K = 12** and asymptotically 50%. Extrapolating the
measured per-chunk costs over `<set-05>`'s 12 fine chunks gives ≈ **1 310 s gated** against
≈ **1 832 s** if every chunk paid the ungated cost — a **28.5%** saving, not 46%.

**The claim that survives:** the gate is taken, it is correct, and it makes non-final chunk decodes
cheaper. **The claim that does not:** that a non-final chunk's decode time scales with the chunk
boundary rather than the file. It plainly does not, and therefore `duration × (K+1)/2` is not a
usable model of exhaustive analysis's decode wall clock. §7 proposes the follow-up.

### 4d. What per-chunk decode time actually tracks

Across every full run the per-chunk fine decode series **falls monotonically with chunk index**,
which no boundary-proportional model can produce:

| file | fine chunk decode wall, in order (s) — last entry is the **ungated** final chunk |
| --- | --- |
| `<set-01>` (2 chunks) | 62.185, *49.927* |
| `<set-02>` (2 chunks) | 62.979, *50.033* |
| `<set-03>` (4 chunks) | 73.431, 61.525, 53.208, *53.093* |
| `<set-04>` (8 chunks, 48 kHz) | 565.256, 513.191, 460.622, 418.494, 384.034, 344.594, 308.468, *274.026* |
| `<set-05>` (12 chunks) | 111.852, 99.688, 91.446, 91.357, 99.336, 102.031, 108.896, 117.425, 123.730, 124.592, 129.189, *50.064* |
| `<set-07>` (25 chunks) | 184.526, 172.714, 164.203, 164.272, 172.529, 174.914, 181.094, 189.290, 195.952, 196.934, 200.926, 210.451, 211.546, 217.525, 222.767, 226.776, 235.282, 239.865, 245.271, 250.380, 257.632, 263.161, 270.215, 273.532, *83.583* |

`<set-04>`'s series **halves** — 565.256 → 274.026 — while its gate boundary grows 8×. `<set-07>`'s
*rises*, but only **1.48×** across a **24× boundary span** (164.203 s at 5 401 s of admitted audio
→ 273.532 s at 43 201 s). Neither is proportionality; one of them has the wrong sign for it. And in
**every** run the *ungated* final chunk, which decodes the whole file, is the **cheapest of all** —
on `<set-07>` it is **83.583 s** against 273.532 s for the gated chunk immediately before it, whose
boundary admits 0.6% *less* audio.

**This is why §4c's controlled A/B matters and the in-situ series cannot replace it.** Inside a
real run the gate's contribution is swamped by a second effect that moves the same direction as
chunk index; the fresh-process probe is the only place the gate is the sole variable. Two things
correlate with the in-situ series, and neither is the boundary:

1. **The process heap.** Over those eight chunks the high-water grows 0.2461 → 3.2435 GiB (§3b).
   The first chunk faults in and zeroes ~1 GiB of fresh pages; the eighth reuses an arena that is
   already 3 GiB wide and was never returned. Decode getting *cheaper* as the leak gets *worse* is
   a consistent reading of both series and is the hypothesis this spike would test first — it is
   **inference from two correlated curves, not a measured mechanism**, and is flagged as such.
1. **The source sample rate.** `<set-04>` is a 48 kHz source and the fine tier is 44.1 kHz, so
   every fine chunk pays a real `libsamplerate` conversion. Its chunk decodes are **5.2× to 7.7×**
   `<set-03>`'s at a comparable chunk index, on a file only 2× longer. `MonoLoader` has no
   ratio-1.0 short circuit, but a 1.0 ratio is evidently still far cheaper than 0.91875. **Source
   sample rate is a first-order term in exhaustive analysis's cost and appears nowhere in the
   current cost model.**

### 4e. `<set-06>` — the same gate A/B on a 10-hour 48 kHz file

`<set-06>` did not get a full run (§1a), but it is the worst shape in the corpus for decode — 10:03
of 48 kHz audio, 21 fine chunks — so its **chunk 0** was measured directly, gated and ungated, in
fresh processes:

| `<set-06>` fine chunk 0 | audio the decode should read | **wall (s)** |
| --- | ---: | ---: |
| gated, `stop_at_sec = 1800` | 1 801 s (**5.0%** of the file) | **1 422.391** |
| ungated | 36 182 s (EOF) | **2 221.179** |

**The gate saves 798.788 s — 36.0%** — the largest saving measured anywhere in this spike, and the
reason is §4d's second bullet: on a 48 kHz source the per-sample resample cost is a larger share of
the decode, so the part the gate *does* remove is worth more. It remains **nowhere near
proportional**: the gated pass reads 5.0% of the audio for 64.0% of the ungated cost.

That single number also settles §5's extrapolation — see below.

______________________________________________________________________

## 5. Heartbeat health against the 1 800 s stall threshold

`analysis_stall_timeout_sec` is 1 800 s of **silence**, and the derived SAQ outer net is
`2 ×` that — 3 600 s (`BaseSettings.analysis_job_heartbeat_sec`,
`_ANALYSIS_OUTER_HEARTBEAT_MULTIPLIER = 2`).

The watchdog resets on *any* child output, so in principle these gaps are an upper bound. In
practice they are exact: across every run the child wrote **one** stderr line in total (§4a), so
protocol heartbeats are the only thing resetting the deadline.

| file | **longest healthy heartbeat gap (s)** | where | % of the 1 800 s inner threshold | % of the 3 600 s outer net |
| --- | ---: | --- | ---: | ---: |
| `<set-01>` | 105.762 | `coarse_decode` → first `coarse_model` | 5.9% | 2.9% |
| `<set-02>` | 104.571 | `coarse_decode` → first `coarse_model` | 5.8% | 2.9% |
| `<set-03>` | 197.446 | `coarse_decode` → first `coarse_model` | 11.0% | 5.5% |
| `<set-04>` (48 kHz) | **565.256** | `fine_decode` → first `fine` (chunk 1 of 8) | **31.4%** | 15.7% |
| `<set-05>` | 454.978 | `coarse_decode` → first `coarse_model` (chunk 1 of 4) | 25.3% | 12.6% |
| `<set-07>` | **930.719** | `coarse_decode` → first `coarse_model` (chunk 1 of 9) | **51.7%** | 25.9% |
| `<set-06>` (48 kHz, decode probe — §4e) | **1 422.391** | fine chunk 0 decode, measured in isolation | **79.0%** | 39.5% |

**On the longest file in the corpus the worst healthy silence is 930.719 s — 51.7% of the
threshold that would have killed it.** That is the margin, measured, not extrapolated: the file
this feature exists for spends over half the stall budget inside a *single* legitimate step, nine
times in one run (930.719, 920.125, 838.364, 797.249, 755.208, 713.295, 673.578, 631.365,
475.494 s — one per coarse chunk).

Three things to take from the table:

- **The worst silence is a chunk decode**, in both tiers — the one stage that cannot heartbeat,
  because it is a single blocking `essentia.run()` inside C++. D-08 sized the threshold against
  exactly this ("one chunk's decode on a 12-hour file"); the sizing is now measured, and the
  answer is 930.719 s against 1 800.
- **It scales with duration.** 105.762 → 197.446 → 565.256 → 454.978 → 930.719 s at 1 / 2 / 4 /
  5.6 / 12.1 hours. A 24-hour file — which the corpus does not currently contain but a
  multi-day festival recording plausibly would — extrapolates past the threshold.
- **And with source sample rate, which is where the margin nearly runs out today.** `<set-04>` is
  a 4-hour file whose worst gap (565.256 s) is **2.86×** the 2-hour 44.1 kHz file's, because at
  48 kHz the fine tier pays a real resample (§4d). Following that to the corpus's worst shape,
  `<set-06>`'s **first fine chunk decodes in 1 422.391 s — 79.0% of the threshold** (§4e). One
  step. One healthy file that exists in the archive today.

**⚠️ This is the loud one, after the memory result.** `1 422.391 / 1 800 = 79.0%`. A 48 kHz source
of ~12.7 hours — a length the corpus already reaches at 44.1 kHz — puts a *single legitimate chunk
decode* past `analysis_stall_timeout_sec`, and the failure mode is a `SIGKILL` on a healthy
multi-hour analysis, reported and stored as a stall, with the file marked terminally failed and
deliberately not retried. That is precisely the `phaze-1b39` shape D-08 exists to prevent,
re-entering through a different door: not a wall clock on total runtime, but a silence bound the
one un-instrumentable stage can exceed on its own. The margin is 21% and shrinking in two
independent variables.

______________________________________________________________________

## 6. Against ADR-0005's limit tier

The deployed tier is `memory_request: 3Gi`, `memory_limit: 4Gi` (`backends.toml`, vox), derived
from `phaze-5lop`'s **1.7383 GiB** end-to-end peak at 1.73× / 2.30× headroom. Measured against
that tier:

| file | duration | **peak RSS** | ÷ 1.7383 GiB (the derivation basis) | vs `request` 3Gi | vs `limit` 4Gi |
| --- | ---: | ---: | ---: | --- | --- |
| `<set-01>` | 1:00 | 2.1107 GiB | 1.21× | 70.4% | 52.8% |
| `<set-02>` | 1:00 | 2.1166 GiB | 1.22× | 70.6% | 52.9% |
| `<set-03>` | 2:00 | 2.9287 GiB | 1.68× | **97.6%** | 73.2% |
| `<set-04>` | 4:00 | **4.1854 GiB** | **2.41×** | **139.5% — over** | **104.6% — OVER THE LIMIT** |
| `<set-05>` | 5:38 | **5.5211 GiB** | **3.18×** | **184.0% — over** | **138.0% — OVER THE LIMIT** |
| `<set-07>` | 12:04 | **10.2768 GiB** | **5.91×** | **342.6% — over** | **256.9% — OVER THE LIMIT** |

**A four-hour file exceeds the cgroup memory limit.** In production `<set-04>` is not a slow job;
it is an `OOMKilled` pod, mapped by `job_runner` to `EXIT_ANALYSIS` and by the SAQ path to a
terminal `reason="crashed"` — deliberately **not** retried. Every longer file is worse.

This is not a request to raise the limit. ADR-0005's own text is explicit that "a limit set too
low converts a would-have-succeeded job into a failure" *and* that the limit exists as a backstop,
not as headroom to be spent; and `backends.toml` says in as many words **"Do NOT raise the limit
to make them stop."** The right response to §3b is to stop the per-chunk growth, not to buy room
for it. A re-derivation is proposed in §7 for the case where the growth turns out to be
irreducible.

______________________________________________________________________

## 7. Proposed follow-ups

*(proposed here, to be filed by the planner — this spike files nothing and fixes nothing)*

### FU-1 — BUG, P0: exhaustive analysis's peak RSS is linear in duration and breaches the 4Gi limit at 4 hours

> **RESOLVED — `phaze-u1n7j`, 2026-08-13. See `phaze-u1n7j-vox-fix-verification.md`.** The cause was
> none of the three guesses below: a chunk's essentia streaming network was **dropped but never
> disconnected**, so each *gated* chunk retained ~5 MiB per window branch (D-09 in
> `services/analysis.py`). Re-measured on this node, this image and these same files, the peaks
> below become **1.4985 / 1.6500 / 1.6725 GiB** at 1:00 / 4:00 / 12:04 — the longest file in the
> corpus goes from **2.57× the 4Gi limit to 41.8% of it** — for **−0.22% / −0.52% / −0.83%** of
> wall clock and a **byte-identical** analysis result. Two guesses to strike explicitly, because
> they cost time: **glibc arena fragmentation is NOT the mechanism** (the retention measures
> 5.42 MiB/branch under glibc 2.41 and 5.14 on macOS — two allocators, one constant), and it is
> **not the hoisted extractors** either. The acceptance line below reads **+11.6%** rather than
> ≤10%, and that residue is a bounded step, not a slope: `<set-01>` is the only file whose coarse
> chunk is part-full. Between the two bands with full coarse chunks it is **+1.4%** for 3.1× the
> fine chunks.

- **Evidence:** §3b. `peak_after_fine_tier ≈ 0.7634 + 0.3108 × n_fine_chunks` GiB, R² 0.99959 over
  four files and 25 chunk boundaries; whole-process peaks 2.1107 / 2.9287 / 4.1854 / 5.5211 GiB at
  1 / 2 / 4 / 5.6 hours. `<set-04>` and `<set-05>` are **over** the deployed `memory_limit: 4Gi`.
- **What it means operationally:** on the burst lane every file past roughly **3 hours** is an
  `OOMKilled` pod, which both lanes map to a **terminal, deliberately un-retried** failure. The
  exhaustive-analysis decision (ADR-0007 §7) was accepted *on the condition* that the chunked
  rework keep ADR-0005's limits valid. Measured, it does not.
- **Where to look first (not investigated here — this spike does not fix):** the per-chunk
  teardown in `_decode_windows_streaming` (`branches.clear()` / `del loader, pool, gate`) versus
  what the `essentia.Pool` and the streaming network actually release; glibc arena fragmentation
  from a repeated allocate-317 MB / free cycle (`M_ARENA_MAX`, `M_MMAP_THRESHOLD` — note
  `_malloc_trim` is *already* called per chunk and is evidently not recovering this); and the
  `RhythmExtractor2013` / `KeyExtractor` instances that `phaze-ap8y` hoisted to live across all
  chunks.
- **Acceptance:** peak RSS flat (within, say, 10%) between a 1-hour and a 12-hour file on vox,
  measured the way this spike measures it.

### FU-2 — DECISION: re-open ADR-0005's premise, and re-derive the tier

ADR-0005 decision point 4 reads "Reject duration-derived requests… Peak is uncorrelated with
duration; sizing on duration would be precisely the wrong variable." That was **true of the
pre-`w55w1` pipeline and is false of the shipped one** — the correlation is now R² 0.99959. The
ADR needs re-opening on its own terms, not silently re-numbered. If FU-1 turns out to be
irreducible, the honest options are a duration-derived request/limit, a per-file admission bound,
or reinstating some form of cap — all of which are operator decisions, not spike decisions.

### FU-3 — DOC/CODE: correct D-07's chunk-gate arithmetic

`duration × (K + 1) / 2` describes the *audio volume* decoded, not wall clock, and the docstring
presents it as the gate's saving. Measured (§4): the gate is taken and correct, saves **18.5–27%**
of a non-final chunk's decode in a controlled A/B, and per-chunk decode time is **not**
proportional to the boundary. The D-07 record in `services/analysis.py` and the corresponding
paragraph in `docs/essentia-analysis.md` should say what was measured.

### FU-4 — RISK: the stall threshold's margin shrinks with duration *and* source sample rate

§5. The worst healthy silence is already **565.256 s** (31.4% of 1 800 s) on a 4-hour 48 kHz file
and it is a **chunk decode** — one blocking `essentia.run` call that cannot beat from Python.
Options: raise `analysis_stall_timeout_sec` (cheap, but the outer net is derived at 2× and the
whole point of D-08 is that the number be defensible); or give the chunk decode a heartbeat of its
own — a watchdog thread in `phaze.analysis_child` that emits a `stage="decoding"` beat on a timer
while `essentia.run` is in C++ would be a few lines and would decouple the threshold from decode
cost entirely.

### FU-5 — PERF: 48 kHz sources cost 5–8× the fine-tier decode of 44.1 kHz sources

§4d. `<set-04>`'s per-chunk fine decodes are 565 → 274 s against `<set-03>`'s 73 → 53 s on a file
only 2× longer, and the difference tracks the 48 000 → 44 100 Hz conversion the fine tier forces.
Worth measuring deliberately and, if it holds, either letting the fine tier follow the source rate
(the tier's algorithms take no rate-dependent parameter) or documenting the cost so lane
scheduling can price it. This is also the multiplier that puts FU-4 within 21% of the stall
threshold, so the two follow-ups are related and should be sequenced together.

______________________________________________________________________

## 8. Operational record

The measurement window ran **2026-08-11T23:13Z → 2026-08-12T21:56Z**. Everything changed to open
it was changed back.

| what | opening the window | closing it |
| --- | --- | --- |
| in-flight cloud work | four analyses were **allowed to finish** (drained 23:13Z → 23:53Z); none was evicted, requeued or lost | — |
| Kueue `vox-cluster-queue` | **held 2026-08-11T23:13:35Z** — `stopPolicy: None` → **`Hold`** (stops admitting; never `HoldAndDrain`, which would have evicted running work) | **released 2026-08-12T21:56:07Z** — back to **`None`**; the queue drained 82 pending → 0 within minutes and resumed analyzing |
| phaze backend registry | **removed 2026-08-11T23:54:20Z** — the `vox` `[[backends]]` block commented out in the deployed `backends.toml`, `phaze-api` + `phaze-worker` restarted, registry logged as `local` only with `cloud_enabled: false` | **restored 2026-08-12T21:55:39Z** — file put back **byte-identical** from a capture taken *before* removal (`diff -q` clean; the capture then deleted), both services restarted, registry logged back as `vox` + `local` with `cloud_enabled: true`. Verified in §8a |
| the measurement pod | a `sleep infinity` pod on `vox`, **no Kueue queue label** (consumed no quota), no memory limit | deleted |
| scratch | 3.3 GB under a vox-local `/scratch` path — read-only copies of seven files, the harness, the outputs | removed entirely |

### 8a. Restoration, verified

Restoration is a deliverable, so it is evidenced rather than asserted. The registry entry was
captured **before** removal and restored by copying that capture back, so the revert is byte-faithful
rather than reconstructed:

```
diff -q backends.toml backends.toml.<pre-removal-capture>   ->   (no output: identical)
```

The control plane's **own** lane snapshot (`services/backends.get_backend_lane_snapshot`, the same
degrade-safe probe the operator lane grid renders) after restoration, secret-free projection:

```
REGISTRY: [{"id":"vox","kind":"kueue","rank":10,"cap":4},{"id":"local","kind":"local","rank":99,"cap":1}]
LANE:     {"id":"vox","kind":"kueue","rank":10,"cap":4,"available":true,
           "in_flight":4,"working":4,"quota_wait":0,"inadmissible":0}
```

`available: true` is the live availability probe, not a config echo. And the lane is not merely
reachable — **work routed to it and is running**: `in_flight`/`working` climbed 2 → 4, i.e. the lane
is saturated at its `cap = 4`, with four healthy analyze pods on the node (ages 4m42s–20m at the
final check) and `cloud_job` showing `running: 4` for `backend_id = vox`. The Kueue queue drained
from 82 pending to 0 within minutes of the un-hold.

No completion landed inside the observation window, and that is expected rather than a gap: the
files now on the lane are multi-hour concert sets and §2 measures them at 0.56–0.79× their own
duration, so the first completions are hours out. Admission + running pods + a true availability
probe is the routing evidence; a completion count would only have been available by waiting out an
analysis.

- **No original file was opened for write**, and nothing was written back to the archive or to the
  staging bucket. Every measured file is a read-only copy.
- No k0s, JuiceFS or gateway configuration was touched. **No product code was changed** — the
  measurement calls the shipped functions and reads the shipped protocol lines.
- One thing worth recording for whoever reads the lane afterwards: when the queue resumed, 80 of
  the 82 held `cloud_job` rows reconciled straight to **`succeeded`** rather than re-running. That
  is `phaze-73sv`'s guard working — those files' analyses had already completed and only the row
  was lagging — and the handful of `Error` pods alongside them are the documented
  `EXIT_DOWNLOAD` 404 shape for a row whose success callback already deleted its staged object,
  not damage from the hold.

______________________________________________________________________

## 9. Appendix — reproducing this

The harness is three small measurement-only scripts, kept out of the repo deliberately (they
import nothing from `phaze` except, in the gate probe, the functions under test):

- **`run_one.py`** — spawns `python -m phaze.analysis_child <file> --models-dir /models` with
  `PYTHONPATH` pointed at the overlaid source, timestamps every protocol line against a monotonic
  clock into `<label>.trace.jsonl`, samples the child's `/proc/<pid>/status` `VmRSS`/`VmHWM` at 1 s
  into `<label>.rss.jsonl`, frames child stderr into `<label>.stderr.log`, and takes the
  authoritative peak from `os.wait4()`'s `ru_maxrss` (KiB on Linux) — one child per harness
  process, so that value is exactly that child's.
- **`gate_probe.py`** — calls the shipped `_iter_windows` / `_chunked` / `_chunk_stop_sec` /
  `_decode_windows_streaming` directly and times one chunk decode, gated or ungated, **one fresh
  process per measurement**.
- a driver that runs the matrix serially with the node otherwise idle.

Four things to get right when re-running:

1. **Take the peak from `wait4`, not from a sampler.** The two agreed to the kibibyte on every run
   here, which is the self-test — but the in-process reader is GIL-starved (`phaze-7i0k` §9) and a
   host-side sampler can miss a spike between ticks.
1. **One fresh process per gate measurement.** The first pass of §4b ran all five decodes in one
   process and produced a 985 s ungated figure that is an artifact of 1.6 GiB of inherited heap;
   re-run in fresh processes the same measurement is **152.722 s**. Every number in §4b is from a
   fresh process for this reason.
1. **`kubectl exec` streams are severed at 4 hours.** Anything longer must be detached inside the
   pod, or the driver will believe a still-running job has finished — which is exactly what
   happened to `<set-07>` (§1d).
1. **Verify the module the CHILD imported**, not the one on disk. Every summary here carries
   `module_path` + `module_sha256` read from `phaze.services.analysis.__file__` under the child's
   own environment.

______________________________________________________________________

<div align="center">
↩️ Back to the <a href="../README.md">docs index</a>
</div>
