# ADR-0012 — Verification fidelity, and what may be called an operator decision

| | |
| --- | --- |
| **Status** | Accepted — decided 2026-08-20 |
| **Date** | 2026-08-20 |
| **Bead** | `phaze-u8qj0` |
| **Applies to** | every bead that changes a production path: what an acceptance criterion obliges, what may be attributed to the operator, and what counts as having verified a claim |
| **Enforced from** | `CLAUDE.md` → *Acceptance criteria, attribution, and verification fidelity* (the five rules, immediately above *Beadhive Workflow Enforcement*) |

## Why this is an ADR in `docs/design/` and not an incident report

There is no `docs/incidents/` directory in this repo, and creating one for this document would
misfile it. Four fifths of what follows is standing policy — five rules and the evidence that each
one would have changed a specific outcome — and only one fifth is history.

The history already has durable homes, and this ADR cites them rather than restating them:

- `phaze-l832u` (the epic) carries the measured outage timeline;
- `docs/design/0007-windowed-analysis.md` §8 carries the memory-claim correction;
- the `D-09` / `D-10` records in `src/phaze/services/video_audio.py` and the `D-07` / `D-08`
  records in `src/phaze/services/analysis.py` and `src/phaze/services/analysis_exec.py` carry the
  mechanisms;
- `tests/analyze/services/backends/test_kube_staging.py` and
  `tests/analyze/services/pipeline/test_extraction_analysis_handoff.py` carry the regressions.

`ADR-0011` is the precedent for the shape: a process decision, committed to `docs/design/`, whose
argument is measurements rather than principle. This is the same kind of document.

______________________________________________________________________

## 1. Finding 1 — a narrow answer was generalized, then stamped with the operator's authority

### The question that was put, and the question that shipped

Confirmed with the operator 2026-08-14. During `phaze-3ea41` the operator was asked about
**detection method**: should a static video-extension whitelist, or `ffprobe`, decide whether a
file has an audio stream? The answer was **ffprobe**.

What shipped was a materially wider proposition: that pre-analysis extraction runs
**unconditionally on every file**, remuxing all audio through `ffmpeg` into a Matroska (`.mka`)
scratch file before analysis. That was never asked.

The two do not entail one another. "`ffprobe` is the authority on whether a file has an audio
stream" is fully compatible with "and when it reports a plain audio container, skip the remux" —
which is exactly what `phaze-l832u.1` now does, with `ffprobe` still the sole authority and the
extension whitelist still gone. Two separable decisions were fused, and the fused version
inherited the authority of the answer to one of them.

### Where the attribution was recorded — three places, verified

1. **Commit `dd7339bb`** (2026-08-11), message line 3: *"any container ffprobe reports an audio
   stream in — probe-based, no extension whitelist, **operator decision**"*, and the heading of the
   six-bullet block below it: *"Design decisions (phaze-3ea41, **operator-confirmed**)"*.
2. **PR #424** (merged 2026-08-12, merge commit `bab5f32b`), body line 1: *"**Operator decisions
   honored**: probe-based container acceptance (ffprobe is the has-audio authority), extraction on
   both lanes, disposition-default track selection"*, over the same six bullets.
3. **The module decision record** at the top of `src/phaze/services/video_audio.py` (`D-09`),
   still present at HEAD: *"**Format scope: probe-based, any container — operator decision**, no
   extension whitelist (phaze-3ea41)"*, and *"The **operator decision** replacing that:
   `extract_audio_track` runs for EVERY file the callers hand it, regardless of extension"*.

### The correction this ADR makes to its own bead

`phaze-u8qj0` names the three places as "the commit message, the bead, and the module decision
record". The third-party artifact is the **PR body**, not the bead. The distinction matters, and
it makes the finding stronger rather than weaker:

> **`phaze-3ea41` has `comment_count: 0`.** Its description records exactly one operator
> utterance — *"Operator observation 2026-08-10: some ANALYSIS_FAILED rows are video containers"* —
> and it lists format scope nowhere. Track selection *is* named in the description, and it is named
> as work to be done, not as a settled answer: *"Decisions to make in-bead: audio-track selection
> when a container carries several (first/default track, log the rest)"*.

So the one artifact with a durable, queryable, per-bead record of operator answers contains **none
of the six** decisions that the commit and the PR describe as operator-confirmed. The attribution
lives only in the two places that are written by the implementer at submit time and read by nobody
afterwards.

It then propagated. `phaze-l832u` — the incident epic, written three days later — reasons from
*"the **operator decision recorded in that bead**, runs on EVERY file rather than only video
containers"*. That claim is false about `phaze-3ea41`, and the fix bead inherited it as settled
ground while diagnosing the outage the same decision caused.

### Why this is the finding with the longest blast radius

"Operator decision" is the marking that tells a reviewer *do not relitigate this*. Applying it to an
agent-made scope choice converts a reviewable judgement into apparent settled authority. The review
that followed was not weak — `phaze-3ea41`'s close reason records that it *"survived a 10-finding
verified review + a bandit round + CI"* — and it did not challenge the format scope. There was no
reason for it to: the change announced that the operator had already decided.

______________________________________________________________________

## 2. Finding 2 — an acceptance criterion was reinterpreted instead of tested

`phaze-3ea41`'s acceptance criteria included, verbatim:

> `existing audio-file analysis is unchanged`

It was not met, and it was not missed. It was reasoned away, in writing, in the decision record
that shipped with the change. From `src/phaze/services/video_audio.py` at `dd7339bb`, verbatim and
including its original casing:

> ```
>   * ``-c:a copy`` is a lossless stream copy (never a re-encode), so for an ALREADY-bare-audio
>     file (mp3, flac, ...) this demux-and-remux round-trip produces bit-identical audio to what
>     essentia would have decoded from the original -- "existing audio-file analysis is
>     unchanged" is a claim about ANALYSIS OUTPUT, not about this module being skipped for
>     audio-typed inputs.
> ```

The substitution is in the last clause. The operator's criterion — *behaviour is unchanged* — was
replaced by a narrower claim — *the audio stream is bit-identical* — the narrower claim was
verified, and the criterion was declared satisfied.

### The substituted claim was true, and irrelevant

`-c:a copy` really is a lossless stream copy. The remuxed audio really is bit-identical. Nothing in
that sentence is false.

But the **container** changed, from MP3 to Matroska, and analysis reads the container. `analyze_file`
takes its duration from `_probe_duration_sec`, which at the time read `es.MetadataReader` — TagLib.
TagLib returns no duration for Matroska. Zero duration makes `_iter_windows` yield nothing, both
`*_total` counts land on 0, and the zero-window guard fails the file.

Measured inside the deployed 2026.8.3 agent image on 2026-08-14 (`phaze-l832u`):

| probe | on the original `.mp3` | on its `.mka` remux |
| --- | --- | --- |
| `es.MetadataReader` duration | 90 | 0 |
| `ffprobe` duration | — | 90.044000 |
| `_decode_windows` | — | 2/2 windows, 1,323,000 samples each |

The audio was intact and essentia decoded it correctly, exactly as the equivalence argument said.
**The argument answered a question nobody needed answered.** An argument that is true and about the
wrong quantity is indistinguishable, at review, from an argument that is true and about the right
one — which is why the rule below is that a criterion is discharged by a test or by the operator,
never by prose.

### What it cost

From `phaze-l832u`, measured on the deployed fleet:

- **216 files** failed on the local lane between 02:18 and 02:20 UTC, the queue draining in
  2.5 minutes.
- The cloud lane's last success was 03:47 UTC; every burst pod after it failed identically, jobs at
  `BackoffLimitExceeded`, re-driven every ~4 minutes indefinitely. Kueue was healthy — 0 pending
  workloads — so this was never capacity.
- **11.5 hours** with zero completions *and zero recorded failures*, because the cloud zero-window
  path exited without storing an `error_message` (`phaze-l832u.2`). The outage read as "the
  pipeline went quiet", not "every file is failing".
- **8,683** `cloud_jobs` awaiting, against a corpus of **11,428** files.

And the collateral: `phaze-w55w1`'s exhaustive analysis shipped in the same release and **never
executed once**. Every completed analysis in the corpus still topped out at exactly 60 fine
windows.

______________________________________________________________________

## 3. Finding 3 — the pattern, and the sharper version of it

### The three incidents

| | `phaze-1b39` → 2026-07-28 | `phaze-b2qs9` / `phaze-u1n7j` → 2026-08-12/13 | `phaze-3ea41` → 2026-08-14 |
| --- | --- | --- | --- |
| **The claim** | a 3 h `activeDeadlineSeconds` bounds hung pods without cutting real work short | peak RSS is a function of chunk size, not of duration; ADR-0005's limits stay valid | `-c:a copy` is bit-identical, so audio-file analysis is unchanged |
| **What verified it** | tests of the recovery path on pods that never start | `test_analysis_long_file.py` — a **mocked** essentia | 704 lines of `test_video_audio.py` against a **faked subprocess**, plus three real-`ffmpeg` tests that stop at `ffprobe` |
| **What production did** | SIGTERM'd every 2–6 h concert-set analyze at exactly 3 h, burned `cloud_submit_max_attempts=3` per file, barred 14 files from Kueue, stalled the burst lane | +0.31 GiB per fine chunk (R² 0.99959); 4.1854 GiB at 4:00 and 10.2768 at 12:04 against a 4Gi limit — **2.57×** — OOMKilling every file past ~3 h | zero windows for every file in an 11,428-file corpus; 11.5 h of silent total failure |
| **The reversal** | `phaze-202e` — no wall clock may kill a run; liveness is pod state | `phaze-u1n7j` — disconnect the streaming network, `gc.collect()`; 1.4985 / 1.6500 / 1.6725 GiB at 1:00 / 4:00 / 12:04 | `phaze-l832u.1` — probe with `ffprobe`; skip the remux for plain audio |

### The mechanism, as `phaze-u8qj0` drafted it

1. A change is justified by an **argument** about equivalence or bounds.
2. The argument is verified against a **proxy that structurally cannot exhibit the failure**.
3. Green CI is read as confirmation of the *argument* rather than of the *proxy*.
4. Production is the first place the real input class ever meets the code.

That is correct, and step 2 needs refining, because the refined version is what makes the rule
enforceable.

### The refinement: the proxy was not always a mock

`phaze-3ea41` **did** ship real-tool tests. `tests/analyze/services/pipeline/test_video_audio.py`
at `d4524c88` contains three of them, gated on `_HAS_FFMPEG = shutil.which("ffmpeg") is not None and
shutil.which("ffprobe") is not None`. They build synthetic `lavfi` containers, run the real
`extract_audio_track`, and assert on the real output. One of them is called
`test_real_extract_audio_track_from_synthetic_video` and its docstring says the result *"is itself
decodable by ffprobe as an audio-only file"*.

That is the whole defect, in the test that was supposed to prevent it. The `.mka` was validated by
**`ffprobe`** — a tool that reads Matroska duration correctly, as the incident measurement above
confirms at 90.044000. It was never handed to **`es.MetadataReader`**, the consumer that could not.
Every test of the new module ended one call before the code that would actually receive its output.

So the proxy is not "a mock". It is **the wrong seam**: the artifact was checked by the tool that
produced it rather than by the component that consumes it. A reviewer applying "don't verify with
mocks" to PR #424 would have found real-binary tests over real containers and cleared it. That is
why guardrail 3 below is written about the *consumer*, not about mocks.

`ADR-0007` §8 had already reached the same conclusion for the memory incident, in one sentence
worth keeping: *"The synthetic proof above measured the right **quantity** on the wrong **scope**,
and the difference is the whole finding."*

### The lesson was written down, narrowly, and not generalized

This is the part that makes it a pattern rather than three accidents — and the dates matter, so
they are stated exactly rather than as "later".

The observation that this repo's evidence was a mocked essentia, and that this was not good enough,
was written down **on 2026-08-11**, in `phaze-b2qs9`'s own description: *"The shipped evidence is
synthetic (mocked essentia, forked per-duration `ru_maxrss`; 12 h synthetic within 25 MB of 2 h).
Measure the real thing."* It was scoped to memory. **`phaze-3ea41` merged the next day**,
2026-08-12, carrying a container change verified at the producer's own seam.

`phaze-u1n7j` then landed `D-09` on 2026-08-13 and `CLAUDE.md` recorded the lesson — again about
memory:

> The synthetic long-file test (`tests/analyze/services/pipeline/test_analysis_long_file.py`)
> proves the claim of a *mocked* essentia only and always did

and, of the two tests that replaced it:

> **neither can be satisfied by a mocked essentia** (the original long-file test was mocked, which
> is why this shipped)

Both sentences are true, both are load-bearing, and both are scoped to `D-09`. Nothing lifted them
to a rule about verification in general — so on 2026-08-14, one day after `D-09`, `phaze-l832u.3`
was filed to build the *same* guard for a *different* seam and opens by saying exactly that: *"This
is the same lesson the D-09 memory fix recorded: a mocked essentia cannot hold this line."*

So the sequence is not "the repo learned and then forgot". It is tighter and worse: within four
days the same lesson was written down **three times** — as a spike's caveat, as a fix's decision
record, and as a new test module's docstring — each time bound to the one seam in front of the
author, and never once as a rule. **Recording a lesson at the site that taught it is not the same
as adopting it.** That is why the guardrails below live in `CLAUDE.md` and not only here.

______________________________________________________________________

## 4. The guardrails

Each is stated in a form a reviewer can check against a diff, and each carries an explicit verdict
against all three incidents. The negative verdicts are the useful half: a guardrail that catches
everything catches nothing, because it is not saying anything falsifiable.

### G1 — An acceptance criterion is discharged by a test or by the operator, never by prose

For every acceptance criterion on a bead, the submission names **either** the test that exercises
it **or** the recorded operator amendment that changed it. A criterion that is argued about in
prose is not met.

Ambiguity goes back to the operator as a question. **Narrowing a criterion is permitted and
sometimes correct** — it is an operator action, recorded before submit, and the remainder becomes
its own bead. `phaze-tzy6s.13` is the worked example: its acceptance criterion was narrowed by
operator decision 2026-08-17 *"to what actually shipped rather than left reading satisfied when it
was not"*, and the remainder was split out as `phaze-fk1ww`. That is the shape. What is forbidden is
the implementer narrowing it silently and calling it satisfied.

| incident | verdict |
| --- | --- |
| `phaze-1b39` | **Would not have caught.** Its criterion — a Job whose pod never starts is recovered — was met, and tested. The defect lived in a consequence no criterion covered. G1 governs criteria that exist; `1b39`'s gap was a criterion that did not. |
| `phaze-b2qs9` / `u1n7j` | **Would not have caught.** ADR-0007 §7's condition is criterion-shaped, and `phaze-w55w1` *did* name a test for it: `test_analysis_long_file.py`. G1 asks whether a test is named, not whether it can fail. Only G3 closes that. |
| `phaze-3ea41` | **Would have caught.** No test anywhere named *"existing audio-file analysis is unchanged"* — the suite covered extraction, not the criterion. Both exits from G1 stop the change: writing the test produces `test_extraction_analysis_handoff.py`, which fails against the code as shipped; asking the operator surfaces the fusion in Finding 1. |

### G2 — "Operator decision" is a citation, not an emphasis marker

Any text asserting an operator decision — commit message, PR body, decision record, bead, code
comment — carries four things:

1. the **question as it was put**;
2. the **answer as it was given**, quoted;
3. the **date**;
4. a pointer to the **durable record** (a bead comment or an ADR section — not a commit message
   and not a PR body, both of which are written by the implementer and read by no one afterwards).

The attribution extends no further than the question asked. When implementation reveals a second
decision inside the first — here, *detection authority* versus *remux unconditionality* — that is a
new question, not a corollary. **The symmetric rule also holds:** a decision may not be narrowed
past the conditions attached to it — ADR-0007 §7 accepted a cost profile *"on the condition that the
implementation bead changes the architecture enough to keep it survivable"*, and shipping the
decision without verifying the condition is the same defect in the other direction.

A claim failing any of the four is not deleted — it is **relabelled as the implementer's decision**,
which is a perfectly good thing for a decision to be, and which invites exactly the review that the
operator label suppresses.

Two records in this repo already meet the bar and are the models: **ADR-0007 §7**, which states the
recommendation, that the operator declined it, what was decided instead, the date, and the
conditions; and the operator-decision comment on **`phaze-b62ri`** (2026-08-20), which separates
*question as put* / *answer as given* / *scope of that answer* / *dispatcher inference, flagged as
such* / *verified by dispatcher*.

| incident | verdict |
| --- | --- |
| `phaze-1b39` | **Would not have caught.** No operator attribution was involved. The 3 h default was an implementer's choice, presented honestly as one, and it shipped anyway. G2 makes provenance legible; it does not make a bound correct. |
| `phaze-b2qs9` / `u1n7j` | **Would not have caught.** ADR-0007 §7 is already G2-compliant, and it is the reason we can say precisely what the operator accepted and on what condition. G2 changed nothing about the defect — it is a documentation rule, not a verification rule. (Its new symmetric clause makes the *unverified condition* visible at review, which is a real but partial gain.) |
| `phaze-3ea41` | **Would have caught.** Six bullets stamped operator-confirmed against a bead with zero comments. Under G2 none is citeable; the format-scope bullet could not have been written as an operator decision at all, and would have appeared as what it was — an implementer's scope choice, open to challenge. |

### G3 — Verify with the artifact's real consumer, not with the tool that produced it

A change that produces a new artifact — a file, a container format, an intermediate, a serialized
payload — **names its real consumer, and the test calls that consumer.** Validating the artifact
with the tool that produced it proves round-tripping, not compatibility.

The general form, of which "a mocked essentia cannot hold this line" is one instance:

- A claim about **real essentia** is not discharged by a mocked one.
- A claim about a **container format** is not discharged by probing it with the muxer's own tooling.
- A claim about **real multi-hour durations** is not discharged by a short synthetic fixture.
- **A claim about the archive's distribution is discharged against the archive's distribution** —
  a query, not a test. This clause is an addition to the version drafted in `phaze-u8qj0`, and §4.1
  below is the argument for adding it.

| incident | verdict |
| --- | --- |
| `phaze-1b39` | **Would not have caught, as drafted. Would have caught, with the distribution clause.** The claim "3 h bounds hung pods without cutting real work short" is a claim about the corpus's duration distribution, and no test at any fidelity can hold it — CI cannot run a 6-hour analysis. What *could* have discharged it is one query against `files.duration` asking how much of the archive exceeds 3 hours. The answer was 2–6 hour sets, in quantity. That is the cheapest check in this document and the only thing on this list that would have stopped `1b39` at review. |
| `phaze-b2qs9` / `u1n7j` | **Would have caught.** The consumer is real essentia's streaming network; `test_analysis_long_file.py` measured a mock. ADR-0007 §8 states the outcome in its own table: *"it proves that of a **mocked** essentia. On real essentia the same comparison moves **gigabytes**."* |
| `phaze-3ea41` | **Would have caught — and only this rule would.** The consumer of `extract_audio_track`'s output is `analyze_file` → `_probe_duration_sec`. No test crossed that boundary; the real-`ffmpeg` tests stopped at `probe_audio_streams`, i.e. at `ffprobe` checking `ffmpeg`'s own output. `tests/analyze/services/pipeline/test_extraction_analysis_handoff.py` is what G3 demands, and `phaze-l832u.3` records that every test in it fails against the pre-fix code. |

#### The seam inventory G3 is owed

`phaze-l832u.3` closed one seam. **§7 R4's inventory of the rest is
[`docs/spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md`](../spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md)**
— 30 producer→consumer seams across six clusters, each with a producer, an artifact, a named real
consumer, the boundary crossed, and a crossed / proxy / not-crossed verdict. Read it before applying
G3 to a change in any of those clusters; the row for the seam you are touching says what is already
held and what is not.

Three things in it bear on how G3 itself should be read:

- **It found one live instance of the `phaze-3ea41` shape**, on a path serving the whole archive:
  COMPANION `FileRecord` rows have **no producer at all**, so the companion chain — LLM naming
  context, `.cue` tracklist candidates, `POST /associate` — is dead corpus-wide, while every test
  that exercises a companion consumer inserts the row by hand. Ingestion's filter and the
  association query were written eight weeks apart and have never met. (§5 of the inventory.)
- **"A mock is present" is not the test.** The inventory records a seam crossed by a suite full of
  mocks and seams left uncrossed by suites full of real infrastructure — real `moto`, real Kubernetes
  client shapes — because the real thing was on the wrong side of the boundary. G3 asks whether the
  artifact reaches its real consumer, and nothing else.
- **Static reading is not always enough to render a G3 verdict.** Two verdicts in the first pass were
  wrong in the pessimistic direction and were only corrected by measurement — running the suite under
  `redis-cli monitor` and matching observed `EVALSHA` digests against the scripts' source hashes.
  Where indirection hides the seam, measure it. (§6 of the inventory.)
- **A seam can have no seam *code*.** This is the sub-case a future auditor is least equipped to find,
  and the inventory found one: a tag write rewrites the file's bytes, so `FileRecord.sha256_hash` goes
  stale, and the consumer that verifies bytes against that column then fails permanently
  (`phaze-2zeu0`). Producer and consumer were written in different phases and **share only a database
  column** — there is no call site to grep for, so a search *for* the seam cannot find it. It surfaced
  only by asking what the producer changes about the artifact **besides the payload**. Read that as
  the third shape alongside the other two: `phaze-3ea41` is a **producer writing the wrong thing**,
  `phaze-j8hjn` is a **producer that does not exist**, and this is a **producer and consumer that were
  never introduced**. G3's question — what does the consumer read that the producer never writes — has
  to be asked of side effects, not just of the artifact the change was about.

### G4 — A change to a working production path owes a blast-radius statement

Before submit, the bead or PR contains three sentences, with the population **measured, not
adjectival**:

> This changes the path for **\<population\>**.
> What currently works that this could break: **\<X\>**.
> The test that proves it still works: **\<T\>**.

"Some files" and "audio files generally" do not satisfy it; "all 11,428 files in the corpus" does.
If no test T exists, that is the finding — escalate rather than write a weaker sentence.

| incident | verdict |
| --- | --- |
| `phaze-1b39` | **Would have caught.** The sentence writes itself: *"this adds a 3 h kill to every cloud analyze; what this could break is any analysis longer than 3 hours; the test that proves it still works is —"* and there is no T. The absence is the escalation. |
| `phaze-b2qs9` / `u1n7j` | **Would not have caught.** `phaze-w55w1` effectively wrote this statement and wrote it accurately: population = every file, risk = memory, test = `test_analysis_long_file.py`. Every sentence was true and the test was a mock. G4 asks whether a test is named; it cannot ask whether the test can fail. |
| `phaze-3ea41` | **Would have caught.** Scoped as "analyze video containers", it silently rewrote the analysis path for all 11,428 files. Forcing the population sentence — *"every file in the archive now passes through ffmpeg into Matroska"* — makes the fusion in Finding 1 visible on its face, before any test exists. This is the earliest-acting of the five. |

### G5 — A lesson recorded at one site states its general form, or states why it has none

When a fix's decision record or test docstring states a lesson of the form *"X cannot be verified
by Y"*, the merging seat does one of two things: names the general form of the lesson and where it
is now written down, **or** states why the lesson is genuinely specific to that call site. Both are
one sentence. Neither is optional.

This is the weakest rule of the five and the one most likely to be waved through, which is why it is
stated as a binary at merge rather than as advice.

| incident | verdict |
| --- | --- |
| `phaze-1b39` | **Would not have caught.** It was the first of the three; there was no prior narrow lesson to generalize. |
| `phaze-b2qs9` / `u1n7j` | **Would not have caught.** The defect predates the lesson it produced. G5 acts on the *output*: `D-09`'s "neither can be satisfied by a mocked essentia" would have been forced to state its general form, which is G3. |
| `phaze-3ea41` | **Would not have caught.** `phaze-b2qs9`'s "the shipped evidence is synthetic (mocked essentia)" was written 2026-08-11 and `phaze-3ea41` merged 2026-08-12, but `phaze-b2qs9` did not *close* until 2026-08-13 — so G5's trigger point falls after the change it would need to stop. `D-09` lands the day after that again. On the real timeline G5 fires on `phaze-l832u.3`, i.e. on the incident *after* this one. |

**G5 catches none of the three, and `phaze-u8qj0` says a guardrail that catches none does not belong
in the deliverable. It is kept anyway, deliberately, on a different ground.** The other four act on
a single change and are judged by whether they stop it. G5 acts on the **recurrence** mechanism,
which no single-change rule can reach, and the evidence for it is a measurement rather than an
intuition: between 2026-08-11 and 2026-08-14 the identical lesson was written down three times — in
`phaze-b2qs9`'s description, in `D-09`, and in `test_extraction_analysis_handoff.py`'s docstring —
each time correctly, each time bound to the seam in front of the author, and never once as a rule.
Judging G5 by the would-have-caught test is judging it by a criterion it was not built for. If a
future reader disagrees, this paragraph is the thing to argue with; G5 is the one rule here that is
offered rather than proven.

### 4.1 Why the drafted guardrails were changed

Four changes to the four rules drafted in `phaze-u8qj0`, each argued rather than assumed:

1. **G1 no longer forbids narrowing outright.** As drafted — *"the implementer may not narrow,
   reinterpret, or satisfy-in-spirit a criterion"* — it misfires on legitimate work. Criteria are
   sometimes ambiguous and sometimes wrong, and this repo has narrowed one correctly inside the
   last week (`phaze-tzy6s.13` → `phaze-fk1ww`). An absolute rule that routine correct work
   violates is worse than no rule, because it trains agents to read `CLAUDE.md` as advisory. The
   enforceable version keeps the prohibition where it belongs: on narrowing *silently*.
2. **G3 is about the seam, not about mocks.** This is the substantive change. As drafted, G3 binds
   "mocked, synthetic, or short-fixture" tests — and `phaze-3ea41` shipped **real**-`ffmpeg`,
   **real**-container tests. A reviewer applying the drafted rule would have found them and cleared
   the change. The rule has to name the consumer or it does not fire on the incident it was written
   for.
3. **G3 gains the distribution clause.** Without it, `phaze-1b39` is caught by exactly one of the
   five (G4), on the absence of a test rather than on the substance. With it, the check that would
   actually have settled the question — how much of the archive runs longer than 3 hours — becomes
   an obligation rather than an idea someone might have had.
4. **G5 is added, and it is the one rule not justified by the would-have-caught test.** Finding 3
   asks that "we wrote the lesson down narrowly" be treated as part of the pattern, and none of
   G1–G4 reaches it. See G5's own note for why it is kept despite catching none of the three.

**No guardrail catches all three incidents, and this is the intended result.** Of fifteen cells,
**seven** are "would have caught" — G1 one, G2 one, G3 three, G4 two, G5 none — and one of G3's
three (`phaze-1b39`) counts only because of the distribution clause added above; the rule as
drafted in `phaze-u8qj0` scores six. G3 also reaches its three in three different registers, not
one: a consumer call, a fidelity requirement, and a corpus query. Fifteen "would have caught" cells
would have been exactly the unfalsifiable claim this ADR exists to criticize.

______________________________________________________________________

## 5. Inventory — "operator decision" claims in tracked source and beads

Swept 2026-08-20 at HEAD. Two populations: tracked files under `src/`, `tests/`, `docs/`,
`scripts/`, `alembic/`, `Dockerfile*` and `CLAUDE.md`; and all **2,892** beads in the local Dolt
export.

- **Code surfaces** (`src/`, `tests/`, `scripts/`, `alembic/`, `Dockerfile`, `CLAUDE.md`,
  `justfile`): **55** case-insensitive matches for
  `operator[- ](decision|confirmed|approved|directed|granted|chose|ruling)` across **31** files. Of
  those, **13 are not attribution claims at all** — `operator-chosen sort order`, `awaiting an
  operator decision` in a UI detail string, `an operator choice of "zero threads"`. They are domain
  nouns about a person using the admin UI and are out of scope. **42** are provenance claims.
- **`docs/`:** 9 matches across 8 files, all cross-references to decisions recorded elsewhere.
- **Beads:** 75 matches across **56** of 2,892 beads.
- **`.planning/`** carries a further **79** matches across **48** archived milestone documents. It
  is a historical archive, superseded by the beads corpus, and is not load-bearing for any current
  decision; it was counted and not audited.

### Not traceable to any recorded answer

| claim | asserted in | status |
| --- | --- | --- |
| **`phaze-3ea41` format scope** — extraction runs on every file | `services/video_audio.py` (×3 sites), `job_runner.py:492`, `tasks/functions.py:329`, and 8 test docstrings across `tests/analyze/core/test_job_runner.py`, `test_phase101_e2e.py`, `test_process_file_scratch.py`, `tests/shared/tasks/test_functions.py`, `tests/analyze/services/pipeline/test_video_audio.py`; plus commit `dd7339bb` and PR #424 | **Confirmed false.** Finding 1. The operator was asked about detection method and answered `ffprobe`. `phaze-3ea41` has zero comments. |
| **`phaze-3ea41` track selection** — prefer `disposition.default` | `services/video_audio.py:245`, `:331`; `tests/analyze/services/pipeline/test_video_audio.py:207`, `:324`; commit and PR | **No recorded answer.** The bead names track selection explicitly as a *"decision to make in-bead"*. The decision itself looks entirely sound; the attribution does not. |
| **`phaze-3ea41` log the other streams' existence** | `services/video_audio.py:376`; `tests/…/test_video_audio.py:339` | **No recorded answer.** Same bead, same zero comments. |
| **`phaze-3ea41` extraction locality / disk headroom / liveness** | commit `dd7339bb`, PR #424 (all under *"operator-confirmed"*) | **No recorded answer.** Not repeated in source, so lower blast radius, but stamped in both submit-time artifacts. |
| *"the operator decision recorded in that bead"* | `phaze-l832u` (epic description) | **False about `phaze-3ea41`.** The propagation step: the incident bead inherited the attribution while diagnosing the incident it caused. |
| *"keep ubuntu-latest per operator decision"* | `phaze-ldvmy` | Undated, uncited. Unverifiable as written; substance not disputed here. |
| *"Operator decision: this rides the NEXT release"* | `phaze-6r39` | Undated, uncited. |
| *"Operator decision: ENQUEUE ALL AT ONCE"* | `phaze-kj8dl` | Undated in the bead. The **source** comment for the same decision (`services/reanalysis_backfill.py:127`) *is* dated 2026-08-11 and the two agree, so this is a citation defect rather than a provenance one. |
| *"WHEN it recomputes is an operator decision"* | `tasks/controller.py:401` | Not a claim that a decision was *made* — a statement that the choice belongs to the operator at runtime. Reads as an attribution on a grep; is not one. Worth rewording. |

### Traceable, and the models to copy

| claim | why it holds |
| --- | --- |
| `phaze-w55w1` — *"Operator decision 2026-08-11 (recorded in ADR-0007…)"* | Cites a durable record that itself states recommendation, decline, decision, date and conditions. **ADR-0007 §7 is the best operator-decision record in the repo.** The corroborating artifact exists too: `phaze-dx9al.2`'s state-change reason is *"Operator decision at review: remove the caps entirely (Option A extended)"*. |
| `phaze-b62ri` (2026-08-20) | Question as put, answer quoted, date, explicit scope limit, and a separately-labelled dispatcher inference. Assessed in §6 below. |
| `phaze-0jpe.6` — chromaprint retained permanently | Dated 2026-07-29, bead-cited, and consistently stated in four places (`Dockerfile:73`, `CLAUDE.md` ×2, `ADR-0002`). |
| `phaze-d4eiq` — no `[tool.uv] exclude-newer` | Dated 2026-08-03, bead-cited, and **enforced by a test that asserts the citation survives**: `tests/shared/test_no_exclude_newer_cooldown.py:63` fails if the comment stops naming the date. This is the only operator decision in the repo with a machine-checked citation, and it is the pattern R2 below generalizes. |
| `phaze-g84sk.2` | Records what the operator **declined** and on what rationale — *"explicitly declined 'failed only' with exactly this rationale presented"*. Recording the rejected option is what makes a later scope challenge decidable. |
| `phaze-ljiee` / `phaze-mrnjq`, `phaze-5fta` / `config.py:867`, `phaze-pw7v` | Dated, and either bead-cited or accompanied by the reasoning that was put to the operator. |

### The honest summary

Almost every operator-decision claim in this repo is **dated**, which already makes it auditable in
principle, and the substance of the decisions is not in question anywhere except `phaze-3ea41`.
Almost none records the **question as put**. `phaze-3ea41` is the only *confirmed* false
attribution — but it is not an isolated slip: it is six propositions, three of which reached
production source and fifteen code sites, and one of which propagated into the incident bead. The
gap that let it happen — that a commit message and a PR body are enough to establish operator
authority — is open for every one of the 42 provenance claims in the tree.

______________________________________________________________________

## 6. Does the `phaze-b62ri` format hold up?

It was written to this bead's standard on 2026-08-20 and is currently being followed by a developer,
so the assessment matters. **Yes, with one gap — and the gap is visible only because the format
made it visible.**

What it does correctly, all five: question as put; answer quoted verbatim (*"lock to the latest
ffmpeg 8.x, same as in github workflows"*); date; an explicit scope limit (*"it decides acceptance
criterion 3 … and NOTHING ELSE. It does not decide the pinning MECHANISM, the guard shape, or the
doc wording"*); and a separately-labelled inference (*"NOT AN OPERATOR DECISION (dispatcher
inference, flagged as such)"*), plus a third register for dispatcher-verified fact.

**The gap.** The block headed `RESOLVES TO:` — resolving *"latest 8.x"* to a specific build,
`ffmpeg-n8.1.2-44-g7c533d0f86-linux64-gpl-8.1.tar.xz` at release tag `autobuild-2026-08-17-13-05`
with its SHA-256 — sits in the same neutral authority register as the answer itself, and it is not
the answer. *"Latest 8.x"* and *"the exact snapshot CI pins today"* are different propositions: they
agree at this instant and diverge the next time either moves. That is the same
two-propositions-fused shape as Finding 1, three orders of magnitude smaller and caught immediately,
because the resolution is stated explicitly and is checkable rather than absorbed silently.

**Recommendation:** label it `RESOLVED BY DISPATCHER` alongside the other flagged registers. One
word, and the note becomes fully self-auditing. No other change; the format works, and the fact that
its one weak spot is legible at a glance is the argument for it.

______________________________________________________________________

## 7. Recommendations needing code changes — specified for filing, not done here

`phaze-u8qj0` changes no product code. These are specified to be filed.

- **R1 — Strip the false attributions and correct the record.** Remove or relabel "operator
  decision" at the sites listed in §5 as untraceable: `src/phaze/services/video_audio.py:29`, `:32`,
  `:245`, `:250`, `:300`, `:331`, `:376`; `src/phaze/job_runner.py:492`;
  `src/phaze/tasks/functions.py:329`; and the eight test docstrings named. Replace with the
  implementer's-decision framing, and add to `D-09` one sentence recording what the operator was
  actually asked and answered on 2026-08-14. Comment-only; no behaviour changes. **P2.**
- **R2 — A citation check for the attribution vocabulary.** A test in the shape of
  `tests/shared/test_no_exclude_newer_cooldown.py`: any tracked file asserting an operator decision
  must carry an ISO date and a bead id within the same paragraph, against a small explicit allowlist
  for the UI-domain uses in §5. Cheap, greppable, and it makes G2 mechanical rather than cultural.
  **P2.**
- **R3 — A corpus-distribution helper for bounds.** A `just` recipe or `scripts/` probe answering
  *"what fraction of the corpus exceeds \<duration | size\>"* against `files.duration`, so G3's
  distribution clause costs one command. `phaze-1b39` is the entire justification. **P3.**
- **R4 — Inventory the remaining producer→consumer artifact seams.** `phaze-l832u.3` closed the
  extraction → analysis seam. The same shape exists wherever one component writes an artifact
  another reads across a process or format boundary — cloud staging push/pull into analysis, tag
  writes read back by `mutagen`, CUE generation. Enumerate them and record, per seam, whether a
  test crosses it with the real consumer. Investigation first; guards filed from the result. **P2.**
  **Done** — `phaze-d2hgv.6`,
  [`docs/spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md`](../spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md).
  30 seams; the three named above are all uncrossed or proxy-only, and the sweep additionally found
  a **live** whole-archive instance of the `phaze-3ea41` shape that the three candidates did not
  point at. §7 of the inventory lists the guards it proposes filing.
- **R5 — `bh work submit` names a test per acceptance criterion.** Mechanizing G1: submit prompts
  for, or refuses without, a criterion→test (or criterion→operator-amendment) mapping. Lowest
  confidence of the five — it touches the `bh` toolchain rather than this repo, and a mapping that
  is easy to fill in badly may buy less than it costs. File as a **decision** bead, not a task. **P3.**

______________________________________________________________________

## Sources

Every claim above traces to one of these. Where a quotation appears, it was read from the artifact,
not from a summary of it.

- **Commits:** `dd7339bb` (feat, phaze-3ea41 — message enumerates the six "operator-confirmed"
  decisions; changes three source files and no tests), `d4524c88` (its test commit),
  `bab5f32b` (PR #424 merge), `e8ee450a` (`phaze-l832u.1`), `e2be4ebb` (`phaze-l832u.3`),
  `47b5e0ce` (`phaze-l832u.2`), `6fca5681` (`phaze-1b39`).
- **PR #424** — body, merged 2026-08-12.
- **Decision records:** `src/phaze/services/video_audio.py` `D-09` / `D-10` (at `dd7339bb` and at
  HEAD); `src/phaze/tasks/reconcile_cloud_jobs.py:93`, `:211` (`phaze-202e`).
- **Beads:** `phaze-3ea41` (title, description, acceptance criteria, `comment_count: 0`, close
  reason), `phaze-l832u` + `.1` `.2` `.3`, `phaze-1b39`, `phaze-202e`, `phaze-b2qs9`,
  `phaze-u1n7j`, `phaze-w55w1`, `phaze-dx9al.2`, `phaze-tzy6s.13`, `phaze-fk1ww`, `phaze-b62ri`,
  `phaze-d4eiq`, `phaze-g84sk.2`, and the 56-bead inventory sweep of `.beads/issues.jsonl`.
- **ADRs:** `docs/design/0007-windowed-analysis.md` §7 (the decision and its conditions) and §8
  (the measured refutation); `docs/design/0011-bug-hunt-cadence.md` (precedent for this document's
  form); `docs/design/0005-analyze-job-memory-limits.md` (the 4Gi limit).
- **Spikes:** `docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md`,
  `docs/spikes/phaze-u1n7j-vox-fix-verification.md`.
- **Tests:** `tests/analyze/services/pipeline/test_video_audio.py` (at `d4524c88` and at HEAD),
  `tests/analyze/services/pipeline/test_extraction_analysis_handoff.py`,
  `tests/analyze/services/backends/test_kube_staging.py:169-186`,
  `tests/shared/test_no_exclude_newer_cooldown.py`.
- **`CLAUDE.md`** — the mocked-essentia lesson as recorded for `D-09`.
