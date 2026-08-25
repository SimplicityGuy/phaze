# ADR-0012 — Verification fidelity, and what may be called an operator decision

| | |
| --- | --- |
| **Status** | Accepted — decided 2026-08-20 |
| **Date** | 2026-08-20 |
| **Amended** | 2026-08-21 (`phaze-d2hgv.3`) — §1, §4 G2, §5 (incl. the new §5.1), §6 and §7 R1 corrected against a recovered primary record. See *Amendment — 2026-08-21* immediately below. §2, §3, and the substance of all five guardrails are unchanged.<br>2026-08-21 (`phaze-d2hgv.10`) — the `phaze-3ea41` site count corrected a second time, from 19 to **20**, after a wrap-tolerant sweep found two claims that no line-scoped search can return; §5's extraction-locality verdict corrected; §5's tree-wide figures restated as **floors**. See *Second correction — 2026-08-21* below. |
| **Bead** | `phaze-u8qj0` |
| **Applies to** | every bead that changes a production path: what an acceptance criterion obliges, what may be attributed to the operator, and what counts as having verified a claim |
| **Enforced from** | `CLAUDE.md` → *Acceptance criteria, attribution, and verification fidelity* (the five rules, immediately above *Beadhive Workflow Enforcement*) |

## Reading the corrections in order

This document has been **corrected** twice since it was accepted, both on 2026-08-21, and the same
figure — how many code sites the `phaze-3ea41` attribution occupies — carries a different value in
each. They are a sequence, not independent retractions, and each value is larger and better sourced
than the last. *(`phaze-d2hgv.5` and `.6` also amended the file that day, but with **additions** —
a delivered corpus-distribution helper in G3, and the artifact-seam inventory G3 was owed. Neither
touches a figure below.)*

| stated | value | by | why it changed |
| --- | --- | --- | --- |
| original, 2026-08-20 (`phaze-u8qj0`) | *"fifteen code sites"* | — | Never sourced. It is not reconstructible from the sites §5 itself lists, so it was corrected rather than defended. |
| first correction (`phaze-d2hgv.3`) | **18** — 9 source + 9 test | a recount at HEAD, using §5's own line-scoped regex | Three `test_video_audio.py` sites had been double-counted into the format-scope row, taking that row's test count from 8 to 6 and the full set from 8 to 9. |
| second correction (`phaze-d2hgv.10`) | **20** — 10 source + 10 test | a recount with a **wrap-tolerant** pattern | A line-scoped regex cannot see a claim split across a line break. Two were: `services/video_audio.py:83` and `tests/…/test_video_audio.py:709`. |

A further value, **19**, was produced between the last two rows and never landed here. It is the figure the
second correction was filed to install, and it is recorded rather than quietly dropped because of
how it arose: the sweep that produced it repeated the exact defect it was correcting. That is the
subject of the *Second correction* section, and §5.1 tabulates it alongside the other three
instances of the same shape.

Every figure in this document now names the search that produced it, and §5's tree-wide totals are
labelled **floors** rather than counts. That is the standing obligation §5.1 states, applied first
to this document.

## Amendment — 2026-08-21: the primary record existed, and §5 had not looked at it

**What was found.** Claude Code session transcripts preserve every `AskUserQuestion` exchange in
full — the question as it was put, every option that was offered with its label *and* its
description, and the operator's selection, each with a timestamp. That is the **primary record**
of an operator answer — the artifact every "operator decision" claim in this repo is ultimately a
report *of* — and it existed the whole time. §5's sweep did not
consult it, because those answers are stored as **tool results**: they are invisible to a grep of
bead comments, invisible to a grep of tracked source, and invisible to a grep of the operator's
own typed turns. Recovered 2026-08-20 from dispatcher session `60b8bf47`.

**What changes, and what does not.**

1. **§1 Finding 1 stands, and gets stronger.** Its conclusion — that a narrow answer was
   generalized and then stamped with the operator's authority — is unchanged. What changes is the
   evidence for it: the finding no longer rests on the operator's 2026-08-14 recollection of what
   was asked. The verbatim question, put 2026-08-12, was *"which **video** containers should the
   analyze lane accept?"*, and the unconditional remux of every bare-audio file is demonstrably
   outside it.
2. **§5's inventory was wrong on four rows, and its heading was wrong about all nine.** Track
   selection, *"log the other streams' existence"* and extraction locality are **genuine operator
   decisions with recorded answers**, and `phaze-kj8dl` has one too; `phaze-6r39` moves from
   *undated, uncited* to traced against an operator utterance quoted verbatim. Six of the nine
   rows have a recovered primary record, and the table grows to ten rows because one of them had
   to be split (see its note). One row — `phaze-ldvmy` — survived a full sweep untraced and stays
   a genuine citation defect, with the sweep now recorded so nobody repeats it expecting a
   different answer.
3. **§7's R1 rested on a false premise and is rewritten.** It prescribed stripping the operator
   attribution from every `phaze-3ea41` site and relabelling it as the implementer's. Executed as
   written, that would have **made the record worse**, demoting three genuine operator decisions.
   R1 now prescribes repair-with-citation, and strips only the one proposition that is genuinely
   unattributable.
4. **§4's G2 verdict on `phaze-3ea41` is refined** from *"none is citeable"* to *"none carried a
   citation"*. The distinction is the whole amendment. G2's field 4 gains a clause naming the
   transcript as the *primary* record to copy fields 1–3 from — and stating why it is still not a
   substitute for the durable record field 4 demands.
5. **§5.1 is new.** It states, in the general form G5 requires, what the sweep actually
   established and what it could not.
6. **§6 gains a third instance** of the fused-propositions shape it already assesses: the
   label-versus-description split on *"Default/first track"*.

**Which counts changed.** *This paragraph was itself corrected the same day by
`phaze-d2hgv.10`; the figures below are the corrected ones, and each shows what it superseded.
The Second correction sub-section immediately after this one is why.*

§5's population paragraph keeps its numbers — **2,892** beads, **55** code-surface matches across
**31** files, **13** non-attribution uses, **42** provenance claims, 9 `docs/` matches across 8
files, 75 bead matches across **56** beads, **79** `.planning/` matches across **48** documents —
but the tree-wide figures among them are now labelled **floors, not counts**: the search that
produced them cannot see a claim whose two words fall on either side of a line break, and three
such claims are known to exist outside the sites this amendment audits. §5 states which search
produced each figure and what that search could not return. Three counts of `phaze-3ea41`'s **own**
sites do change:

- §5's format-scope row said **8 test docstrings** and named five test files. Verified at
  `d0805b02`: the format-scope claim appears in **six** test sites across **four** files. The
  fifth file, `tests/analyze/services/pipeline/test_video_audio.py`, carries sites belonging to
  the *track-selection* and *log-the-other-streams* rows that were double-counted into the
  format-scope row. The full `phaze-3ea41` set of test sites across all three propositions is
  **ten** — the figure went 8 → 9 → **10**, and the Second correction below names the tenth and
  says why two successive sweeps could not see it.
- §7 R1's *"the eight test docstrings named"* becomes **ten**, for both of those reasons.
- §5's honest summary said the three propositions reached *"fifteen code sites"*. Counted at
  `d0805b02` against a **wrap-tolerant** pattern, it is **ten** source sites plus **ten** test
  sites — **20**; the figure went 15 → 18 → **20**. It is also **four** propositions rather than
  three, because extraction locality turns out to be in source too. Fifteen is not reconstructible
  from the sites §5 lists; it is corrected rather than defended.

One date changes: R1 asked for a `D-09` sentence recording what the operator answered *"on
2026-08-14"*. 2026-08-14 is when the operator **recalled** the exchange; the exchange itself is
2026-08-12, and that is the date a citation must carry.

**This document demonstrated its own thesis.** §5 is an argument — *these nine claims have no
recorded answer* — and it was verified against a proxy that structurally could not exhibit the
failure: a grep over bead comments, tracked source and user turns, in a world where the answers
live in tool results. The grep came back clean, and clean was read as confirmation of the
argument rather than of the proxy. That is step 2 and step 3 of the mechanism in §3, run at full
scale, by the document defining the mechanism, three days after it defined it. An inventory that
concluded *"not traceable"* was reporting *"not traced"*. This paragraph is left standing
deliberately: it is the most persuasive evidence in the file that the guardrails below are
addressed at a failure mode nobody is above, including whoever is reading this.

### Second correction — 2026-08-21 (`phaze-d2hgv.10`): the count is 20, and the correction's own verification was blind in the way it had just diagnosed

**The number.** The `phaze-3ea41` attribution occupies **twenty** code sites — **ten** in source
and **ten** in tests. Enumerated at `d0805b02`, the commit this molecule branched from, so the line
numbers are the ones the earlier recounts used and predate the repair beads moving them:

| | sites |
| --- | --- |
| **source — 10** | `services/video_audio.py:29`, `:32`, `:83`, `:245`, `:250`, `:300`, `:331`, `:376`; `job_runner.py:492`; `tasks/functions.py:329` |
| **tests — 10** | `test_job_runner.py:61`, `:463`; `test_phase101_e2e.py:65`; `test_process_file_scratch.py:66`; `test_functions.py:95`, `:462`; `test_video_audio.py:207`, `:324`, `:339`, `:709` |

Two secondary corrections fall out of enumerating them individually. Within `video_audio.py` the
format-scope claim occupies **four** sites (`:29`, `:32`, `:250`, `:300`), not the *"×3 sites"* §5's
row states — and the row is corrected below. And *"docstrings"* is loose for the test side:
`test_video_audio.py:207` is a section-banner comment rather than a docstring, so these are counted
as **test sites**.

**The two that were invisible, and why each was invisible.** This is the substance; the arithmetic
is not.

- **`services/video_audio.py:83`** — the phrase wraps: *"…no audio-push plumbing) -- **operator**"*
  ends line 83 and *"**decision** (phaze-3ea41)"* begins line 84. No line-scoped regex can match
  it, at any vocabulary. Its content is the **extraction-locality** claim — and §5 assessed that
  claim as *"not repeated in source, so lower blast radius"*. That verdict is **false**: the claim
  sits in a module-level decision-record heading, which is among the highest-visibility places an
  attribution can live in this repo. The row and its blast-radius assessment are both corrected in
  §5, not just the count.
- **`tests/analyze/services/pipeline/test_video_audio.py:709`** — **two independent blind spots**,
  and this is the sharpest part of the finding. Its docstring reads *"…the disposition.default
  preference (**operator**"* / *"**decision**) against genuine ffprobe JSON…"*, so it wraps; **and**
  it names no bead id, so it is equally invisible to a `phaze-3ea41` grep. Either property alone
  would have left it findable by the other search. Two partial searches, each blind in a different
  dimension, intersected to hide one site completely.

**Instance 4 of the §3 mechanism, and why it is the most valuable one.** *"19"* — the figure this
correction was originally filed to install, and the reason its bead title still says nineteen — came
from a verification run by the same author who had, minutes earlier, diagnosed the line-scoping
defect in the previous sweep and reported it as the finding. That author then checked their own
repair with a **line-scoped** pattern, and was therefore structurally blind in precisely the way
they had just described. **Knowing the lesson conferred no immunity; only changing the search did.**
That is the strongest evidence in this document for guardrail G5, whose whole claim is that a lesson
recorded at its own site does not transfer — and here it failed to transfer across one hour, in the
same author's very next step. §5.1 carries all four instances as a table and states the general
form.

**What this sweep ran, so the next one can be compared against it.** Whole-file (not per-line),
case-insensitive:

```
operator[-\s#*>/]{1,40}(decision|confirmed|approved|directed|granted|chose|ruling)
```

The separator class contains `\n`, so the pattern spans line breaks, and it admits the `#`, `*`,
`>` and `/` glyphs a wrapped comment or docstring line begins with.

**Run over three scopes, and reported separately.** §5's populations are not one corpus, and a
single figure spanning all of them would be exactly the conflation this document exists to object
to. At `d0805b02`:

| scope | wrap-tolerant | line-scoped | delta |
| --- | --- | --- | --- |
| **code surfaces** — `src/`, `tests/`, `scripts/`, `alembic/`, `Dockerfile*`, `CLAUDE.md`, `justfile` (§5's audited population; 34 files carry a match) | **63** | 58 | **5** |
| `docs/` | 42 | 41 | 1 |
| `.planning/` | 82 | 80 | 2 |
| **whole tree** | **187** | **179** | **8** |

**63 is the code-surface figure, not the tree's** — the tree's is 187, and reading one for the other
is a threefold error. The five code-surface deltas are the two `phaze-3ea41` sites named above plus
the three sites §5's inventory never listed, which is why §5's totals are now marked as floors.

The other three deltas move no §5 verdict, and are still worth naming. The `docs/` one is **this
ADR itself**: its pre-amendment §7 R1 wrapped *"operator"* / *"decision"* across lines 482–483, so
the sweep could not see the very sentence it was written in. The two in `.planning/` sit in the
archive §5 counted and deliberately did not audit, so they change nothing it concluded. Together
they establish that the wrap blindness was **tree-wide** rather than a property of the audited
population — which strengthens the floors reading rather than softening it.

**What it still cannot see**, stated so the next sweep is compared against something rather than
trusted: a claim split across lines by more than 40 separator characters; a claim using attribution
vocabulary outside the seven words above; a claim phrased without the word *"operator"* at all; a
claim in an untracked file; and — the one that matters most, because it is the same shape as
instance 2 — anything about **which §5 row** a match belongs to, which no regex decides and which
has now been got wrong once. This enumeration was therefore cross-checked against a `phaze-3ea41`
bead-id grep, which returns a *different* incomplete set: the two searches disagree on
`test_video_audio.py:709`, and neither would have found it alone.

______________________________________________________________________

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

The question is recoverable verbatim, and this is the primary record: a single `AskUserQuestion`
call in dispatcher session `60b8bf47`, put at **2026-08-12T00:30:59Z** and answered at
**00:31:36Z**. Three of its questions bear on this inventory; the first is Finding 1's:

> **Q — `phaze-3ea41`: which video containers should the analyze lane accept?**
> **A — "Probe-based, any container"** — chosen over the offered alternatives *"Common concert
> formats"* and *"mkv + avi only"*.

Read the question, not only the answer. It asks **which video containers to accept**, and the
three options it offers are three answers to that question: any container, a curated list, or two
extensions. *"Probe-based, any container"* decides that no extension whitelist gates **video**
acceptance — `ffprobe`, not a maintained list, is the authority on whether a file has an audio
stream. That is exactly what the code still does, and it is a good decision.

What shipped was a materially wider proposition: that pre-analysis extraction runs
**unconditionally on every file**, remuxing all audio — including files that were never video
containers at all — through `ffmpeg` into a Matroska (`.mka`) scratch file before analysis. No
option on the question said that, and no question was ever asked about bare audio. The operator
confirmed the same reading from memory on 2026-08-14; the transcript makes it demonstrable
rather than recollected, which is the stronger footing and the reason this section was amended
on 2026-08-21.

The answer and the shipped proposition do not entail one another. "`ffprobe` is the authority on
whether a file has an audio stream" is fully compatible with "and when it reports a plain audio
container, skip the remux" — which is exactly what `phaze-l832u.1` now does, with `ffprobe` still
the sole authority and the extension whitelist still gone. Two separable decisions were fused, and
the fused version inherited the authority of the answer to one of them.

The other two answers from the same call are **not** part of this finding, and are correctly
attributed wherever they are claimed — §5 reclassifies them:

> **Q — where should audio extraction run?** → **A — "Both lanes"**.
> **Q — when a container carries multiple audio tracks, which one gets analyzed?** → **A —
> "Default/first track"**, an option whose description read *"Take the container's
> default-flagged audio stream (falling back to the first), log the others' existence in the
> analysis record."*

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

**Amended 2026-08-21.** That is a statement about the *bead*, and it remains exactly true: the
bead records none of them. It is not a statement about the world. A durable primary record of
three of the six did exist — the `AskUserQuestion` exchange quoted above, answered
2026-08-12T00:31:36Z, **one hour and fifty-eight minutes** before `dd7339bb` was authored at
02:29:42Z — and neither the implementer at submit time nor this ADR's own sweep three days later
went and got it. Under G2 the citation is owed **at the moment the claim is written**, when the
answer is under two hours old and the transcript is the session the author is sitting in. The
defect is not that the answer was unknowable; it is that a claim of
operator authority was published without carrying its source, in a repo where the source was one
lookup away.

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
takes its duration from `_probe_duration_sec`, which at the time read `es.MetadataReader` — and
`es.MetadataReader` returns no duration for Matroska on the deployed platform. Zero duration makes
`_iter_windows` yield nothing, both `*_total` counts land on 0, and the zero-window guard fails the
file. (This record previously named TagLib as the mechanism. That attribution was never verified;
see the platform note below, added by `phaze-gppj2`.)

Measured inside the deployed 2026.8.3 agent image on 2026-08-14 (`phaze-l832u`):

| probe | on the original `.mp3` | on its `.mka` remux |
| --- | --- | --- |
| `es.MetadataReader` duration | 90 | 0 |
| `ffprobe` duration | — | 90.044000 |
| `_decode_windows` | — | 2/2 windows, 1,323,000 samples each |

The audio was intact and essentia decoded it correctly, exactly as the equivalence argument said.

### Correction 2026-08-22 (`phaze-gppj2`): the mechanism above was misattributed

This section previously explained the zero duration by "MetadataReader is TagLib, and TagLib returns
no duration for Matroska". **The incident, the numbers, and this section's whole argument are
unaffected** — the container really did change, the duration really did read 0, and 11,428 files
really were broken by a substituted claim that was true and about the wrong quantity. What was wrong
is the *mechanism*, and it is corrected here because rule 3 below is stated on the strength of it.

Measured inside the deployed image (ffmpeg 7.1.5, real essentia-tensorflow) and reproduced in a CI
runner image: `.mka` → duration 0 / samplerate 0, while `.mp3` and `.m4a` read correctly in the same
container, same binary, same run. The ffmpeg major was ruled out (7.1.5, 8.1.2 and 9.0.1 all mux a
`.mka` that macOS reads fine), and so was codec support. **The variable is the essentia wheel** — the
linux x86_64 build cannot read Matroska metadata and the macOS arm64 build can, and both CI and
production run the linux one. Which component *inside* that wheel is responsible was never
established; TagLib is a suspect, not a finding.

Two things worth keeping from how this was caught, both of which are this ADR's own rules turned on
its own text:

- It surfaced because a bead was required to verify against the artifact's **real consumer**. A
  developer on macOS measured `es.MetadataReader` reading the `.mka` duration *correctly* — the exact
  opposite of what this record claimed — and filed the contradiction instead of quietly trusting the
  document. Rule 3 caught an error in the section that argues for rule 3.
- The fix that followed did **not** make the test tolerant of duration 0. It asserts the production
  contract (`ffprobe` reads the duration) *and* pins `MetadataReader == 0` on linux, so a future
  essentia wheel that gains Matroska support fails loudly and D-10 is revisited deliberately rather
  than silently.

`es.MetadataReader` still returns 0 for Matroska on the deployed platform, so the guards this
incident produced remain load-bearing.

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

*Added 2026-08-21 (`phaze-d2hgv.10`).* This mechanism has a sibling that operates on **searches**
rather than on tests, and this document produced four instances of it while auditing itself: a
search whose form cannot return part of what it enumerates, whose clean result is read as a fact
about the world. §5.1 tabulates all four — including the one where the correction to a
line-scoping defect was itself verified with a line-scoped pattern — and states the general form.

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

*Added 2026-08-21:* the **primary** record of an `AskUserQuestion` answer is the session
transcript, and it is where fields 1–3 should be copied **from**, at the moment of writing. It is
not a substitute for field 4: transcripts are local, untracked, and carry account names in their
paths, so the obligation is still to transcribe the exchange into a bead comment or an ADR
section that a future reader can actually reach. Citing by session id and timestamp — as §5.1 and
the Sources section do — is the supported form.

The attribution extends no further than the question asked. When implementation reveals a second
decision inside the first — here, *which video containers to accept* versus *whether bare audio is
remuxed at all* — that is a new question, not a corollary. **The symmetric rule also holds:** a decision may not be narrowed
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
| `phaze-3ea41` | **Would have caught.** Six bullets stamped operator-confirmed, and **not one of them carried a citation**. (Amended 2026-08-21: the original text read *"none is citeable"*, which the recovered transcript refutes — three of the six had a real answer, under two hours old at commit time. Not citeable and not cited are different failures, and only the second one happened.) G2 bites either way, because it is owed at the moment the claim is written: producing the four fields would have taken the author back to the exchange, where three bullets survive with a citation and the format-scope bullet cannot be written as an operator decision at all — it would have appeared as what it was, an implementer's scope choice, open to challenge. |

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
  below is the argument for adding it. `just corpus-distribution <duration-sec> [size-bytes]`
  (`scripts/corpus_distribution.py`, `phaze-d2hgv.5`) is that query, made one command: it reports
  the population it measured and the fraction of it exceeding a duration and/or size bound, read
  against the real `files`/`metadata` tables. Run it at the moment a bound is being picked.

| incident | verdict |
| --- | --- |
| `phaze-1b39` | **Would not have caught, as drafted. Would have caught, with the distribution clause.** The claim "3 h bounds hung pods without cutting real work short" is a claim about the corpus's duration distribution, and no test at any fidelity can hold it — CI cannot run a 6-hour analysis. What *could* have discharged it is one query against `files.duration` asking how much of the archive exceeds 3 hours. The answer was 2–6 hour sets, in quantity. That is the cheapest check in this document and the only thing on this list that would have stopped `1b39` at review. |
| `phaze-b2qs9` / `u1n7j` | **Would have caught.** The consumer is real essentia's streaming network; `test_analysis_long_file.py` measured a mock. ADR-0007 §8 states the outcome in its own table: *"it proves that of a **mocked** essentia. On real essentia the same comparison moves **gigabytes**."* |
| `phaze-3ea41` | **Would have caught — and only this rule would.** The consumer of `extract_audio_track`'s output is `analyze_file` → `_probe_duration_sec`. No test crossed that boundary; the real-`ffmpeg` tests stopped at `probe_audio_streams`, i.e. at `ffprobe` checking `ffmpeg`'s own output. `tests/analyze/services/pipeline/test_extraction_analysis_handoff.py` is what G3 demands, and `phaze-l832u.3` records that every test in it fails against the pre-fix code. |

#### The qualification: an INDEPENDENT consumer is not automatically a DISCRIMINATING one

*Added 2026-08-25 (`phaze-e38cg`), from `phaze-wt9vw`.* G3 as written above says **which** tool not
to verify with — the producer. It does not say which tool to verify **with**, and the obvious
substitution a diligent reader makes is "any genuinely different implementation". That is not
sufficient, and this is the case that shows it: **applied honestly and in full, G3 would still have
failed on `phaze-wt9vw`.**

The defect was a `.wma` tag write taking `_write_vorbis`'s catch-all and putting literal Vorbis key
names into an ASF file. The producer is mutagen, so G3 says stop reading the file back with
mutagen — and the nearest independent reader to hand is `ffprobe`, a different implementation by
different authors, in a different language, shipped by a different project. Measured against the
pre-fix code, `ffprobe` reported `TAG:artist=Test Artist` for the **wrong** file as readily as for
the right one, because ffmpeg's ASF demuxer passes unknown extended-content-description attributes
through verbatim. It differed on the two writes only in an incidental detail (`TAG:tracknumber` for
the bogus write against the mapped `TAG:track` for the correct one) — nothing an assertion about
`artist` would ever have seen.

The cause is a property of the **consumer**, not of the seam. ffmpeg's ASF demuxer is permissive,
and surfacing unknown attributes is good behaviour for a probe and fatal for an oracle: **a reader
that accepts anything can tell you an artifact is parseable, never that it is correct.**

So G3's instruction to name the artifact's real consumer carries a second question, and the second
one is the one that bites:

> Not *"is this a different implementation?"* but *"would this tool have **rejected** the wrong
> artifact?"* — and the cheap way to find out is to feed it one.

On `phaze-wt9vw` that check was actually performed, before the key map was written: all six
candidate ASF attribute names were written to real ffmpeg-produced containers, one per file, and
each file handed to both readers. `es.MetadataReader` discriminated completely; `ffprobe` did not
discriminate at all.

`es.MetadataReader`, on the two `.wma` writes (recorded in that test module's docstring):

```
phaze-written .wma  ->  ('', '', '', '', '', '', '')          # every field EMPTY
spec-correct .wma   ->  ('Real Title', 'Real Artist', ...)    # all fields readable
```

`ffprobe`, on the same pair: `TAG:artist=...` for **both**, differing only in `TAG:tracknumber`
(the bogus write) against the mapped `TAG:track` (the correct one). Empty-versus-populated is a
verdict; a differing incidental key is not one, and no assertion about `artist` would have reached
it.

That is why `es.MetadataReader` is the reader in
`tests/review/services/test_tag_write_real_containers.py` and why `ffprobe` is not, and it is the
same probe that derived every entry of `_WRITE_ASF_MAP` — six attribute names measured against a
real consumer rather than read off a spec. Both call sites carry the local form of this lesson (the
module docstring of that test file, and the comment above `_WRITE_ASF_MAP` in
`src/phaze/services/tag_write_disk.py`); this section is its general form, written here because G5
requires exactly that and because an unevidenced rule decays into folklore the moment its author
leaves the room.

**This qualifies G3; it does not replace it.** G3 remains the rule that gets you off the producer
and onto the artifact's real consumer; the qualification is what stops that substitution from
landing on a consumer that cannot fail. Usually they agree, because the real consumer is also the
strict one for the same reason it is the consumer at all — it has to do something with the value.
Where they pull apart, a verifier satisfying only one of the two is a **finding to escalate**, not
a choice to make quietly: G4's shape applies, and "the reader I used could not have rejected the
wrong artifact" is the sentence to write in the bead.

| incident | verdict |
| --- | --- |
| `phaze-wt9vw` | **Would have caught, and G3 alone would not.** The `ffprobe` substitution is the move G3 licenses and this qualification forbids: asked whether `ffprobe` would have rejected the wrong `.wma`, the answer is measured and it is no. The check that answers it — write the wrong artifact, hand it to the candidate reader — cost one ffmpeg invocation per candidate. |
| `phaze-3ea41` | **Would not have caught. Plain G3 catches that one.** There `ffprobe` *was* discriminating about the property in question: it reads Matroska duration correctly, and would have rejected a container whose duration was wrong. Its failure was that it was not the consumer that mattered — `es.MetadataReader` was, and it reads no duration from Matroska on the deployed platform. Discrimination was never the gap; consumer identity was. |
| `phaze-1b39` | **Would not have caught.** No artifact seam is involved. The claim was about the corpus's duration distribution, discharged by a query and not by any reader, strict or permissive. |
| `phaze-b2qs9` / `u1n7j` | **Would not have caught.** No artifact seam either: the proxy was a mocked essentia standing in for the real streaming network, which plain G3 already refuses. A mock is not a permissive consumer — it is not the consumer at all. |

Read the two negative verdicts on `1b39` and `b2qs9` as the boundary of the qualification rather
than as weakness: it says nothing about changes that produce no artifact, and it is not owed on
them.

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

*Added 2026-08-21 (`phaze-d2hgv.10`).* The strongest evidence for G5 arrived after it was written,
and from this document rather than from the incidents: §5.1 records the same search-shape defect
occurring **four** times inside the `phaze-d2hgv` molecule, the fourth being the correction to the
third, written by an author who had diagnosed the third within the hour. G5's claim is that a
lesson recorded at its own site does not transfer. Instance 4 is that failure to transfer measured
at its shortest possible range — one author, one hour, one document — which is a stronger form of
the claim than "three teams over four days" and is the reason G5 stays.

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
export. **Two, and that was the defect** — §5.1 records the third population this sweep did not
search, why no grep of these two could have contained the answer, and what the verdicts below
looked like before it was searched.

> **Every tree-wide figure in this section is a floor, not a count** *(added 2026-08-21,
> `phaze-d2hgv.10`)*. They were produced by a **line-scoped** regex,
> `operator[- ](decision|confirmed|approved|directed|granted|chose|ruling)`, which structurally
> cannot return a claim whose two words fall on either side of a line break. Re-running the same
> vocabulary with a **wrap-tolerant** pattern over the same code-surface population returns
> **63** matches where the line-scoped one returns 58. *(58 rather than the 55 below is a day's
> tree drift between the two sweeps, not a correction — which is itself why this note marks the
> figures as floors instead of restating them.)* Of the **five** matches only the wrap-tolerant
> pattern can return, two are `phaze-3ea41` sites the *Second correction* names, and the other
> three are attribution sites in `src/` that this inventory does not list at all:
> `routers/pipeline/tracklists.py:213` (a genuine, previously uninventoried citation defect),
> `services/date_convention.py:18` (substantively fine — its paragraph carries both 2026-08-04 and
> `phaze-5fta`, which this section already lists among the traceable — but invisible to the sweep
> nonetheless) and `tasks/filename_convention.py:6` (a second instance of the runtime-choice false
> positive flagged below only at `tasks/controller.py:401`). All three are repaired by
> `phaze-d2hgv.2`. The figures below are **not** restated as a new precise number, because this
> amendment has not audited the whole tree either; they are marked as floors and the search that
> produced them is named. The wrap-tolerant pattern and what *it* cannot see are recorded in the
> *Second correction* above.

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

### The claims that carried no citation

> **This table was corrected on 2026-08-21** (`phaze-d2hgv.3`). It was headed *"Not traceable to
> any recorded answer"*, and that heading was wrong about all nine of its rows: it asserted a
> property of the world on the strength of a sweep that had not searched where the answers are
> kept. **Six of the nine have a recovered primary record.** The verdicts below are the corrected
> ones, and each says what changed. There are now ten rows rather than nine, because
> *"extraction locality / disk headroom / liveness"* had to be split: the first was decided by the
> operator and the other two were explicitly delegated to the developer, so a single verdict
> could not be right about them. The one thing every row still has in common — the thing that was
> always the finding — is that **none of them carried a citation at the point of assertion**.

| claim | asserted in | status |
| --- | --- | --- |
| **`phaze-3ea41` format scope** — extraction runs on every file | `services/video_audio.py` (×4 sites: `:29`, `:32`, `:250`, `:300`), `job_runner.py:492`, `tasks/functions.py:329`, and **6** test docstrings across `tests/analyze/core/test_job_runner.py` (`:61`, `:463`), `test_phase101_e2e.py:65`, `test_process_file_scratch.py:66` and `tests/shared/tasks/test_functions.py` (`:95`, `:462`); plus commit `dd7339bb` and PR #424 | **Confirmed false — and now demonstrably so.** Finding 1. The question put on 2026-08-12 was *"which **video** containers should the analyze lane accept?"*, answered *"Probe-based, any container"*. That licenses probe-based acceptance of video containers, which the code still does; it does not license remuxing bare audio. The operator authority here is real but **narrower than the claim it was attached to** — strip the attribution from the unconditional-remux proposition, keep it on probe-based detection. `phaze-3ea41` still has zero comments. *(Corrected 2026-08-21: the site count was **8 test docstrings across five files**, which double-counted `test_video_audio.py`'s sites from the two rows below. Corrected again the same day by `phaze-d2hgv.10`: the source-site count for this row was **×3**, one short — `:250` and `:300` are both format-scope sites and both were in §7 R1's list all along.)* |
| **`phaze-3ea41` track selection** — prefer `disposition.default` | `services/video_audio.py:245`, `:331`; `tests/analyze/services/pipeline/test_video_audio.py:207`, `:324`, **`:709`**; commit and PR | **Traced. A genuine operator decision, correctly attributed.** Asked 2026-08-12T00:30:59Z — *"when a container carries multiple audio tracks, which one gets analyzed?"* — answered *"Default/first track"* at 00:31:36Z. *(Corrected 2026-08-21 from "No recorded answer". The bead does name track selection as a "decision to make in-bead"; the decision was then made, by the operator, and the bead was never updated to say so — which is why a bead-scoped sweep missed it. `test_video_audio.py:709` added 2026-08-21 by `phaze-d2hgv.10`: it wraps across lines **and** names no bead id, so a vocabulary grep and a `phaze-3ea41` grep each miss it for a different reason.)* |
| **`phaze-3ea41` log the other streams' existence** | `services/video_audio.py:376`; `tests/…/test_video_audio.py:339` | **Traced, with a distinction that must be preserved.** It was not a separately-asked question. The operator clicked the **label** *"Default/first track"*; the instruction to *"log the others' existence in the analysis record"* rode in that option's **description**. Label and description do not carry identical authority — the label is what was chosen, the description is what the chooser was shown — and a repaired citation should say which is which rather than flattening them. This is the same fused-propositions shape as Finding 1 and as the `RESOLVES TO:` gap assessed in §6, at its smallest scale. *(Corrected 2026-08-21 from "No recorded answer".)* |
| **`phaze-3ea41` extraction locality** — both lanes | **`services/video_audio.py:83`** (the module-level `D-09` record), commit `dd7339bb`, PR #424 (under *"operator-confirmed"*) | **Traced. A genuine operator decision.** Asked in the same call — *"where should audio extraction run?"* — answered *"Both lanes"*. *(Corrected 2026-08-21 from "No recorded answer". Corrected again the same day by `phaze-d2hgv.10`: this row was assessed as "not repeated in source, so lower blast radius", and that was **false**. It **is** in source, at `video_audio.py:83`, in a module-level decision-record heading — one of the highest-visibility places an attribution can sit in this repo, and the first thing a reader of that module meets. It was missed because the phrase wraps: "operator" ends line 83 and "decision" begins line 84, so no line-scoped regex can match it at any vocabulary.)* |
| **`phaze-3ea41` disk headroom / liveness** | commit `dd7339bb`, PR #424 (under the same *"operator-confirmed"* heading) | **No recorded answer, and explicitly delegated.** These were never put to the operator; the dispatch message reserved them for the developer — *"Disk-headroom handling for long sets remains yours to design"*. Where they claim operator authority they are implementer decisions and are relabelled as such. Not repeated in source, so lower blast radius, but stamped in both submit-time artifacts. *(2026-08-21, `phaze-d2hgv.10`: "not repeated in source" holds for **these two** and for these two only — it was verified against the wrap-tolerant enumeration in the Second correction, in which none of the twenty sites is a disk-headroom or liveness claim. It does **not** extend to extraction locality, which shared a row with them before `phaze-d2hgv.3` split it; see the row above.)* |
| *"the operator decision recorded in that bead"* | `phaze-l832u` (epic description) | **Still false about `phaze-3ea41`, on both halves.** No operator decision is recorded in that bead — it has zero comments — and the decision that *was* recorded elsewhere is about video-container acceptance, not about running extraction on every file. The propagation step: the incident bead inherited the attribution while diagnosing the incident it caused. |
| *"keep ubuntu-latest per operator decision"* | `phaze-ldvmy` | **Not traced, after a sweep that now searched the right corpus.** A full pass over the session-transcript corpus on 2026-08-20 — the source §5's original sweep missed — found no matching question and no matching operator statement. This is the one row the amendment does not rescue, and it stays a genuine citation defect: relabel as the implementer's decision, which is what the evidence supports. The substance is not disputed. The negative result is recorded here with its date and method so nobody re-runs the same sweep expecting a different answer. |
| *"Operator decision: this rides the NEXT release"* | `phaze-6r39` | **Traced.** The operator typed it, verbatim, on 2026-08-04T19:19:29Z: *"yes, please dispatch it. we'll deploy it in the next release, so no need to cut a new release for just this. i'll likely have other bugs as I use the updated release."* *(Corrected 2026-08-21 from "Undated, uncited". The claim needed a date and a quote, not a demotion.)* |
| *"Operator decision: ENQUEUE ALL AT ONCE"* | `phaze-kj8dl` | **Traced.** Asked 2026-08-11T19:03:30Z — *"Re-running every previously-sampled file means enqueueing many multi-hour exhaustive analyses. How should the one-time script feed them in?"* — answered *"Enqueue all, let lanes drain"* at 19:04:21Z. The **source** comment for the same decision (`services/reanalysis_backfill.py:127`) is dated 2026-08-11 and agrees, so this was always a citation defect rather than a provenance one; only the bead text needs the citation. |
| *"WHEN it recomputes is an operator decision"* | `tasks/controller.py:401` | Not a claim that a decision was *made* — a statement that the choice belongs to the operator at runtime. Reads as an attribution on a grep; is not one. Worth rewording. |

### Claims that already carry their citation — the models to copy

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
attribution — but it is not an isolated slip: it is six propositions, **four** of which reached
production source and **20** code sites, and one of which propagated into the incident bead. The
gap that let it happen — that a commit message and a PR body are enough to establish operator
authority — is open for **at least** the 42 provenance claims this sweep could see in the tree.

*(Corrected 2026-08-21: this read "fifteen code sites", then "18". Counted at `d0805b02` against a
**wrap-tolerant** attribution pattern, the propositions occupy **ten** source sites —
`video_audio.py:29`, `:32`, **`:83`**, `:245`, `:250`, `:300`, `:331`, `:376`,
`job_runner.py:492`, `functions.py:329` — and **ten** test sites, so **20**. Fifteen is not
reconstructible from the sites this section lists; it is corrected rather than defended. The count
of propositions reaching production source goes from three to four for the same reason: extraction
locality is in source at `:83`, which is why that row's blast-radius assessment is corrected above.
`:83` and `test_video_audio.py:709` are invisible to a line-scoped regex; see the Second
correction for both, and for why the figure has now moved three times.)*

**Amended 2026-08-21 — the sharper gap underneath that one.** Everything above stands. But
"a commit message and a PR body are enough to establish operator authority" is the *permissive*
half of the failure, and the recovered record exposes the other half: **the durable primary
record existed, and every party in the chain treated it as unavailable.** The implementer did not
cite it, under two hours after the exchange, in the session that contained it. The reviewer did not
ask for it. This ADR's own sweep, three days later, searched for it in three places it could not
be and reported its absence as a fact about the world. A claim that could have been made
bulletproof in one line at zero cost instead became, in sequence, an unsupported assertion, a
propagated falsehood, and an inventory verdict that was itself false. §5.1 states the general
form.

### 5.1 What this sweep established, and what it could not — the general form (G5)

G5 obliges a lesson recorded at one site to state its general form or say why it has none. The
lesson this section learned about itself has one, and it is not about transcripts:

> **An absence-of-evidence finding is a claim about the sources it searched, never about the
> world. It names the corpora it swept, names the corpora it could not, and its verdict reads
> "not found in \<these\>" — not "does not exist".** A negative result whose search space is
> unstated is indistinguishable from a negative result whose search space was wrong, which is the
> §3 mechanism with the sign flipped: the proxy that cannot exhibit the failure is now a corpus
> that cannot contain the answer.

**What the original sweep actually ran**, stated so the gap is checkable rather than asserted:

| corpus | searched? | can it contain an `AskUserQuestion` answer? |
| --- | --- | --- |
| tracked files under `src/`, `tests/`, `docs/`, `scripts/`, `alembic/`, `Dockerfile*`, `CLAUDE.md`, `justfile` | **yes** — 55 matches / 31 files | no — only an author's *report* of an answer |
| all 2,892 beads in the local Dolt export (descriptions, comments, close reasons) | **yes** — 75 matches / 56 beads | only if someone transcribed the answer into a comment; `phaze-3ea41` has `comment_count: 0` |
| commit messages and PR bodies | **yes** — `dd7339bb`, PR #424 | no — same, and both are written by the implementer |
| `.planning/` archive | counted, not audited — 79 matches / 48 documents | no — superseded historical prose |
| **Claude Code session transcripts** | **no — the gap** | **yes. This is the primary record**, and the only one: question as put, every option with its label and description, the selection, and timestamps on both. |

The reason the miss was systematic rather than careless: `AskUserQuestion` answers are stored as
**tool results**. A grep of the operator's typed turns does not see them — the operator did not
type anything, they clicked. A grep of bead comments does not see them — nothing writes them
there. A grep of tracked source sees only what an author later chose to write down, which is the
very thing under audit. Three plausible searches, all clean, all structurally incapable of
returning the answer.

**Two consequences for how this repo works, both filed rather than asserted here.** First, the
right time to cite an operator decision is **when it is made**, not when it is audited: `phaze-d2hgv.4`
files a provenance-recovery helper so the four G2 fields cost one command at the moment of
writing. Second, an absence-of-evidence finding in this repo now carries its searched-and-unsearched
list, in the shape of the table above — that is the general form, and this is where it is written
down.

#### The same mechanism, four times, all inside this one molecule

*Added 2026-08-21 (`phaze-d2hgv.10`).* The general form above is about **which corpus** a search
covers. There is a second general form, about **what shape** a search has, and this molecule
produced four instances of it across two days — three of them within hours of each other. Every
one is the same: a search whose form structurally cannot return part of what it claims to
enumerate, whose clean result is then read as a fact about the world.

| # | the search | what its shape could not return | what it reported | truth |
| --- | --- | --- | --- | --- |
| 1 | grep of bead comments, tracked source and the operator's typed turns | `AskUserQuestion` answers, which live in **tool results** | *"not traceable to any recorded answer"* for nine rows | six of the nine had a recovered primary record |
| 2 | attributing each match to a §5 row by hand | that three `test_video_audio.py` sites belong to two *other* rows | *"8 test docstrings"* for the format-scope row | six for that row — and the cross-proposition total it produced, *"nine"*, was itself superseded by rows 3 and 4 to **ten** |
| 3 | line-scoped `operator[- ]…` regex | a claim wrapped across a line break | *"nine source sites"*, and *"extraction locality is not repeated in source"* | **ten** source sites, and it **is** in source, at `video_audio.py:83` |
| 4 | the correction to (3), verifying itself with a **line-scoped** pattern | the same wrapped claims (3) had just been diagnosed on | *"19"* | **20** — `test_video_audio.py:709` |

**Instance 4 is the one to keep.** Instances 1–3 are a document being wrong about its own
inventory. Instance 4 is the *correction* to instance 3 reproducing instance 3 while fixing it,
written by an author who had, within the hour, diagnosed the line-scoping defect and reported it as
the finding — and who then reached for a line-scoped pattern to verify their own repair. The
general form, in the sentence G5 requires:

> **A search is a proxy for the population it claims to enumerate, and its shape — what it scans,
> at what granularity, over what corpus — fixes what it structurally cannot return. An enumeration
> reports what its search could see, never what exists.** Knowing this confers no immunity;
> instance 4 is the proof. Only changing the search does.

**Why `phaze-d2hgv.7` must stay paragraph-scoped and wrap-tolerant.** R2's citation check
(`phaze-d2hgv.7`) is specified to operate at **paragraph** granularity rather than per line, and to
match the attribution vocabulary across line breaks. `video_audio.py:83` and
`test_video_audio.py:709` are the motivating examples, and they motivate the two halves separately:
a per-line check would not merely fail to find their citations, it **could not see the claims at
all**. Paragraph scope is not a convenience for authors who like long sentences — it is what makes
the check able to observe its own population.

`test_video_audio.py:709` is the pointed case, because it is exactly the kind of site R2 exists to
fail on: it asserts an operator decision and carries no bead id. R2 would have caught it on the
merits — **if** R2 could see it. A line-scoped implementation of the same rule cannot, so it would
have passed the tree while the defect it was written to find sat in it. A future reader who reads
per-line as equivalent-but-cheaper should read this paragraph and the table above first; the
"cheaper" version is the one that returned three different wrong numbers.

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

**The same shape, found again by the 2026-08-21 amendment.** §5's *"log the other streams'
existence"* row is this gap in its native habitat: the operator chose an option whose **label**
read *"Default/first track"*, and the logging instruction rode in that option's **description**.
Both were shown to the operator and the choice covers both, so this is not a false attribution —
but the label is what was clicked and the description is what accompanied it, and a citation that
flattens the two loses the ability to say which half a later challenge is challenging. Three
instances now: Finding 1 (fused and absorbed, cost an outage), `RESOLVES TO:` (fused and
labelled, cost nothing), label-versus-description (fused and small, caught in audit). The
recurring lesson is that **an option is not a proposition** — it is a bundle of them, and the
citation should say which part carried what.

**Recommendation:** label it `RESOLVED BY DISPATCHER` alongside the other flagged registers. One
word, and the note becomes fully self-auditing. No other change; the format works, and the fact that
its one weak spot is legible at a glance is the argument for it.

______________________________________________________________________

## 7. Recommendations needing code changes — specified for filing, not done here

`phaze-u8qj0` changes no product code. These are specified to be filed.

*Filed 2026-08-21 as the `phaze-d2hgv` molecule: R1 → `phaze-d2hgv.1` + `.2` (rescoped to repair,
below), R2 → `.7`, R3 → `.5`, R4 → `.6`, plus `.4` — a provenance-recovery helper that R1's
premise failure showed was missing, `.3`, this amendment, and `.10`, the second correction to
its site counts. R5 is unchanged and still unfiled.*

- **R1 — Repair the attributions and correct the record.** *(Rewritten 2026-08-21. As originally
  written — "**Strip** the false attributions", remove or relabel "operator decision" at every
  site listed in §5 and replace them all with the implementer's-decision framing — R1 rested on
  the false premise corrected above. Executed as written it would have **made the record worse**,
  demoting three genuine operator decisions to implementer decisions and destroying, rather than
  supplying, the provenance the rule exists to protect. That is worth stating plainly: a repair
  prescribed from an un-sourced negative finding is as capable of damaging a record as the defect
  it was aimed at.)*

  The sites are the same; the treatment is per proposition, not per site. Across the **ten**
  source sites — `src/phaze/services/video_audio.py:29`, `:32`, `:83`, `:245`, `:250`, `:300`,
  `:331`, `:376`; `src/phaze/job_runner.py:492`; `src/phaze/tasks/functions.py:329` — and the
  **ten** test sites named in §5 (`tests/analyze/core/test_job_runner.py:61` and `:463`,
  `test_phase101_e2e.py:65`, `test_process_file_scratch.py:66`,
  `tests/shared/tasks/test_functions.py:95` and `:462`,
  `tests/analyze/services/pipeline/test_video_audio.py:207`, `:324`, `:339` and `:709` — eight,
  then nine, were miscounts; see the amendment note and the *Second correction* for why two
  successive sweeps could not see `video_audio.py:83` and `test_video_audio.py:709`):

  1. **Format scope — strip the operator attribution from the unconditional-remux proposition**
     and relabel it as the implementer's. Say what the operator *did* decide — probe-based
     acceptance for video containers, which the code still does. Note that the behaviour is
     already gone (`phaze-l832u.1`), so what remains is stale prose asserting a false provenance
     for a design that no longer exists.
  2. **Track selection, extraction locality — give each a real citation**: question as put,
     answer quoted, 2026-08-12, and a pointer to a durable record. This ADR's §1 and §5 are that
     record; prefer one pointer per site over restating the full exchange at every site.
     Extraction locality's site is `video_audio.py:83`, in the module-level `D-09` heading — the
     highest-visibility of the ten, and the one an earlier draft of §5 believed did not exist.
  3. **"Log the other streams' existence" — cite it as an option description**, not as a
     separately-asked question, and not as an implementer decision either.
  4. **Disk headroom, liveness — relabel as implementer decisions.** They were delegated
     explicitly and never asked.

  Add to `D-09` one sentence recording what the operator was actually asked and answered on
  **2026-08-12** (the original R1 said 2026-08-14, which is the date the operator *recalled* the
  exchange, not the date of the exchange). Comment-only; no behaviour changes. **P2.**
  *Filed as `phaze-d2hgv.1` (the `phaze-3ea41` sites) and `phaze-d2hgv.2` (the four non-`phaze-3ea41`
  claims: `phaze-6r39` and `phaze-kj8dl` gain their recovered citations, `phaze-ldvmy` is
  relabelled with its null sweep recorded, `tasks/controller.py:401` is reworded so it stops
  reading as a provenance claim).*
- **R2 — A citation check for the attribution vocabulary.** A test in the shape of
  `tests/shared/test_no_exclude_newer_cooldown.py`: any tracked file asserting an operator decision
  must carry an ISO date and a bead id within the same paragraph, against a small explicit allowlist
  for the UI-domain uses in §5. Cheap, greppable, and it makes G2 mechanical rather than cultural.
  **P2.** *Filed as `phaze-d2hgv.7`.* **Paragraph granularity and cross-line matching are load-bearing,
  not stylistic** — `video_audio.py:83` and `test_video_audio.py:709` are the motivating examples,
  and a line-scoped version of this check would not have flagged either, because it cannot see
  them. The argument, and why "per line is equivalent and cheaper" is wrong, is in §5.1.
- **R3 — A corpus-distribution helper for bounds.** A `just` recipe or `scripts/` probe answering
  *"what fraction of the corpus exceeds \<duration | size\>"* against `files.duration`, so G3's
  distribution clause costs one command. `phaze-1b39` is the entire justification. **P3.**
  Delivered by `phaze-d2hgv.5` as `just corpus-distribution` / `scripts/corpus_distribution.py`
  (referenced from G3's distribution clause above).
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
  **Added 2026-08-21:** `phaze-d2hgv` (the amendment epic — it carries the recovered exchanges
  verbatim, so no reader has to re-run the archaeology) and its children `.1`, `.2`, `.3`, `.10`.
- **Session transcripts** *(added 2026-08-21 — the primary record for operator answers, and the
  source the 2026-08-20 sweep did not consult)*: dispatcher session `60b8bf47`, the
  `AskUserQuestion` call put 2026-08-12T00:30:59Z and answered 00:31:36Z (`phaze-3ea41`: video
  container acceptance, extraction locality, track selection); the call put 2026-08-11T19:03:30Z
  and answered 19:04:21Z (`phaze-kj8dl`); the operator turn of 2026-08-04T19:19:29Z
  (`phaze-6r39`); and the null result for `phaze-ldvmy` from the full-corpus sweep of 2026-08-20.
  Cited by session id and timestamp deliberately — transcript file paths carry local account
  names and are not committed.
- **ADRs:** `docs/design/0007-windowed-analysis.md` §7 (the decision and its conditions) and §8
  (the measured refutation); `docs/design/0011-bug-hunt-cadence.md` (precedent for this document's
  form); `docs/design/0005-analyze-job-memory-limits.md` (the 4Gi limit).
- **Spikes:** `docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md`,
  `docs/spikes/phaze-u1n7j-vox-fix-verification.md`.
- **The site enumeration** *(added 2026-08-21, `phaze-d2hgv.10`)*: a whole-file, case-insensitive
  sweep for `operator[-\s#*>/]{1,40}(decision|confirmed|approved|directed|granted|chose|ruling)`
  over every tracked file under `src/`, `tests/`, `docs/`, `scripts/`, `alembic/`, `.planning/`,
  plus `CLAUDE.md`, `justfile` and `Dockerfile*`, read from the tree at `d0805b02`. Cross-checked
  against a `phaze-3ea41` bead-id grep, which finds a different — and also incomplete — set: the
  two searches disagree on `test_video_audio.py:709`, which neither would have found alone.
- **Tests:** `tests/analyze/services/pipeline/test_video_audio.py` (at `d4524c88` and at HEAD),
  `tests/analyze/services/pipeline/test_extraction_analysis_handoff.py`,
  `tests/analyze/services/backends/test_kube_staging.py:169-186`,
  `tests/shared/test_no_exclude_newer_cooldown.py`.
- **`CLAUDE.md`** — the mocked-essentia lesson as recorded for `D-09`.
