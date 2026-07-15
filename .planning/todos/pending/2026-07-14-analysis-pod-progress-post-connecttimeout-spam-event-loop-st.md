---
created: 2026-07-14T22:05:26Z
title: Cloud analysis pod progress-POST ConnectTimeout spam (in-pod event-loop starvation, not the API)
area: observability
severity: minor
found_by: 2026.7.6 post-deploy cloud-burst drain — live investigation 2026-07-14
owner: next-milestone backlog
blocks: nothing (no data/correctness impact); log cleanliness + live progress bar only
resolves_phase: 99
files:
  - src/phaze/job_runner.py  # _make_progress_cb / _safe_post_progress
  - src/phaze/services/agent_client.py  # _request funnel (timeout=30.0, stop_after_attempt(3)); post_analysis_progress
  - src/phaze/routers/agent_analysis.py:278  # post_analysis_progress (counter-only upsert)
---

## Problem

During the 2026.7.6 cloud-burst drain, every analysis pod (both vox and xenolab Kueue
clusters) emits a continuous, bursty stream of warnings for the entire multi-minute
analysis of each file:

```
agent_api method=POST path=/api/internal/agent/analysis/<file_id>/progress error=ConnectTimeout
```

5+ near-identical lines within ~2ms, sustained the whole time a file is analyzing.

### Root cause (confirmed by live measurement — it is NOT the control-plane API)

- `phaze-api` is idle/healthy during the spam: CPU ~0.15%, and it **received only 1 progress
  POST in 90s** across 7 concurrent pods — the posts aren't arriving, not overwhelming it.
- Raw TCP from a pod: 12/12 connects to the API OK. httpx HTTPS from a **separate process in
  the same pod**: 6/6 fresh + 20/20 concurrent OK. So network path, TLS, keepalive, and
  concurrency are all fine.
- The only variable distinguishing the failing progress posts from everything that works:
  they run **in-process, concurrent with the CPU-bound essentia analysis**. `analyze_file`
  runs in an `asyncio.to_thread` worker but holds Python's **GIL** for long stretches
  (Python-level windowing/aggregation between C++ calls), starving the pod's asyncio event
  loop. The fire-and-forget progress connects (`_make_progress_cb` →
  `run_coroutine_threadsafe` → `_safe_post_progress` → `agent_client.post_analysis_progress`)
  are scheduled on that starved loop and can't complete within the 30s connect timeout →
  `httpx.ConnectTimeout` → tenacity retries 3× (`stop_after_attempt(3)`) → the warning in
  `agent_client._request`'s `except httpx.TransportError` branch fires per exhausted post.
- Corroborating timing: `presign` (pod startup, before the analysis thread exists) and the
  final result PUT (after analysis ends) both succeed for the SAME files — the loop is only
  starved DURING analysis.

### Impact

None on the drain or correctness. Results are 100% correct (verified live: real windows,
e.g. fine=84 / coarse=15, bpm 149.1) because the completion `put_analysis` writes the final
counts; progress is best-effort/swallowed (D-16, KJOB-04). Casualties: (1) the live progress
bar in the admin UI never advances mid-analysis (jumps 0→100% at completion — see the paired
todo on friendly logs / progress); (2) log spam that can mask real warnings.

## Solution

Small, safe PR (do NOT do mid-drain). Recommend #1 + #2 as the minimal fix:

1. Give progress posts a dedicated **short connect timeout (~2s) and ZERO retries** — they're
   best-effort; the 30s × 3-retry budget per dead ping is exactly what makes them pile up and
   spam. (Own httpx client variant, or a per-call timeout/retry override on the progress path.)
2. **Demote the transport-error log for the progress path to `debug`** (the caller already
   swallows the exception) — or route progress through a client variant that logs at debug.
3. Optional: lengthen `analysis_progress_interval_sec` so far fewer fire.
4. Larger/optional: run essentia analysis in a **subprocess** instead of a thread so the GIL
   can't starve the event loop — this also restores a working live progress bar (pairs with
   the friendly-logs/progress-indicator todo).

Add a regression guard that the progress path uses the short-timeout/no-retry client so the
30s×3 spam budget can't silently return.
