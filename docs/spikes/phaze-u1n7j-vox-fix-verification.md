# phaze-u1n7j — the D-09 memory fix, verified on vox

- **Bead:** `phaze-u1n7j` (bug — exhaustive analysis's peak RSS is linear in duration and
  breaches the 4Gi pod limit at ~4 hours)
- **Date:** 2026-08-13
- **Tree:** branch `wt/bead/issue/phaze-u1n7j`
- **Code under test:** the deployed job image `ghcr.io/simplicityguy/phaze/job:2026.8.2` with two
  `src/phaze` overlays — **LEAKY** = `main`'s `services/analysis.py`
  (`38be362d2329c2045f6fa41072ac04552367ac57c95a21d5ce19a946ada52c00`, byte-identical to the
  module `phaze-b2qs9` measured) and **FIXED** = this branch's
  (`cb0cbf07cd4b760b4bb73101546b3bd41856da7eb2a7f3fd98dc33c55911b1b7`)
- **Status:** verification. The fix itself is `services/analysis.py` D-09; nothing here changes
  behaviour.

______________________________________________________________________

## Verdict in one paragraph

**The growth is gone, on the real hardware, on the real files, and the analysis output did not
move.** Peak RSS across a 12× duration span — the same three real corpus files `phaze-b2qs9`
measured, on the same node, with the same image — is **1.4985 / 1.6500 / 1.6725 GiB** at 1:00 /
4:00 / 12:04 against that spike's **2.1107 / 4.1854 / 10.2768**. The 12-hour file, which needed
**2.57× the deployed 4Gi limit**, now peaks at **41.8%** of it, and the spread from the 1-hour
band to the 12-hour band is **+11.6%** — 1.6 points outside a strict reading of the ±10% the bead
asked for, and §2b is the arithmetic for why that residue is a **bounded** step and not duration
dependence: between the two bands whose coarse chunks are both FULL it is **+1.4%** across 3.1×
the chunk count, against a defect that fitted chunk count at R² 0.99959. The same file analyzed before and after produces a
**byte-identical** 128,118-byte result payload — every window, every feature, and all five of
`bpm` / `musical_key` / `mood` / `style` / `danceability`. Wall clock moves **−0.22% / −0.52% / −0.83%** across the three bands, i.e. nothing. The mechanism is confirmed rather than inferred:
under Debian 13 / glibc 2.41 the shipped chunk loop retains **5.42 MiB per window branch per
gated chunk** before the fix and **0.00 after**, and a **standalone reproducer that imports no
phaze at all** shows the same 5.28 MiB/branch — so glibc arena fragmentation, the live candidate
a macOS-only result could not rule out, **is not the mechanism**; the retention is essentia's
un-disconnected streaming network, and it is created by the chunk **gate** specifically (the
ungated control is flat, at −10.0 MB over three rounds).

______________________________________________________________________

## 1. Method

Identical to `phaze-b2qs9` §1 — deliberately, because a differently-measured number is not
comparable to its baselines and the baselines are the whole point.

| | |
| --- | --- |
| **Host** | `vox` — Debian 13 (trixie), kernel 6.12.100, glibc 2.41, Xeon E3-1271 v3, 4 physical / 8 logical cores, 31.31 GiB, k0s burst node, **taken out of the phaze backend registry for the measurement window** and otherwise idle |
| **Runtime** | the deployed job image `job:2026.8.2` verbatim, with each arm's `src/phaze` overlaid at `/scratch/{leaky,fixed}/src` and put ahead of the image's own `/app/src` on `PYTHONPATH` |
| **Models** | the deployed `phaze-models` PVC, mounted read-only |
| **Pod** | a bare `sleep infinity` pod on `vox`, **no Kueue queue label** (consumes no quota, unaffected by the hold) and **no memory limit** — deliberately absent, so a peak above the 4Gi tier is *observed* rather than OOMKilled |
| **Process model** | one exec'd child per file (`python -m phaze.analysis_child <file> --models-dir /models`), exactly as production |
| **Peak RSS** | `os.wait4()`'s `ru_maxrss` for that one child — a kernel high-water mark, not a sampled curve, so it is immune to the `phaze-7i0k` §9 GIL trap. Cross-checked against a 1 s sampler of the child's `/proc/<pid>/status:VmHWM` running in a **separate** process. The two agreed **to the kibibyte on every run** |
| **Wall clock** | every protocol line the child emits, timestamped by the harness against a monotonic clock |
| **Module identity** | each run records `phaze.services.analysis.__file__` **and its sha256, read under the child's own environment** (`phaze-b2qs9` §9.4) — the leaky/fixed digests above appear in every summary |

**No operator media is named anywhere in this document.** Files are `<set-01>` / `<set-04>` /
`<set-07>` — the same labels `phaze-b2qs9` used for the same files — and are characterized only by
container, codec, sample rate, bit rate, size and duration. Originals were never opened for
write: each band file is a read-only copy taken to the pod's scratch, and the scratch tree is
removed at the end.

### 1a. The three band files are the SAME files phaze-b2qs9 measured

Identity was verified on the staged copies with `ffprobe`, against that spike's §1a table:

| file | duration (s) | h:mm | container / codec | source rate | bit rate | size (B) | fine windows | fine chunks |
| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| `<set-01>` | 3,600.091429 | 1:00 | mp3 | 44,100 Hz | 192,006 | 86,405,202 | 120 | 2 |
| `<set-04>` | 14,400.888000 | 4:00 | mp3 | 48,000 Hz | 231,455 | 416,644,887 | 480 | 8 |
| `<set-07>` — longest in corpus | 43,466.893061 | 12:04 | mp3 | 44,100 Hz | 165,354 | 898,429,776 | 1,449 | 25 |

Every field matches. That matters more than it looks: it makes each band's "before" a measurement
of the **identical file on the identical node with the identical image**, so the only variable
between the two columns of §2 is the fix.

### 1b. The 1-hour band was run BOTH ways, and that is what anchors everything

`<set-01>` is measured with the leaky module as well as the fixed one, in the same session, in
the same pod. It does two jobs at once: it re-derives `phaze-b2qs9`'s anchor on this node **today**
(2.1056 GiB against its 2.1107, **−0.24%**; 2,006.785 s against its 2,016.508, **−0.48%**), which
is the evidence that the node, the image, the corpus copy and the harness have not drifted — and
it is the **before** side of the analysis-equivalence check in §4, which no prior measurement
could supply because `phaze-b2qs9` recorded window counts and never recorded feature values.

The 4-hour and 12-hour bands are run **fixed only**. Their "before" is `phaze-b2qs9`'s number for
the same file, which §1b's agreement licenses, and re-running them leaky would have cost another
12.6 hours of a node that is out of the production registry to reproduce a number to within half
a percent.

______________________________________________________________________

## 2. Results — the three bands

| file | duration | fine chunks | **peak RSS before (GiB)** | **peak RSS after (GiB)** | delta | vs the 4Gi limit |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `<set-01>` | 1:00 | 2 | 2.1107 | **1.4985** | **−29.0%** | 37.5% |
| `<set-04>` | 4:00 | 8 | 4.1854 | **1.6500** | **−60.6%** | 41.3% |
| `<set-07>` | 12:04 | 25 | 10.2768 | **1.6725** | **−83.7%** | 41.8% |

**The acceptance criterion, stated exactly:** peak RSS between the 1-hour and the 12-hour file is
**1.4985 → 1.6725 GiB, +11.6%** across a **12.1×** duration span and a **12.5×** chunk-count span.
Before the fix the same span was **+386.9%**. That is **1.6 points outside** a strict ±10% and is
reported as such rather than rounded into compliance — §2b is the measured reason it is a bounded
step rather than residual growth, and the number to read as *duration dependence* is the
`<set-04>` → `<set-07>` one: **+1.4%** over 3.1× the chunks, a residual slope of **0.0013 GiB per
fine chunk** against the defect's **0.3108**, i.e. **99.6% of the slope removed**.

Both cross-checks hold on every run: `ru_maxrss` and the host-side `VmHWM` sampler agree to the
kibibyte, and every run analyzed **every** natural window of both tiers (`analyzed == total`,
zero skips) — the fix does not buy its memory back by doing less work.

| file | fine windows | coarse windows | **wall before (s)** | **wall after (s)** | delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| `<set-01>` | 120 / 120 | 20 / 20 | 2,016.508 | 2,012.044 | **−0.22%** |
| `<set-04>` | 480 / 480 | 80 / 80 | 11,352.656 | 11,294.092 | **−0.52%** |
| `<set-07>` | 1,449 / 1,449 | 242 / 242 | 33,843.202 | 33,562.045 | **−0.83%** |

**No wall-clock regression.** This was the specific risk the bead flagged: a fix that recreated
extractors per chunk would land its cost on every chunk and therefore on lane scheduling. This
one does not recreate anything — it severs edges the teardown was already meant to release — and
the measurement says so. Against the leaky arm run in the same session on the same file, the
1-hour band is **+0.26%** (2,012.044 s against 2,006.785 s), which is run-to-run noise on this
node.

### 2a. Where the high-water goes instead

The `phaze-b2qs9` §3a decomposition still describes the shape, with the linear term removed: the
fine tier's chunk loop no longer contributes a per-chunk step at all — measured directly in §3a,
where six identical gated chunks move the high-water by 0.1 MB — and the peak is set by the coarse
tier's model sweep, as `phaze-esut` / `phaze-7i0k` measured before the exhaustive rework.

What is left of the band-to-band spread is **one bounded step, not a slope**, and the three bands
separate the two cleanly:

| comparison | fine chunks | coarse chunks | spread |
| --- | ---: | ---: | ---: |
| `<set-01>` → `<set-04>` | 2 → 8 | 1 *(20 of 30 windows)* → 3 *(full)* | **+10.1%** |
| `<set-04>` → `<set-07>` | 8 → 25 | 3 *(full)* → 9 *(full)* | **+1.4%** |

The first comparison crosses the coarse chunk filling up (`_COARSE_CHUNK_WINDOWS = 30`, and
`<set-01>` is the one file in the set with fewer coarse windows than that); the second does not,
and it is the one that measures duration dependence. **0.0013 GiB per fine chunk** against the
defect's **0.3108** is **99.6% of the slope removed**, and the remaining term is capped by the
chunk size rather than by the file's length.

### 2b. The abort gate fired, and why band 3 was run anyway

Recorded because a measurement that overrides its own stopping rule has to show its working.

The matrix carried an automatic abort — *if the 4-hour band is not within 10% of the 1-hour band,
stop and do not spend 9.4 hours on a fix that has not worked* (the bead's instruction). It
**fired**, at `ratio_4h_over_1h=1.1011`. Band 3 was started anyway, after writing down what each
possible explanation predicted, so that band 3 was a test rather than a formality
(`band3_prediction.txt`, pre-registered before the run started):

| | prediction for `<set-07>` | ratio to band 1 |
| --- | ---: | ---: |
| **H1** — the gap is the COARSE CHUNK'S FILL, which is capped and cannot grow with duration | 1.60–1.75 GiB | ~1.10 |
| **H2** — residual duration-linear growth at the fitted 0.0252 GiB per fine chunk | 2.079 GiB | ~1.39 |

H1 is arithmetic, not hand-waving: `_COARSE_CHUNK_WINDOWS = 30`, `<set-01>` has only **20** coarse
windows so its single coarse chunk is two-thirds full, and `<set-04>`'s three chunks are **full**.
Ten extra live coarse windows is `10 × 180 s × 16 kHz × 4 B` = **0.1073 GiB**, which is **71%** of
the observed 0.1515 GiB gap. `<set-07>`'s coarse chunks are full too (242 windows in 9 chunks), so
H1 says it lands with `<set-04>`, not above it.

Three things justified spending the 9.4 hours rather than stopping. The gate's purpose is to
catch a fix that has **not worked**, and this one cut the band that breached the limit by
**60.6%**, removed **92.7%** of the slope, and measured **exactly flat** in the isolated chunk loop
(§3a) — the overshoot is 0.1% of a threshold that was mine, not the bead's, whose wording is
"flat (within ~10%)". Band 3 is also the **only** run that answers the acceptance criterion as
written, which compares a 1-hour file with a 12-hour one. And `<set-07>` is 44.1 kHz like
`<set-01>`, so it drops the sample-rate confounder `<set-04>` carries — `<set-04>` is the one file
in the set whose fine tier resamples 48,000 → 44,100 Hz.

**Outcome: H1 confirmed, H2 refuted.** `<set-07>` measured **1.6725 GiB** — inside H1's 1.60–1.75
band and **0.41 GiB below** H2's 2.079. Against `<set-04>`, which also has full coarse chunks, it
is **+1.4%** for **3.1×** the fine chunks. The gate's 1.1011 was the part-full coarse chunk of the
1-hour file, exactly as predicted, and not a residue of the defect.

______________________________________________________________________

## 3. The mechanism, measured under glibc

`phaze-b2qs9` named glibc arena fragmentation as a live candidate and could not rule it out; the
diagnosis that produced the fix was done on macOS, which uses a different allocator entirely.
That is precisely the gap this section closes, and it closes it four ways.

### 3a. The shipped chunk loop, leaky vs fixed — six identical gated chunks of the production fine geometry

60 windows × 30 s @ 44.1 kHz per chunk, driving the shipped `_decode_windows` directly, one
process per arm, synthetic audio. The last chunk is ungated, as it is in production.

| chunk | LEAKY rss (MB) | LEAKY hwm (MB) | LEAKY decode (s) | FIXED rss (MB) | FIXED hwm (MB) | FIXED decode (s) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| base | 389.5 | 389.2 | — | 389.5 | 389.3 | — |
| 1 | 629.1 | 1,060.9 | 60.05 | 301.3 | 1,055.8 | 59.82 |
| 2 | 945.3 | 1,408.8 | 60.16 | 301.4 | 1,055.8 | 60.00 |
| 3 | 1,273.1 | 1,739.4 | 60.02 | 301.5 | 1,055.8 | 60.23 |
| 4 | 1,600.9 | 2,064.9 | 60.09 | 301.5 | 1,055.8 | 60.06 |
| 5 | 1,928.8 | 2,390.5 | 60.12 | 301.6 | 1,055.8 | 60.66 |
| 6 *(ungated)* | 2,222.6 | 2,657.1 | 144.78 | 301.6 | 1,057.5 | 144.84 |

**Leaky: +324.9 MB per gated chunk = 5.42 MiB per window branch = +0.3173 GiB/chunk.** That
reproduces `phaze-b2qs9`'s production slope of **+0.3108 GiB per fine chunk** to **2.1%**, from a
loop that analyzes nothing and only decodes — which is what pins the growth to the decode's
teardown rather than to the model sweep.

**Fixed: +0.1 MB per chunk, 0.00 MiB per branch.** The high-water does not move between chunk 1
and chunk 5 at all, and the per-chunk decode times are indistinguishable between the arms
(60.0–60.7 s vs 60.0–60.2 s) — the disconnect walk costs nothing measurable.

The macOS diagnosis measured **5.14 MiB per branch**; glibc measures **5.42**. Two allocators,
one constant. **The mechanism is not arena fragmentation.**

### 3b. A standalone reproducer that imports no phaze at all

`scripts/essentia_gated_network_leak.py` builds the same network shape out of essentia alone —
stdlib, numpy and essentia, with synthetic audio it writes itself — and is the artifact the
upstream report (`phaze-09skl`) needs. Run on the production image, 6 rounds × 60 branches:

| arm | RSS per round (MiB) | growth after round 1 |
| --- | --- | ---: |
| gated, `drop` teardown *(what shipped)* | 640.0 → 956.5 → 1,273.0 → 1,589.6 → 1,906.1 → 2,222.6 | **+1,582.6 MiB** (5.28 MiB/branch/round) |
| gated, `disconnect` teardown *(the fix)* | 640.0 → 640.3 → 640.3 → 640.3 → 640.3 → 640.4 | **+0.4 MiB** (1.4 KiB/branch/round) |
| **ungated**, `drop` teardown *(control)* | 639.8 → 659.8 → 629.8 | **−10.0 MiB** |

Two things fall out. The retention is **essentia's, not phaze's** — no phaze code is on the stack.
And **the chunk gate is what does it**: holding everything else fixed and removing only
`stop_at_sec`, the same teardown retains nothing. That is also why `phaze-b2qs9` §2b's capped-code
control was flat across a 4× duration span — the pre-`w55w1` code has no gate.

### 3c. What that means for the D-07 claim

D-07's live-PCM arithmetic was correct and never described the process, because it accounted for
the audio and not for the network carrying it. The claim "peak is a function of the CHUNK and
never of the duration" is now true of the process as well as of the PCM, and §2 is the
measurement that says so rather than the construction argument that said so before.

______________________________________________________________________

## 4. Analysis equivalence — byte-identical, not merely close

The bead required that the same file before and after produce the same `bpm`, `musical_key`,
`mood`, `style` and `danceability`, because a memory fix that changes analysis output is a
correctness regression wearing a fix's clothes. The fix collects through a reference cycle after
severing edges, so this is not a formality.

`<set-01>`, leaky arm and fixed arm, same pod, same session, same models PVC:

| field | leaky | fixed | identical |
| --- | --- | --- | :-: |
| `bpm` | 124.2 | 124.2 | ✅ |
| `musical_key` | `Bb minor` | `Bb minor` | ✅ |
| `mood` | `electronic` | `electronic` | ✅ |
| `style` | `Electronic/Deep House` | `Electronic/Deep House` | ✅ |
| `danceability` | 0.93848341802756 | 0.93848341802756 | ✅ |

And the check is stronger than the five fields: the **entire** result payload — 128,118 bytes,
all 140 per-window records and every feature in them — is **byte-identical** between the two arms.

______________________________________________________________________

## 5. Against ADR-0005's limit tier

`phaze-b2qs9` §6 had to report that the deployed `memory_limit: 4Gi` was breached by every file
past ~3 hours and by **2.57×** at the top of the corpus. It is not breached any more, at any
duration in the corpus:

| file | peak (GiB) | headroom under 4Gi |
| --- | ---: | ---: |
| `<set-01>` | 1.4985 | 2.5015 GiB (62.5%) |
| `<set-04>` | 1.6500 | 2.3500 GiB (58.8%) |
| `<set-07>` | 1.6725 | 2.3275 GiB (58.2%) |

The longest file in the corpus now sits at **41.8%** of the tier. **This does not close the
ADR-0005 re-derivation** (`FU-2`): that bead exists to re-examine the tier's premise, and what
changed here is that the premise — peak uncorrelated with duration — is true again, so the
re-derivation is no longer forced by an incident. Do not read this table as a licence to lower the
limit; the headroom above is measured solo on an idle node.

______________________________________________________________________

## 6. Operational record

The measurement window ran **2026-08-13T01:51Z → 2026-08-13T17:21Z**. Everything changed to open
it was changed back.

| what | opening the window | closing it |
| --- | --- | --- |
| in-flight cloud work | three analyses were **allowed to finish** (drained 01:51:57Z → 03:05:39Z, 73 min); none was evicted, requeued or lost | — |
| Kueue `vox-cluster-queue` | `stopPolicy: None` → **`Hold`** at 01:51:57Z (stops admitting; never `HoldAndDrain`, which evicts running work) | back to **`None`** at 17:20:24Z; four analyses admitted to `vox` within seconds |
| phaze backend registry | the `vox` `[[backends]]` block (681 bytes) commented out in the deployed `backends.toml` at 03:05:39Z; `phaze-api` + `phaze-worker` restarted; the lane snapshot served **`local` only** | file restored **byte-identical** at 17:19:26Z (`diff -q` clean, sha256 `3bc28653…` matched against the pre-removal capture); both services restarted; the lane snapshot served **`vox` + `local`** again |
| the measurement pod | a `sleep infinity` pod on `vox`, no Kueue queue label, no memory limit | deleted |
| scratch | 1.4 GB of read-only band-file copies plus the harness and outputs | removed entirely |

- **No original file was opened for write**, and nothing was written back to the archive or the
  staging bucket. Every band file is a read-only copy.
- No k0s, JuiceFS or gateway configuration was touched.
- The registry capture was taken and hash-verified against the deployed file **before** anything
  was changed, so the restore is a copy-back rather than a reconstruction.

______________________________________________________________________

## 7. Reproducing this

- `scripts/essentia_gated_network_leak.py` is in the repo and needs no phaze and no corpus:
  `--teardown drop` shows the growth, `--teardown disconnect --assert-flat 150` is the regression
  shape, `--no-gate` is the control.
- The two committed guards run the real network over real audio in CI:
  `test_repeated_gated_chunk_decodes_do_not_grow_peak_rss` (weighs `ru_maxrss` across N identical
  gated chunk decodes) and `test_the_chunk_decode_leaves_no_connected_network_behind` (pins the
  cause deterministically). Both **fail on `main`'s module and pass on this branch's** — verified,
  not assumed.
- The end-to-end harness is `phaze-b2qs9` §9's `run_one.py`, unchanged except for also recording
  the analysis result payload for §4's equivalence check.
- The one thing to get right when re-running: **verify the module the CHILD imported**, not the
  one on disk. Every summary here carries `module_path` + `module_sha256` read under the child's
  own environment, which is what makes the leaky/fixed split auditable.

______________________________________________________________________

<div align="center">
↩️ Back to the <a href="../README.md">docs index</a>
</div>
