# ADR-0016 — A transferred model carries no pointer, so nothing triggers the check

| | |
| --- | --- |
| **Status** | Accepted — decided 2026-08-25 |
| **Date** | 2026-08-25 |
| **Bead** | `phaze-0vsqf` — found by `dev/bhcite` on 2026-08-25, which diagnosed the pattern in itself after being wrong twice in one session and argued it deserved its own record rather than a clause inside the bead that surfaced it. The framing below is largely that seat's words. |
| **Applies to** | every seat, every bead — including docs-only ones. Three of the four instances below are not production-path changes at all. |
| **Enforced from** | `CLAUDE.md` → *A belief that is true in a neighbouring system is a claim, not knowledge* (immediately after the five rules of *Acceptance criteria, attribution, and verification fidelity*) |
| **Grows** | §3 is a catalogue. Adding an instance is a normal, cheap edit to this file — see §8. |

______________________________________________________________________

## 1. The pattern, and the property that defines it

A belief carried in from a neighbouring system — another OS, another version of the same tool,
another project with the same tool in it — presents itself as **something you already know**
rather than as **a claim you are making**. That is the whole mechanism. Verification fires on
claims. Knowledge is not a claim, so nothing fires.

**The defining property is that a transferred model has no referent.** There is nothing to
dereference, nothing to 404, nothing to notice going stale. It is not that the check is skipped;
it is that no check is ever scheduled, because at the moment of use the belief does not look like
the kind of thing that has a check.

Every instance in §3 was **true somewhere** — just not here. Every one cost **under a minute** to
settle once someone thought to look. Every one was produced by a careful seat reasoning correctly
from a sound model, and every one was caught within minutes. This document is about **why the
check does not fire**, not about carelessness; if it reads as scolding it has failed, because a
section that reads as scolding is a section that gets skipped.

**The objection this document has to answer, stated up front.** *If the conclusion was right
anyway, what did the thirty seconds buy?* Sometimes it buys nothing, and the honest answer is that
you cannot know which case you are in beforehand — that is the whole reason the check has to be a
condition rather than a judgement call. But the more common payoff is subtler than catching a
wrong answer: **the check replaces a wrong reason with a right one under an unchanged
conclusion.** §3.1 is the worked example, and it is worth reading for this alone — the belief was
load-bearing for a real design choice, running the tool left the choice standing and **inverted
the reason for it**, and the corrected reason is what generated the guard and the tests that now
defend it. The original reason would have defended nothing. **A right answer held for a wrong
reason has no defence against the next change, and is indistinguishable from a right answer held
for a right one until something moves.**

## 2. Two forms, and the second is the harder one to see

- **Form A — a model borrowed from another SYSTEM.** Instances 1–3 and 5. The belief is true of
  Linux, or of every other option the same tool takes, or of the version of that tool you were
  running last week. "Neighbouring" gets as close as *the rest of the same program* (§3.1's belief
  is a correct generalisation from all but three of pytest's options) and as close as **the same
  session ten tool calls earlier** (§3.5, where the same word named two different programs).
- **Form B — a VERIFIED MECHANISM vouching for an UNOBSERVED INSTANCE in this system.** Instance
  4. You confirm that a mechanism is real, then assert that a particular case *is* an instance of
  it, and label the assertion measured — because the mechanism was.

Form B is the sharper one and it is the reason this document exists rather than a one-line
reminder. The seat that found it named it against itself, and its own account is the best
statement of the failure available:

> **"the mechanism felt like it carried the instance with it."**

The mechanism was real; the instance was false. See §3.4 for the worked example — it is the one
entry here where the belief was not borrowed from anywhere, and it still had no referent.

## 3. The catalogue

§§3.1–3.4 were measured **2026-08-25** by `dev/bhcite`; **§3.5 was found the same day while this
document was in review**, by `team-lead` re-running a command `dev/transmodel` had cited — which is
the catalogue growing the way §8 intends, on its first day. Everything that is a **standing fact
about an installed artifact** — instances 1, 3 and 5 in full, instance 4's mechanism — was
**re-verified independently by `dev/transmodel` while writing this document**, in this
environment, against the versions installed here; that is the discipline of §4 applied to the
document that states it, and it sharpened §3.1 and §3.3 rather than merely confirming them. What
could **not** be re-verified is **point-in-time** — one machine's page counts, one moment's process
counts — or needs a version not installed here, and every such row says so. A row that does not
say so was run.

### 3.1 "pytest does not expand environment variables in `addopts`"

Widely believed, and false.

| | |
| --- | --- |
| **True where** | of pytest's option handling **in general** — there is no global expansion of `addopts`, and for most options the belief holds. Expansion is opt-in **per option, at the consumer**, so the neighbouring system this one transfers from is *the rest of the same tool*. |
| **Source** | `_pytest/junitxml.py:467` — `logfile = os.path.expanduser(os.path.expandvars(logfile))` |
| **Measured** | pytest **9.1.1**. `$MYDIR` **set** → the report is written to the expanded directory. `$MYDIR` **unset** → a directory *literally named* `$MYDIR` is created in cwd, because `os.path.expandvars` leaves an undefined name untouched rather than substituting empty. |
| **Re-verified** | 2026-08-25, `dev/transmodel`, in this repo's `.venv` (Python 3.14, pytest 9.1.1). `grep -rn --include='*.py' expandvars .venv/lib/python3.14/site-packages/_pytest/` prints **four lines, three of which are calls** — the fourth is the `from os.path import expandvars` import at `pathlib.py:20`. The three calls are all path-valued options: `junitxml.py:467` (`--junit-xml`), `config/findpaths.py:332` (`--rootdir`), and `pathlib.py:425` in `resolve_from_str`, whose only caller is `cacheprovider.py:141` (the `cache_dir` ini key). Without `--include='*.py'` the line count depends on **which `grep` you get** — see §3.5, which is about exactly that and is why this row names its implementation. |
| **What it cost** | it was load-bearing for `phaze-ea6kp`'s central design choice — a conftest hook gated on the variable's presence, versus one `addopts` line — and survived exactly **two minutes** of contact with the tool. |
| **Durable record** | `phaze-ea6kp` landed on `main` as `e81b7a17` (2026-08-25). `tests/bh_test_report.py`'s module docstring carries the same measurement at its own call site, including `expandvars('$UNSET/x') -> '$UNSET/x'`. This entry is the **general form** of the lesson recorded there — `CLAUDE.md` rule 5, discharged in both directions. |

Two things are worth carrying. **The unset behaviour:** the failure is not an error, it is a
plausibly-named directory in the wrong place. **The three-call-site enumeration:** it is what makes
the belief's persistence make sense rather than look like a lapse. "pytest does not expand env
vars" is a correct generalisation from almost every option there is; it is wrong only at the three
that expand themselves, and nothing in the option's spelling says which kind it is.

**And the part that answers the obvious objection to this whole document.** Running the tool did
**not** reverse `phaze-ea6kp`'s conclusion: the hook was still the right shape, and the `addopts`
line still wrong. So why did the thirty seconds matter? Because the *reason* changed completely.
Under the belief, the `addopts` line fails inside bh and the hook is a workaround. In fact the
`addopts` line **works** inside bh and breaks **everywhere else** — every plain `uv run pytest`,
every `just test`, every CI run — because `BH_TEST_REPORT_DIR` is set only inside a validation
subprocess and an undefined name is left untouched rather than emptied. That corrected reason is
what generated the actual guard and the tests around it; the original reason would have defended
nothing. **A right answer held for a wrong reason has no defence against the next change**, and
it is indistinguishable from a right answer held for a right one until something moves.

### 3.2 "140 MB free means this machine is nearly out of memory"

True on Linux. Meaningless on Darwin.

| | |
| --- | --- |
| **True where** | Linux, where the free-page count is a usable headroom proxy |
| **Measured** | `vm_stat`: **`Pages free` 0.14 GB** against **`Pages inactive` 9.98 GB** reclaimable — **~10.34 GB actually available** — with `memory_pressure` reporting **65% free**. |
| **Why** | macOS parks reusable pages on the inactive list, so a near-zero free count is the **healthy steady state**, not a warning. |
| **Not re-verified** | the figures are one machine at one moment and cannot be reproduced after the fact; they are carried as `dev/bhcite` measured them. The *mechanism* — the inactive list — is checkable on any Darwin box in one `vm_stat`. |
| **What it nearly cost** | the seat was about to hold a gate on it. |

### 3.3 "bh's `work.py` has a line 3557"

True of bh **0.14.0**. False of the **0.15.0** installed here.

| | |
| --- | --- |
| **True where** | bh 0.14.0, where `work.py` was one module long enough to have a line 3557. **Not re-verified — 0.14.0 is not installed on this machine**, so that half is carried from the boundary table's own citation, not measured here. Saying so is the point of the entry. |
| **Measured** | on 0.15.0 `work.py` is **1777 lines** and the module has been split. |
| **Re-verified** | 2026-08-25, `dev/transmodel`: `bh --version` → `0.15.0`; `wc -l /opt/homebrew/Cellar/beadhive/0.15.0/libexec/lib/python3.13/site-packages/beadhive/work.py` → **1777**. The split is visible as ~20 sibling `work_*.py` modules in the same directory (`work_merge.py`, `work_submission.py`, `work_guards.py`, …). |
| **Consequence** | every line number in the Caller column of `CLAUDE.md`'s boundary table — **1909, 2417, 3557 (twice), 2923/2948, 2972, 3525**, plus `work.py:3646` cited in the prose beneath it — is past EOF at 1777. They do not point at the wrong code; they point at *nothing*. Tracked as `phaze-g9cus`; **do not repair them from here.** |

This is the instance closest to the sibling case in §5, and the difference is instructive: a line
number at least *looks* like a pointer, so a reader who thinks to dereference it finds it missing.
The belief *"work.py is one big module"* that made the line number plausible has no such
affordance.

### 3.4 "The gate count tells me how many gates are running" — the Form B instance

It counts gates that have reached **pytest**.

| | |
| --- | --- |
| **Mechanism, and it is real** | `check-fast: lint typecheck test-fast` (`justfile:1253`); `just --dry-run check-fast` shows `uv run ruff check .` then `uv run mypy .` before the test step. So a live gate is invisible to the documented `ps -eo args= \| grep -cE '^[^ ]*/\.venv/bin/python[0-9.]* .*pytest'` for its first ~30–60 seconds. |
| **Measured** | live at one seat's own launch: pytest-visible count **2**, against **5** processes matching `just check\|uv run mypy`. |
| **Re-verified (mechanism only)** | 2026-08-25, `dev/transmodel`: the recipe composition and the dry-run ordering, above. The process counts are point-in-time and are not reproducible after the fact. |
| **Direction of the error** | undercounting, which is the direction that **adds** a gate. |

**And then the second failure, which is the one this entry is here for.** Having verified the
mechanism, the seat asserted an *instance* it had never observed — *"phaze-irby2 is live and
invisible"* — and labelled it measured. It was holding a gate slot on that claim. The instance was
false: that bead had no gate running at all.

Nothing was borrowed from a neighbouring system here. The mechanism was established **in this
environment, minutes earlier**. The transfer was from *a verified general fact* to *an unverified
particular*, and it produced the identical signature: a claim that did not feel like one, so
nothing checked it.

### 3.5 "`grep` is one program" — found while reviewing §3.1, and the reason §3.1 names its tool

Two seats ran the same command against the same path on the same machine and got different output.
Neither had made an error, and **it took both of them to find out**.

| | |
| --- | --- |
| **True where** | almost everywhere — on most machines `grep` resolves to one binary, and a bare `grep -rn` in a citation is about as checkable as prose gets. That near-universality is what makes it invisible here. |
| **False here** | this shell defines `grep` as a **function** that shadows the binary and dispatches to **`ugrep 7.8.4`** with `-I --ignore-files --hidden` among other flags. `-I` skips binary files. `/usr/bin/grep` does not. |
| **Measured** | 2026-08-25, `dev/transmodel`, same worktree, same pattern, same path, within one minute. `/usr/bin/grep -rn expandvars .venv/.../_pytest/` → **7 lines, 3 of them `Binary file … matches`** for the `__pycache__` copies. The shell's `grep` (the ugrep wrapper) → **4 lines, 0 binary**. `find … -name '*.pyc' \| wc -l` → **76**, so the `.pyc` files were present for both runs; their presence is not the variable. |
| **How it surfaced** | `dev/transmodel` had cited the 7-line output. `team-lead` re-ran the cited command, got 4 lines and 0 binary matches — in the same worktree — and correctly reported that the claim did not reproduce. Both observations were real. |
| **What it cost** | ~10 minutes, and it nearly deleted a true statement from the document. |

**The general form, and it sharpens §5.** A command in a citation *looks* like the most checkable
thing you can write — better than a line number, better than a filename, because the reader can
run it. It is not, unless it names its implementation: `grep`, `sed`, `awk`, `find`, `date`, `ps`
and `stat` all name different programs on macOS than on Linux, and a shell function or alias can
shadow any of them without the caller ever seeing it. So a bare command is a **pointer whose
target is resolved at read time in the reader's environment, not the author's** — which is why §5's
table now carries a row for it, sitting between the redundancy-free citation and the transferred
model. It had been in this document from the first draft, unrecognised. (Cited by what the row
says rather than by its position, on this document's own argument: an ordinal is a pointer with no
redundancy, and inserting a row above it silently moves every citation written against it.)

**Two clarifications, because the first pass at classifying this entry got them backwards.** The
seven-line output was **observed, not inferred** — it is in the run that opened §3.1, and it
reproduces exactly under `/usr/bin/grep`. And this is **Form A, not Form B**: the belief carried
in was *"`grep` means the same program each time I write it down"*, true of essentially every
other shell, and the neighbouring system was **the same session ten tool calls earlier**, where
the unshadowed binary had answered. Getting that classification right matters more than it looks:
a Form B reading would prescribe *"look at the particular"*, and the seat **had** looked at the
particular. The check that would have caught this is the Form A one — `which -a grep`, three
seconds — and no amount of re-observing the output would have produced it.

### 3.6 "`OTEL_EXPORTER_OTLP_TIMEOUT` is in milliseconds, like every knob next to it"

The OpenTelemetry **specification** says milliseconds. opentelemetry-python reads it in
**seconds**, while every `OTEL_BSP_*` and `OTEL_METRIC_EXPORT_*` variable set beside it in the same
dict really is in milliseconds. So a value written in the spec's units, sitting in a block of
four-digit millisecond values that all look identical, is wrong by **1000x** — in the direction that
does not fail, it just waits.

| | |
| --- | --- |
| **True where** | the OTel specification, and in SDKs that implement it as written. It is also true of every neighbouring variable in this repo's own defaults block, which is what made the wrong value look consistent rather than anomalous. |
| **False here** | `opentelemetry-exporter-otlp-proto-http` **1.44.0** reads it in **seconds** and feeds it straight to `requests`' `timeout=`. `.venv/lib/python3.14/site-packages/opentelemetry/exporter/otlp/proto/http/trace_exporter/__init__.py:68` — `DEFAULT_TIMEOUT = 10  # in seconds` — and line 120 reads the environment variable into that same slot. The SDK's own docstring for the variable (`opentelemetry/sdk/environment_variables/__init__.py:320`) also says *"in seconds"*, i.e. the library documents its divergence from the spec; nobody read it, because nobody thought there was a question. |
| **Measured** | 2026-08-26, `dev/telemetry`, worktree `wt/batch/phaze-m1drf`. `uv run python -c "from opentelemetry.exporter.otlp.proto.http.trace_exporter import DEFAULT_TIMEOUT; print(DEFAULT_TIMEOUT)"` → **`10`**. Versions from `importlib.metadata.version`: `opentelemetry-sdk` **1.44.0**, `opentelemetry-exporter-otlp-proto-http` **1.44.0**. The value phaze had set was `"5000"`, meaning five seconds; it was read as **5,000 seconds — 83 minutes** — as the deadline for one batch export attempt. |
| **How it surfaced** | not by review. A real analysis measured against a black-holed collector (RFC 5737 `192.0.2.1`) finished its work in 258.04 s and then **would not exit**. The analysis itself was never at risk — export runs on its own thread — but process teardown sat behind the exporter. |
| **What it cost** | one measurement arm abandoned, a stuck background run to kill, and roughly 20 minutes. In production it would have been a k8s analyze Job refusing to die, holding a Kueue slot, once per file — and it would have been read as a phaze bug, not an SDK units question. |

`tests/shared/telemetry/test_export_timeout_units.py` now pins it by constructing the real exporter
and reading back the timeout it resolved, so the day upstream unifies the units — the fix everyone
wants — phaze finds out from a red test rather than from a timeout 1000x too short.

### 3.7 "A BuildKit cache mount survives `--no-cache`; only `docker builder prune` clears it"

Written into `Dockerfile.agent-arm64` and `docs/arm64-agent-image.md` as the reason an sccache
object cache in a `--mount=type=cache` would make a `--no-cache` rebuild warm. It was the model
the verification run was designed around: "`--no-cache` discards every layer but keeps the
mount, so a second build must show hits". The second build showed **0 hits / 338 misses**, and
the first reading of that was "the hash key is unstable", which is the wrong system entirely.

| | |
| --- | --- |
| **True where** | older BuildKit, where the standing complaint was the opposite — cache mounts could *not* be cleared by `--no-cache` and needed `docker builder prune --filter type=exec.cachemount`. Every write-up of `RUN --mount=type=cache` the seat had read was about that era. |
| **False here** | Docker Engine **29.5.2** (colima, `Server Version: 29.5.2`, containerd v2.2.4) with the `docker/dockerfile:1` frontend: a `--no-cache` build is given a **fresh, empty** cache mount for the same `target`, and that fresh mount then becomes the one subsequent normal builds see. The previous mount's contents are orphaned, not deleted — `docker system df -v` listed two `exec.cachemount` records for one target. |
| **Measured** | 2026-09-04, worktree `wt/bead/issue/phaze-jfhr0`. A three-line Dockerfile writing `stamp-$STAMP` into the mount and listing it first: two `--no-cache` builds printed `before:` **empty** both times; two following builds *without* `--no-cache` (layer busted by a changed `--build-arg`) printed `before: stamp-2` and then `before: stamp-2 stamp-3`. The full image then confirmed it: a `--no-cache` rebuild of `Dockerfile.agent-arm64` compiled **338 / 338 misses in 394.4 s**; a layer-bust rebuild with no intervening `--no-cache` compiled **338 / 338 hits in 55.0 s** (cold: 417.8 s). `docker --version` client 20.10.12, `docker info` server 29.5.2. |
| **How it surfaced** | the zero-hit result was investigated as a hash-key problem first: two images' compilers, headers and generated waf caches were diffed byte-for-byte (identical), before a stamp file in the mount showed the mount itself was new. |
| **What it cost** | two full arm64 image builds (~14 min) spent proving nothing, plus the diffing — about 25 minutes. It could have cost more: the docs would have told the next operator to verify the cache with exactly the command that empties it. |

The general form is the one this ADR already states, with the twist that this belief was true
*of the same tool* — the neighbouring system was an earlier version of BuildKit, and the
mitigation was the three-line stamp probe, which took under a minute once it was run.

______________________________________________________________________

## 4. The trigger

Not "be careful". This repo has the receipts on what exhortations achieve — `CLAUDE.md`'s five
rules exist precisely because three production incidents shipped through seats that were being
careful. The rule is a condition with a bright line:

> **When a belief about a tool's behaviour is load-bearing for a decision, and you did not read it
> or run it IN THIS ENVIRONMENT, run it. Thirty seconds, every time.**

**"Load-bearing" is the qualifier that makes this usable rather than paralysing.** It applies when
the belief *changes what you do* — a design conclusion, a gate you hold or release, a number you
cite, a line you tell another seat is safe to edit. It does not apply to every incidental
assumption, and a rule that did would be ignored within a day.

**"In this environment" is doing the other half of the work.** Not "in a repo like this", not "the
last time I used this tool", not "in the version documented upstream". The installed version, this
OS, this checkout. Instance 3 is true in bh 0.14.0 and false in the 0.15.0 on this machine; nothing
about the belief changed between those two states, only the machine did.

For **Form B**, the trigger has an extra clause, because the general one does not reach it: **a
mechanism you verified does not vouch for an instance you did not observe.** Verify the mechanism
*and* look at the case. In §3.4 that second look was one `ps` away.

## 5. The sibling case, and the relationship between them

`CLAUDE.md` → *Cite ADRs by filename, never by bare number* argues that a bare number is a
**pointer with no redundancy**, so no tool can check it. That argument is not restated here; read
it there, along with the `phaze-f70y9` census it rests on.

**The relationship is the interesting part, and it is a strict ordering:**

| | can a reader dereference it? | can a tool check it? |
| --- | --- | --- |
| A citation *with* redundancy (`docs/design/0015-shared-session-gather.md`) | yes | yes — `tests/shared/test_adr_citation_resolution.py` |
| A citation *without* redundancy (a bare `ADR-0015`, a bare `work.py:3557`) | yes, and finds it missing or misresolving | partly — a link check catches a dangling number, never a misresolving one |
| A **bare command** (`grep -rn …`) | yes — but it resolves in the **reader's** environment, not the author's, so two readers can get two answers and both be right (§3.5) | no |
| A **transferred model** | **there is nothing to dereference** | no, and there never can be |

So the two failures are the same shape at different depths. The bare-number case has a referent
that can be *wrong*; the transferred-model case has **no referent at all**. That is why the
mitigation cannot be a checker, and has to be a condition the reasoner applies at the moment the
belief becomes load-bearing — §4. `phaze-f70y9`'s conclusion generalises exactly: *the redundancy
has to be written in at authoring time*, and for a belief rather than a citation, "written in"
means "run it".

## 6. This repo has already paid for three instances of this, separately

Named as the same shape rather than re-argued; each is fully documented where it lives.

- **`phaze-b62ri`** — *re-measure a tool against the version you are running*. The ffmpeg version
  claim was true of one image and false of the other; the fix was a base-image move, not a pin.
  `docs/design/0013-ffmpeg-pin.md`, and the FFmpeg row of `CLAUDE.md`'s stack table.
- **The repowise 0.44-vs-0.45 entry** in `CLAUDE.md` → *Beads Workflow Integration* → *Key
  concepts*. Prose faithful to its bead's evidence and stale about the tool: the measurements were
  taken on 0.44.0 and written up while 0.45.0 was already installed. Its own boxed note is the
  best short statement of the lesson in the tree, and it records that the *first correction then
  overshot* — a second instance of the same pattern inside the correction to the first.
- **`phaze-g9cus`** — the dangling caller line numbers of §3.3. In flight as of 2026-08-25.

Three prior instances, each fixed at its own site, none of which named the general form. That is
the condition `CLAUDE.md` rule 5 exists to prevent, and this document is the general form those
three were owed.

## 7. Why this file, and why not the three candidates that were considered

`phaze-0vsqf` left placement genuinely open and named three candidates. The constraint it set is
the one that decides it: **the catalogue needs somewhere it can grow without bloating whatever
hosts it.** §3 is the durable value here, and §3 is a list that gets longer.

The choice below is the implementing seat's judgement (`dev/transmodel`, `phaze-0vsqf`,
2026-08-25). No question was put to the operator and none of it carries the operator's authority;
reopen it freely.

- **A growing section in `CLAUDE.md` — rejected.** `CLAUDE.md` is read in full at the start of
  every session by every seat, so a line there is paid for on every turn of every session, whether
  or not anyone needs it. It is the one file in this repo that structurally cannot host a list
  that accretes. Measured on this bead: `CLAUDE.md` went **1587 → 1638** lines (+51, and fixed —
  the §3.5 added during this bead's own review did not move it), while §3 grew by a whole instance
  in the same review and cost a non-reader nothing. That is the split working as designed, observed
  once already before the document had even landed. What `CLAUDE.md` *does* get is the
  fixed-size half — the name, the defining property, the trigger, and a pointer here — which is the
  half that has to fire without anyone going looking for it.
- **A rule 6 inside *Acceptance criteria, attribution, and verification fidelity* — rejected, and
  this was the tempting one.** That block opens by claiming *"Each rule below is checkable against
  a diff"*, and this rule is not: it is a condition on the reasoner, not a property of the change.
  Adding it as a peer would falsify the block's claim about itself. Two further mismatches: those
  five rules are scoped to *beads that change a production path*, while three of the four
  instances here are not production-path changes at all; and the five map one-to-one onto
  `docs/design/0012-verification-fidelity-and-operator-attribution.md`'s G1–G5, each with its own
  would-have-caught verdict against three named incidents, so a sixth with no such verdict would
  be a peer in numbering only.
- **Appended to `docs/design/0012-verification-fidelity-and-operator-attribution.md` — rejected,
  and it is the closest call.** Topically it is the right neighbourhood: 0012's subject is a check
  that did not fire because verification was aimed at a proxy, and this is that failure one layer
  up, where the proxy is a *neighbouring system*. Three things decide against it. It is an
  **incident-analysis** document anchored to three specific 2026-07/08 incidents and one bead
  (`phaze-u8qj0`), **1137 lines** before this bead touched it and already carrying two corrections
  and a G3 qualification — appending an open-ended catalogue degrades what is already the hardest document
  in the tree to navigate. Its header scopes it to *"every bead that changes a production path"*,
  which this pattern outruns; grafting §3 in would either falsify that line or require widening a
  settled scope. And **discoverability runs the wrong way**: a seat that has just been burned and
  wants to add instance #5 has to already know that 0012 exists and covers this, whereas
  `0016-transferred-model-verification.md` says so in its name.

The two documents are cross-referenced rather than merged. Read 0012 for verification aimed at the
wrong proxy; read this for verification that was never aimed at all.

## 8. Adding an instance

The catalogue's value is that every entry is **checkable**. An instance stripped to its moral is
folklore, which is this repo's standing complaint about unevidenced rules — see `CLAUDE.md`'s
`M`/`I` evidence letters, which exist for the same reason.

A new §3 entry carries, at minimum:

1. **The belief, quoted as it was actually held.** Not the corrected version.
2. **Where it is true.** Every entry here was true somewhere, and naming that is what makes a
   reader recognise their own version of it. Look harder than "widely believed" before concluding
   it is true nowhere: §3.1 was written that way first, and the neighbouring system turned out to
   be *the rest of the same program*.
3. **The measurement**, with whatever makes it re-runnable: a file and line plus the version they
   are true of, a command and its output, the two numbers that disagreed. Dates on everything.
   **If the command's raw output does not equal the number you state, say why in the same breath.**
   §3.1's grep prints four lines for three call sites; a reader re-running it and getting a
   different number than the text claims has to work out whether you miscounted or they misread,
   and a claim that does not check cleanly to its own stated answer is a poor advertisement inside
   this document in particular.
   **And name the implementation of any tool whose output you quote** — `which -a <tool>` plus a
   `--version`, three seconds. §3.5 is the entry that earned this clause: two seats ran the same
   command in the same worktree and got 7 lines and 4 lines respectively, because a shell function
   was shadowing the binary. A bare command name is not a pointer; it resolves in whoever's
   environment runs it.
4. **What it cost, or nearly cost.** Minutes, a gate slot, a design conclusion. This is what keeps
   the entry proportionate — none of these were disasters, and the document is weaker if it
   pretends otherwise.

Do not add an instance whose evidence is "I remember this happening". That entry is the pattern.

______________________________________________________________________

## Sources

- **Bead** `phaze-jfhr0` (§3.7, measured 2026-09-04 while verifying the sccache cache mount on
  the arm64 agent image; the stamp probe and the four build logs are described in the bead's
  submit comment).
- **Bead** `phaze-m1drf.1` / `phaze-m1drf.2` (§3.6, measured 2026-08-26 while wiring the OTLP
  exporter; the pin is `tests/shared/telemetry/test_export_timeout_units.py`).
- **Bead** `phaze-0vsqf` (description and acceptance criteria — §§3.1–3.4 as measured by
  `dev/bhcite`, 2026-08-25); `phaze-ea6kp` (§3.1's source bead, landed as `e81b7a17`);
  `phaze-g9cus` (the dangling caller line numbers); `phaze-b62ri`; `phaze-f70y9` (the
  ADR-citation census).
- **Source** `tests/bh_test_report.py` — the module docstring landed by `phaze-ea6kp`, which
  records §3.1's measurement at its own call site and which this document generalises.
- **Re-verified in this environment, 2026-08-25, `dev/transmodel`:** the three `expandvars` call
  sites under pytest 9.1.1 in this repo's `.venv` (`junitxml.py:467`, `config/findpaths.py:332`,
  `pathlib.py:425` via `cacheprovider.py:141`); `bh --version` → 0.15.0 and `wc -l` on its
  `beadhive/work.py` → 1777; `justfile:1253` and `just --dry-run check-fast`; the Caller column of
  `CLAUDE.md`'s boundary table read against that 1777; and, for §3.5, `which -a grep` (a shell
  function shadowing the binary), `grep --version` → `ugrep 7.8.4`, and the same pattern run
  through both `/usr/bin/grep` (7 lines, 3 binary) and the wrapper (4 lines, 0 binary) inside one
  minute. **Not re-verified, and labelled as such where they appear:** bh 0.14.0's `work.py`
  length, §3.2's `vm_stat` figures and §3.4's two process counts — all three are point-in-time or
  need a version not installed here.
- **`team-lead`**, 2026-08-25 — the independent re-run of §3.1's cited command that produced §3.5.
  The entry exists because a second party ran it; neither seat could have found it alone.
- **`CLAUDE.md`** — *Cite ADRs by filename, never by bare number* (§5's sibling argument);
  *Acceptance criteria, attribution, and verification fidelity* (the five rules, and rule 5's
  general-form obligation); *Concurrent gates are bounded by headroom, not by isolation* (the
  `ps` counting command of §3.4); the repowise 0.44-vs-0.45 entry under *Key concepts*.
- **ADRs:** `docs/design/0012-verification-fidelity-and-operator-attribution.md` (§4 G1–G5 and
  their would-have-caught verdicts); `docs/design/0013-ffmpeg-pin.md`.
- **Tests:** `tests/shared/test_adr_citation_resolution.py`,
  `tests/shared/test_adr_numbering.py` — the checkable half of the sibling case in §5.
