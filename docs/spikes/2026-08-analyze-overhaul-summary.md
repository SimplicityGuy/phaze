# The 2026-08 analyze-pipeline overhaul — what was measured, and what it overturned

- **Trigger:** the k8s-burst node hard-crashed 2026-08-04 after 55 OOM kills in twelve days
- **Shipped as:** `2026.8.1`
- **Scope:** eight spikes, nine implementations, three migrations
- **Status:** complete. One question deliberately left open (§7).

This is the index and the narrative. Every number here is measured, and every claim links to
the spike that measured it — go there for method, raw data, and caveats. Nothing in this
document is a new finding; if it disagrees with a spike, the spike is right.

______________________________________________________________________

## 1. The headline

| | before | after | spike |
| --- | ---: | ---: | --- |
| analyze design peak | 7.986 GiB | **1.7383 GiB** | [`phaze-5lop`](phaze-rc1q-streaming-vs-standard-mode.md) (measured jointly) |
| 720-minute file, decode | 6.14 h | **20.6 min** (17.9×) | [`phaze-rc1q`](phaze-rc1q-streaming-vs-standard-mode.md) |
| end-to-end, 60-min file at saturated caps | — | **−38.8%** | `phaze-5lop` |
| safe concurrency (`cap`) | 3 | **4**, confirmed by re-measurement | [`phaze-8r6t4`](phaze-8r6t4-concurrency-knee-recheck.md) |

**1.7383 GiB is a measurement, not a sum.** An earlier arithmetic estimate of 2.812 GiB was
superseded once the two changes were run together — see §6.

______________________________________________________________________

## 2. What actually shipped

| change | effect | output |
| --- | --- | --- |
| **`phaze-15sw`** — model-major iteration: one TF graph resident, not 34 | −68.9% peak (7.986 → 2.482 GiB) | byte-identical |
| **`phaze-0582`** — `batchSize` 64 → 32, a default phaze never overrode | −33.7% peak, +0.40% wall | 1 ulp, 0/34 argmax flips |
| **`phaze-5lop`** — hybrid streaming decode: 90 decode passes collapsed to 2 | 17.9× on long files, −38.8% end-to-end | byte-identical |
| **`phaze-ap8y`** — hoist the fine-tier extractors out of the window loop | −24% *construction cost*; −0.5% end-to-end | byte-identical |
| **`phaze-k6d5`** — emit `resources.limits.memory` ([ADR-0005](../design/0005-analyze-job-memory-limits.md)) | no peak change; converts node crash → pod OOMKill | unchanged |
| **`phaze-rvcn`** — derive thread counts from **physical** cores | decouples peak from host core count | unchanged |
| **`phaze-1q4g`** — bound Job re-drive after node loss (migration `054`) | one file had crashed the node 8× in 5 days | — |
| **`phaze-2mwyo`** — durable per-file cloud budget (migration `055`) | stops unlimited fresh attempt chains | — |
| **`phaze-x8tof`** — rename five double-prefixed check constraints (migration `056`) | one had already truncated past PostgreSQL's 63-char limit | — |

`ap8y` is listed honestly: **−24% is construction cost in isolation.** End-to-end it is
−0.5%, because the fine tier is decode-dominated. It is free and correct, not a headline.

______________________________________________________________________

## 3. Where the memory actually was

Two numbers explain nearly all of it.

**96.9% of peak lands inside one call** — the first `_run_model_sets`
([`phaze-esut`](phaze-esut-analysis-memory-profile.md) §2). Audio decode across both tiers
costs 0.053 GiB, or 0.5%. Aggregation, payload construction and serialisation contribute
nothing measurable. **The audio was never the problem.**

Inside that call: **34 TensorFlow graphs held co-resident** while used strictly one at a
time — `+4.09 GiB` with zero inference performed — and **`batchSize`, defaulting to 64 and
never set by phaze**, worth another third on top.

Neither was a bug. The graph cache was a deliberate, documented time optimisation, priced
against wall-clock and never against a memory bound. The batch size was simply never
considered. Both are repricings.

______________________________________________________________________

## 4. The falsification ledger

This is the part worth reading. Nearly every headline finding **refuted a prior hypothesis**,
usually via a pre-registered discriminating test rather than an argument.

| believed | measured |
| --- | --- |
| Peak is two co-resident copies of the decoded signal (12.1 h file ≈ 15.3 GiB × 2 ≈ 30 GB observed) | **Refuted.** Pre-registered test predicted **39.2 GiB**, measured **9.03 GiB** — 77% error. Coefficient on one decoded copy is **0.00 ± 0.05**; a 3.3-minute file peaks *higher* than a 12-hour one. `phaze-esut` |
| Linux glibc arena non-trimming ratchets the macOS floor up to production's 16.7–30.2 GB | **Refuted.** Linux is **25–39% cheaper**. A 1042-sample host-side trace shows RSS below its own running max in **96%** of samples, shedding up to 1.485 GiB at a time — a sawtooth, not a ratchet. [`phaze-7i0k`](phaze-7i0k-linux-memory-measurement.md) |
| The node is memory-bound; at 2.482 GiB/job a 31 GB box fits 8–10 extractors (~3× throughput) | **Refuted.** CPU-bound by ~6×. Knee at **W=2**, ceiling ~30 files/h. Per-process peak flat at 2.074–2.151 GiB across W=1…12 — memory never binds. Also: the extractor is **not single-threaded**, and the node is **4 physical cores**, not 10. [`phaze-3j67`](phaze-3j67-concurrent-extractor-capacity.md) |
| Streaming mode is a documented performance trade, and switching breaks `TensorflowPredict` normalization across all 34 models | **Refuted, both halves.** There is no separate standard implementation — `standard::EasyLoader` wraps `streaming::EasyLoader`, and phaze already runs **80–90 streaming networks per file**. The normalization asymmetry is `TensorflowPredictFSDSINet`-**only**, verified in C++ source. [`phaze-rc1q`](phaze-rc1q-streaming-vs-standard-mode.md) |
| Production OOMs cluster at 2×/3×/4× the working set with a hard floor at 15.27 GiB — a multiplicative signature implying co-resident copies | **Refuted.** The kernel also dumps a full *Tasks state* table, which nobody had read. All 22 dumps sum to **30.76–30.82 GiB** — the node's capacity. The victim's RSS is `30.80 − Σ(neighbours)`. **One parameter reproduces all nineteen values.** The "floor" is `30.80 − 2 × 7.81`; below it is **left-censored**, not quantised. [`phaze-wcrb`](phaze-wcrb-oom-multiplier-forensics.md) |
| Rewriting the analysis layer in modern C++ would escape the GIL and buy parallelism | **No-go.** Python orchestration is **0.011–0.053% of wall** — that is the entire ceiling. GIL cost is **zero** in phaze's real architecture, which already execs one child process per file for exactly this reason. Best narrow-extension candidate: 0.98 ms/file. [`phaze-i93a`](phaze-i93a-cpp-rewrite-evaluation.md) |
| The concurrency knee moved once decode stopped being the serializer | **Refuted.** Re-measured on the final code: **still W=2.** The changes moved *per-extractor cost* (solo CPU 74.7% → 43.3%, cores 5.98 → 3.47) but not the curve — the freed cores are SMT siblings the second process takes straight back. `phaze-8r6t4` |
| The TF **intra-op** cap is what saves memory (−41.7%) | **Refuted by attribution.** Isolated: intra-op 4 alone **+0.2%**, OMP 4 alone **+0.5%**, **inter-op 1 alone −15.3%**. Two earlier spikes set all three together against batch 64 and mis-attributed it; `phaze-0582` had already removed the arena intra-op appeared to multiply. `phaze-rvcn` |

______________________________________________________________________

## 5. Options explored and closed

- **C++ rewrite** — no-go, measured (above). Upstream's own reference `MusicExtractor` is 650
  lines of C++ that **decodes each file three times** — less efficient than the Python hybrid.
- **ONNX Runtime / TFLite as a memory play** — *reachable* (`TensorflowPredict*` is a 7-node
  composite of which only one node is TensorFlow; ~40 lines of glue, proven at 0/34 argmax
  disagreements, max |Δ| 8.94 × 10⁻⁷) but **rejected**: ONNX's isolated win does not survive
  the real pipeline — 1.800 GiB at **+99% wall** vs TF-at-batch-32's 1.501 GiB. Upstream's
  `OnnxPredict` PR is open, unmerged and dirty; **zero** of phaze's 34 models publish `.onnx`.
  [`phaze-mqq5`](phaze-mqq5-alternative-model-runtimes.md)
- **Quantisation** — rejected. fp16 fails the numeric tolerance at 2.03 × 10⁻²; int8 fails
  both halves — 6.51 × 10⁻¹ and a **genre label flip**.
- **Wholesale streaming fan-out** — rejected. Fanning one decode to all 34 models requires all
  34 graphs co-resident, precisely the +4.007 GiB `phaze-15sw` removed. **Streaming fan-out and
  model-major iteration are structurally opposed.** Hence the hybrid.
- **Raising concurrency for throughput** — closed. The node is CPU-bound at W=2; W=4→12 buys
  **+2.5%** while per-file wall grows +115.9 s per added worker.

One live option remains open and unexercised: `phaze-mqq5` found `onnxruntime` and
`ai-edge-litert` ship **cp314 aarch64 wheels while TensorFlow's ceiling is cp313**. Moving
inference off TF would unpin `Dockerfile.agent-arm64` from Python 3.13 — the one file in the
repo not on 3.14. That is a *maintenance* argument, not a memory one.

______________________________________________________________________

## 6. Method — why these numbers are trustworthy

Four traps were found mid-flight, each of which had produced or nearly produced a wrong answer.

- **In-process RSS sampling lies.** essentia holds the GIL through inference (measured: 99.86%
  of wall), so a sampler thread only runs *between* models and reports a clean monotone curve —
  a **false confirmation** of the ratchet hypothesis, max drawdown 0.081 GiB. Host-side sampling
  showed the real sawtooth. Every subsequent spike sampled from the host.
- **`tracemalloc` would have undercounted everything.** The footprint is native C/C++
  allocation. Every figure derives from `ru_maxrss` or `VmHWM` — kernel high-water marks — and
  the platform unit difference (bytes on Darwin, kB on Linux) was verified by experiment.
- **Contaminated runs were caught and re-measured.** A CPU-mean admissibility gate rejected
  eight runs in one spike, **two of which would have inverted a conclusion**.
- **Overlays must be proven.** `phaze-8r6t4` ran release code over the deployed image and
  verified the module hash *inside the container* and in all 222 children, which abort rather
  than measure on a mismatch. `analysis_sizing.py` does not exist in `2026.8.0`, so a silent
  fallback was a live risk.

**Output identity was proven, not asserted** — sha256 plus `cmp`, including 285 individual
float32-buffer comparisons on the streaming work and mutation-checking (a 0.5 ms `Trimmer`
shift fails four tests). Where identity was *not* achievable — `phaze-0582` moves
`danceability` by 3.0 × 10⁻⁹ — it is reported against a stated tolerance rather than claimed.

**Arithmetic was not accepted where measurement was possible.** `phaze-mqq5` refused to ship a
summed joint peak; `phaze-i93a` measured it at 2.812 GiB; `phaze-5lop` then measured the
shipped code at **1.7383 GiB**. Each step corrected the last.

All measurement used **synthesised ffmpeg sine audio only**. No file from the music library was
read, copied, or identified.

______________________________________________________________________

## 7. Still open

**Why ~4 files in ~520 grow one process past 8 GiB**, monotonically, to ≥30.41 GiB — tracked
as bead `phaze-6ck1`; the evidence is in
[`phaze-wcrb`](phaze-wcrb-oom-multiplier-forensics.md) §6d.

Far narrower than the original framing. `phaze-wcrb` refuted duplication directly: all 22 task
dumps show each pod as `pause` + `uv` + `job_runner` + **exactly one** `analysis_child`, and
`pgtables_bytes / rss` at 8.08–8.16 B/page confirms a single address space with no COW twin.
Retries cannot overlap — each attempt is a new pod in its own cgroup. Duration is ruled out as
the selector: only 4 files exceed 8 h and **one of them completed successfully**.

The growth *shape* is undetermined, because the journal records only kill-time size.

**Deferred deliberately.** With ADR-0005's limit now emitted, the next occurrence is a
pod-scoped OOMKill that leaves the pod, its `cloud_job` row and `/var/log/pods` intact — the
per-file identification five spikes could not recover, because a node crash destroys its own
evidence. Waiting is cheaper than reproducing blind.

______________________________________________________________________

## 8. Index

| spike | question |
| --- | --- |
| [`phaze-esut`](phaze-esut-analysis-memory-profile.md) | where does the memory go? |
| [`phaze-7i0k`](phaze-7i0k-linux-memory-measurement.md) | does it behave the same on Linux? |
| [`phaze-3j67`](phaze-3j67-concurrent-extractor-capacity.md) | how many extractors fit? |
| [`phaze-rc1q`](phaze-rc1q-streaming-vs-standard-mode.md) | streaming or standard mode? |
| [`phaze-mqq5`](phaze-mqq5-alternative-model-runtimes.md) | a different model runtime? |
| [`phaze-i93a`](phaze-i93a-cpp-rewrite-evaluation.md) | would C++ pay for itself? |
| [`phaze-wcrb`](phaze-wcrb-oom-multiplier-forensics.md) | what were the production OOMs? |
| [`phaze-8r6t4`](phaze-8r6t4-concurrency-knee-recheck.md) | did the knee move after all this? |

Sizing guidance for operators — which knob each layer owns, with provenance and superseded
values — lives in [`docs/k8s-burst.md`](../k8s-burst.md), not here.
