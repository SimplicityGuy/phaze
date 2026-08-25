# Validation gates, test isolation, and concurrency

> **Extracted verbatim from `CLAUDE.md` on 2026-08-25** when that file was consolidated.
> `CLAUDE.md` keeps the operational rules; this document keeps the **evidence** behind them —
> the dated measurements, the incidents, the negative controls, and the derivations.
>
> Read this when you need to know *why* a rule exists, re-derive a number, or cite one. Every
> figure here carries the date and the command that produced it, because a measurement without
> its provenance decays into folklore. Cross-references to "above"/"below" are internal to this
> document unless they name a `CLAUDE.md` section.

## Which commands are gates, and which only look like one (phaze-jnj90 / phaze-nqawu / phaze-pv3kk)

Read this before citing any command as evidence in a bead. Before 2026-08-21 `just check` ran no
coverage and `bh work check`/`submit` ran no tests at all, and that gap produced epic
phaze-1i0h6's four-of-four unevidenced validation claims. **Since 2026-08-25 (phaze-pv3kk), every
per-bead boundary — `check`, `submit`, `merge`, `merge-main` — stopped running the full suite too.**
They now run a change-driven subset selected by repowise's per-test coverage map (`just
check-fast`), escalating to the full suite whenever that map cannot speak for the change.
Three boundaries still run a full suite unconditionally or near it — `molecule`, `postland` and
`union` — but **no boundary an ad-hoc bead traverses does**: `check`, `submit`, `merge` and
`merge-main` are all fast, and an ad-hoc bead never reaches `molecule`/`postland`/`union` at all.
Citing the pre-2026-08-25 shape of this table as current evidence is the phaze-jnj90 / phaze-nqawu
failure recurring under a new cause: a change-selected run cited as though it were a full-suite one.

### Recipes

| Command | ruff | mypy | tests | coverage | pytest header | evidence |
|---------|------|------|-------|----------|---------------|----------|
| `just test` | — | — | yes, **`-x`: stops at the first failure** | no | **no (`-q`)** | **I** — recipe text via `just --dry-run test`; never executed |
| `just test-cov` | — | — | whole suite | yes, 95% line floor | yes | **M** — run 2026-08-21: 7323 passed, 98.78%, `real 1193.88` |
| `just test-validate` | — | — | whole suite | yes, 95% line floor | yes | **M** — executed as `check`'s delegate in the red-gate run below, and as `check-fast`'s escalation delegate |
| `just check` | yes | yes | whole suite | yes, 95% line floor | yes | **M** — the red-gate run below. Since phaze-pv3kk this is no longer any per-bead boundary's default recipe — it survives as the `postland`/`union` recipe, as `check-fast`'s escalation target, and as the manual escape hatch |
| `just check-all` | yes (via pre-commit) | yes (via pre-commit) | whole suite | yes, 95% line floor | yes | **I** — never executed as one command; measures itself at the next `bh work finish`. The only *routinely traversed* full-suite boundary — `postland`/`union` (below) also run a full suite, but conditionally |
| `just check-fast` | yes | yes | **change-selected subset + a fixed always-run floor**, escalating to the whole suite when the coverage map cannot speak for the change | no on a selected run (a subset's figure is meaningless against the 95% floor and would overwrite the artifact `just branch-check` reads); the full 95% floor on an escalated run, because escalation delegates to `just test-validate` | yes | **M** — measured 2026-08-25 (dev/fastsuite): RUN verdict 417 passed, 1 skipped, 35.57s; ESCALATE verdict on this bead's (phaze-pv3kk's) own diff — 5 changed files with no coverage-map rows — exit 3, deferring to `just check`'s 19:37; FAIL verdict on a dirty worktree, exit 1 |

**M** = the command itself was executed and its transcript read; the cell says which run.
**I** = inferred from the phaze block in `~/.beadhive/config.yaml` plus `bh`'s documented phase
model, **not** executed. Treat an **I** row as a claim to re-measure the first time you hit that
boundary, not as established fact. **Do not upgrade an I to an M without running the command** —
a bare letter with no named run decays into folklore the moment its author leaves the room, which
is why every M here cites its evidence.

**The general form, because this table caught its own author out.** The first draft of it marked
`just check`, `just check-all`, `bh work check` and `bh work submit` as **M** when none of the four
had been run; the reasoning was "`check` delegates to `test-cov`, and `test-cov` was measured."
That is the lesson worth keeping: **an inference that a wrapper is measured because its delegate
was measured is still an inference — the composed command is its own claim.** A wrapper adds
ordering, environment, error propagation and exit-code handling, and every one of those is a place
the composition can fail while each part works. It is the same shape as
[ADR-0012](docs/design/0012-verification-fidelity-and-operator-attribution.md) rule 3 (verify with
the artifact's real consumer, not the tool that produced it), and it will recur in this repo
wherever a change argues it is equivalent because its parts are.

### Boundary → recipe, after phaze-pv3kk — all seven boundaries

**Measured against bh 0.15.0** — `bh --version` → `0.15.0`, 2026-08-25, source root
`/opt/homebrew/Cellar/beadhive/0.15.0/libexec/lib/python3.13/site-packages/beadhive/`. **If your
`bh --version` disagrees with that, this table is unverified for you**: re-derive it with the one
command under "Regenerating this table" below before citing a row. The version sits in this header
rather than in the prose because the four `bh 0.14.0` citations this section used to carry went
stale in silence — the version and the evidence lived apart, so nothing about the table's
appearance changed when the tool moved underneath it (phaze-g9cus).

Every row below carries **two different kinds of evidence, kept apart on purpose**.
`beadhive.config.validate_cmd` — imported live from the installed bh — was **executed** against
the actual `~/.beadhive/config.yaml` block with the `(phase, main_gate)` pair each caller passes:
that is the BLOCK → COMMAND mapping, and it is **M** (dev/fastsuite + dispatcher, 2026-08-25,
against bh 0.14.0; re-executed unchanged by dev/bhcite the same day against bh 0.15.0). Which
`(phase, main_gate)` each `bh work` verb actually passes was established by **reading** the call
sites, not by running them: that is the BOUNDARY → PHASE mapping, and it is **I**. These are different claims — "the resolver returns X for phase Y" is not
"`bh work merge` passes phase Y" — and merging them into one M is exactly the wrapper/delegate
mistake above, one layer down. Only a live run of the verb after the config is applied upgrades a
row's I half; `bh work brief` printing `just check-fast` for `validate_cmd` is live corroboration
of the block, not a boundary run, and does not do that upgrading.

| Boundary | Caller — symbol, and the resolver call as written | Resolves to | Evidence |
|---|---|---|---|
| `bh work check <id>` | `work_submission.py::impl_check` — `validate_cmd(cfg, entry)`, **no phase argument at all** | `just check-fast` | **M** (block→command) + **I** (boundary→phase — with no phase passed there is nothing left to misread) |
| `bh work submit <id>` | `work_submission.py::impl__validate_submit_checkout` — `validate_cmd(cfg, entry, "submit")` | `just check-fast` (or a replayed ledger verdict — see "A gate's M is a property of a RUN" below) | **M** + **I** |
| `bh work merge <id>` → molecule | `work_merge.py::impl__postland_revalidate_bead` — `validate_cmd(cfg, entry, "merge", main_gate=on_main)`, reached with `on_main=False` | `just check-fast` — but **inert under `validation: relaxed`** (the phaze default): a per-bead merge landing into a molecule is not validated at all | **M** + **I** |
| `bh work merge <id>` → main (every ad-hoc bead) | the same site with `on_main=True`, so `merge-main` wins over `merge` | `just check-fast` — **the boundary every ad-hoc bead actually hits**, since an ad-hoc bead (parent = NONE) lands here and never reaches `molecule` | **M** + **I** |
| `bh work finish <epic>` (= molecule pre-land) | `work_merge.py::impl__validate_molecule_checkout`, and `::impl__open_molecule_pr` on the `landing: pr` path — both `validate_cmd(cfg, entry, "molecule")` | `just check-all` — the only *routinely traversed* full-suite boundary (the two rows below also run a full suite, but only conditionally) | **M** (block→command) + **I** (boundary→phase — no `bh work finish` has yet landed under this config; the first one upgrades this row) |
| post-land molecule re-test (`postland`) | `work_merge.py::impl__postland_revalidate_molecule` — `validate_cmd(cfg, entry, "postland")` | `just check` — kept full; see the citation immediately below the table | **M** + **I** |
| union-merge resolution (`union`) | `work_merge.py::impl__merge_bead_no_ff` → `worktree.try_merge_rebase(..., validate_cmd=…)` — `validate_cmd(cfg, entry, "union")` | `just check` — **preserved**, not decided: this is what it already resolved to before phaze-pv3kk, and it was never put to the operator, because leaving an unasked boundary unchanged needs no authority while changing it would | **M** + **I** |

**Regenerating this table, which is the whole point of citing symbols rather than lines.** One
command enumerates the entire boundary population against whatever bh is installed:

```bash
grep -rn "config\.validate_cmd(" --include='*.py' \
  "$(dirname "$(readlink -f "$(command -v bh)")")"/../lib/python*/site-packages/beadhive/
```

Run 2026-08-25 exactly as written, it printed **15 lines — 14 real call sites plus one docstring
mention in `prepush.py`**: the seven boundaries above (7 lines, since `molecule` has two),
`submit --group` and `merge --group` (see the group-merge paragraphs later in this section), and five read-only
consumers — `bh doctor`, `bh hive ready`, `bh work brief`, `bh work show --run`, and the pre-push
lookup. Run it first and read the rows against it: a **newly added** boundary shows up there and in
no per-row citation of any form. One limitation, stated because an unstated one is how this table
failed the first time — the pattern assumes the module-qualified call form
(`config.validate_cmd(` / `api.config.validate_cmd(`), which is what all 14 sites use today; a
future `from .config import validate_cmd` would slip past it.

**Why symbols and a regeneration command, and not line numbers (phaze-g9cus).** This table
previously cited seven `work.py` line numbers — 1909, 2417, 3557, 2923, 2948, 2972 and 3525. bh
0.15.0 split that module into `work_merge.py` / `work_group.py` / `work_submission.py` /
`work_logic.py` and more, leaving `work.py` at **1777** lines, so `sed -n "${n}p"` returned
**empty for all seven**. They did not point at the wrong code — a wrong citation can be read and
recognised as wrong. They **dangled**, which is neither readable nor checkable, and nothing about
the table's appearance changed.

That is this file's ADR-numbering argument one level down: **a pointer with no redundancy cannot be
checked by any tool, so the redundancy has to be written in at authoring time.** `sed -n '3557p'`
succeeds against any file long enough — there is no assertion in `work.py:3557` that a reader or a
tool could falsify. The citation form above is mutually redundant and so fails loudly:
`grep -n "def impl__postland_revalidate_bead" work_merge.py` returns nothing if the symbol moved or
was renamed, and `grep -n 'validate_cmd(cfg, entry, "merge"' work_merge.py` independently
corroborates the phase. The cost is real and small — a symbol names a ~48-line function rather than
one line — but a stale line number recovers nothing, while a symbol recovers its line in one grep.
**This is not an argument from principle: every one of the seven boundaries changed file and line
between the two versions, and every one is still findable today by its function name and its phase
literal. Symbols survived the reorganisation; line numbers survived none of it.**

**The general form, because this bead hit it twice in two different shapes.** A **fixed
enumeration** is a pointer with no redundancy in exactly the same way a bare line number is:
nothing in `{a, b, c, d, e, f}` says whether the list is complete, so it decays in silence and the
next reader inherits it as fact. That is not an analogy — it is the same defect, and the ledger's
field list in "A gate's M is a property of a RUN" below is where the second instance was found. Where a list can grow, name
the optional members *as* optional; where a pointer can move, cite something that fails loudly when
it does.

**And the recurrence is the most interesting fact here, not the citations.** This is the repowise
0.44-vs-0.45 shape — see the `analyzed_commit` entry in the beads section below, which states its
own lesson as *re-measure a tool's behaviour against the version you are running* — recurring
**inside the one table in this file whose declared purpose is to keep measured evidence apart from
inferred**, written the day after that entry landed, and stale the same day it was written. Nothing
here was careless: prose faithful to its author's evidence and stale about the tool is not a lapse
of attention, because **nothing in the prose changes when the tool moves underneath it.** Care
cannot catch that. What catches it is a citation that fails loudly and a command that regenerates
the claim, which is what the two blocks above exist to be.

**Why `postland` stays full, cited in full (ADR-0012 rule 2).** Question as put (dispatcher →
operator, 2026-08-25): *"your decision said 'merge-main too — nothing full-suite before main; CI
catches it after'. Reading bh 0.14.0's source turned up a boundary that decision didn't enumerate:
the postland phase — the molecule post-land re-test, which under relaxed fires only when the base
moved underneath a land. It runs AFTER the land, but while still holding the merge slot, and rolls
back on red, so nobody ever sees a broken main. It currently inherits `validate_cmd`, so pointing
that at the fast recipe silently makes it fast too. Which should it be?"* Answer as given — the
option **label** selected, verbatim (its *description* was the dispatcher's framing and carries no
operator authority; only the label below is the operator's words, "(Recommended)" included since
it was part of that label): *"Keep postland on the full suite (Recommended)"*. Date: 2026-08-25.
Durable record: `phaze-pv3kk`'s bead comments. *(The question above names bh 0.14.0 because that is
the version whose source was read the day it was asked. It is quoted verbatim and is **not**
updated to 0.15.0: [ADR-0012](docs/design/0012-verification-fidelity-and-operator-attribution.md)
(verification fidelity and operator attribution) rule 2 requires the question **as it was put**,
and editing it so the asker appears to have said something they did not would falsify the durable
record this rule exists to protect. The same reasoning keeps the two dated measurements in the ledger section below
as they were written, with re-verification appended rather than substituted — correct a standing
claim, preserve a dated one.)*

**Three things the seven rows understate, re-derived 2026-08-25 against bh 0.15.0 (phaze-g9cus).**
None of these changes a resolved recipe — all seven resolve exactly what the table already claimed.
They are places a row was less specific than the source, and the difference matters when reading a
transcript:

- **`merge` / `merge-main` is a POST-land re-test, not a pre-merge gate.** Its only call site sits
  inside `impl__postland_revalidate_bead`, which runs *after* the `--no-ff` merge while the merge
  slot is still held, and rolls a safe-to-rewrite tip back on red — the same shape the `postland`
  row describes, and why nobody ever sees a broken `main` from it. Whether it fires at all is one
  line in `work_merge.py::impl__merge_bead`, worth quoting because both merge rows depend on it:
  `revalidate = mode == "conservative" or (on_main and mode != "loose")`. Under `relaxed`,
  molecule → `False`, main → `True`.
- **`postland` is the MOLECULE post-land only.** The per-bead post-land resolves `merge` /
  `merge-main`, never `postland`, so those are two separate boundaries rather than a general rule
  and its narrower relative.
- **`union` can only ever fire on a per-bead merge.** It is passed only into
  `worktree.try_merge_rebase`, which has exactly one caller. The molecule land
  (`impl__merge_molecule`) and the batch land both call `merge_no_ff` directly, with no union tier,
  so `union: just check` is unreachable from either.

**An eighth phase exists and is deliberately not an eighth row.** bh 0.15.0 has a `push-main` phase
(`prepush.py::PUSH_MAIN_PHASE`, consumed by `prepush.py::push_main_cmd`); it does not *run* a
command but resolves the one a pre-push gate would look a ledger verdict up under, and it **fails
closed** — with the key unset it refuses rather than inheriting `validate_cmd`, the opposite of the
silent-inheritance hazard below — measured 2026-08-25: `push_main_cmd` returns
`('', '• no work.validate.push-main configured for hive phaze …')`, and no `pre-push` hook is
installed in this clone, so it is doubly inert. Whether it predates 0.15.0 was not established.

**A bounce from `merge-main` is not necessarily a test failure — check whether anything ran
(observed 2026-08-25, dispatcher seat).** On that day `bh work merge` onto `main` failed **3 for 3**
whenever it actually validated, and the failure was not in the tests. The transcript signature:

```
🎯 selector: fail  the worktree has uncommitted changes, which the main clone's index cannot see.
❌ the test selector failed (exit 1); no verdict was produced and nothing was run.
✗✗ … main is RED in combination (exit 1) … Bounced the bead; fix forward.
```

Three beads were set to `review:changes-requested` by a boundary that **ran zero tests**, while
each bead's own gate was green and a full-suite check on the merged tree passed clean; a fourth
escaped only because it replayed a ledger verdict. The condition was not reproducible outside bh's
own invocation and is filed upstream — **so read this as a dated observation, not as a property of
the boundary**, and expect it to be fixed. The `merge-main` row above is unaffected: which recipe
resolves is a separate question from whether the run reaches it.

**The durable half is the general form, and it is this section's own rule in a mirror.** "A gate is
green only if its own pytest summary line says so" has an exact counterpart: **a gate is RED only
if its own pytest summary line says so.** An exit 1 with no verdict is not a failing gate, it is an
**unmeasured** one, and the two demand opposite responses — the first is a regression to fix, the
second is a harness problem to escalate. Here the last line printed says `main is RED in
combination`, which reads unmistakably as the first; the line that tells you it is the second is
three lines earlier and easy to scroll past. A seat that trusts the verdict line starts hunting a
regression that does not exist, which is the phaze-jnj90 / phaze-nqawu family arriving through the
harness rather than the code — the same shape as a green that measured nothing, only inverted.

`validation: relaxed` (the phaze default) does not make an explicit `merge:` key redundant to set
even though it makes it inert today: the phaze block still pins `merge: just check-fast` so that a
future switch to `validation: conservative` does not silently pick up whatever `validate_cmd`
happens to be at that time.

**Negative control, executed against the same live block (dev/fastsuite / dispatcher,
2026-08-25):** drop the `postland` and `union` keys and both resolve to `just check-fast` by silent
inheritance; `molecule` and `merge` do **not** move when other keys are dropped, because an
explicitly-present key returns before the resolver ever consults the fallback. And if `molecule`
itself were ever lost, it too falls to `just check-fast` — at which point **nothing** runs the full
suite before `main`, and every gate still prints green. That is why every boundary above is pinned
explicitly rather than left to inherit, and why deleting any one of these five keys is a regression
even though nothing in the diff around it would look wrong.

**The control cannot cover every boundary, and one sits outside it.** `bh work merge --group`
resolves the bare `validate_cmd` and **no `work.validate` key can reach it** — mechanism in the
group-merge paragraphs later in this section. It is not a key that could be deleted; it is a key that never
existed, so the control has nothing to protect there. Three measurements say what that costs
(dev/bhcite, 2026-08-25, bh 0.15.0):

- **The obvious fix validates clean and does nothing.** `WorkConfig.validate_overrides` is a
  `dict[str, str]`, so `config_schema.WorkConfig.model_validate` **accepts** an invented `batch:`
  key — and a `check:` key — without complaint, while the resolver consults neither. A config that
  looks like it pinned the boundary has pinned nothing. Read the `bh work check` row the same way:
  it is not that a `check:` key is rejected, it is that one would be silently inert.
- **A group of ad-hoc beads lands on `main` through this hole.** `merge_group` takes its base from
  `worktree.integration_base`, which falls back to the integration branch when the members have no
  started container ancestor — so a batch of parent-less beads merges onto `main` validated by
  `validate_cmd` rather than by `merge-main`, the one override whose entire purpose is to make the
  main-landing boundary differ from the intermediate ones.
- **Escalated as `hq-f9w`** (`bh escalate`, tool `bh work merge --group`, 2026-08-25). Read as a bh
  defect rather than a design choice on four grounds: it is the only landing site in 0.15.0 that
  omits the phase; its sibling `submit --group` passes `"submit"`; bh's own telemetry beside it
  already names the phase (`{"bh.work.phase": "batch"}`), so the concept exists and merely never
  reaches the resolver; and the schema accepts the remediation that does not work. **Do not try to
  work around it in `~/.beadhive/config.yaml`** — there is nothing to write there that takes effect.

**For an ad-hoc bead, nothing runs the full suite before `main`.** Ad-hoc beads (parent = NONE)
land through `merge-main`, never through `molecule`/`finish`, so post-push CI becomes the *first*
full-suite run such a bead ever gets. This is the model "Workflow: Features and PRs" already
describes for direct pushes ("On a direct push, CI runs after the fact — nothing gates `main`"),
and its instruction to **treat a red post-merge CI run as a fix-forward P0, not a routine failure
to triage later** is materially more load-bearing under this shape than it was before phaze-pv3kk.

**What this gives up, measured rather than argued.** `phaze-sy8z3` was bounced on 2026-08-25 by
`merge-main`'s **old**, still-full-suite gate: `2 failed, 7985 passed, 3 skipped, 180 deselected in
1305.43s (0:21:45)`, both failures `pydantic_core.ValidationError` on
`ExecuteBatchProposalItem.source_path` — a field `phaze-xzjrr` renamed from `original_path` while
`sy8z3` was in flight. Neither branch was wrong alone; only the combination was, and that is
precisely the class `merge-main`'s full suite existed to catch. That gate ran under the config as
it stood *before* this bead's boundaries went live — under the wiring above, `merge-main` is fast,
and a future instance of the same shape (two independently-green branches, red only in combination)
reaches `main` and is caught by CI instead. Read this as the measured cost of the operator's
decision, not as an argument to reverse it — reversing it is the operator's call, not this table's.

**Escalation is the common case here, not the exception.** Every Jinja template, `pyproject.toml`,
the justfile and all YAML sit outside repowise's per-test coverage map — and phaze renders its
whole UI from Jinja — so a bead touching any of them runs the full suite via escalation, while a
bead touching only well-covered Python selects a handful of tests. Both are correct behaviour, not
a defect to tune. `.git/fast-gate-escalations.log` (untracked, shared by every worktree of the
clone, so it survives the ephemeral bead worktree that wrote to it) records every escalation with
its reason, because "is this gate escalating a lot lately" is a trend question no single transcript
can answer.

**The coverage map is a standing maintenance obligation, not a one-time cost.** `repowise update`
does **not** refresh it — that command's own `--help` scopes it to re-parsing files, the dependency
graph, and git/dead-code artifacts; only `just repowise-coverage` (~21 min: a full `pytest --cov
--cov-context=test` run plus `repowise coverage add`) rebuilds the test-to-code map, and nothing
else does. Measured 2026-08-25: the map (ingested 2026-08-22 at `a3fd169a`) was **105 commits**
behind tip `ec87a670`, with **47 of 272** `src/phaze` files and **48** test files changed since it
was built. The same drift figure read **103** commits against an earlier tip (`190a9e30`) and
**107** against a later one (`0cd4e0fc`) within the same session — quote the tip alongside any
drift number; the number alone is meaningless, the same trap CLAUDE.md's repowise entry already
names for `analyzed_commit`. At high drift a large fraction of changed files demote to `unknown`
and escalate, which is correct behaviour and simultaneously the signal that a refresh is overdue.

**Two numbers, and neither is honest alone.** `just check-fast` measured **417 tests in 35.57s**
when the coverage map can speak for the change, and **19:37** (the full `just check`) when it
cannot. The first number alone implies the gate is always fast; the second alone implies it does
not work. Cite both together or cite neither.

**`bh work merge <id>` re-validates nothing when it lands into a molecule under `validation:
relaxed`** (the phaze default) — unchanged from before phaze-pv3kk, and still by design: submit
already validated this exact tip, and it is `finish`/`molecule` that catches "green in isolation,
red in combination" for a molecule's children. **When the same `merge` verb instead lands the bead
on `main` (every ad-hoc bead), it *is* validated** — that path resolves `merge-main`, a different
key from `merge` — but as of phaze-pv3kk that validation is `just check-fast`, not `just
check-all`. Set `validation: conservative` in the phaze block if a molecule ever needs re-testing
after every per-bead merge.

**`bh work merge --group <ids>` re-validates nothing either — and its own `--help` says otherwise
(phaze-hr66n).** Measured 2026-08-21 on the phaze-jnj90/phaze-nqawu group merge: the verb took
**6 seconds** and printed **no pytest header at all**, only

```
✓ validation verdict reused (sha fafd16b, tree 4fb18ff, recorded 2026-08-21T12:50:37-07:00)
```

**Record the MECHANISM, not the absence.** This is a deliberate verdict cache, not an omission, and
it is sound: `submit --group` had already validated this exact tree with this exact command, and the
batch branch had not moved between submit and merge. Written as a bare "does not validate" it reads
as a defect and invites someone to "fix" it. Read from the installed source
(`beadhive/work_group.py:587`, bh 0.15.0), the landing check is
`clean_checkout(..., validate_cmd(cfg, entry), reuse=True)`, and the comment above it states the
rule: *"the ledger is keyed on (TREE, cmd_hash), so a hit here means this exact content already
passed this exact command … Anything else (a rebase onto a moved base, a changed command, a stale or
red entry) misses and runs."* **The printed `sha` is therefore DISPLAYED, not KEYED** — reading
"(sha, tree)" off that transcript line as the cache key is the natural mistake and it is wrong, since
a different commit with the same tree hits the same entry. That agrees with the independent source
read in the next section (phaze-qsyc0); the live ledger, re-read 2026-08-25, stores
`{tree, cmd_hash, rc, at, host, sha, shas}`.

**It resolves a documentation conflict, and that is the durable half.** `bh work merge --help` says
group mode will "validate the shared `wt/batch/<group>` branch once" (bh 0.15.0, re-read 2026-08-25;
the 2026-08-21 run recorded the then-current wording as "validates the shared branch once" — the same
claim, differently worded, so the conflict has survived at least one release). `work.validation:
relaxed`
scopes validation to submit plus the assembled-molecule pre-land. Those two disagree for a group
merge, and **measured, `relaxed` is the accurate one and `merge --help` is the misleading one.** A
future reader hitting both sentences should not have to re-derive that.

**What a group-merge MISS would run, and why this is prose rather than an eighth row above.**
`merge_group` calls `validate_cmd(cfg, entry)` with **no phase argument at all** — unlike
`submit --group`, which passes `"submit"` (`work_group.py:413`) — so it can only ever resolve to the
bare `validate_cmd`, which is `just check-fast` today. **No `work.validate` override key can point it
anywhere else**, which makes it the one boundary the five-pinned-keys negative control above cannot
protect: if `validate_cmd` ever reverts to the machine-wide lint-only default, a group merge follows
it with nothing pinned in the way. Do not be misled by the telemetry beside it —
`otel.count_validation(..., {"bh.work.phase": "batch"})` labels the span `batch`, and that string is
**never** passed to the resolver, so a `batch:` key in the config would be inert. This is a
BOUNDARY → PHASE read from source (**I** in this section's sense) paired with the same **M** block →
command mapping the table already carries. It is deliberately **not** grafted on as an eighth row,
and the reason survived the table being rebuilt: every row there is keyed on the `(phase,
main_gate)` pair its boundary passes, and a group merge passes **no phase at all**, so it has no
key to sit under. It belongs in prose, here, where the absence itself is the subject.

*(When this paragraph was written, that table cited seven `work.py` line numbers, and **bh 0.15.0's
split of `work.py` into `work_merge.py` / `work_group.py` / `work_submission.py` left none of them
resolving** — `bh --version` → 0.15.0, checked 2026-08-25, against a table written against 0.14.0
the same day. Re-deriving all seven was its own measurement pass and was done as `phaze-g9cus`; the
table above now cites symbols, and carries the command that regenerates it.)*

**What the 2026-08-21 measurement does NOT license.** It was taken before phaze-pv3kk, when
`validate_cmd` was `just check` rather than `just check-fast`. The *reuse* is unaffected — the ledger
key is `(tree, cmd_hash)` and that mechanism is unchanged — but the recipe a **miss** would run is a
different command now, so nothing here says what a group merge costs on a miss today. Nor does it
upgrade any **I** half in the table above: a run that replayed a verdict resolved no phase to a
command, and a *group* merge is a different call site from either `bh work finish <epic>` or
`bh work merge <id>`, both of which remain unrun.

**The ownership rule is not uniform across the mutating verbs (phaze-hr66n).** Re-verified 2026-08-25
against bh 0.15.0:

- `bh work submit <id>` takes `--as dev/<name>` and enforces the claim — a seat that is not the
  holder is refused (`held by dev/<name> — <you> no longer holds the claim`, measured 2026-08-21).
- `bh work check <id>` has **no** `--as` and verifies no ownership at all; its only options are
  `--hive` and `--help`.
- `bh work merge <id>` has **no `--as` at all**, and passing it is a usage error rather than a
  no-op: `bh work merge <bogus-id> --as dev/gatetable` exits **2** with `No such option: --as`,
  rejected during option parsing before the bead is ever looked up.

**This is the wrapper-vs-delegate lesson above, one step sideways: an inference from one verb to its
SIBLING VERB is still an inference.** `submit` requiring an identity says nothing about whether
`merge` accepts one, and a seat that assumes it does gets an exit 2 that reads like a merge failure.

`bh work check` / `submit` / `merge` / `merge-main` run `just check-fast` **because**
`~/.beadhive/config.yaml` gives the phaze rig `work.validate_cmd: just check-fast` plus a
`work.validate` block pinning `merge` (`just check-fast`, inert under `relaxed`), `merge-main`
(`just check-fast`), `postland` (`just check`), `union` (`just check`) and `molecule` (`just
check-all`, unchanged). That file is **outside this repo and shared by every hive on the
machine**; the global default at the bottom of it is still `sh -c "just lint && just typecheck"`,
which runs no tests.

**The config file now has two silent failure modes, not one.** `validate_cmd` reverting to that
machine-wide default is the original phaze-nqawu failure — a gate that runs no tests while
printing success. The second is new, and quieter: **any one of the five `work.validate` override
keys above going missing.** A dropped key does not error; the boundary simply inherits
`validate_cmd`, and since phaze-pv3kk that inherited default is itself *fast*, so the boundary
keeps printing green with no output difference except a shorter run — see the negative control
above. `tests/shared/test_validation_gate_recipes.py` guards the justfile half of this; nothing in
this repo can guard the config half.

**That paragraph describes what submit runs on a ledger MISS.** `bh work submit`'s own `--help`
promises to validate "from a clean checkout", and this repo read that as meaning every green submit
is its own pristine-checkout, full-suite measurement. It is not: the verdict ledger is keyed on
`(tree hash, validate-cmd hash)` and **not** on which command validated, so a `bh work check`
verdict earned in the **seat worktree** is replayed by a submit that never makes a checkout at all
(phaze-qsyc0, established 2026-08-24 against bh 0.14.0; **re-verified unchanged against bh 0.15.0
on 2026-08-25**, phaze-g9cus). A submit is evidence of a full-suite run
only when its transcript carries the pytest summary and coverage lines; since phaze-pv3kk a submit
is evidence of a *change-selected* run only when its transcript carries `check-fast`'s RUN verdict
(or its escalation to `just check`'s pytest summary and coverage lines); a replay prints a
`validation verdict reused (…)` line instead, either way. The next section has the mechanism, what
invalidates a verdict, and the general form.

## A gate's M is a property of a RUN, never of a COMMAND (phaze-qsyc0)

`bh work submit` does not always validate. It consults a **verdict ledger** and, on a hit, skips
the throwaway checkout entirely and prints a line like:

```
validation verdict reused (sha 0fe39f1, tree f7beb62, recorded 2026-08-23T20:40:28-07:00)
```

That submit ran no tests. It is not unsound — but it is **not its own measurement**, and the table
row above would otherwise read as though it always were. Established 2026-08-24 from
`beadhive/validation_ledger.py`, `beadhive/config.py` and the live ledger file, against bh 0.14.0
— and **re-verified against bh 0.15.0 on 2026-08-25** (phaze-g9cus), which left the mechanism
untouched and corrected two of the details below:

- **The key is `(tree hash, validate-cmd hash)` — the validating COMMAND is not part of it.** So a
  `bh work check` verdict, earned in the **seat worktree**, is replayed by `bh work submit`, whose
  `--help` promises a **clean checkout**. That is deliberate (bh-i0p1.4), and `check` records only
  from a CLEAN worktree — but the two are interchangeable under one key, so "submit validated from
  a pristine checkout" can be discharged by a run that never made one. Both use phaze's
  `work.validate_cmd`, so their hashes always match — `just check` until 2026-08-25, `just
  check-fast` since. **M** — the ledger at `.git/bh-validation-ledger.json` stores `{tree,
  cmd_hash, rc, at, host, sha, shas}`, plus a **conditional eighth `report`** written only when the
  validate command emits a parseable test summary. Re-read 2026-08-25 against bh 0.15.0: all **49**
  live entries carry exactly those seven and **none** carries `report`. **Of the seven, two are the
  key and five are not** — `validation_ledger.py::record` says so, and it is worth quoting because
  the transcript line invites the opposite reading: *"Commit shas are METADATA, never identity
  (bh-ku9n9.3): `sha` is the one observed by this run, `shas` every distinct one seen at this tree
  — the join key a later historical upload needs … Nothing here reads them back. `host` is
  diagnostic-only too."* Written as a flat list of names, this enumeration silently lost `shas`
  once already; that is the fixed-enumeration trap named under the boundary table above, which is
  why the optional field is named *as* optional here.
- **Whether a replayed verdict can carry test counts is a property of the LEDGER ENTRY, not of
  replay — so check, rather than assuming either state.** bh never invokes a test runner: it exports
  `BH_TEST_REPORT_DIR` into every validation subprocess and parses whatever JUnit XML turns up
  there, and a hive opts in from its own test config (`beadhive/test_report.py`). An entry recorded
  while the hive opts into nothing is **rc-only** and can supply no pytest summary line at all; one
  recorded after it opts in carries a `report`. **Both shapes coexist in the same file**, because
  the field is per-entry and nothing rewrites old entries. Measured 2026-08-25: **1 of 56** entries
  carried `report` (`{"tests": 8041, "passed": 8038, "failures": 0, "errors": 0, "skipped": 3}`);
  the other 55 were rc-only.

  **The field tracks the TREE THAT WAS VALIDATED, which is what makes the ratio predictable rather
  than merely unstable.** A clean-checkout gate validates the tree under test, so the JUnit opt-in
  is in effect only where *that tree* carries it — bh's ingest behaves correctly whether or not a
  report appears. The opt-in (`tests/bh_test_report.py`) reached `main` on **2026-08-25**: entries
  recorded against trees predating it are rc-only, entries against trees descended from it carry
  counts, and nothing rewrites the old ones. That is why both shapes sit in one file, and why the
  ratio climbs from here rather than holding at the figure above.

  **An absent `report` says nothing whatever about the run — only about the tree.** Measured on this
  bead's own first gate, the cleanest available case because the suite indisputably ran: a green
  **8027 passed** full-suite run, on a tree cut before the opt-in landed, recorded **rc-only**. A
  seat that finds no counts on a replayed verdict and concludes "nothing ran" has it exactly
  backwards. **Do not read the ratio forward** — it is one command:

  ```bash
  python3 -c "import json,subprocess;p=subprocess.check_output(['git','rev-parse','--git-common-dir'],text=True).strip()+'/bh-validation-ledger.json';d=json.load(open(p));print(sum('report' in e for e in d),'of',len(d),'entries carry counts')"
  ```

  **`--git-common-dir` is load-bearing, not fastidiousness — do not "simplify" it to `.git/`.**
  Inside a bead worktree `.git` is an 87-byte **file** pointing at the real gitdir, so the obvious
  path raises `NotADirectoryError: [Errno 20] Not a directory` (measured 2026-08-25) — it would
  break in exactly the place a seat runs it, and work fine wherever you tested it. The ledger lives
  in the main clone regardless of which worktree asks.

  **None of this makes an rc-only verdict weaker.** `rc` is authoritative by design —
  `test_report.py` states it as the first of three binding constraints: the report is *detail, never
  a verdict*, and **may never upgrade one**; a missing report is explicitly "not a failure". An
  rc-only verdict carries no detail, not less authority.
- **The ENVIRONMENT is not in the key, and phaze does not use bh's mitigation for that.** bh
  re-derives the environment from the tree via `verify: true` worktree-init rules; phaze declares
  **none** (its three init rules carry no `verify` flag — only the homelab rig has one), so no
  environment establishment happens in either writer. `TEST_DATABASE_URL`,
  `MIGRATIONS_TEST_DATABASE_URL` and `PHAZE_REDIS_URL` are shell exports, and a tree hash cannot
  see them. In practice `just check` and `just check-fast` both self-provision a seat when they are
  unset (phaze-bk9el.23), so isolation still holds — but the key does not cover what the pytest
  header records.
- **What DOES invalidate a verdict:** the TTL (`work.ledger_ttl`, default **P1D** — 24 h; bh's own
  docstring says operators are expected to tune this **down**, not up); a red verdict, which is
  recorded but never reused; any edit to the `justfile`, which is *in* the tree and so changes the
  tree hash; and any change to phaze's `validate_cmd`, which changes the cmd hash. That last one
  covers the phaze-nqawu hazard — a verdict earned under the old lint-only command cannot be
  replayed under `just check`, and (since phaze-pv3kk) a verdict earned under `just check` cannot
  be replayed under `just check-fast` either, for the same reason.
- **There is no flag to force re-validation.** `bh work submit --help` offers none (re-read
  2026-08-25 on bh 0.15.0). The knobs are `work.ledger_ttl` and `work.always_run` in
  `~/.beadhive/config.yaml` (phaze sets neither), or deleting `.git/bh-validation-ledger.json`.
  **`work.validate_precheck` no longer exists** — it was named here until phaze-g9cus, and a
  `grep -rn validate_precheck` over the whole 0.15.0 package returns nothing. `work.always_run` is
  the nearest current thing and is not the same knob: it names a command run *before* any recorded
  verdict is honored — the small set a tree hash cannot vouch for, because it reads git metadata
  rather than file content — and a non-zero exit **refuses the hit and seals the ledger for the
  rest of the process**. It is paid on every hit, so it is seconds-not-minutes work by design.

**The general form, and it is the next case after the wrapper/delegate rule above.** That rule says
an inference that a wrapper is measured because its delegate was measured is still an inference.
This is sharper: here the composed command genuinely **was** measured — once — and the question is
whether **this invocation** measured anything. So: **a gate's M is a property of a RUN, never of a
command.** A command that can replay a cached verdict has two paths, and only the transcript of the
invocation in front of you says which one you got. This is why the rule below — a gate is green only
if its own pytest summary line says so — already catches it: a replayed verdict prints no pytest
summary line and no coverage line at all. If you cannot see those lines, you did not measure
anything, whatever the command was called.

**`just test` is deliberately retained and is deliberately not a gate.** `-x` gives a tight
edit/run loop, and `-q` gives dot-density. Both cost evidence — a red `-x` run characterises only
the prefix of the suite before the first failure, and `-q` suppresses the pytest header entirely.
Use it while iterating; cite `just check-fast` (or `just check`, if that is what you actually ran
by hand as the manual escape hatch named in the recipes table above).

**Dropping `-x` from the gate has a measured price, and it is the right one to pay on the full
suite.** The red-gate run above put a deliberately failing test in `tests/shared/` and the gate
still took **19m18s** to reach `1 failed, 7329 passed` — a bead with a genuine failure pays full
freight to learn it, and pays it again on every re-run. That is the correct trade for a *full-suite
gate*, whose job is to characterise the whole suite rather than to fail fast: a truncated prefix
tells you nothing about the 7000 tests after the first failure. `just check-fast` earns most of
that speed back differently — by running fewer, targeted tests rather than by stopping early — so
it does not reopen this trade-off; it still runs every selected test to completion. Iterate with
`just test`, which is fail-fast precisely so you do not pay twenty minutes while debugging. **Do
not "optimise" `-x` back into `just check` or `just check-all`** — you would be trading complete
counts for wall-clock at the boundaries where completeness is the entire point.

**Consequence: a submit's cost is now conditional, and "usually fast" is not the same claim as
"always fast."** Before phaze-pv3kk, "a submit costs a full suite run" was unconditionally true.
Since 2026-08-25 it is change-selected — typically well under the old ~20 minutes — unless the
diff escalates, in which case it costs exactly what it always did. Export a per-worktree seat
(`just test-db-for <name>`) before submitting either way, or the gate falls back to the shared
`phaze_test` seat and two concurrent submits collide on the session advisory lock described below.

**Evidencing seat isolation.** The gate now prints its own header, so the transcript of the gate
run is the evidence — no separate invocation is needed. Two older techniques still work when you
need the header from a command that suppresses it, and are recorded here because three agents
independently reinvented the first one: `uv run pytest --collect-only` is exempt from the session
lock (it never opens the schema) and prints the header, and launching a second pytest against the
same DSN mid-run and capturing its refusal (`already owned by phaze-pytest-session-lock pid=...`)
is stronger evidence still. Both prove the **seat**; only the gate's own header proves the **run**.

## Test databases

The test suite resolves its target from `TEST_DATABASE_URL`, validated by a single guard in
`tests/db_guard.py`. Two rules, both enforced:

- **The database name must contain a `test` segment** — `phaze_test`, `phaze_test_<bead>`, and
  `phaze_<bead>_test` are all accepted; `phaze` and `phaze_prod` are not. A name that fails this
  check **errors the run**. It does not skip. A skip would silently drop ~18 integration tests
  while pytest still reported green, which is exactly the defect this guard replaced.
- **Port 5433, never 5432.** 5433 is the ephemeral test harness (`just test-db`); 5432 is
  reserved for the developer's own database. The fixtures create and drop schema, so a default
  pointing at 5432 is a live-data-loss shape, not just a confusing error. An unset
  `TEST_DATABASE_URL` defaults to `postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test`,
  so the bare `uv run pytest` above stays safe and needs no extra setup.

Every run prints its resolved target in the pytest header
(`phaze test database: 'phaze_test' on localhost:5433 (from TEST_DATABASE_URL, exclusive)`) — check
it before trusting a green run. `exclusive` means this pytest process holds the session lock
described below. The header renders the other state as the literal
`unlocked (Postgres unreachable or bypass set)`, and it has **three** causes, not one:

1. **Postgres was unreachable** at session start (no harness up) — the lock could not be taken.
2. **`PHAZE_TEST_DB_ALLOW_SHARED=1`** was set, deliberately bypassing the guard.
3. **`--collect-only`**, which is exempt: it imports modules and never opens the schema, so it
   cannot corrupt a live run and stays usable for inspecting a suite *while* it runs.

In cases 1 and 2 the run is *not* protected and its failures are not trustworthy under any
concurrency.

**`just check` provisions a seat for itself when you have not (phaze-bk9el.23).** With
`TEST_DATABASE_URL` unset, `just test-validate` — the test step both gates run, and therefore what
`bh work check` and `bh work submit` run — derives a seat from the worktree (the branch name for
legibility plus a digest of the absolute worktree root for uniqueness) and provisions it through
the same `scripts/provision-test-seat.sh` that `just test-db-for` runs. Derived seats are named
`auto_<branch>_<hash>_<hash>`, show up in `just test-db-seats`, and are reclaimed by
`just test-db-reclaim` under the usual rules — O1 frees them when the worktree that minted them is
removed, which is the normal end of `bh work merge`.

Two things follow, and they pull in opposite directions:

- **A solo worktree with no seat still just works, and is now genuinely isolated.** Until
  phaze-bk9el.23 that path landed on the SHARED `phaze_test` + Redis DB 0, so any concurrent seat
  that forgot to export its own rig collided there. phaze-ieqg's advisory lock refuses the second
  pytest rather than corrupting both — a red run that passes on isolated re-run, the shape named
  below as the worst possible one.
- **This is a floor, not a substitute for `just test-db-for <name>`.** Export your seat anyway.
  The derived name is opaque, which makes a `just test-db-seats` listing harder to read back to a
  bead, and a seat you did not name is a seat you will not think to release.

An exported `TEST_DATABASE_URL` is still honoured **verbatim** and provisions nothing — CI depends
on that, since it exports its own DSN against a 5432 service container.

**Never share Postgres OR Redis between concurrent agents.** Both are stateful, both are shared by
default, and both must be isolated per worktree. Saying "test database" here was the phaze-fwo7
defect: it taught agents to isolate Postgres and left every seat on the same logical Redis.
That mistake has a general form, and a third instance — see "Every writable path shared by
concurrent seats is a collision surface" below.

```bash
just test-db-for <name>    # derives <derived> from <name> (see below), creates
                           # phaze_<derived>_test + phaze_<derived>_migrations_test,
                           # allocates a dedicated Redis logical DB, and prints all three exports
```

`<name>` is not used verbatim (phaze-fmfk): `test-db-for` normalizes it into `<derived>` by
turning hyphens into underscores and appending a short hash of the original `<name>`, e.g.
`review-polite` → `phaze_review_polite_7a21035a_test`. The hash exists so that `my-seat` and
`my_seat` — which normalize to identical text — don't silently collide onto one shared seat, the
same class of defect described in "Why Redis matters" below; do not "simplify" the recipe back to
a bare `phaze_<name>_test` substitution. **Always copy the exports the recipe prints** rather
than hand-constructing the DSN from `<name>` yourself — the two agree only when `<name>` has no
hyphens.

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_<derived>_test"
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_<derived>_migrations_test"
export PHAZE_REDIS_URL="redis://localhost:6380/<index>"
```

**Why Redis matters as much as Postgres.** Two redis-backed test modules
(`tests/review/routers/test_execution_dispatch.py` and `test_agent_exec_batches.py`) run a global
`scan_iter`+`delete` sweep over `exec:*`, `exec_progress_req:*` and `execdispatch:*` in fixture
setup *and* teardown. On a shared logical database one agent's fixture deletes another agent's live
keys mid-test, and assertions that count the keyspace see foreign keys. The result is a failure
indistinguishable from a real regression that passes on isolated re-run — the worst possible shape,
because it trains reviewers to dismiss red runs.

Redis DB indices are allocated from an atomic registry on the test container (DB 0 holds the
registry; seats get 1 upward), so re-running `test-db-for` for the same worktree is idempotent and
two worktrees can never collide. The container is started with 64 logical databases; allocation
past that fails loudly rather than wrapping onto a shared index. **Leaving `PHAZE_REDIS_URL` unset
is still valid for single-agent and CI runs** — it defaults to DB 0.

**Give the index back when a worktree is done — and never tear the harness down to free one
(phaze-68wky).** Allocation used to be a monotonic counter with no reclaim, so every seat that ever
ran `test-db-for` burned an index permanently; the counter walked past the cap (68, 73, 74 and 80
were seen live) and then refused every new seat, offering only `just test-db-down` as the way out.
Five separate agents hit that wall, and each worked around it differently — one by dropping Redis
isolation altogether, i.e. straight back into the defect above. Three recipes replace that, none of
which stops, clears or recreates anything:

```bash
just test-db-seats                 # who holds which logical DB, and the evidence for each verdict
just test-db-release <name>        # hand ONE finished seat's index back (run this when a worktree is done)
just test-db-reclaim               # dry run: which seats a sweep would free
just test-db-reclaim --apply       # free every seat that is no longer in use
```

A seat is in use, and left alone, while **any** of three signals holds:

- **L1** — a client is connected to its Redis logical DB.
- **L2** — a Postgres backend is on `phaze_<seat>_test` or `phaze_<seat>_migrations_test`. pytest
  holds one for the whole session and often holds no Redis connection at all, so this is what
  protects an idle-looking suite. It is **mandatory for `reclaim`**: if the Postgres container
  cannot be reached, the sweep *refuses* rather than reading unknown as free (phaze-gmkua).
  `--no-postgres-check` waives the refusal, not the evidence — it warns, and a reachable Postgres
  is still consulted and still protects a seat.
- **L3** — its lease is unexpired. `test-db-for` stamps the lease on every call and it runs
  `PHAZE_TEST_REDIS_SEAT_LEASE_HOURS`, default 72.

**Two conditions override L3 — a live lease is not on its own enough to keep a seat.** Neither ever
overrides L1 or L2; nothing reclaims a seat that is demonstrably in use.

- **O1 — its origin worktree is gone.** The sweep records the directory that ran `test-db-for` and
  frees the seat once that directory no longer exists, *however much lease is left*. A seat with 60
  hours still on its 72-hour lease is reclaimable the moment its worktree is removed — which is the
  normal end of `bh work merge`, so this is the common case, not the exotic one. Read it as the
  intended design: the worktree is gone, so the seat cannot come back.
- **O2 — its index is past the container's database count.** Redis refuses `SELECT` there, so no
  client can be using it. These are the 68/73/74/80 allocations the old counter minted.

Allocations made before this existed have no lease stamp, so their age is genuinely unknown; the
sweep reports them and leaves them alone unless you pass `--include-unstamped`.

`test-db-release <name>` is the operator-driven path and deliberately ignores L3/O1/O2 — naming a
seat *is* the assertion that it is finished — but it still refuses on L1/L2 unless you add
`--force`. A registry value that is not an index is reported and released: no logical database can
correspond to it (phaze-nbfuc).

A freed index is not first in line for the next seat. The allocator prefers an index no seat has
ever held over recycling one, because a shell with a stale `PHAZE_REDIS_URL` still exported is the
one hazard no liveness check can see — so **after a sweep, expect the next `test-db-for` to hand out
a fresh index rather than one it just freed** (phaze-08sww). Recycling happens only once the space
genuinely demands it.

`scripts/redis-seat-registry.sh` documents the full rule set. **A full registry is never a reason to
run `test-db-down`** — reclaim first, and only consider raising `PHAZE_TEST_REDIS_DATABASES` if the
sweep frees nothing.

## One database, one pytest process (phaze-ieqg)

`TEST_DATABASE_URL` isolates a **worktree**. It never isolated a **process**, and that gap — not
some undiscovered third shared surface — is what made full-suite runs untrustworthy under
concurrency for two dispatch rounds.

Two pytest processes on one database destroy each other. `tests/conftest.py`'s session-scoped
`async_engine` runs `Base.metadata.create_all` at session start and `drop_all` at session teardown,
so whichever process finishes **first** drops the schema out from under the other. Measured:
`pytest tests/analyze/routers` (61 tests, 6.8 s) and `pytest tests/review` sharing one database left
the second run at **238 failed + 12 errors**, all `UndefinedTableError: relation "agents" does not
exist`, all green on isolated re-run. The most common way to hit this is the most natural one:
re-running a subset "to check something in isolation" in a second terminal while the full suite is
still going, or a reviewer running the suite in the same worktree the developer is working in.

`pytest_sessionstart` now takes a session-level Postgres advisory lock on the resolved database and
holds it for the whole run. A second process is **refused before collection** with the holder's pid
and the fix, instead of silently corrupting both runs. `PHAZE_TEST_DB_ALLOW_SHARED=1` bypasses it;
pytest-xdist against one database is this exact defect and is not a reason to set it (CI keeps every
DB bucket serial for the same reason).

Two suites in two worktrees with their own `test-db-for` databases are unaffected — that is the
supported way to run concurrently, and it is verified green.

## `pg_locks` and `pg_stat_activity` are cluster-wide — always scope them

A per-worktree database isolates table data completely and the system catalogues not at all. Any
test that reads `pg_locks` or `pg_stat_activity` sees **every** seat's backends. Two concurrent
suites, each correctly isolated, both went red on
`tests/integration/test_tag_bulk_write_advisory_lock.py` with `assert 2 == 1` — an advisory-lock
count that had picked up the other seat's copy of the same application key. The three
`_wait_for_blocked_waiter` barriers had the nastier version: `SELECT EXISTS (SELECT 1 FROM pg_locks
WHERE NOT granted)` is satisfied by any blocked backend in the cluster, so the barrier returned
before the test's own waiter had queued and everything after it raced.

Scope every such query with `current_database()`. For an advisory-lock count use
`and database = (select oid from pg_database where datname = current_database())`; for a
"somebody is blocked" barrier join the waiting backend instead
(`pg_locks.database` is NULL for `transactionid` locks, so the column filter never matches there) —
`tests/db_guard.BLOCKED_WAITER_SQL` is the shared correct form.
`tests/shared/test_cluster_wide_catalog_scoping.py` fails the build on an unscoped query.

## Never `just test-db-down` while another seat is running

`phaze-test-db` and `phaze-test-redis` are **one shared pair of containers**; `test-db-for` carves
seats out of them rather than giving each seat its own. On 2026-07-29 a `test-db-down` + recreate
mid-round destroyed 89 per-worktree databases and reset the Redis allocation registry while five
suites were in flight, producing the same false-red signature from a different cause. `test-db-down`
now refuses while any client is connected to a `phaze%test` database, listing the seats it is
protecting; `PHAZE_TEST_DB_FORCE_DOWN=1` overrides for genuinely stale connections.

If you got here because Redis logical-DB allocation ran out, this is the wrong tool entirely: use
`just test-db-reclaim` (above), which frees the seats nobody is using and touches neither
container. Reaching for `test-db-down` to free an index was the phaze-68wky defect, and it is how
the 2026-07-29 incident starts.

## Every writable path shared by concurrent seats is a collision surface (phaze-rlshw)

Postgres was the first instance and Redis the second — and the phaze-fwo7 note above records what
enumerating instead of generalising costs: saying "test database" taught agents to isolate Postgres
and left every seat on one logical Redis. The **scratchpad** is the third instance, so carry the
general form rather than a third bullet: **anything a concurrent seat can open for writing must
carry the bead id in its path** — databases, cache namespaces, log files, transcripts, scratch
directories, report files. The surface is "writable and shared", not "stateful service".

**Evidence-bearing paths are the costly ones, because they fail silently.** A clobbered database
raises; a clobbered gate log does not. It yields a *confident wrong citation* — a seat reading back
a pass/fail count, a coverage figure or a `phaze test database:` header that belongs to some other
run. That is the phaze-jnj90 / phaze-nqawu family, a signal that looks like evidence, arriving
through the **harness** rather than through the code or the config; and on this repo the transcript
of the gate run *is* the deliverable.

**The scratchpad is per-SESSION, not per-SEAT, and its "session-specific" label is the whole trap.**
Every sub-agent of one dispatch is handed the *identical*
`/private/tmp/claude-501/<repo-slug>/<session-uuid>/scratchpad`. "Session-specific" is true and
reads as isolation: it isolates you from other *sessions*, never from your sibling *seats*.
Measured 2026-08-22 on the six-way dispatch of phaze-bk9el.25–.29 + phaze-4jvy1 — three seats
independently redirected `just check` into `<scratchpad>/check.log`, `>` truncated at open, and the
runs interleaved. `dev/scanauth` grepped that log for its pytest header, read another seat's
database name, correctly recognised the two-pytest-processes-on-one-database shape described above,
and killed a **healthy** gate: right reasoning, wrong evidence, ~20 minutes of full-suite runtime
lost. Nothing in Postgres, Redis or git was corrupted — per-seat isolation held across all six.

**The convention:** write scratch output under `<scratchpad>/<bead-id>/`, or simply inside the
bead's own worktree, which is per-bead by construction and needs no new convention at all. Prefer
the worktree for gate logs — **and name that log `*.log`** (phaze-5c0o1). Preferring the worktree is
safe only because `.gitignore:83` carries `*.log`: `git status --porcelain` respects `.gitignore`,
so `check.log` in the worktree is invisible, while `check.out`, `gate.txt` or any name matching no
ignore pattern is untracked, dirty, and refused by `just check-fast`'s selector
(`scripts/select_impacted_tests.py::assert_clean_worktree`) **before it runs anything**:

> the worktree has uncommitted changes, which the main clone's index cannot see. Commit first (that
> is already the dispatch protocol), then re-run.

That message names uncommitted *changes* and will not read as "your log file" — recognise it
anyway, because the cost is a gate that **measured nothing**, not an untidy directory. It is also
loud and lands before collection, so a run that produced any pytest output was not stopped by this.

**Prescribing the name rather than the check is deliberate.** The general rule — "run `git
check-ignore -v <name>` on whatever you are about to write" — is strictly more correct and survives
`.gitignore` moving, but it is a procedure to remember before an *incidental* action, which is the
same shape as the trap this whole section documents, and it will be skipped. `*.log` is a one-word
rule with no judgement in it, and misremembering it fails loudly and immediately rather than
silently. Keep `git check-ignore -v <name>` as the escape hatch for a seat that wants a different
name — or that needs to re-establish the claim above, since the safety is a property of
`.gitignore`, not of the filename. Fixing the tension in the selector instead — having it ignore
untracked files, or name the offending paths in its refusal — was considered here and left to a
code bead.

> **Operator decision 2026-08-22.** Question as put: *"the shared-scratchpad-as-'session-specific'
> trap will recur on every future fan-out, and it produced a wasted gate run and two near-misses in
> one wave. The durable fix is either a per-seat scratchpad convention or a line in CLAUDE.md's
> dispatch guidance. Want me to file that as a bead?"* Answer as given, verbatim: *"yes, always use
> per-bead scratchpads."* Durable record: bead phaze-rlshw. The operator said **per-bead**, not
> per-seat as the question offered — the bead id is the unit, and it outlives a seat being
> reassigned or resumed.

## A gate is green only if its own pytest summary line says so (phaze-rlshw)

Never a wrapper's exit code, never a background-task "completed" status, never the absence of
visible errors. Read the **pytest summary line** and the **coverage line**, and confirm the pytest
header names **your own** seat's database. Measured 2026-08-22, same wave: a background-task wrapper
reported `completed (exit code 0)` over a `just check` that had exited **143** — SIGTERM at 19%,
zero failed tests. The `0` was a trailing `echo`, not the gate. This belongs beside the scratchpad
rule because it is the same class: the harness told a seat it was fine, and no code was wrong.

**The same mechanism hides genuine FAILURES, and that is the direction to design against
(phaze-o24tm).** Measured 2026-08-25: a background-task wrapper reported `completed (exit code 0)`
over a gate that had genuinely failed — `1 failed, 7922 passed`, a coverage line, 20:19 of wall
clock, and `GATE_EXIT=1` in the log. The 2026-08-22 case above had **zero** failed tests underneath,
so it cost a slot; this one had a real failure underneath, and a seat reading the wrapper would have
carried a red bead to submit as green. **A kill rendered as green wastes twenty minutes; a failure
rendered as green lands a broken change.** Same mechanism, opposite consequences, and only the
second is unrecoverable by waiting.

**The positive tell, which is a procedure rather than an attitude (phaze-o24tm).** Capture the
status onto the gate command itself, then read the LOG:

```bash
just check > gate.log 2>&1; GATE_EXIT=$?; echo "GATE_EXIT=$GATE_EXIT" >> gate.log
```

| what the log holds | verdict |
|---|---|
| `GATE_EXIT=0` + pytest summary + coverage line | green |
| `GATE_EXIT=1` + pytest summary | genuinely red — a verdict |
| **no `GATE_EXIT` line at all**, log truncated | **KILLED. Not a verdict.** |

The third row is what earns the technique: the line's **absence** is diagnostic, because a SIGTERM
kills the shell before the appended `echo` can run, so a killed gate cannot forge it. Both
2026-08-25 kills were identified this way. Two bounds come with it, and without them the tell is
actively misleading.

**It must come BEFORE any trimming, and it makes the wrapper's status permanently meaningless.**
The `echo` is now the last command and it succeeds, so an enclosing wrapper or list reports **0** by
construction — deliberately arriving at the shape the 2026-08-22 incident hit by accident, where
"the `0` was a trailing `echo`, not the gate". A seat that appends the tell and then reads the
notification is worse off than one that never appended it, because that `0` is now *guaranteed*
rather than coincidental. **Append it AND read the log — the two halves are one instruction.**

**The tell is only as good as your access to where it printed.** It writes to the compound's
stdout; under backgrounding that stdout goes to a task-output file while the harness surfaces its
own status in the notification you actually read, so a seat can have the shape exactly right and
still be handed nothing but the wrapper status at the moment of decision. Reported 2026-08-25 from a
seat that hit it and read the log anyway — **relayed, not reproduced**, so treat the frequency as
unknown. **What survives all three mechanisms** — pipeline, list, and wrapper — is reading the log
body: the pytest summary, the coverage line and the `phaze test database:` header are written by the
gate itself into the redirect target, and no wrapper, list or backgrounding can alter them. That is
the same conclusion the redirection-order paragraph below reaches from its own side, which is why it
is stated once there and not repeated as a fourth rule here.

**And the durable record barely tells these apart either.** `.git/bh-validation-ledger.json` records
an `rc` and nothing else that separates these two cases (its full field list is in the ledger
section above — do not re-enumerate it here, for the reason that section gives), so a signal death
is `rc=143`, a genuine failure is `rc=1`, and both are recorded as verdicts. Measured 2026-08-25: **55 of 56** entries carry no counts at all;
the single exception is one `rc=0` entry carrying a `report` of `{tests, passed, failures, errors,
skipped}`, a field that had only just begun to be populated. Read that as **dated, not permanent** —
and note it changes nothing for the case that matters: this bead's own green check, run minutes
after that exception appeared, recorded **no** `report` either, and a killed run has no summary to
ingest in the first place. The lie this section is about reaches the artifact, not just the
transcript.

**A shell pipeline eats the status too, and that is the common case (phaze-skhmm).** A pipeline's
exit status is its **last** command's, and `tail` succeeds at printing a failure — so `| tail`,
`| head`, `| grep`, `| jq` and even `| cat` all report 0 over a command that died. Measured
2026-08-24: `bh work merge phaze-rlshw --wait 2>&1 | tail -30` exited **0** because `--wait` is not
an option and `tail` happily printed the usage error; the dispatcher read the 0 and told the
operator the merge was queued, which it was not. Re-run under `set -o pipefail` it is 1. Keep
trimming output — just make the status survive it: `set -o pipefail; just check 2>&1 | tail -40`.
**Do not reach for `${PIPESTATUS[0]}` instead (phaze-oufc4).** That array is bash-only; this
repo's shell is zsh, where `${PIPESTATUS[0]}` is not an error but silently expands to the **empty
string** — verified in this repo's own zsh: `false | true; echo ${PIPESTATUS[0]}` → empty. An empty
status reads as benign at a glance, which is worse than the masking it was meant to fix. zsh's own
array is spelled `pipestatus` — lowercase, **1-indexed**, so the first command's status is
`${pipestatus[1]}` — but there is no need to reach for either spelling: `set -o pipefail` alone is
already correct in both bash (CI's shell) and zsh (this repo's local shell), needs no dialect
switch, and is the only remedy this file recommends **for a pipeline**. **It is not a general
remedy for a masked status, and reading it as one has already produced a false green
(phaze-o24tm).** `pipefail` governs `a | b` and does nothing whatever for `a; b` — a sequential
**list**, whose status is simply its LAST command's. Measured 2026-08-25: a seat with `set -o
pipefail` correctly set ran `bh work check <id> > check.log 2>&1; echo "CHECK_EXIT=$?"; tail -30
check.log`; the trailing `tail` succeeded, so the list exited **0**, and the harness reported
`completed (exit code 0)` over a check that had exited **1** and run **zero** tests (`selector:
fail — the worktree has uncommitted changes`). Both facts sat seven lines apart in one output file.
A trailing `tail` to trim output is the idiomatic thing to write and the whole reason the trimming
advice exists, so this is the construct a backgrounded gate invocation naturally lands in — not an
exotic one. The list case has its own remedy, in the section below: `echo "GATE_EXIT=$?"`
**immediately** after the command and **before** any trimming. **`grep` is the sharper edge, because it
inverts as well as masks:** `cmd | grep -q PASS` returns 0 when `cmd` FAILED but its error text
quoted the pattern, and 1 when
`cmd` SUCCEEDED but printed nothing matching — a status wrong in *both* directions, which is worse
than one that is merely optimistic.

**Redirection ORDER is the same family and fails more quietly (phaze-2ng7c).** `cmd 2>&1 > file`
and `cmd > file 2>&1` are different commands, not two spellings of one. Redirections apply **left to
right**, and `2>&1` duplicates whatever stdout is *at that moment* — the terminal — so the later
`> file` re-points stdout alone and stderr keeps going to the screen. The correct order points
stdout at the file first, and `2>&1` then duplicates the file. For a command that writes to
**stderr**, the wrong order leaves the file **empty, with exit 0**. Measured 2026-08-25
(phaze-pv3kk): `just --dry-run` prints the recipe on stderr, so `just --dry-run test-fast 2>&1 >
recipe.sh` left `recipe.sh` empty while the recipe scrolled past on the terminal — it looked like it
had worked, and the failure surfaced one step later when four string substitutions against the file
all reported not-found. `> recipe.sh 2>&1` held **62** lines. **It still reproduces, and re-deriving
it costs ten seconds** — re-measured 2026-08-25 in this repo against a *different* recipe:
`just --dry-run check-fast 2>&1 > f` writes **0 bytes** and exits **0**, `> f 2>&1` writes **64**
lines. Different recipe, different line count, same failure, so this is a property of the shell
rather than of the recipe that happened to expose it. A masked pipeline gives a wrong status
over real output; this gives **no output and a successful status**, so whatever runs next measures
nothing and reports fine — the defect class this file already names. It is also **invisible in a
transcript**: the command appears to run, prints what you expected, and exits 0.

**And that paragraph will not, on its own, stop this happening (phaze-2ng7c).** The pipeline hazard
above was already written down, and quoted in a message a few minutes earlier, when the same seat in
the same hour wrote `... | grep ... | head -5` followed by `echo "exit $?"`. Prose about a silent
hazard raises the odds of **recognising** it after the fact; it does not reliably **prevent** it.
So: where a mechanical check exists, the check is the control — `tests/shared/test_validation_gate_recipes.py`
and `tests/shared/test_cluster_wide_catalog_scoping.py` are what actually hold their rules, not the
paragraphs describing them. Where none exists — an ad-hoc diagnostic nobody will ever lint — the
rule that survives is **read the output rather than trust the status**: an empty file, a zero-length
capture or a suspiciously short log *is* the finding. Recording that limitation beside the guidance
is worth more than adding one more confident imperative to a list that has already failed once.

**The summary line is the standard of green; the COUNT in it is not a portable quantity
(phaze-ljfi5).** Everything above is about a status that **lies**. This is the adjacent case where
nothing lies and a reader is still misled: the summary line is true, and its number describes **the
population that ran**, not the suite. Two green lines are therefore not comparable, and neither
carries what you would need to compare them. This **qualifies** the rule at the top of this section
and does not weaken it — the pytest summary line remains the only acceptable evidence that a gate
ran and what it found.

Two mechanisms move the number, and they differ in the way that matters — **whether the line admits
to it**:

- **The marker filter, which the line DOES declare.** `addopts = "-m 'not browser'"`
  (`pyproject.toml`) applies to every run in this repo, and pytest reports what it removes: the
  `180 deselected` in every full-suite line quoted in this file is exactly this, all of it. Measured
  2026-08-25 on this tree: `uv run pytest --collect-only -q -m browser tests/browser` collects
  **180 tests**, and `grep -rn -- "--deselect" justfile pyproject.toml .github/ scripts/` returns
  nothing — there is no separate `--deselect` set, so the marker filter accounts for the whole
  figure. A `--deselect` set added later lands in the same counter and stays equally visible.
- **Which branch of the recipe ran, which the line does NOT declare — and this is the one that
  bites.** `just check-fast` does not run one population. Where it selects, it hands pytest an
  explicit list of node ids, so the tests it did not select are never collected and leave **no
  `deselected` and no trace whatever** in the summary line; where it runs the whole suite, the same
  command produces a figure two orders of magnitude away. Measured 2026-08-25 (dev/fastsuite, the
  `check-fast` row of the recipes table above): a selected run printed **417 passed, 1 skipped in
  35.57s**, against roughly **20 minutes** and roughly **8,000** for the whole suite — a **factor of
  19**, both green, both printing a summary line. **Do not read that as a list of two.** The set of
  populations one command can produce has already grown once (`phaze-fqfds`, 2026-08-25), so it is
  the *variation* that is the hazard rather than any particular count of branches; this note
  deliberately does not enumerate them, and a reader who needs the enumeration should read
  `scripts/select_impacted_tests.py`, which is the thing that decides.

**And the number moves even with the recipe held fixed.** This file records two green full-suite
runs eleven apart — **8027 passed** in the ledger section above, and the **8038 passed of 8041**
carried by the `report` beside it — because the suite itself grew between them. Neither is wrong;
they measure different populations that share a name. So a remembered figure is the weakest thing
you can check a fresh one against, and "the suite passes" is not a quantity that survives being
carried from one run to the next.

**The ledger cannot close the gap, and the reason is not the counts.** The ledger section above
already establishes when an entry carries a `report` and when it is rc-only, and warns against
reading that ratio forward; none of it is restated here. The gap this note is about survives a
`report` being present: **no field records which branch of the recipe ran.** The key is `(tree hash,
validate-cmd hash)`, the branches are branches of one command, and every phaze boundary hashes that
same command — so counts replayed from a verdict are a number with no population attached to it.

**What tells you which population you got is the recipe's own output, not this paragraph** — which
is the point the paragraph above makes about prose in general. `just test-fast` prints
`🎯 selector: <verdict>` before it runs anything, and every branch that is not the whole suite says
so in the line it prints afterwards. Those lines are written by the gate into the same log body as
the pytest summary, the coverage line and the `phaze test database:` header. A replayed verdict
prints none of them.

**So: cite a count with the recipe that produced it, and never compare one across runs.**
phaze-qsyc0 above establishes that a gate's **M** is a property of a RUN, never of a command. This
is that same idea one level down — **a count is a property of a run, never of a suite.**

## Concurrent gates are bounded by headroom, not by isolation (phaze-rlshw, revised phaze-o24tm)

The isolation rules protect **correctness** and say nothing about **capacity** — and exceeding
capacity does not look like a failure, it looks like a SIGTERM that a wrapper renders as exit 0.

**Gate on HEADROOM, not on a count of running gates (phaze-o24tm, 2026-08-25).** This deliberately
reverses the "if three are already running, wait rather than adding a fourth" rule that stood here
from 2026-08-22, and the argument is below rather than assumed — the count was chosen for real
reasons and deserves one. Before launching a gate:

```bash
sysctl vm.swapusage                                                 # hard NO-GO if "used" is non-zero
memory_pressure | tail -1                                           # the headroom read: "free percentage: N%"
ps -eo args= | grep -cE '^[^ ]*/\.venv/bin/python[0-9.]* .*pytest'  # who else is here — context, not a verdict
```

**Swap at `0.00M` is NECESSARY, not SUFFICIENT — and the difference is the whole rule.** Swap
answers *"is this machine thrashing right now"*; a launch decision needs *"will it thrash if I add
~1.3 GB"*. Those are different questions, and only the second is a launch decision. So non-zero swap
is a **hard no-go** — by the time swap moves, free memory is already exhausted and you are past the
point the check existed to catch, which makes it a stop signal rather than the primary signal.
Clearing it establishes only that nothing is thrashing *now*; it does **not** establish headroom.
The forward-looking read is `memory_pressure`'s free percentage, and it still carries **no
calibrated threshold** — none has been derived, inventing one here would be exactly the unmeasured
arithmetic this section exists to correct, and it is recorded so that one can be. Between the two,
a seat is exercising judgement rather than reading a verdict, and that is an honest description of
what the evidence supports rather than a gap in the rule.

Swap earns the no-go slot on evidence, and how thin that evidence is has to be stated plainly,
because carrying a claim stronger than its evidence is this section's own defect. **Exactly one kill
has a recorded memory state**: 2026-08-22, **461 MB of 1024 MB** in use across the
five-gate run in which one gate was SIGTERM'd — and even that is the wave's state, not a reading
taken at the instant of the kill. The two 2026-08-25 kills (**18%** and **21%**, below) have **no
recorded memory state at all**. If either of them died at `0.00M`, this discriminator is wrong, and
nothing in the record can currently say. It therefore rests on **one measured kill with swap in use,
plus every measured survivor at `0.00M`** — survivors over the old count included. **That is why the
launch check is something you RECORD, not merely read:** write the swap reading and `WAITED_SEC`
into the gate log, and the next kill either confirms this discriminator or refutes it. A rule that
accumulates the evidence for its own threshold is more than the count it replaces ever had.

**Why swap is a stop signal and not the primary one: it LAGS.** It moves only once free memory is
already exhausted, so `0.00M` describes the state you are **in**, never the state your launch will
**produce**. It is kept in the no-go slot for two reasons: on macOS swap moving *is* approximately
"free is exhausted", so the reading is sound about what it reports, and every reading a seat reaches
for instead is easier to misread (see the `vm_stat` trap below). Read it as *nothing is thrashing
right now* — never as *nothing will be*. That limit binds every quantity here, the count it replaced
included: a launch check is a snapshot, which is why the check-then-launch race below is reported
rather than closed.

The process count survives only as context, and it may make you **more** conservative, never less:
three live gates is a reason to look at swap, not on its own a reason to wait, and **never** a
reason to kill a running gate. Two seats declined to kill healthy runs on a count alone during the
2026-08-25 wave and were right both times. If the count moves under you *after* you launch, report
it rather than act — a check-then-launch race cannot be closed by a poller, and the shared lock file
that would close it is the writable-path collision this repo keeps relearning.

**Waiting is cheap, and that is measured rather than asserted (`phaze-sy8z3`, 2026-08-25).** A wide
fan-out staggers its **gates**, not its claims. Poll until the machine is quiet, then take the slot;
cap the poll so a stuck wait surfaces as a late gate rather than a silent hang.

```bash
waited=0
while [ "$(ps -eo args= | grep -cE '^[^ ]*/\.venv/bin/python[0-9.]* .*pytest')" -gt 1 ] \
      && [ "$waited" -lt 3600 ]; do sleep 30; waited=$((waited+30)); done
echo "WAITED_SEC=$waited SWAP=$(sysctl -n vm.swapusage)"   # into the gate log, before the gate
```

That seat's first attempt launched at three concurrent and was SIGTERM'd at **21%** after ~20
minutes; its next attempt waited **390 s** for the machine to quieten and then ran clean in
**19:52**. **390 seconds of waiting replaced ~1190 seconds of lost suite.** Recording `WAITED_SEC`
and the gate count at launch is what makes the *next* kill legible — it separates "the machine was
overloaded" from "something is wrong with my tree", a distinction that could not be drawn cleanly on
the first one. The loop waits for **≤1**, stricter than any ceiling, on purpose: a ceiling is the
point at which gates start dying, and a seat with no deadline should not sit on it.

**Why the count went, and why nothing about it ever looked wrong.** A count assumes a stable
per-gate cost, and that cost is unstable in three independent ways, all measured on the same 32 GB
machine. It drifts with **suite growth** — 545–671 MB per gate on 2026-08-22 against **1297–1340
MB** on 2026-08-25, roughly double, because the suite grew (one 20-bead wave alone added tests
across ~10 beads). It drifts **within a single run** as pytest moves between partitions: the same
two gates read 1039/1019 MB and, four minutes later, 1243/1232 MB, while lighter partitions sampled
the same day read 711/659/831 MB — a ~2× swing. And a count is blind to **everything else on the
machine**, which is evidently what differed on 2026-08-25 between three gates that died and three
that did not. The cost of that blindness was measured, not predicted: `phaze-rhs6m`'s **merge**
validation was killed at **18%** and `phaze-sy8z3`'s re-gate at **21%**, both at three concurrent —
the count's own limit — and **neither was a code failure**. `bh` converted the first kill's signal
exit into "main is RED in combination" and bounced a green bead to `review:changes-requested`. Hours
later, same machine and same date, **three** concurrent gates at 1040/1243/1063 MB sat at **65%**
memory free with **zero** swap in use and ~10.5 GB available, under no distress at all. **Two killed
validations have a durable record, and reading it corrects this paragraph twice.**
`.git/bh-validation-ledger.json` holds `rc=143` — SIGTERM — for `210cb2029` (`chore(merge): bead
phaze-rhs6m`, ledger `at` **2026-08-24 19:12:40** local) and for `ec7deb2f2` (`chore(merge): bead
phaze-shzdj`, ledger `at` 14:47:56 the same day) — **`at` is when the VERDICT was recorded, not the
commit date**, and the two differ by 10m08s and 26m27s respectively, which is roughly how long each
validation ran before it died. So one of the kills above is confirmed by artifact, one is a **third**
kill at an unrecorded concurrency, and `phaze-sy8z3`'s re-gate has **no ledger entry at all**. The
timestamps are the 24th; the percentages above come from session transcripts dated the 25th, the
wave ran overnight, and nothing reconciles the two beyond that. Take from the ledger only what it
stores (field list in the ledger section above), and for these two entries **no counts** (some
entries do carry them; see the masking section above): it establishes that the kills happened and
were recorded as failing verdicts, never the memory state at the moment of the kill, which is the
gap named above. So the rule
was wrong in *both* directions at once — it permitted the runs that died, and here it would have
forbidden three runs with ample room — and a rule wrong in both directions is not repaired by moving
its threshold. What the count had going for it in 2026-08-22 was real, and is why this is a swap
rather than a deletion: it is one command, easy to check and hard to get wrong under pressure.
`sysctl vm.swapusage` keeps that property, because `used = 0.00M` is a zero/non-zero read with no
threshold to get wrong.

**Re-measure the figures above rather than inheriting them — and sample LATE.** Read a live gate's
RSS in KB with `ps -eo rss=,args= | grep -E '/\.venv/bin/python[0-9.]* .*pytest'` (**M**,
2026-08-25, dev/gateceiling: one live gate at **1349968 KB ≈ 1318 MB**, a third independent sample
inside the 1297–1340 band). Naming the command is necessary and **not sufficient**: per-gate RSS
grows through a run, so a prompt sample reads ~20% low — 1039 MB at t0 against 1243 MB four minutes
later, the *same* process — and any ceiling derived from it is set too high. **Sample late in a run,
or sample repeatedly and take the maximum.** Two simpler spellings of the *count* are wrong, both
measured: `grep pytest` counts the `uv` and shell wrappers **and the measuring pipeline itself**
(**3** reported against **0** live gates), and matching `ps aux` field 11 on `python$` fails the
other way — it misses an interpreter spelled `python3`, reporting **1** when **2** were live.
Undercounting is the direction that adds a gate. **A third miscount runs the same dangerous way and
is invisible in the command's output: it counts gates that have reached PYTEST, not gates that are
RUNNING.** `just check-fast` is `lint typecheck test-fast`, so for its first ~30–60 seconds a live
gate is inside ruff and mypy with no pytest process at all, and matches nothing — measured
2026-08-25, a seat's own gate invisible to the count at launch. The form above also does not
distinguish a **full suite** from a **targeted slice** a seat is iterating on, so it overstates load
in the other direction. Three miscounts, two of them silent and toward *adding* a gate: that is a
fourth reason the count is context rather than the rule, and it is the reason a reading taken from
the machine beats one assembled by matching process names.

**And do not reach for `vm_stat`'s `Pages free` while re-deriving any of this.** It is the Linux
`MemFree` intuition applied to a kernel that parks reclaimable pages on the inactive list, so a low
value is the **healthy steady state** rather than a warning. Measured 2026-08-25 on this 32 GB
machine: `Pages free` **0.12 GB** against **9.87 GB** inactive — about **10.07 GB** actually
available — while `memory_pressure` reported **63%** free and swap read `0.00M` at the same instant.
A seat reading `Pages free` alone concluded the machine was tight when it had ~10 GB spare. That is
the sharpest argument for `sysctl vm.swapusage` holding the no-go slot: not that it is the most
informative number available, but that it is the hardest one to read backwards.

**The general form: the old ceiling was sound arithmetic on stale inputs.** It was not wrong when it
was written, and nothing about it looked wrong afterwards, because **a ceiling derived from a
measurement does not re-measure itself** — the divisor stays put while the suite it was divided
against grows. The repowise 0.44-vs-0.45 entry under "Beads Workflow Integration" below
(`phaze-ia4ah`) is the prior instance of this shape in this file, and it is written up there rather
than restated here. The defence is the same in both places, and it is why every figure above carries
its date and the command that produced it: write the derivation down so the next reader can
re-derive it, because nothing else in the system will notice when the inputs move.

## A gate measures a TREE, and that tree may not be the one anyone merges (phaze-fkha3)

The section above rations gate slots against the machine's **headroom**. This one rations them
against the result's **validity** — the same decision one step earlier: *is it worth starting a gate
right now?* A gate runs 20–25 minutes whenever the coverage map cannot speak for the diff, and
`origin/main` does not hold still that long.

**The check, immediately before `bh work check` / `bh work submit`:**

```bash
git fetch origin && git rev-list --left-right --count HEAD...origin/main
```

The right-hand count is how far `origin/main` has moved past you. Non-zero means the tree you are
about to spend twenty minutes characterising is not the tree that will land.

**The asymmetry is one-sided.** The check costs about a second; not running it costs 20–25 minutes
plus the reasoning time to establish that a red is not yours. **M**, 2026-08-25: `origin/main` took
**65 commits in 24 h — 2.71/h** (bursty; 1.50/h across the same day's quietest 12 h, so read it as a
wave-period rate). Against that rate the expected number of commits landing *during one gate* is
**0.99 for a 22-minute full-suite run** and **0.027 for a 35.57 s `check-fast` selected run**. Cite
both or neither, exactly as with the two numbers in the gates table: the first says the base moves
under roughly every full run, the second says it essentially never moves under a selected one. **The
check earns its keep on the escalated path.**

**It is NOT a mandate to rebase, and reading it that way has a measured cost.** Most landed commits
will not interact with your bead. A rebase changes your tree, which changes the `(tree, cmd_hash)`
ledger key, which means a second full gate: `phaze-irby2` ran a gate to green, ran this check, found
four commits had landed in `CLAUDE.md` — its own file — rebased, and paid a second ~22-minute gate
for it (**M**, 2026-08-25). That was the right call there and is the wrong call by default;
rebase-looping against a main moving at 2.71 commits/h never converges. The rule is **know before
you spend the slot**, not *always rebase*.

### Two questions do most of the work

One is mechanical and takes a second. One is judgement, and is the one that costs money when skipped.

**Mechanical — does it still merge?**

```bash
git fetch origin
git merge-tree --write-tree HEAD origin/main >/dev/null; echo "MERGE_TREE=$?"
```

**Do not answer this by comparing hunk line numbers.** `git diff origin/main...HEAD` is
`git diff $(git merge-base origin/main HEAD) HEAD`, so its left-side hunk headers are in
**merge-base coordinates** — not `origin/main`'s. Measured on `CLAUDE.md`, 2026-08-25: a **22-line
offset** between the three-dot and two-dot headers for the same edit, because a commit landed in
between. Comparing your three-dot header against another branch's two-dot header compares two
coordinate systems and calls the result overlap or disjointness. It is right by luck when the edits
are far apart and silently wrong when they are adjacent, which is the case you actually need it for.

Two things about that exit code, both measured on git 2.50.1:

- **Exit 1 is ambiguous — conflict *or* a ref it could not resolve.** `git merge-tree --write-tree A
  nosuchref` exits **1** with `merge-tree: nosuchref - not something we can merge` on stderr. So a
  worktree that has never fetched, or whose remote is named differently, reports "conflict" when it
  has measured nothing — and this check runs precisely when you suspect your `origin/main` is stale.
  Fetch first, and read stderr before believing a 1. A misread here buys a needless rebase, which
  costs the second slot this section exists to save.
- **Clean is necessary, not sufficient: `merge-tree` answers *textual conflict*, never *will my green
  survive*.** `phaze-sy8z3` merged clean and then went `2 failed, 7985 passed` on a field another
  branch had renamed while it was in flight (the gates table above has the full run). `merge-tree`
  would have returned 0. A rename landing on the base is invisible to a textual merge test and fatal
  to a gate result.

**Judgement — do the new commits falsify or duplicate a claim your text makes?**

A content grep, and **nothing structural sees this**. It is the criterion seats skip, because the
mechanical checks return a clean, confident answer and it feels as though the question has been
settled. Note the asymmetry that makes it worth writing down: every other question here tells you
whether to **rebase**. This one can tell you your text is now **wrong** even when no rebase is
needed. The two failures are independent, and a seat that checks only for conflicts will ship a
contradiction with a green gate and a clean merge.

Measured, 2026-08-25: a branch that merged clean and was disjoint by file *and* by hunk would have
landed a section arguing that fixed enumerations decay because nothing in them says whether they are
complete — while two decayed enumerations sat a few hundred lines below it, both landed twenty
minutes earlier, one phrased as an exhaustive instruction.

### Four more, when the first two leave it open

- **Do the moved commits touch my files?** Read `git diff --stat origin/main...HEAD` and
  `git log --oneline -5 origin/main` — the diff, never the commit count.
- **Is a moved commit a whole-tree guard?** A test under `tests/shared/` that walks the repo
  intersects every diff by construction, so a disjoint file list is the wrong answer for that class.
  This population is not exotic: **78 of 585 test files** read a tracked repo file and assert on its
  content (**M**, 2026-08-25 — one in eight). The litellm guard in the red-gate example below is one
  of the 78, which is why that incident was structural rather than bad luck.
- **Does my prose assert the contents of `main`?** Then gate against the tree you will land on, or
  the assertion is only ever checked against a base that no longer exists. A seat's sentence *"the
  opt-in is not on `main` yet"* was true when written and false forty minutes later; its first gate
  had already validated it green (**M**, 2026-08-25).
- **Does my text cite a path that only exists on the new base?** That is a forward citation, dangling
  where written — `test -e` answers it, and it is the only mechanically checkable one of these four.

**Which sentences rot: claims about STATE go stale, claims about MECHANISM do not.** The same
paragraph that needed fixing for the third bullet carried a dated ratio that needed no edit at all,
because it cited its date and gave the command for the current value. That distinction tells you
which sentences to worry about instead of telling you to worry generally.

### On a red gate, establish provenance BEFORE debugging

This is the first move, not a footnote — it is the half that saves the *second* slot.

```bash
git diff --stat origin/main...HEAD    # is the failing file even in my diff?
git log --oneline -5 origin/main      # did the base move, and what landed?
```

If the failing file is not in your diff, the red is probably not yours. **M**, 2026-08-24: a seat's
gate went red 23 minutes in on `test_litellm_pin_is_unchanged`, in a bead whose own diff was one ADR
plus three comment-only edits. The guard asserted a pin string that a P0 on `main` had already moved;
`origin/main` had advanced four commits while the gate ran. The seat checked provenance instead of
debugging litellm, and that instinct is the only reason it did not lose a second slot. Writing it
down is the point — instinct is not evenly distributed.

### What is NOT already covered (verified against bh 0.15.0, not inferred)

`~/.beadhive/config.yaml`'s validation model has a staleness backstop, and it is easy to assume it
covers this. It does not. Read against bh 0.15.0's source, 2026-08-25:

- **It is the MOLECULE boundary only.** `work_merge.py:282` re-validates a landed molecule under
  `relaxed` exactly when `stale` (`work_merge.py:394`). The per-bead path computes
  `revalidate = mode == "conservative" or (on_main and mode != "loose")` (`work_merge.py:854`) —
  **`stale` does not appear in it at all**. `merge-main` re-validates because it is on main, not
  because anything noticed the base move.
- **It cannot see `origin/main`, because nothing in the lifecycle fetches.** `stale` compares local
  refs in the main clone — `git rev-parse` and `git merge-base` — and `git fetch` appears **nowhere**
  in `work.py`, `work_merge.py`, `work_submission.py`, `work_group.py` or the `worktree*` modules
  (the one hit, `worktree.py:823`, is `upstream/<base>` for `kind=external` hives; phaze is not one).
  So the backstop notices local `main` moving under a molecule and is structurally blind to the
  remote.
- **The developer gate has nothing whatsoever.** `impl_check` (`work_submission.py:10–71`) locates
  the worktree, runs the `verify: true` init rules, runs `validate_cmd`, records the verdict. No base
  resolution, no `merge-base`, no fetch. `impl_submit` resolves `base` only to bound the commit-count
  and conventional-subject guard and to record commit linkage; it never compares the base to
  anything.
- **`bh work merge --help` is silent on all of this** — it says only "validate it" for `--molecule`.
  The source is the only authority here, which is why this bullet cites line numbers rather than help
  text.

The 20–25 minutes and the confusing red both land on the developer gate. That is the gap, and this
section's check is what closes it.

### The general form

CLAUDE.md already says a gate's **M** is a property of a RUN, never of a COMMAND. This is the next
term in the same series: **an M is also a property of a MOMENT, never of the repo in perpetuity — an
M about STATE decays, an M about MECHANISM does not.** A long verification measures a **tree**, and
the base is part of that tree; when the base moves, a green is still an honest report about something
that is no longer what anyone will merge.

The label makes this worse rather than better, which is the part worth remembering. **M** is the
marker that tells the next reader *do not re-verify this* — so an M attached to a claim about state is
the one most likely to be carried forward unchecked after it has stopped being true. That is not an
argument against the convention but for dating every M and naming the command that produced it —
the same defence the ceiling section above arrives at from the other direction. That ceiling went
stale because its inputs grew; a gate result goes stale because the base moved. Neither announces
itself, and neither is repaired by trusting the label harder.


## Testing

- Minimum **95% LINE coverage** repo-wide, plus a **90% per-module line floor**. Both are
  enforced by `scripts/coverage_floor.py`, which `just test-cov` and `just coverage-combine` run.
- Upload coverage to Codecov with service-specific flags
- Codecov config: precision 2, round down, range 70-100%, project target auto with 1% threshold, patch target 80% with 5% threshold

## Branch coverage: measured everywhere, gated per bead (phaze-bk9el.21)

**Operator decision 2026-08-21.** Question as put: *"Branch coverage is off, and runs 4-8 points
under line coverage on the refactor targets. What should this epic do about it?"* Answer as given
(selected option label, verbatim): *"Enable it, gate the refactor targets only (Recommended)"*.
Durable record: bead `phaze-bk9el.21`.

`branch = true` is set in `[tool.coverage.run]`, so **every** coverage run in this repo measures
branches and the number is visible everywhere. The **gate** is deliberately narrow:

- **Repo-wide, the floors stay on LINES** — 95% total, 90% per module. Branch coverage sits below
  the line figure on most files here, so a repo-wide branch floor would fail on day one and the
  backfill would dwarf whatever work it was meant to protect. Do **not** raise `fail_under` against
  branches.
- **Per bead, branch coverage is gated on the files that bead touched.** `just branch-check` reads
  the `coverage.json` any gate run leaves behind, checks only the `src/phaze/**.py` files changed
  against `--base-ref` (committed, staged *and* unstaged, so it is useful mid-flight), names every
  file it checked, and prints the **uncovered branch line numbers** rather than a bare percentage.
  Raising branch coverage is welcome, holding it steady is fine, **lowering it fails the bead**. A
  file the bead did not touch is out of scope for that bead's check.
- **It fails closed on a missing baseline** — if the check could not perform a comparison, it did
  not pass. The baseline is written by `phaze-bk9el.1` (`just branch-check --write-baseline`); that
  bead alone passes `--allow-missing-baseline`, because it cannot be blocked by a check that
  consumes the artifact it exists to produce. **No other bead should pass that flag** — seeing it
  in one is a signal something is wrong. An exemption written at the call site is auditable; a
  lenient default is invisible to every bead downstream, and "exit 0 having measured nothing" is
  the same defect class as `phaze-jnj90` (a gate producing no coverage) and `phaze-nqawu` (a submit
  running no tests).

Why per-bead is where the value is: decomposing a long function, flattening a nest or splitting a
file are all operations where **every line still executes** and only the branch combinations
change. Line coverage is structurally unable to see that regression, and a repo-wide average is far
too coarse to. Measured on the refactor targets — `job_runner.py` 97.12% lines / **89.13%**
branches, `services/analysis.py` 97.91% / **93.97%**, `services/video_audio.py` 94.62% /
**87.50%** — all three clear the line gates while sitting below them on branches.

**The trap, if you ever change a coverage floor.** With `branch = true`, coverage.py's own
`fail_under` measures the **combined** `(covered_lines + covered_branches) / (num_statements +
num_branches)`, not lines, and offers no option to select the metric (verified against coverage
7.15.4). Enabling branch measurement therefore silently re-points every floor left on that knob —
which is why both repo-wide floors read `percent_statements_covered` explicitly in
`scripts/coverage_floor.py`, why the two ARTIFACT WRITERS — `coverage json` and `coverage xml` —
carry `--fail-under=0` in `coverage-combine` while `coverage report` carries the real floor
(`--fail-under=95`), and why every line the gate prints names the metric it measured. `fail_under
= 95` remains in `pyproject.toml` purely as a backstop for an ad-hoc `uv run pytest --cov=phaze`
outside those recipes.

The two writers are disarmed for a measured reason, not a stylistic one (`phaze-jktlb`). Before
that bead, `coverage-combine` ran `xml` / `json` / `report` in that order with no `--fail-under`
overrides on the first two, so each writer inherited pyproject's `fail_under = 95` — and a writer
that fails its own floor still **writes its file first**, then exits nonzero. Measured over
deliberately partial shards: that ordering aborted at `coverage xml`, which wrote `coverage.xml`
and then exited on the floor, so `coverage json` never ran and `coverage.json` was never
written — the exact file `scripts/coverage_floor.py` and `just branch-check` read to find which
module dropped. `--fail-under=0` on `coverage json` and `coverage xml` forces both artifacts to
be written in full regardless of the score; `coverage report --fail-under=95`, last and on
purpose, is where the run actually fails.

`tests/shared/test_coverage_gate.py` pins the half of this that prose cannot: it asserts
`coverage-combine`'s `coverage report --fail-under=<N>` equals `pyproject.toml`'s
`[tool.coverage.report] fail_under`, so `coverage report` carrying the real floor is not something
a future edit can quietly relax — the two sites must move together or the guard fails the build.
