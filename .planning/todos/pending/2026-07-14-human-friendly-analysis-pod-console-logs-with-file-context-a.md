---
created: 2026-07-14T22:05:26Z
title: Human-friendly analysis pod console logs — file identity, source path, and progress
area: observability
severity: minor
found_by: 2026.7.6 post-deploy cloud-burst drain — operator request 2026-07-14
owner: next-milestone backlog
blocks: nothing (operator observability / UX of logs)
resolves_phase: 100
files:
  - src/phaze/job_runner.py  # step logging (presign/download/verify/analyze), progress_cb
  - src/phaze/routers/agent_analysis.py  # progress upsert (source of a real progress %)
---

## Problem

The analysis pod's console output is machine-first structured JSON keyed only by `file_id`,
with no human-friendly framing of *what* is being processed or *where it came from*, and no
readable progress. Watching a live drain, an operator sees only:

```
{"file_id": "f481311f-...", "step": "presign",  "elapsed_ms": 196,   "event": "job_runner_step_ok", ...}
{"file_id": "f481311f-...", "step": "download", "elapsed_ms": 84688, "event": "job_runner_step_ok", ...}
{"file_id": "f481311f-...", "step": "verify",   "elapsed_ms": 438,   "event": "job_runner_step_ok", ...}
[   INFO   ] MusicExtractorSVM: no classifier models were configured by default
```

Pain points:
- No human-readable file identity — only a UUID. The operator can't tell it's
  `Angy Dee - Goliath 10 The Anniversary (2002.05.04).mp3` without a DB lookup.
- No source **path** / origin (which fileserver, original path, cluster, backend_id, bucket).
- No progress indicator during analysis — after `verify`, output goes silent for minutes
  (essentia only prints its own INFO banners); the operator can't tell fine/coarse-window
  progress. (The live progress bar is also broken by the GIL/event-loop-starvation defect —
  see the paired todo on progress-POST ConnectTimeout spam.)

## Solution

Pretty-ify the pod's operator-facing log without losing structured logging:

1. **Startup banner** at job start: human-readable filename, `file_id`, source path / origin
   (original_filename, current_path, agent_id/fileserver), duration, size, target cluster +
   `backend_id` + staging bucket. One friendly line so the operator immediately knows what's
   being processed and where it came from.
2. **Step lines** phrased for humans (e.g. `⬇ downloaded 130 MB in 84.7s`,
   `✓ verified sha256`) while keeping the structured `event`/`step`/`elapsed_ms` fields for
   machine parsing (dual sink, or a friendly renderer over the same events).
3. **Progress indicator** during analysis — periodic `analyzed N/M fine windows (P%)` lines
   from the same counter the progress-POST carries (`fine_windows_analyzed`/`_total`), gated
   to a sane interval so it doesn't spam. Consider tying this to the same fix that restores
   the UI progress bar (subprocess analysis) so console + UI progress share one source.
4. Keep essentia's own stdout banners but frame them (or downgrade/route them) so they don't
   look like the app's logs.

Coordinate with the paired todo (`...connecttimeout-spam-event-loop-starvation`): fixing the
event-loop starvation is what makes a *live* progress indicator actually possible, so these
two may land together or in sequence.
