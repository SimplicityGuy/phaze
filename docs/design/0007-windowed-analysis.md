# ADR-0007 — Why analysis is windowed/sampled instead of exhaustive, and whether that should change

| | |
| --- | --- |
| **Status** | **Decided 2026-08-11 — operator elected to REMOVE the caps (recommendation B declined)** |
| **Date** | 2026-08-11 |
| **Investigation** | Bead `phaze-dx9al` (operator question, 2026-08-10) |
| **Decider** | Repository owner |
| **Implementation** | Bead `phaze-w55w1` — chunked rework, wall-clock→heartbeat/stall liveness, sampling-machinery removal (§7) |

______________________________________________________________________

## The question

> Analysis runs windowed (fine/coarse tiers, `fine_windows_total` progress) over a sampled
> subset of each file rather than exhaustively covering it up front. Why, and is an
> exhaustive/full-coverage mode worth offering?

This note documents the actual rationale, grounded in the shipped code and the spike history
that produced it, states what the current window budget is and what raising it or removing it
would cost, and evaluates whether the existing per-file **`deepen`** re-analysis path already
answers the need. §§1–4 are the investigation record (what was known, and what was analyzed) as
of decision time; §5 is the recommendation that was offered; §7 (added after review) is the
operator's actual decision, which **declined** the recommendation. Read §7 for what the codebase
is moving toward — §§1–6 describe the state at investigation time and are kept as the record of
that evidence, not as a description of where the design is headed.

______________________________________________________________________

## 1. What the window budget actually is

`analyze_file()` (`src/phaze/services/analysis.py`) runs two passes per file:

| Pass | Sample rate | Window length | Cap (max windows) | Algorithms |
| --- | --- | --- | --- | --- |
| **FINE** | 44.1 kHz | `analysis_fine_window_sec` = 30 s | `analysis_fine_cap` = 60 | `RhythmExtractor2013(method="multifeature")` + `KeyExtractor` → `bpm`, `musical_key` |
| **COARSE** | 16 kHz | `analysis_coarse_window_sec` = 180 s | `analysis_coarse_cap` = 30 | 34 TensorFlow graphs → `mood`, `style`, `danceability`, `features` |

All five knobs are `AgentSettings` fields (`config.py`), overridable via
`PHAZE_ANALYSIS_FINE_WINDOW_SEC` / `PHAZE_ANALYSIS_COARSE_WINDOW_SEC` /
`PHAZE_ANALYSIS_FINE_MIN_SEC` / `PHAZE_ANALYSIS_FINE_CAP` / `PHAZE_ANALYSIS_COARSE_CAP`, and can
also be overridden **per job** via `ProcessFilePayload.fine_cap` / `.coarse_cap` (both
`int | None`, unvalidated at the payload level — this is exactly the seam `deepen` uses; see §4).

A file whose **natural** window count (duration ÷ window length) exceeds the cap is not
truncated to the first N windows — it is strided **evenly across the whole file** by
`_stride_to_cap()` (endpoint-inclusive: window 0 and the last window are always kept). The
result carries a five-field coverage contract per file:
`fine_windows_analyzed` / `fine_windows_total` / `coarse_windows_analyzed` /
`coarse_windows_total` / `sampled` — `sampled` is `True` when either pass was strided.

**The caps only bite on long files.** Doing the arithmetic on the defaults:

- FINE strides once duration exceeds `60 × 30 s = 1 800 s` = **30 minutes**.
- COARSE strides once duration exceeds `30 × 180 s = 5 400 s` = **90 minutes**.

Given the archive's two file classes (CLAUDE.md "Project"): individual tracks are essentially
always under 30 minutes and are **never** sampled — every window they have is analyzed. The
window budget is, in practice, exclusively a concern for **concert sets / full live recordings**,
which routinely run 1–12+ hours. That framing matters for §3 and the recommendation.

______________________________________________________________________

## 2. Why it exists — the actual rationale, not the presumed one

The operator's question presumed the reason was "bounded compute/memory... and predictable
per-file wall clock for lane scheduling." That is *directionally* correct but the mechanism
changed under a 2026-08-06 fix (`phaze-5lop`), and the current, accurate rationale is narrower
than the presumption. Both eras are recorded below because the pre-fix era is why the caps were
introduced at all, and the post-fix era is why they are still the right call today.

### 2a. Origin: an actual 4-hour timeout incident (Phase 43)

Before Phase 43's caps existed, `analyze_file` had no per-file cost bound at all. On long files
this was **O(duration)** in the number of windows analyzed, and it caused a real production
timeout (`RhythmExtractor2013` on long buffers; `docs/spikes/phaze-ytgo.2-essentia-embeddings.md`
line 477: "Phase 43 O(1) cap exists because `RhythmExtractor2013` on long buffers caused a
4-hour timeout"). Phase 43's fix was exactly the cap-plus-even-stride design described in §1:
bound windows analyzed to a constant per file (`docs/essentia-analysis.md`: "cost is O(constant),
not O(duration) — the root-cause fix for the 4h-timeout incident").

### 2b. Memory: the `analysis_child` >8 GiB spike and ADR-0005

Spike `phaze-esut` (`docs/spikes/phaze-esut-analysis-memory-profile.md`) measured the analyze
pipeline end to end against a production incident: 55 kernel OOM kills over six days on an
8 GiB-`request`, no-`limit` pod, with observed peak RSS of **8.5–10.5 GiB**, and pathological
outliers up to **15.27–30.41 GiB** (`anon-rss`, never reproduced under controlled variation —
filed as its own open question). This directly produced ADR-0005
(`docs/design/0005-analyze-job-memory-limits.md`): emit `resources.limits.memory` on analyze
Jobs so a pod-scoped fault stays pod-scoped instead of taking out cluster infrastructure
(confirmed collateral: `coredns` ×2, `metrics-server` ×2, `local-path-provisioner` ×2).

The *caps themselves* (as opposed to the Job memory limit) are one contributor to keeping that
floor bounded: `_analyze_coarse_windows`'s docstring states it holds **all** `coarse_cap` 16 kHz
buffers concurrently by design (~345 MB at the default 30 — deliberately traded for *not* holding
the ~4 GiB of co-resident TF graphs a window-major loop used to hold, per `phaze-15sw`), and FINE
holds `fine_cap` 44.1 kHz buffers the same way (~317 MB at the default 60). Both docstrings state
explicitly: "Retention stays bounded by the CAP and never by duration, which is the invariant
that matters." Uncapped, both figures become a function of file length — see §3.

Follow-on memory work (`phaze-15sw` model-major restructure, `phaze-0582` batch-size tuning,
`phaze-rvcn` core-derived thread sizing, `phaze-5lop` streaming decode) drove the measured Linux
peak for a 60-minute file at saturated (capped) defaults down from 7.986 GiB to **1.7383 GiB**
end to end — a real, substantial, and *independent* improvement. It does not remove the caps'
role; it changed what floor they are bounding relative to.

### 2c. Wall clock / lane scheduling: decode used to dominate, now the models do

This is the part of the presumed rationale that most needed correcting. `docs/essentia-analysis.md`
records a compute-profile finding that reversed between the pre- and post-`phaze-5lop` state,
measured on a 60-minute file at production caps (60/20 fine/coarse windows in that specific
measurement):

| | before `phaze-5lop` | after `phaze-5lop` |
| --- | ---: | ---: |
| total wall | 3 205.05 s | **1 960.37 s** |
| audio decode + resample | 1 370.77 s (42.8%) | **126.98 s (6.5%)** |
| 34 TF graphs × N windows + `RhythmExtractor2013`/`KeyExtractor` × N windows | 1 834.28 s (57.2%) | **1 833.39 s (93.5%)** |

The root cause `phaze-esut` §8 found: `es.EasyLoader` does not seek (`Trimmer`'s
"tell my parent to stop" optimization cannot cross the `MonoLoader` composite boundary), so
every one of the 80–90 per-file window decodes re-decoded and re-resampled the file **from byte
0**. `phaze-5lop` (2026-08-06) replaced that with `_decode_windows_streaming()`: one streaming
decode network per tier, fanned out to a `Trimmer` per window, run once. Measured effect on
decode-of-first-180-seconds wall time, same shape before and after, confirms the mechanism
(`phaze-esut` §8, remeasured on the burst node):

| total file duration | before (§8) | after (`phaze-5lop`, "standard, remeasured") |
| ---: | ---: | ---: |
| 10 min | 6.6 s | 7.890 s *(one-time streaming setup cost, not yet amortized)* |
| 60 min | 45.9 s | 52.070 s |
| 720 min | 600.2 s | 631.570 s |

*(these top-line "after" decode-time-per-window numbers look similar to "before" because they
measure the SAME single first-window decode; the multiplier `phaze-5lop` removed is the redundant
re-decode across the OTHER 80-plus windows, which no longer happens — end-to-end decode share
fell from 42.8% to 6.5% of total wall, per the table above this one.)*

**One nuance that changes what the cap is actually for now:** the streaming decode pass still
reads the *entire file* end to end regardless of the cap — `MonoLoader` exposes no seek, and
because `_stride_to_cap` always keeps the first and last window, the strided window set spans the
whole file, so the loader cannot stop early. **Decode cost is `O(duration)` today whether or not
a file is sampled**, and the caps do nothing to reduce it. What the caps bound is the **per-window
model work**: `RhythmExtractor2013` + `KeyExtractor` (fine) and the 34 TensorFlow graphs (coarse),
which the essentia-analysis.md table above shows now dominates wall clock at 93.5%. That is the
actual, current mechanism by which the cap delivers "predictable per-file wall clock for lane
scheduling" — it is no longer bounding decode (that part of the original presumption is now
false), it is bounding TF inference count.

______________________________________________________________________

## 3. What raising the cap, or going exhaustive, would cost

Scoped to the file class the caps actually affect (§1): concert sets / long live recordings.
Individual tracks are unaffected either way — they never hit the cap.

**Memory.** Each pass holds all of its *kept* windows' PCM concurrently by design (§2b). That
scales linearly with window count once uncapped:

- FINE: ~30 s × 44 100 Hz × 4 B (float32) ≈ 5.05 MB/window. At the default cap (60) that's the
  documented ~317 MB. Uncapped on a 12-hour set (natural window count 1 440, a 24× increase over
  the cap): **≈ 7.3 GiB** held transiently for the fine pass alone.
- COARSE: ~180 s × 16 000 Hz × 4 B ≈ 11.52 MB/window. At the default cap (30) that's the
  documented ~345 MB. Uncapped on the same 12-hour set (natural window count 240, an 8× increase):
  **≈ 2.8 GiB** held transiently for the coarse pass.

These are back-of-envelope extrapolations from the documented per-window buffer sizes, not fresh
measurements — flagged explicitly because CLAUDE.md requires measured numbers to stay exact and
this repo's history (`phaze-esut`, `phaze-rc1q`) has a standing lesson that "adding the
components" this way has been wrong by a gigabyte before when the components interact (e.g.
allocator/thread effects). The two figures above don't stack (the passes run sequentially,
§2b), but either one alone materially raises the pipeline's peak above the current whole-process
measured floor of 1.7383 GiB (§2b) and above the interim `3Gi`/`4Gi` limit tier ADR-0005 sized
against that floor — i.e. **exhaustive analysis of a multi-hour set plausibly exceeds the memory
limit ADR-0005 put in place**, and would fail via that limit's designed-in cgroup OOMKill (a
contained, pod-scoped failure per ADR-0005 — better than the pre-ADR-0005 node-killing failure
mode, but still a failure, not a completed analysis).

**Wall clock.** Decode cost is unaffected by the cap either way (§2c) — a 12-hour file's two-tier
decode is ~19–21 minutes regardless of sampling. What scales is the now-dominant (93.5%) model
work: exhaustive mode on the same 12-hour set runs **24× more fine windows and 8× more coarse
windows** than the capped default. Applying that multiplier to the 1 833.39 s of model work
measured on a 60-minute file at capped defaults (not a rigorous extrapolation — window-processing
cost need not be perfectly linear, but no evidence in the corpus suggests otherwise) lands in the
range of many hours to over a day for the coarse (dominant) pass alone. That exceeds
`analysis_inner_timeout_sec` (6 600 s / 110 min, `config.py`) and the SAQ `process_file` outer net
(7 200 s) by a wide margin on the SAQ worker path, where the inner timeout SIGKILLs the child
deterministically. On the k8s burst path specifically, `phaze-esut` §8 records that
`job_runner` passes `timeout=None` and emits no `activeDeadlineSeconds` — so an exhaustive job
would not be killed, it would simply occupy a lane slot for an unpredictable, multi-hour-to-day
span. `phaze-esut` §8 also records why a blanket wall-clock bound is not the fix for that: a prior
attempt (`phaze-1b39`, a required 3 h `activeDeadlineSeconds`) SIGTERM'd legitimate 2–6 hour
concert-set analyses, burned `cloud_submit_max_attempts`, and stalled the whole burst lane
(2026-07-28 incident) — a wall clock cannot distinguish a long analysis from a hang. **This is
precisely the lane-scheduling-predictability cost the operator's question named,** now grounded
in a documented incident rather than an assumption.

**Net:** going exhaustive by default would reproduce, for every long file, the exact class of
problem (`O(duration)` cost, unbounded wall clock, unbounded memory) that Phase 43's caps and
ADR-0005's limit were both independently built to close.

______________________________________________________________________

## 4. Does the existing `deepen` path already answer this?

**Yes — functionally, it already is a full per-file exhaustive mode, and it is already surfaced.**

`deepen_analysis` (`routers/pipeline.py`, `POST /pipeline/files/{file_id}/deepen`) re-enqueues
that one file's `process_file` job with **`fine_cap=0, coarse_cap=0`** — the sentinel
`_stride_to_cap` treats as a no-op (§1: `cap <= 0` → return every natural window unchanged). Its
own docstring states the intent plainly: "re-analyze ONE sampled file at the full (unbounded)
window budget." This is not a different code path from what an "exhaustive mode" checkbox would
need to trigger — it is the same `analyze_file` call with the caps disabled, funneled through the
same `enqueue_process_file` path used everywhere else (full `ProcessFilePayload`, deterministic
`process_file:<file_id>` dedup key, proper per-agent queue routing).

**It is already surfaced in the UI**, not merely available via a hand-built request:
`templates/proposals/partials/sampled_badge.html` renders an amber "Sampled — more data
available" pill (tooltip carrying all four coverage counts), and
`templates/proposals/partials/analysis_timeline.html` renders a "Deepen analysis" button next to
it — both gated on the same `analysis.sampled` condition, in the per-file analysis-timeline
partial of the proposals review page.

**What it does *not* provide**, which is the real gap if one exists:

1. **No bulk action.** Deepen is one file at a time; there is no "deepen every sampled file in
   this tracklist/set" or "...in this scan" action. For an operator working through many
   multi-hour concert sets, that is a real amount of manual clicking.
2. **Only discoverable from the proposals review page.** The sampled badge / deepen button live
   in the per-file analysis-timeline row on `proposals`, not on the pipeline dashboard's file
   list or anywhere a file is browsed before it reaches proposal review.

Neither gap changes the answer to "is `deepen` the exhaustive mode" — it is — only whether it is
*convenient enough* for the archive's actual usage pattern. See Proposed follow-ups.

______________________________________________________________________

## 5. RECOMMENDATION *(offered for operator review — declined, see §7)*

| Option | Verdict |
| --- | --- |
| **A. Offer a new "exhaustive by default" analysis mode** | **Not recommended.** §3 shows it reproduces the exact O(duration) memory/wall-clock blowup Phase 43 and ADR-0005 were built to close, for a benefit (full coverage) that `deepen` already delivers on demand for the files that actually need it. |
| **B. Rely on the existing `deepen` path, with discoverability follow-ups** | **Recommended.** |
| **C. Reject exhaustive mode entirely, do nothing further** | Not recommended as-is — `deepen`'s bulk-action and discoverability gaps (§4) are real and cheap to close; rejecting outright leaves value on the table. |

**Recommendation: B — keep the caps as the default, and treat `deepen` as the already-shipped
answer to "I want exhaustive coverage on this file."** Rationale in one paragraph: the caps
almost never engage in the first place (§1 — only concert sets/live sets past 30/90 minutes ever
get strided; ordinary tracks get full coverage today, with no caveat), so a global exhaustive mode
would be paying §3's memory/wall-clock/lane-predictability cost on exactly the files it is most
expensive on, in exchange for a capability that `deepen` already provides per-file, on-demand, at
zero cost to every file that doesn't need it. The one legitimate improvement — making that
on-demand path easier to use across many long files at once — is a UI/workflow change on top of
the existing mechanism, not a new analysis mode, and is captured as a follow-up below rather than
folded into this recommendation.

**This recommendation was declined by the operator — see §7 for the actual decision.** It is kept
verbatim above as the record of what was known and recommended at review time; §3's cost analysis
is unaffected by the decision and remains the standing evidence for what exhaustive analysis
costs under the *current* (pre-removal) pass architecture.

______________________________________________________________________

## 6. Proposed follow-ups *(drafted at investigation time — largely mooted by §7; see notes below)*

### Follow-up 1 — Bulk "deepen all sampled files" action

- **Title:** Bulk deepen: re-analyze every sampled file in a scan/tracklist at once
- **Description:** `deepen_analysis` (`routers/pipeline.py`) already re-enqueues a single file at
  the full window budget (`fine_cap=0, coarse_cap=0`). Add a bulk variant — scoped to, e.g., a
  scan or a tracklist's candidate set — that iterates every file with `analysis.sampled = True`
  in that scope and enqueues a deepen for each, reusing `enqueue_process_file`'s existing
  per-agent routing, dedup key, and `NoActiveAgentError` handling so no new queue-safety logic is
  needed. Needs an operator-visible progress surface (count queued / already in flight / blocked)
  given deepen's own per-file guards can classify a collision as "blocked" (§4/`deepen_analysis`
  docstring D-05) — a bulk run should report that per file, not silently.
- **Acceptance criteria:** an operator can trigger "deepen all sampled" for a scoped set of
  files; each file's deepen goes through the existing single-file funnel unchanged; the response
  surfaces per-file outcome (queued / already in flight / blocked / no active agent); no new
  queue-routing or payload-construction logic is introduced (reuse `enqueue_process_file`).
- **Suggested priority:** P2 — real operator convenience gap, moderate implementation size
  (mostly wiring, not new analysis logic).
- **Status after §7: mooted.** The operator's decision removes `deepen` entirely rather than
  making it easier to use in bulk — every file gets full analysis up front, so there is no
  "sampled" set left to bulk-deepen. Not carried into the implementation bead.

### Follow-up 2 — Surface the sampled badge / deepen action outside the proposals review page

- **Title:** Show "Sampled" status and Deepen action on the pipeline dashboard file list
- **Description:** `sampled_badge.html` / the deepen button currently render only inside the
  proposals review page's per-file analysis-timeline row (`analysis_timeline.html`), which an
  operator only sees after expanding a file's row on that specific page. A file that was sampled
  is not flagged anywhere earlier in the pipeline (e.g. `pipeline_scans.py` / dashboard file
  list), so the operator has no way to find "which of my concert sets got sampled" without
  visiting proposals for each one individually.
- **Acceptance criteria:** the pipeline dashboard's file list (or scan detail view) surfaces a
  sampled indicator for files with `analysis.sampled = True`, reusing `sampled_badge.html`
  semantics (renders nothing when not sampled — no NULL/false-is-an-error regression per its
  existing D-03 contract).
- **Suggested priority:** P3 — discoverability improvement, not blocking any existing workflow
  (the deepen action is still reachable via proposals today).
- **Status after §7: mooted.** The badge and the button it sits next to are both being removed
  (§7) — there is nothing left to surface elsewhere. Not carried into the implementation bead.

### Follow-up 3 — Measure exhaustive-mode cost directly, if raising the caps is pursued instead

- **Title:** Measure `analyze_file` memory/wall-clock at fine_cap=0/coarse_cap=0 on a real
  multi-hour set
- **Description:** §3's memory and wall-clock figures for exhaustive mode are back-of-envelope
  extrapolations from documented per-window buffer sizes and the 60-minute capped-default
  measurement in `docs/essentia-analysis.md` — not a direct measurement of `deepen`'s own code
  path at scale. If a future decision leans toward raising the *default* caps (as opposed to
  relying on per-file `deepen`), get a real measurement first, in the style of `phaze-esut` /
  `phaze-rc1q` / `phaze-5lop` (peak RSS via `VmHWM`, wall clock, on the burst node, on a real
  multi-hour production-shaped file) rather than extrapolating further.
- **Acceptance criteria:** a spike report with measured (not extrapolated) peak RSS and wall
  clock for `deepen`'s `fine_cap=0/coarse_cap=0` path on at least one file in the 6–12 hour range,
  using placeholders per CLAUDE.md's no-local-identifiers convention (`<set-01>` etc.).
- **Suggested priority:** P4 — only relevant if a future decision reopens raising the default
  caps; not needed to act on this note's recommendation (B), which changes no defaults.
- **Status after §7: superseded, rationale transferred.** The caps aren't being raised, they're
  being removed, so this follow-up's original framing ("measure before raising the default") no
  longer applies verbatim. Its underlying point stands and transfers directly to the
  implementation bead `phaze-w55w1` (§7): the chunked/bounded-memory rework needs the same kind
  of real, measured (not extrapolated) peak-RSS/wall-clock validation on a genuine multi-hour file that
  this follow-up asked for, so that ADR-0005's limits are re-validated against the new pass
  architecture rather than assumed to still hold.

______________________________________________________________________

## 7. DECISION *(2026-08-11, operator)*

The operator reviewed this note's recommendation (§5, option B — keep the caps, rely on `deepen`)
and **declined it**. The decision instead:

1. **Remove `analysis_fine_cap` / `analysis_coarse_cap` entirely.** Every file — including
   multi-hour concert sets / live recordings — receives full fine **and** coarse analysis, with
   no striding and no `sampled` result. §1's "the caps only bite on long files" framing describes
   the pre-decision state; post-implementation there is no cap left to bite.
2. **Remove the `deepen` re-analysis path as redundant.** With no sampling, there is nothing left
   to deepen: `deepen_analysis` / `deepen_progress` (`routers/pipeline.py`), the "Deepen analysis"
   button and "Sampled — more data available" badge (`templates/proposals/partials/{analysis_timeline,sampled_badge}.html`),
   and the `fine_cap=0`/`coarse_cap=0` sentinel path through `_stride_to_cap` all go away together.

**This is a separate implementation bead (`phaze-w55w1`), not this note.** §§1–4's investigation
and §3's cost analysis remain the standing record of what full-window analysis costs under the
pass architecture *as it existed at decision time* (sequential fine-then-coarse passes, each
holding all of its kept windows' PCM concurrently) — that is precisely the cost profile the operator is
choosing to accept, on the condition that the implementation bead changes the architecture enough
to keep it survivable. The implementation bead is scoped to:

- **Rework both passes to chunked, bounded-memory window processing** so the linear-in-duration
  memory growth §3 projects (~7.3 GiB fine / ~2.8 GiB coarse PCM on a 12-hour set, extrapolated)
  does not actually occur — i.e. stop holding all of a tier's windows concurrently, so a file's
  peak stays a function of chunk size, not of duration, and ADR-0005's memory limits (§2b) remain
  valid against the reworked passes rather than being invalidated by this decision.
- **Replace the wall-clock timeout (`analysis_inner_timeout_sec` / the SAQ `process_file` outer
  net, currently 6 600 s / 7 200 s, `config.py`) with progress-based (heartbeat/stall) liveness
  detection, not merely raise or remove it.** The operator's direction is explicit: the analysis
  child heartbeats window-completion progress, and the supervising layer kills it only when no
  progress occurs for a configurable stall threshold — elapsed wall time alone must never kill an
  analysis that is still progressing. This is workstream 4 of `phaze-w55w1`. `phaze-esut` §8's
  caution against a blanket wall-clock bound (the `phaze-1b39` stall incident, §3) is the evidence
  *for* this choice — a wall clock cannot distinguish a long-but-progressing exhaustive analysis
  from a genuine hang, and `phaze-1b39` is a real incident of that exact failure mode; heartbeat
  liveness is what actually distinguishes the two.
- **Fully remove the sampling machinery**, not just stop calling it: `_stride_to_cap`,
  `analysis_fine_cap`/`analysis_coarse_cap` (config knobs, env vars, docs), `ProcessFilePayload.fine_cap`/`.coarse_cap`,
  the `sampled` flag and the `*_windows_total` vs. `*_windows_analyzed` distinction it drives
  (keep `windows_analyzed`/`windows_total` themselves as plain progress-reporting counts — they
  remain meaningful once `analyzed == total` always — but the coverage-gap concept they currently
  express goes away), the `sampled_badge.html` partial, and the `deepen`/`deepen-progress` routes
  and templates.

Follow-ups 1 and 2 in §6 are **mooted** by this decision (both were about making a
soon-to-be-removed `deepen` path easier to use). Follow-up 3's core ask — measure real peak
RSS/wall clock on a genuine multi-hour file rather than extrapolating — is **not** mooted; it
transfers to the implementation bead as the validation its chunked rework needs.

______________________________________________________________________

## Sources

- `src/phaze/services/analysis.py` — `analyze_file`, `_iter_windows`, `_stride_to_cap`,
  `_decode_windows_streaming`, `_analyze_fine_windows`, `_analyze_coarse_windows`
- `src/phaze/config.py` — `analysis_fine_window_sec` / `analysis_coarse_window_sec` /
  `analysis_fine_min_sec` / `analysis_fine_cap` / `analysis_coarse_cap` / `analysis_inner_timeout_sec`
- `src/phaze/routers/pipeline.py` — `deepen_analysis`, `deepen_progress`
- `src/phaze/schemas/agent_tasks.py` — `ProcessFilePayload.fine_cap` / `.coarse_cap`
- `src/phaze/templates/proposals/partials/sampled_badge.html`,
  `src/phaze/templates/proposals/partials/analysis_timeline.html`
- `docs/essentia-analysis.md` — compute profile, before/after `phaze-5lop` measurements
- `docs/spikes/phaze-esut-analysis-memory-profile.md` — the >8 GiB `analysis_child` memory
  incident, §8 (decode-is-O(duration) finding), the `phaze-1b39` wall-clock-bound incident
- `docs/spikes/phaze-rc1q-streaming-vs-standard-mode.md`, `docs/spikes/phaze-ytgo.2-essentia-embeddings.md`
- `docs/design/0005-analyze-job-memory-limits.md` — ADR-0005, the memory-limit decision this note
  builds on

______________________________________________________________________

<div align="center">
↩️ Back to the <a href="../README.md">docs index</a>
</div>
