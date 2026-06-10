---
plan: 31-01
phase: 31-windowed-time-series-audio-analysis
status: complete
type: spike
self_check: PASSED
---

# 31-01 SUMMARY — Mandatory windowed-decode spike

## What was built

A throwaway spike (`scripts/spike_windowed_analysis.py`, **untracked / not committed**) that
decodes a file per-window with `EasyLoader`, runs `RhythmExtractor2013(multifeature)` +
`KeyExtractor(edma)` per 30 s fine window and the 34 TF model sets per 180 s coarse window,
and logs per-window decode time + RSS + coarse inference time. Task 1 (synthetic local run)
was skipped per operator choice; Task 2 (real-file homelab run) was executed and is the source
of the numbers below.

## Decode Decision Log

**LOCKED DECODE STRATEGY: `EasyLoader-primary`** (Plan 04 implements this; do NOT use the
decode+Resample hybrid).

**Run environment:** GHCR image `ghcr.io/simplicityguy/phaze:v4.0.9`, one-off `docker run`,
`PHAZE_ROLE` agent image (essentia-tensorflow 2.1b6.dev1438), `phaze_models` volume at `/models`,
archive at `/data/music` (ro).

**Real file:** `Cosmic Gate - 2007-10-18 - Amsterdam Dance Event.mp3` (VBR mp3), duration
**5350.3 s (1.49 h)**. Slightly under the 2 h target, but the seek-constancy and flat-RSS
trends across 178 fine windows / 30 coarse windows are conclusive and extrapolate linearly.

| Validation | Threshold | Measured | Verdict |
|------------|-----------|----------|---------|
| (a) per-window decode works | all windows decode, BPM returned | 178/178 windows, bpm_returned=178, **failures=0** | ✅ PASS |
| (b) `RhythmExtractor2013` on 30 s buffer | no `OnsetDetectionGlobal` overflow | **no overflow** | ✅ PASS |
| (A1) seek cost vs `window_index` | roughly constant (non-quadratic) | decode_seconds first/last/min/mean/max = **5.162 / 5.084 / 5.084 / 6.464 / 16.375**; last ≈ first, no upward trend with index (16.375 is a single outlier, not a slope) | ✅ PASS → **EasyLoader-primary** |
| (c) bounded memory (fine pass) | peak RSS flat, < ~1.5 GB, not scaling with length | fine-pass RSS first/last/peak = **254.2 / 270.7 / 270.7 MB**, flat across whole file | ✅ PASS |
| (d) coarse TF inference time | acceptable for 8× concurrency; record sec/hour | **1308.7 sec/hour-of-audio** (1944.9 s over 30 coarse windows; ~62–67 s per 180 s window for 34 models) | ⚠️ works; heavy (see findings) |

### Why EasyLoader-primary (not the hybrid)
Per-window `decode_seconds` is constant (~5–6 s) regardless of position — window 0 = 5.16 s,
window 170 = 5.32 s — proving timestamp seeking, **not** rescan-from-start. The hybrid
(single 44.1k decode + slice + `Resample`) is only the fallback for O(position) seek growth,
which did not occur; the hybrid would also reintroduce a ~945 MB whole-file resident buffer
(1.49 h × 44100 × 4 B), defeating the OOM fix. **Hybrid rejected.**

### Operational findings for Plans 04 / 05 (not decode-strategy issues)
1. **Audio memory is bounded (270 MB flat) — the OOM fix works.** The coarse pass RSS plateaus
   at **~8.2 GB** (5756 → 8184 MB, stable from coarse window ~9 onward). This is the **34 TF
   models resident** (lazy `_classifier_cache`), the *same* footprint the original whole-file
   `analyze_file` already had — windowing does **not** regress peak RSS; it only bounds the
   *audio* buffer. RSS does **not** grow with file length, which is the requirement. With
   `worker_process_pool_size=4`, expect ~4 × 8 GB ≈ 32 GB resident under full analysis load —
   flag for the redeploy host sizing (informational; design unchanged — 34 models per coarse
   window is the locked time-series design).
2. **Coarse inference dominates wall time** (~22 min/hour-of-audio; a 2 h set ≈ ~70 min wall).
   Plan 05 **must** ship unbounded/generous `process_file` timeout + low retries (retries=1) so
   a slow-but-valid long file is not killed and re-run 4× (the v4.0.6 timeout-restart-loop class).

## Verification
- `uv run scripts/spike_windowed_analysis.py <real_2h_set.mp3> --models-dir /models` ran to
  completion on the homelab agent image with no buffer overflow, flat fine-pass RSS, and a
  printed coarse inference figure.
- Decode strategy LOCKED: **EasyLoader-primary**.

## Deviations
- Real file was 1.49 h vs the ≥2 h target — accepted: trends are conclusive across 178 windows
  and extrapolate linearly (constant seek, flat RSS). No additional run needed.
- Task 1 synthetic local run skipped per operator choice (`Write script, skip local run`); the
  real-file Task 2 run supersedes it for all five validations.

## Self-Check: PASSED
Spike validated a/b/c/A1 on a real VBR file; EasyLoader-primary locked with real numbers;
spike script left untracked (throwaway).
