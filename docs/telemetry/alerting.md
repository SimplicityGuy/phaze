# Alerting: three rules, and the four phaze deliberately does not ship

**Audience: whoever runs homelab's Prometheus.** `alerts/phaze-alerts.yml` is a portable
rules file you may load. **Nothing in this repo deploys it**; wiring, routing and any
Alertmanager are yours.

A single-operator home server does not need a pager. `phaze-m1drf.5` made "conclude that
dashboards are enough and ship nothing" a first-class outcome, and the conclusion reached
is in between: **three rules survive the bar that every threshold must trace to a measured
baseline, and four candidates do not.** The four are documented here at the same length as
the three, because a rule that should not exist is the more expensive mistake — it fires on
something settled, the operator learns to ignore alerts, and then the one that matters is
ignored too.

Every threshold below cites its measurement.
`docs/spikes/phaze-zaf2l-where-phaze-spends-time.md` and
`docs/design/0005-analyze-job-memory-limits.md` are where the measurements live.

______________________________________________________________________

## Verifying the rules before you adopt them

`promtool` is the real consumer of both the rules and their unit tests, and it is what this
repo checks them with:

```bash
docker run --rm -v "$PWD/alerts:/alerts:ro" --entrypoint /bin/promtool \
  prom/prometheus:v3.10.0 check rules /alerts/phaze-alerts.yml
docker run --rm -v "$PWD/alerts:/alerts:ro" --entrypoint /bin/promtool \
  prom/prometheus:v3.10.0 test rules /alerts/phaze-alerts.test.yml
```

The unit tests are the interesting half. `alerts/phaze-alerts.test.yml` builds the
accepted-drain-rate condition — an 8,079-file backlog draining at the measured 2.4480
files/hour — and asserts that **every one of the three rules stays silent**, and it builds
an idle pipeline and asserts the same. `tests/shared/telemetry/test_alert_rules.py` holds
the CI-runnable half: the properties about what the rules must never become.

______________________________________________________________________

## PhazeAnalysisProgressStalled

**Fires when:** analyze work was scheduled in the last hour and **no analysis window
completed anywhere** in that hour, sustained for 30 minutes.

**Why 1 hour, and why not a completion-rate alert.** The analysis child's own stall
watchdog (D-08, `analysis_stall_timeout_sec`) kills a child after **1800 s** of total
silence. One hour is that existing production constant doubled — not a round number chosen
for comfort. It is deliberately **not** built on completions: at the measured **2.4480
files/hour** a single 12 h 04 m file takes roughly 18 hours of wall clock at **1.4951×**, so
four concurrent long files can legitimately produce no *completion* for most of a day while
producing window completions the whole time. Alerting on completions would page for the
archive's longest files.

**Why the "work was scheduled" term is load-bearing.** Without it, an idle phaze — which is
its normal night-time state — trips the rule every night. The two terms together say
*something is being asked for and nothing is happening*, which is a fault; either alone is
not.

**What to check:** are analyze pods being admitted by Kueue; is the agent worker running; is
the burst node reachable. This rule cannot distinguish those (see *the burst node* below).

## PhazeAnalysisFailureRateElevated

**Fires when:** more than **5%** of analyses failed over 6 hours, **and** the window holds
more than 40 runs, sustained for 30 minutes.

**Where 5% comes from.** The measured baseline is **4 hard failures against 4,383 completed
analyses — 0.0913%** (`phaze-zaf2l` §2: three `AnalysisDecodeError`, one
`AnalysisProbeError`). 5% is about **55×** that.

**Where the >40 guard comes from, and why it is not padding.** Without it a *single* failure
among 19 runs is 5%. At 40 runs the baseline expectation is 0.037 failures, so crossing 5%
needs at least two — probability about **0.0007** under the measured rate. Six hours at the
measured rate is only ~14.7 runs, so in normal operation this rule does not evaluate at all
until a burst has genuinely put enough work through the window to be evidence.

**Note what does *not* count:** a partial failure — some windows skipped by per-window
failure isolation — is a successful analysis. These are file-level failures only.

## PhazeAnalysisChunkPeakRssApproachingLimit

**Fires when:** the p99 of per-chunk peak RSS exceeds **3.5 GiB**, sustained for 15 minutes.

**Where 3.5 GiB comes from.** The deployed pod limit is **4Gi = 4,294,967,296 B**
(`docs/design/0005-analyze-job-memory-limits.md`, `backends.toml`). The measured post-D-09
whole-process peak on the burst node is **1.50 / 1.65 / 1.67 GiB** at 1:00 / 4:00 / 12:04 of
audio (`docs/spikes/phaze-u1n7j-vox-fix-verification.md`). 3.5 GiB is 87.5% of the limit and
more than **twice** the measured peak: nothing healthy reaches it, and a breach is a warning
rather than an OOMKill that already happened.

**What it is really watching for.** Duration-linear growth. `phaze-b2qs9` measured
**+0.31 GiB per fine chunk** (R² 0.99959), reaching **10.28 GiB at 12 h**, and every file
past ~3 hours OOMKilled. **If this fires, the fix is the chunk teardown, not a larger limit.**
Growth is a bug, never a sizing input.

> **Do not evaluate this rule against a developer machine.** macOS reports a much higher
> peak for identical work — measured locally at **4.51–4.87 GiB** for a 10-minute file,
> against 1.50–1.67 GiB on the burst node — because thread sizing and the allocator differ.
> The threshold is a statement about the Linux pod under its cgroup, and the rule's selector
> scopes it to the analysis service for that reason.

______________________________________________________________________

## The four rules phaze deliberately does not ship

### Backlog depth — a settled decision, not a fault

> **Operator decision 2026-08-26.** Durable record: repowise decision `e1e3374e`. The
> current drain rate is **ACCEPTED**.

So an 8,079-file `awaiting` queue and a 137.5-day projected drain are the expected state,
not a fault. An alert firing on a settled operator decision is worse than no alert: it
teaches the operator that phaze's alerts are noise.

There is a second, independent reason. `phaze_pipeline_backlog` is **poll-driven** — it is
sampled by the admin UI's own `/pipeline/stats` read, so the series go stale the moment
nobody has a tab open. A rule built on it would be silent exactly when nobody is watching.
`tests/shared/telemetry/test_alert_rules.py::test_no_rule_fires_on_backlog_depth` forbids
both.

### An analysis "running too long" — the phaze-1b39 incident

`phaze-1b39` is the incident where a wall-clock bound SIGTERM'd legitimate 2–6 hour analyses
and stalled the whole burst lane. A multi-hour concert set is **expected** to take hours;
liveness is progress-based and never elapsed-based. Moving that same false premise up to the
monitoring layer would page instead of kill — still wrong, and trained on the same mistake.
A duration used as a **ratio against audio seconds** is a perfectly good dashboard reading;
a duration used as a **bound** is not, and the CI test enforces the distinction.

### The burst node unreachable — phaze cannot write this rule

This is a legitimate thing to want and phaze **cannot** supply it from its own telemetry.
"`vox` is unreachable" and "there is no analysis work right now" produce the **identical**
absence of series, so any rule phaze could write would either miss the outage or fire every
idle night.

The rule that is actually wanted needs node-level facts — Kueue admission, kubelet
readiness, `up` on a node exporter — which belong to homelab's own cluster monitoring and
not to an application's metric contract. **This is the coordination item this molecule hands
to homelab.**

### Window skip rate — no measured baseline exists yet

Windows skipped by per-window failure isolation are coverage quietly eroding, and a rising
skip rate is worth knowing about. But `phaze-zaf2l` did not measure a skip-rate baseline, and
`phaze-m1drf.5` acceptance 3 requires every threshold to trace to one. **Inventing a number
here would be exactly the unmeasured arithmetic these rules exist to avoid.**

The dashboard panel *Windows skipped / min* on **phaze / Analysis pipeline (live)** shows the
series, and the first weeks of this telemetry are what will produce the baseline. Filing the
rule then is a follow-up, not a gap in this molecule.
