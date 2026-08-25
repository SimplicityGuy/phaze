# Branch topology, landing, and verification fidelity

> **Extracted verbatim from `CLAUDE.md` on 2026-08-25** when that file was consolidated.
> `CLAUDE.md` keeps the operational rules; this document keeps the **evidence** — the base-skew
> and contamination incidents, the measured cost of each missed check, and the full argument for
> the five verification-fidelity rules.
>
> Cross-references to "above"/"below" are internal to this document unless they name a
> `CLAUDE.md` section.

## Workflow: Features and PRs

- **Every feature gets its own git worktree** — no cross-contamination between features
- **Code changes may go straight to main.** An agent that has taken a change through the beadhive
  lifecycle — validated, resolved at the bead's review gate, merged `--no-ff` — may push `main` to
  origin directly. No PR is required for a code bead, and none should be opened for one.
- **Docs changes require a PR.** Anything under `docs/**`, any root-level `*.md` (`CLAUDE.md` and
  `README.md` included), and any other prose-only change gets its own branch and a PR for human
  review before it lands. Prose is where decisions and rationale are recorded, and a wrong line in
  it reads as authoritative to every future agent for as long as it stands — that is what the
  review is for, and CI cannot supply it.
- **A change touching both is a docs change** for this purpose: open the PR.
- **One PR per feature**, wherever a PR is opened at all — no mixing unrelated changes.
- **`bh work merge` / `bh work finish` are LOCAL — they never push.** A bead reaching CLOSED/merged
  means the local integration branch took the merge, and nothing more. `bh work merge` prints
  `✓ merged … and closed it` for a merge that exists only in your clone; measured 2026-08-24, that
  line appeared while `origin/main` was unchanged. **To verify a landing, check the REMOTE** —
  `git fetch origin && git merge-base --is-ancestor <sha> origin/main`, or `git ls-remote origin
  refs/heads/main`. **Do NOT verify against local `main`, and the reason is the whole point: the
  merge just wrote that ref, so `--is-ancestor <sha> main` cannot fail.** A verification that cannot
  fail is not one — it caught two dispatchers on the same day, the second after the bead was already
  filed. *The general form:* "merged" is a claim about a repository; a local check is a claim about a
  working copy.
- **Before provisioning ANY worktree, check for base skew** (phaze-aox61):
  `git fetch origin && git rev-list --left-right --count main...origin/main`. Zero/zero is the only
  safe state — `bh work claim` reports `"start_point": "main"`, the LOCAL ref, so every worktree cut
  while local `main` is ahead inherits those commits and the PR renders foreign files (measured: PRs
  #516 and #517 each showed 5 foreign files including a 423-line test belonging to another bead).
  If it was skipped, the detection pair for **that** direction — *"do I carry commits that are not
  mine?"* — is `git log --oneline origin/main..<branch>`, which must list **only** this bead's
  commits, and `git diff --stat origin/main...<branch>`, which is the authoritative diff. **That
  pair is half a base check, not a base check**; the other half is the bullet immediately below, and
  reading this one as complete is what phaze-4hayr cost. **A GitHub file list lagging a base push
  and a genuinely contaminated branch look identical in the UI** — on PR #516 `baseRefOid` still
  named the pre-push tip while git already computed the correct 2-file diff — so only those two
  commands tell them apart. The fix is
  `git rebase --onto origin/main <old-base>`; note that **pushing `main` does not always clear it**,
  because content that reached origin by a different route has a different sha and no shared
  ancestor. *The general form:* content equality does not imply a shared ancestor — "already on
  origin" is a claim about topology, checked with `--is-ancestor`, never inferred from the same work
  having landed.
- **Before you SUBMIT, and again before the seat that MERGES lands it, check the other direction —
  has the base moved underneath me?** (phaze-4hayr). The two questions are the same tool pointed in
  opposite directions, and only both together describe a branch's relationship to `origin/main`:

  ```bash
  git fetch origin
  git log --oneline origin/main..HEAD   # "do I carry commits that are not mine?"  — CONTAMINATION
  git log --oneline HEAD..origin/main   # "what landed since I branched?"          — STALENESS
  ```

  **The contamination check is CLEAN precisely when you are behind**, and that property is what kept
  this gap invisible: a branch cut from an older `main` lists only its own commits in
  `origin/main..HEAD` however far `origin/main` has advanced since. So the documented pair reports
  healthy in exactly the state that produces a red merge. It is not a weak check — it is one whose
  green is *produced by* the condition you wanted it to find. This is the mirror of the
  `--is-ancestor` rule two bullets up, which exists because content equality does not imply shared
  ancestry: same reachability question, asked the other way round, and the two are meant to be read
  together rather than restated.

  **Measured cost, 2026-08-25: one bounce paid, and a second avoided only because a seat exceeded
  the documented check.** `phaze-ooe68`'s merge validation exited 1 with 4 failures in
  `tests/review/routers/test_exec_hash_client_mode.py`, every one
  `ValidationError: 2 validation errors for ExecuteBatchProposalItem` — `source_path Field required
  [type=missing]` and `original_path Extra inputs are not permitted [type=extra_forbidden]` —
  because `phaze-xzjrr` renamed that wire field and landed while `ooe68` was in flight. `ooe68`'s
  own gate was correctly green against its base and `origin/main..HEAD` was clean throughout, so
  nothing documented could have surfaced it; the bill was a ~25-minute merge validation, a bounced
  bead, a rolled-back local `main`, a rebase and a ~21-minute re-gate. Preparing to resubmit, the
  same seat ran the mirror unprompted and found `phaze-x533t`'s newly-landed ADR-citation guard — a
  test that fails the build on an ADR number resolving to no design doc — against a diff that cited
  `docs/design/0012-verification-fidelity-and-operator-attribution.md` by bare number in five
  places. It rebased, ran the guard (24 passed) rather than reasoning about whether the regex would
  match, and reported both. That was the second bounce, not taken.

  **WHERE it belongs: both moments, because they are two different checks.** Before submit is the
  *cheap* moment — the gate you are about to spend is the expensive thing, and a stale base spends
  it against a tree nobody will merge onto; caught here, the rebase and re-gate **replace** the run
  you were about to make instead of adding to it. Before the merge is the *load-bearing* moment,
  because that is where the failure actually surfaces and where it compounds: **`bh work merge`
  writes the merge commit BEFORE validating**, so a staleness red arrives after local `main` has
  already moved, and `bh` then declines to roll it back — it judges `main` "shared" from the
  **branch** existing on origin, not from whether **this commit** was pushed. Neither moment covers
  the other: a submit-time green says nothing about a merge forty minutes later, and under fanout
  dispatch the window between the two is exactly when sibling beads land, by construction. The
  merge-side check belongs to whoever runs the merge — dispatcher or merger, never the developer,
  who has no merge to run — and a non-empty result there means **bounce the bead to its seat before
  spending the merge validation**, not after.

  **WHAT to do with a non-empty result — and it does not conflict with "do not rebase unbidden".**
  The standing instruction forbids the *silent* rebase: a seat quietly rewriting its branch under a
  dispatcher that is tracking shas, or rebasing reflexively at every landing so branches churn
  continuously. It has never forbidden the rebase. **A rebase reported together with what landed and
  what was re-run is the documented remedy; a rebase nobody hears about is the thing the instruction
  is about.** The procedure:

  - **Empty → proceed.** The base you gated against is the base you will merge onto.
  - **Non-empty → read the DIFF, do not count the commits.** `git diff --stat HEAD...origin/main`.
    How far behind you are is not the question; whether what landed touches what you touch is.
  - **Intersecting → rebase** (`git rebase --onto origin/main <old-base>`, the same fix as the
    bullet above), **re-run the guards it implicates, and state both in the submit report.** Run the
    guard; do not reason about whether it would have fired — that is rule 3 of
    [ADR-0012](docs/design/0012-verification-fidelity-and-operator-attribution.md) (verify with the
    artifact's real consumer) at the scale of one test.
  - **Two shapes make a disjoint file list the WRONG answer**, and the measured record above is one
    of each: a landed change to a **shared wire contract or schema your tests construct**
    (`ooe68` and `xzjrr` touched no file in common and it still went red), and a landed **whole-tree
    guard** under `tests/shared/`, which intersects every diff by construction (`x533t`). Treat
    either as intersecting whatever `--stat` says.
  - **Escalate instead of rebasing** when the rebase is not mechanical — conflicts, or a landing
    that invalidates the bead's premise. That is a question for the dispatcher, not a conflict to
    resolve alone inside the bead.

  **No mechanical guard is practical, and the reason is worth more than the absence.** Every guard
  in this repo asserts a property of the **tree** — bytes CI checks out
  (`tests/shared/test_adr_citation_resolution.py`, `test_coverage_gate.py` and
  `test_validation_gate_recipes.py` are the pattern). Base staleness is not in the tree at all; it
  is a property of a branch's relationship to a remote ref at an instant, and there is no byte to
  assert. A test that tried could not even **observe** it: on a PR build `actions/checkout` takes
  the **merge** ref, which already has the base merged in, so from inside the suite the branch is
  never behind; on a push build there is no branch to be behind at all. Either way the assertion is
  unconditionally true — a guard that exits 0 having measured nothing, the `phaze-jnj90` /
  `phaze-nqawu` class, and strictly worse than no guard because it reads as coverage. It is
  self-invalidating besides: a green answer certifies a **moment**, and this repo's guards certify
  **states**. The two things that could help are not tests. GitHub's "out-of-date with the base
  branch" banner is genuine mechanical detection, but only on a PR — so it covers docs beads and
  never a code bead, which lands by direct push. And the fast gate could `git fetch` and print what landed since the
  base in its header, beside the `phaze test database:` line; that is the shape worth building, it
  buys a network dependency in a gate that has none today, and it is out of a docs-only bead's
  scope. *The general form:* a check whose clean result is **produced by** the condition it was
  meant to catch does not merely miss the failure — it converts the failure into positive evidence
  of health. Ask of any check *"what does its GREEN look like while the thing I am worried about is
  happening?"*, and if the answer is "exactly the same as always", it is answering a different
  question from the one you are asking it.
- **A `--soft` reset target is a claim about topology too — never point one at `origin/main`**
  (phaze-irby2). The two bullets above ask about a branch you **cut**; this one is about a branch you
  **rewrite**, and it is where those two directions meet and produce a third failure. A seat
  squashing its checkpoints reaches for `git reset --soft origin/main` because that is where the
  branch is *going*. `--soft` keeps its promise — the working tree is untouched — but it moves HEAD
  **forward** onto a ref that has advanced, and the staged diff becomes "my worktree relative to
  `origin/main`": every intervening commit staged as its **inverse**. Measured 2026-08-25
  (phaze-pv3kk): a worktree based on `c146842a`, with `origin/main` since moved to `190a9e30`, staged
  **105 commits of other seats' work as a revert** — `D` lines for files that legitimately exist on
  `main` included.

  **This is the worse direction, and the reason is the signature, not the size.** The phaze-aox61
  case produces foreign **additions**, and a 423-line test belonging to another bead announces itself
  to the first reviewer who scrolls past it. This one produces **deletions and modifications**: a `D`
  line is indistinguishable from a deliberate removal, an `M` line from an intentional edit. There is
  no foreign-looking artifact to notice. Committed and merged, it would have silently reverted those
  105 commits and read as a normal, well-scoped change.

  **Both documented checks go green on it — and the staleness one goes green BECAUSE of it.**
  Verified 2026-08-25 in two scratch repos independently — the implementing seat's and the
  dispatcher's — each reproducing the shape above rather than the incident's scale. Once the bad
  squash is committed, `git log --oneline origin/main..<branch>` lists **exactly one commit, your
  own squash**, satisfying the "only this bead's commits" criterion; and `git log --oneline
  <branch>..origin/main` is **empty**, because the reset made your branch a descendant of the moved
  ref by fiat. Note that the staleness check does not fail to *run* — it correctly reports "nothing
  landed since I branched", and that is true of the rewritten history it is now being asked about.
  This is the previous bullet's own lesson arriving one level down, and the **second** instance of
  it in this section: a check made clean by the very operation it exists to catch. Only the
  authoritative half sees it: `git diff --stat
  origin/main...<branch>`, where the revert surfaces as those `D` and `M` lines. So run the pair
  before you push a squash, and **before you commit one, read what you actually staged** — `git diff
  --cached --stat`, or `git status --short`, where this failure is a wall of `D` lines beside your
  one `A`.

  **The safe forms reset to YOUR OWN parent, never to a ref that has moved.** Both were verified
  against the same fixture to stage only the bead's own work:

  ```bash
  git reset --soft HEAD~<n>                                # n = your own checkpoint count
  git reset --soft "$(git merge-base origin/main HEAD)"    # the base you actually cut from
  ```

  *The general form:* a reset target is a claim about topology, and `origin/main` is not a stable
  point — it is whatever origin last said, which on this repo moves several times an hour during a
  dispatch wave. Name a commit you own, or compute the one you branched from. `HEAD~<n>` and
  `merge-base` are both immune to the ref moving underneath you, because neither one names it as a
  destination.
- **On a direct push, CI runs after the fact — nothing gates `main`.** The bead's own validation is
  therefore the real gate, but since phaze-pv3kk (2026-08-25) it is no longer necessarily a
  full-suite one: `bh work check`/`submit`/`merge`/`merge-main` all resolve to `just check-fast`, a
  change-selected subset that escalates to the full suite only when repowise's coverage map cannot
  speak for the diff. **`merge-main` — the boundary every ad-hoc bead actually lands through — no
  longer runs the full suite either**, so for most such beads post-push CI is the *first* complete
  run of the suite they ever get, not a redundant re-check of one that already ran. Confirm the
  header line in the submit transcript names your own seat, and see "Which commands are gates"
  above for the full boundary → recipe table and what still does not re-test. **Treat a red
  post-merge CI run as a fix-forward P0, not a routine failure to triage later** — this instruction
  is materially more load-bearing under change-driven selection than it was when it was written.


## Acceptance criteria, attribution, and verification fidelity

Five rules that bind every bead changing a production path. They exist because three production
incidents — `phaze-1b39` (2026-07-28), `phaze-b2qs9`/`phaze-u1n7j` (2026-08-12/13) and
`phaze-3ea41` (2026-08-14) — shipped through a green suite by the same mechanism: a change is
justified by an **argument** about equivalence or bounds; the argument is verified against a
**proxy that structurally cannot exhibit the failure**; green CI is then read as confirmation of
the argument rather than of the proxy; and production is the first place the real input class ever
meets the code. Each rule below is checkable against a diff, and
[ADR-0012](docs/design/0012-verification-fidelity-and-operator-attribution.md) argues each one
against all three incidents with an explicit would-have-caught / would-**not**-have-caught verdict.
Read the verdicts before applying a rule: none of the five catches all three, and knowing which
one is doing the work on a given change is the point.

**1. An acceptance criterion is discharged by a test or by the operator — never by prose.** For
every criterion on the bead, name the test that exercises it, or the recorded operator amendment
that changed it. A criterion you reasoned about in a decision record is not met. Ambiguity goes
back to the operator as a question. **Narrowing a criterion is allowed and sometimes right** — it
is an operator action, recorded before submit, with the remainder filed as its own bead
(`phaze-tzy6s.13` → `phaze-fk1ww` is the worked example). Narrowing it silently and calling it
satisfied is what broke `phaze-3ea41`: its criterion *"existing audio-file analysis is unchanged"*
was replaced in prose with the narrower *"the audio stream is bit-identical"*, which was **true,
verified, and about the wrong quantity** — the container had changed to Matroska, `es.MetadataReader`
reads no duration from Matroska on the deployed platform, and zero duration produced zero windows for
all 11,428 files in the corpus. (This sentence used to attribute that to TagLib; `phaze-gppj2`
measured that the responsible component is the platform's essentia wheel, not a verified TagLib
behaviour. The lesson is untouched — it is what caught the misattribution.)

**2. "Operator decision" is a citation, not an emphasis marker.** Any text claiming one — commit
message, PR body, decision record, bead, code comment — carries **the question as it was put, the
answer as it was given (quoted), the date, and a pointer to the durable record**. The durable
record is a bead comment or an ADR section; a commit message and a PR body are neither, because
both are written by the implementer at submit time and read by nobody afterwards. The attribution
extends no further than the question asked: when implementation reveals a second decision inside
the first, that is a **new question for the operator, not a corollary**. The symmetric rule also
holds — a decision may not be narrowed past the conditions attached to it. A claim that fails any
of the four is not deleted, it is **relabelled as the implementer's decision**, which is a
perfectly good thing for a decision to be and which invites the review the operator label
suppresses. `ADR-0007` §7 and the operator-decision comment on `phaze-b62ri` are the models to
copy. ADR-0012 §5 inventories every such claim currently in the tree.

**Before writing "operator decision" from memory, check `scripts/recover_operator_decisions.py`.**
The question-as-put and answer-as-given this rule requires are frequently NOT in anything a normal
grep finds: an `AskUserQuestion` exchange is recorded as an assistant `tool_use` (the question, every
option) matched to a `tool_result` (the selection) — neither is a `user` turn, so both are invisible
to a grep of bead comments or of user messages. That blind spot is why ADR-0012 §5 first read six
genuine decisions as untraceable (`phaze-d2hgv`, 2026-08-21). The script recovers both that shape and
plain human-typed turns from local session transcripts, given a date range or a bead id, in output
quotable directly into the durable record — read its module docstring for what it can and cannot
prove (local-only, unversioned, and a null result means "not found here", never "never decided").
**In an `AskUserQuestion` result, quote only the selected option LABEL as the operator's decision —
never an option's DESCRIPTION**, which is the assistant's framing of the choice, not the operator's
words; this exact conflation has produced real citation defects more than once in this repo's
history. The script's own output keeps the two apart under separate headings for this reason.

**3. Verify with the artifact's real consumer, not with the tool that produced it.** A change that
produces a new artifact — a file, a container format, an intermediate, a serialized payload —
names its real consumer, and the test calls that consumer. Validating an artifact with the tool
that produced it proves round-tripping, not compatibility. `phaze-3ea41` **did** ship
real-`ffmpeg`, real-container tests; they asserted the extracted `.mka` was *"decodable by
ffprobe"* — and `ffprobe` reads Matroska duration correctly. `es.MetadataReader`, the consumer that
could not, was never handed the file. The general form of the lesson `D-09` recorded narrowly:

- a claim about **real essentia** is not discharged by a mocked one;
- a claim about a **container format** is not discharged by probing it with the muxer's own tooling;
- a claim about **real multi-hour durations** is not discharged by a short synthetic fixture;
- a claim about **the archive's distribution** is discharged against the archive's distribution —
  a query, not a test. One query over `files.duration` would have stopped `phaze-1b39`.

**And an INDEPENDENT consumer is not automatically a DISCRIMINATING one** (`phaze-wt9vw`) — the
question is not *"is this a different implementation?"* but *"would this tool have **rejected** the
wrong artifact?"*, answered by feeding it one. Measured: `ffprobe`, the obvious independent reader
for a `.wma` tag write, reported `TAG:artist=...` for the **wrong** file too, while
`es.MetadataReader` returned every field empty for it. This **qualifies** rule 3 rather than
replacing it; the argument, the evidence and the would-have-caught verdicts are in
[ADR-0012](docs/design/0012-verification-fidelity-and-operator-attribution.md) §4 G3.

**4. A change to a working production path owes a blast-radius statement.** Three sentences in the
bead or PR before submit, with the population **measured, not adjectival**: *"This changes the path
for `<population>`. What currently works that this could break: `<X>`. The test that proves it still
works: `<T>`."* "Some files" does not satisfy it; "all 11,428 files in the corpus" does. If no test
`T` exists, that is the finding — escalate rather than write a weaker sentence. `phaze-3ea41` was
scoped as "analyze video containers" and silently rewrote the analysis path for the whole archive;
nothing in the bead or the review forced that sentence to be written.

**5. A lesson recorded at one site states its general form, or states why it has none.** When a fix's
decision record or test docstring says *"X cannot be verified by Y"*, the merging seat either names
the general form and where it is now written down, or says why the lesson is genuinely specific to
that call site. One sentence, not optional. This exists because the most expensive component of the
pattern was not a missing lesson but a **captured, correct, un-generalized** one: `CLAUDE.md`
recorded that the long-file test *"proves the claim of a mocked essentia only and always did"* and
scoped it to memory, so three weeks later the same class of gap shipped a container change verified
at the producer's own seam.

## A belief that is true in a neighbouring system is a claim, not knowledge (phaze-0vsqf)

The five rules above assume a check gets scheduled. This is the failure where none is, and it sits
one layer up from all of them.

**A belief carried in from a neighbouring system — another OS, another version of the same tool,
another project with the same tool in it — presents as something you already know rather than as
something you are claiming. Verification fires on claims, so nothing fires.** The defining property
is that a transferred model has **no referent**: nothing to dereference, nothing to 404, nothing to
notice going stale.

**The trigger, which is the actionable half.** Not "be careful" — the rules above exist because
three incidents shipped through seats that were being careful:

> **When a belief about a tool's behaviour is load-bearing for a decision, and you did not read it
> or run it IN THIS ENVIRONMENT, run it. Thirty seconds, every time.**

**"Load-bearing"** is what keeps this from being paralysis: it applies when the belief changes what
you *do* — a design conclusion, a gate you hold or release, a number you cite — not to every
incidental assumption. **"In this environment"** does the other half: the installed version, this
OS, this checkout, not the version documented upstream.

**There is a second form, and it is the harder one to see:** a **verified mechanism vouching for an
unobserved instance** in this system. A seat confirmed a real mechanism (a gate is invisible to the
pytest-matching count for its first ~30–60 s, while ruff and mypy run) and then asserted an
instance it had never observed, labelling it measured — while holding a gate slot on it. Its own
account: *"the mechanism felt like it carried the instance with it."* So: **a mechanism you
verified does not vouch for an instance you did not observe.** Verify the mechanism *and* look at
the case.

**The catalogue lives in
[ADR-0016](docs/design/0016-transferred-model-verification.md), not here** — it opens with the four
instances measured on 2026-08-25 (pytest `addopts` env expansion; Darwin `Pages free` read as
headroom; bh `work.py`'s length across 0.14.0 → 0.15.0; the gate count), each with its evidence
intact, and grows from there under its §8. That split is deliberate: `CLAUDE.md` is read in full by every seat
on every session, so it hosts the fixed-size half — the name, the property, the trigger — and the
list that accretes goes where it is read on demand. **Add new instances to the ADR.** Every entry
there was produced by a careful seat reasoning correctly from a sound model, every one was true
somewhere else, and every one was caught within minutes.

*Its sibling is the section above on citing ADRs by filename, and the relationship is a strict
ordering rather than a repetition:* a bare number is a pointer with **no redundancy**, so a reader
can still try to dereference it and find it missing, and a link check
(`tests/shared/test_adr_citation_resolution.py`) catches the dangling case; a transferred model has
**no pointer at all**, which is why its mitigation cannot be a checker and has to be the condition
above. This repo has already paid for three instances of it separately — `phaze-b62ri` (re-measure
a tool against the version you are running), the repowise 0.44-vs-0.45 entry under *Key concepts*
below, and `phaze-g9cus`'s dangling caller line numbers — each fixed at its own site, none of which
named the general form. Rule 5 above is the obligation those three were owed;
[ADR-0016](docs/design/0016-transferred-model-verification.md) is the payment.
