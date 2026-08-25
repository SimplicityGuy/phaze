# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**phaze** — A music alignment tool. Python 3.14, MIT licensed.

## Development Setup

- **Python**: 3.14 exclusively
- **Package manager**: `uv` only — never use bare `pip`, `python`, `pytest`, or `mypy`. Always prefix with `uv run`.
- **Pre-commit**: Must be installed and active. All hooks must pass before commits.

### Key Commands

```bash
uv sync                    # Install dependencies
uv run pytest              # Run tests
uv run pytest tests/test_foo.py::test_bar  # Run a single test
uv run pytest --cov --cov-report=term-missing  # Run tests with coverage
uv run ruff check .        # Lint
uv run ruff format .       # Format
uv run mypy .              # Type check
uv run pre-commit run --all-files # Run all pre-commit hooks

just test                  # Fast LOCAL ITERATION only: -x -q. Not a gate — see below
just check-fast            # THE per-bead gate (phaze-pv3kk): lint + typecheck + the tests
                           # repowise says the change touches, escalating to the full suite
                           # when it can't tell. What `bh work check`/`submit`/`merge` run
just check                 # lint + typecheck + the full suite WITH coverage (95% LINE floor
                           # enforced). No longer any per-bead boundary's default; runs as
                           # `check-fast`'s escalation target, as `postland`/`union`'s recipe,
                           # and by hand
just check-all             # THE molecule gate: every pre-commit hook + the full suite. What
                           # `bh work finish` runs — no boundary an AD-HOC bead traverses runs
                           # a full suite; `molecule`/`postland`/`union` still do, see below
just branch-check          # Per-bead BRANCH-coverage gate: fail if this bead lowered branch
                           # coverage on any src/phaze file it touched. Free after any `check`
just test-db               # Bring up the shared test Postgres (5433) + Redis (6380) harness
just test-db-for <name>    # Carve an isolated seat out of that harness — REQUIRED for
                           # concurrent worktrees; prints three exports to set
```

> Bare `uv run pytest` needs the harness up (`just test-db`); without it the integration tests
> skip, so a green run means less than it looks like. Check the database line in the pytest
> header before trusting any result.

### Which commands are gates, and which only look like one (phaze-jnj90 / phaze-nqawu / phaze-pv3kk)

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

#### Recipes

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

#### Boundary → recipe, after phaze-pv3kk — all seven boundaries

Every row below carries **two different kinds of evidence, kept apart on purpose** (dev/fastsuite,
verified independently by the dispatcher, 2026-08-25). `beadhive.config.validate_cmd` — imported
live from the installed bh 0.14.0 — was **executed** against the actual `~/.beadhive/config.yaml`
block with the `(phase, main_gate)` pair each caller passes: that is the BLOCK → COMMAND mapping,
and it is **M**. Which `(phase, main_gate)` each `bh work` verb actually passes was established by
**reading** the call sites in bh's `work.py`, not by running them: that is the BOUNDARY → PHASE
mapping, and it is **I**. These are different claims — "the resolver returns X for phase Y" is not
"`bh work merge` passes phase Y" — and merging them into one M is exactly the wrapper/delegate
mistake above, one layer down. Only a live run of the verb after the config is applied upgrades a
row's I half; `bh work brief` printing `just check-fast` for `validate_cmd` is live corroboration
of the block, not a boundary run, and does not do that upgrading.

| Boundary | Caller (`work.py`) | Resolves to | Evidence |
|---|---|---|---|
| `bh work check <id>` | `1909` — **no `check` phase exists**, so this is unconditionally `validate_cmd` | `just check-fast` | **M** (block→command) + **I** (boundary→phase — though with no override key possible for this phase, there is nothing left to misread) |
| `bh work submit <id>` | `2417`, phase `submit`, no override | `just check-fast` (or a replayed ledger verdict — see "A gate's M is a property of a RUN" below) | **M** + **I** |
| `bh work merge <id>` → molecule | `3557`, phase `merge`, `main_gate=False` | `just check-fast` — but **inert under `validation: relaxed`** (the phaze default): a per-bead merge landing into a molecule is not validated at all (`revalidate` evaluates `False`, work.py:3646) | **M** + **I** |
| `bh work merge <id>` → main (every ad-hoc bead) | `3557`, phase `merge`, `main_gate=True` — `merge-main` wins over `merge` | `just check-fast` — **the boundary every ad-hoc bead actually hits**, since an ad-hoc bead (parent = NONE) lands here and never reaches `molecule` | **M** + **I** |
| `bh work finish <epic>` (= molecule pre-land) | `2923`/`2948`, phase `molecule` | `just check-all` — the only *routinely traversed* full-suite boundary (the two rows below also run a full suite, but only conditionally) | **M** (block→command) + **I** (boundary→phase — no `bh work finish` has yet landed under this config; the first one upgrades this row) |
| post-land molecule re-test (`postland`) | `2972`, phase `postland` | `just check` — kept full; see the citation immediately below the table | **M** + **I** |
| union-merge resolution (`union`) | `3525`, phase `union` | `just check` — **preserved**, not decided: this is what it already resolved to before phaze-pv3kk, and it was never put to the operator, because leaving an unasked boundary unchanged needs no authority while changing it would | **M** + **I** |

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
Durable record: `phaze-pv3kk`'s bead comments.

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
because that table's caller citations are `work.py` line numbers and **bh 0.15.0 has split `work.py`
into `work_merge.py` / `work_group.py` / `work_submission.py`, so none of those seven line numbers
still resolve** (`bh --version` → 0.15.0, checked 2026-08-25; the table was written against 0.14.0
the same day). Re-deriving all seven is its own measurement pass, not a side effect of this one.

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
(phaze-qsyc0, established 2026-08-24 against bh 0.14.0). A submit is evidence of a full-suite run
only when its transcript carries the pytest summary and coverage lines; since phaze-pv3kk a submit
is evidence of a *change-selected* run only when its transcript carries `check-fast`'s RUN verdict
(or its escalation to `just check`'s pytest summary and coverage lines); a replay prints a
`validation verdict reused (…)` line instead, either way. The next section has the mechanism, what
invalidates a verdict, and the general form.

### A gate's M is a property of a RUN, never of a COMMAND (phaze-qsyc0)

`bh work submit` does not always validate. It consults a **verdict ledger** and, on a hit, skips
the throwaway checkout entirely and prints a line like:

```
validation verdict reused (sha 0fe39f1, tree f7beb62, recorded 2026-08-23T20:40:28-07:00)
```

That submit ran no tests. It is not unsound — but it is **not its own measurement**, and the table
row above would otherwise read as though it always were. Established 2026-08-24 from
`beadhive/validation_ledger.py`, `beadhive/config.py` and the live ledger file, against bh 0.14.0:

- **The key is `(tree hash, validate-cmd hash)` — the validating COMMAND is not part of it.** So a
  `bh work check` verdict, earned in the **seat worktree**, is replayed by `bh work submit`, whose
  `--help` promises a **clean checkout**. That is deliberate (bh-i0p1.4), and `check` records only
  from a CLEAN worktree — but the two are interchangeable under one key, so "submit validated from
  a pristine checkout" can be discharged by a run that never made one. Both use phaze's
  `work.validate_cmd`, so their hashes always match — `just check` until 2026-08-25, `just
  check-fast` since. **M** — the ledger at `.git/bh-validation-ledger.json` stores exactly `{tree,
  cmd_hash, rc, at, host, sha}`.
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
- **There is no flag to force re-validation.** `bh work submit --help` offers none. The knobs are
  `work.ledger_ttl` and `work.validate_precheck` in `~/.beadhive/config.yaml` (phaze sets neither),
  or deleting `.git/bh-validation-ledger.json`.

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

### Test databases

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

### One database, one pytest process (phaze-ieqg)

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

### `pg_locks` and `pg_stat_activity` are cluster-wide — always scope them

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

### Never `just test-db-down` while another seat is running

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

### Every writable path shared by concurrent seats is a collision surface (phaze-rlshw)

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

### A gate is green only if its own pytest summary line says so (phaze-rlshw)

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

**And the durable record barely tells these apart either.** `.git/bh-validation-ledger.json` stores
`{tree, cmd_hash, rc, at, host, sha}`, so a signal death is `rc=143`, a genuine failure is `rc=1`,
and both are recorded as verdicts. Measured 2026-08-25: **55 of 56** entries carry no counts at all;
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

### Concurrent gates are bounded by headroom, not by isolation (phaze-rlshw, revised phaze-o24tm)

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
stores — `{tree, cmd_hash, rc, at, host, sha}`, and for these two entries **no counts** (one recent
entry does carry them; see the masking section above): it establishes that the kills happened and
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

### A gate measures a TREE, and that tree may not be the one anyone merges (phaze-fkha3)

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

#### Two questions do most of the work

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

#### Four more, when the first two leave it open

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

#### On a red gate, establish provenance BEFORE debugging

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

#### What is NOT already covered (verified against bh 0.15.0, not inferred)

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

#### The general form

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

## Code Quality

### Ruff Configuration

Line length: 150. Ruff lint `target-version` is `py313` — intentionally one minor behind the 3.14 runtime. Python 3.14's PEP 649 deferred annotations make ruff's `TC`/`UP037` rewrites want to move type-only imports into `TYPE_CHECKING` blocks and unquote annotations, which breaks Pydantic/SQLAlchemy/FastAPI (they resolve annotations at runtime via `get_type_hints`). Keep `py313` until those rewrites are safe.

**Enabled rule sets**: `ARG`, `B`, `C4`, `E`, `F`, `I`, `PLC`, `PTH`, `RUF`, `S`, `SIM`, `T20`, `TCH`, `UP`, `W`, `W191`

**Ignored rules**: `B008`, `C901`, `E501`, `S101`

**Per-file ignores**: `__init__.py` ignores `F401`. `T201` (print) is allowed in `scripts/parity/**`, `src/phaze/cli/**`, `src/phaze/main.py`, and tests. `services/**` ignores `S603`/`S607`. Tests (`tests/**`) also ignore `PLC`, `S105`, and `ARG001`.

**isort**: `lines-after-imports = 2`, `combine-as-imports = true`, `split-on-trailing-comma = true`, `force-sort-within-sections = true`. Set `known-first-party` to project package name.

**Format**: `quote-style = "double"`, `indent-style = "space"`, `docstring-code-format = false`.

### Mypy Configuration

```toml
[tool.mypy]
python_version = "3.14"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
disallow_untyped_decorators = true
no_implicit_optional = true
warn_redundant_casts = true
warn_unused_ignores = true
warn_no_return = true
warn_unreachable = true
strict_equality = true
mypy_path = ["src"]
exclude = "^(tests/|prototype/|services/|vulture_whitelist\\.py)"
```

Tests are excluded entirely (see `exclude` above), not run under a relaxed override.

### Pre-commit Hooks

Use frozen SHAs (not just tags) for all hooks. Required hooks:

- **pre-commit-hooks**: large files, executable shebangs, merge conflicts, TOML, YAML, JSON (check + pretty-format), AWS credentials, private keys, EOF fixer, trailing whitespace, mixed line endings
- **ruff-pre-commit**: `ruff --fix` + `ruff-format`
- **bandit**: `-x tests,services -s B608`
- **check-jsonschema**: GitHub workflows/actions validation
- **hadolint**: Dockerfile linting
- **actionlint**: GitHub Actions linting
- **yamllint**: strict mode
- **shellcheck-py**: `--shell=bash --severity=warning`
- **pre-commit-shfmt**: `--indent=2 --case-indent --language-dialect=bash --write`
- **Local mypy hook**: `uv run mypy .` with `pass_filenames: false`

## Testing

- Minimum **95% LINE coverage** repo-wide, plus a **90% per-module line floor**. Both are
  enforced by `scripts/coverage_floor.py`, which `just test-cov` and `just coverage-combine` run.
- Upload coverage to Codecov with service-specific flags
- Codecov config: precision 2, round down, range 70-100%, project target auto with 1% threshold, patch target 80% with 5% threshold

### Branch coverage: measured everywhere, gated per bead (phaze-bk9el.21)

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

## CI (GitHub Actions)

Follow the discogsography pattern:

- **Reusable workflows** via `workflow_call` — separate jobs for code quality, tests, security
- **Code quality job**: runs all pre-commit hooks
- **Test job**: runs pytest with coverage, uploads to Codecov with flags and `disable_search: true`
- **Security job**: pip-audit, bandit, osv-scanner, Semgrep, TruffleHog secret scanning, Trivy container scanning
- **Concurrency groups** with `cancel-in-progress` on PR workflows
- Emoji prefixes on all step names

## Code Style

- 150-character line length
- Type hints on all functions
- Double quotes for strings
- PEP 8 conventions
- `pyproject.toml` section order: `[build-system]` → `[project]` → `[project.scripts]` → `[tool.*]` → `[dependency-groups]`, with alphabetically sorted dependencies

<!-- GSD:project-start source:PROJECT.md -->
## Project

**Phaze**

A music collection organizer that ingests music files (mp3, m4a, ogg) and concert video streams, analyzes them, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

**Core Value:** Get messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

### Constraints

- **Language**: Python 3.14 exclusively
- **Package manager**: uv only
- **Deployment**: Docker Compose on home server, private network
- **Database**: PostgreSQL
- **Scale**: Must handle large file counts efficiently — batch processing and parallelization required
- **Existing code**: Must integrate with provided analysis prototypes and respect their per-file interface
- **Naming format**: AI filename proposals — specific format TBD (will be provided later)
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Recommended Stack
### Core Technologies
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.14 | Runtime | Project constraint. essentia-tensorflow dev1438+ ships cp314 wheels only, requiring Python 3.14. |
| FastAPI | >=0.141.1 | Web framework / API | De facto standard for async Python APIs. Native async, auto-generated OpenAPI docs, Pydantic integration, SSE support for real-time UI updates. Massive ecosystem and community. |
| SQLAlchemy | >=2.0.52 | ORM / database toolkit | Industry standard Python ORM. Full async support via `create_async_engine` + asyncpg driver. Declarative models, relationship management, migration support via Alembic. |
| asyncpg | >=0.31.0 | PostgreSQL async driver | Fastest Python PostgreSQL driver. Purpose-built for asyncio. Used as SQLAlchemy's async backend. |
| Alembic | >=1.19.1 | Database migrations | Official SQLAlchemy migration tool. Async template support (`alembic init -t async`). Autogenerate from model changes. |
| PostgreSQL | 16+ (pinned to `postgres:18-alpine` in docker-compose/CI) | Primary database | Project constraint. Handles large-scale file metadata, complex queries, JSON columns for flexible metadata, full-text search for future features. |
| Redis | 8.x in prod (`redis:8-alpine`), client pinned `redis>=8.1.0,<9.0` | Cache / pub-sub | No longer the SAQ broker (Phase 36 migrated the task queue to Postgres); used for caching analysis results and rate-limiting LLM API calls. **The test harness runs `redis:7-alpine`** (`justfile:352,358`) while production runs `redis:8-alpine` (`docker-compose.yml:143`) — a version-skew gap the suite does not cover. |
| Docker Compose | 2.x | Deployment orchestration | Project constraint. Runs PostgreSQL, Redis, API server, worker processes as separate containers. |
### Audio / Music Libraries
| Library | Version | Purpose | Why Recommended |
|---------|---------|---------|-----------------|
| mutagen | >=1.48.1 | Audio metadata read/write | The standard for audio tag manipulation in Python. Supports ID3v1/v2, Vorbis, MP4, FLAC, OGG, AIFF. Zero dependencies. Read AND write capability needed for renaming workflows. |
| essentia-tensorflow | >=2.1b6.dev1438 | Audio feature extraction (BPM, key, mood, style) | Comprehensive MIR library with pre-trained TensorFlow models. Beat tracking, tempo estimation, key detection, mood/style classification. Used for all audio analysis in the main application. |
| pyacoustid | *(not a pyproject.toml dependency — never used)* | N/A — historical | Originally recommended for Chromaprint/AcoustID bindings. The audio-fingerprinting feature it would have served (the `audfprint`/Panako pipeline) was implemented independently of pyacoustid and removed from the product entirely 2026-07-28 (epic phaze-0jpe; see `docs/design/0002-fingerprint-removal.md`). pyacoustid remains unused. |
| chromaprint (system) | latest | retained permanently — no known consumer | C library (`libchromaprint`) kept in the app/agent images through the phaze-0jpe removal. **Correction (phaze-0jpe.6):** it was previously described here as an essentia-tensorflow runtime requirement; that was tested against the live deployment and found false — `ldd` on the deployed `_essentia` extension shows no chromaprint link, and `import essentia` succeeds without it. No `phaze` source calls `fpcalc`/`chromaprint`/`Chromaprinter`/`acoustid`. It plausibly dates from the original `pyacoustid`/AcoustID plan that was never implemented. **Operator decision 2026-07-29: KEEP permanently** — the open phaze-0jpe.6 question is closed as "keep". A runtime `dlopen` path was never exhaustively ruled out and the install cost is trivial, so retention is deliberate, not deferred; do not re-open it as a cleanup task. See `docs/design/0002-fingerprint-removal.md`. Provides the `fpcalc` binary. |
| FFmpeg (system) | **7.1.x**, fixed by the base image tag (Debian trixie) — no apt version pin | The `ffmpeg`/`ffprobe` **CLI binaries** phaze shells out to: video-container audio extraction (`services/video_audio.py`) and the analysis duration probe (`services/analysis.py::_probe_duration_sec`, D-10) | **The previous `8.x` claim was wrong for both images** (phaze-b62ri, 2026-08-20): measured, the app image (trixie) served **7.1.5** and the arm64 agent (bookworm) served **5.1.9** — two different majors. The agent base moved **bookworm → trixie** to close that; the app image already was. **There is deliberately no `ffmpeg=<version>` pin.** Debian locks the upstream MAJOR.MINOR line per release (bullseye 4.3.x, bookworm 5.1.x, trixie 7.1.x), so the base tag already fixes the line, and an explicit pin would select *away* from security updates — bullseye currently serves `4.3.7-0+deb11u1` from main and the `4.3.9-0+deb11u2` candidate from **bullseye-security**. 7.1 is also the line the essentia-tensorflow wheel statically links, so base and analysis library agree by construction. **CI still tests 8.1** — a settled operator decision, not an oversight: see `docs/design/0013-ffmpeg-pin.md` §7. |
### Web UI
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Jinja2 | >=3.1 | Server-side templating | Ships with FastAPI. Server-rendered HTML means no separate frontend build, no SPA complexity. Perfect for admin-only tool. |
| HTMX | 2.x (CDN) | Dynamic UI interactions | Eliminates need for React/Vue/Angular. Adds SPA-like interactivity (approve/reject buttons, live search, pagination) via HTML attributes. Zero build step. 90% of SPA functionality, 10% of complexity. |
| Tailwind CSS | 4.x (standalone binary, pinned in `justfile`) | Styling | Utility-first CSS. Compiled at image-build time by the pinned standalone Tailwind binary (`just tailwind`) — no Node, no CDN, no client-side compiler. DaisyUI component library optional for pre-built components. |
| Alpine.js | 3.x (CDN) | Lightweight JS interactions | 3KB library for dropdown menus, modals, toggling states. Complements HTMX for client-side state that HTMX doesn't handle. |
### Task Processing
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| SAQ | >=0.26.4 (`saq[postgres]`) | Async task queue | Purpose-built for asyncio. Inspired by arq with active maintenance. Broker migrated from Redis to Postgres in Phase 36 (`PostgresQueue`, `saq_jobs` table). Perfect for file analysis jobs (BPM, metadata extraction). Supports retries with backoff, job results, cron jobs, built-in web UI. Single-user app doesn't need Celery's complexity. |
### AI / LLM Integration
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| litellm | >=1.98.0,<1.99.0 (pin exact minor) | Unified LLM API client | Single interface to 100+ LLM providers (OpenAI, Anthropic, local models). Avoids vendor lock-in. Use for filename/path proposals. **IMPORTANT:** Pin exact minor line due to the March 2026 supply chain incident on versions 1.82.7-1.82.8. Verify checksums. |
| pydantic | >=2.10 | Data validation / LLM structured output | Already a FastAPI dependency. Use for validating LLM responses (proposed filenames, paths). Structured output parsing. |
### Configuration / Infrastructure
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| pydantic-settings | >=2.15.0 | Configuration management | Type-safe config from env vars, .env files, Docker secrets. Native Pydantic integration. Supports `SecretStr` for API keys. |
| uvicorn | >=0.52.3 | ASGI server | Standard production server for FastAPI. Use with `--workers` for multi-process or behind gunicorn for production. |
### Development Tools
| Tool | Purpose | Notes |
|------|---------|-------|
| uv | Package management | Project constraint. Fast, deterministic. Use `uv run` prefix for all commands. |
| ruff | Linting + formatting | Already configured in CLAUDE.md. Replaces flake8, black, isort. |
| mypy | Type checking | Already configured. Strict mode excluding tests. |
| pytest | Testing | With pytest-asyncio for async tests, pytest-cov for coverage. |
| pytest-asyncio | Async test support | Required for testing async endpoints, database operations, task queue jobs. |
| httpx | HTTP test client | FastAPI's recommended test client. Use `AsyncClient` for async endpoint testing. |
| pre-commit | Git hooks | Already configured in CLAUDE.md. |
## Installation
# Core application
# Audio processing
# AI integration
# Dev dependencies
# System dependencies (Dockerfile)
# apt-get install -y ffmpeg chromaprint-tools
# NOTE: ffmpeg is deliberately NOT version-pinned (phaze-b62ri). The BASE IMAGE TAG is the
# version control — Debian locks the upstream MAJOR.MINOR line per release, so trixie means
# 7.1.x. An explicit `ffmpeg=<version>` would pin security updates OUT. Unpinned, this used
# to resolve to 7.1.5 on trixie and 5.1.9 on bookworm; the fix was moving the arm64 agent's
# base to trixie, not adding a pin. See docs/design/0013-ffmpeg-pin.md.
| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| FastAPI | Litestar | If you want more explicit DI and slightly lower memory usage. FastAPI wins on ecosystem size, docs quality, and community support. |
| SQLAlchemy | SQLModel | If models are simple and you want less boilerplate. SQLModel is a thin FastAPI-aligned wrapper over SQLAlchemy but has fewer features and weaker async story. Stick with SQLAlchemy for large-scale systems. |
| SAQ | Celery | If you need multi-broker support, complex routing, or canvas workflows. Overkill for a single-user app. Celery's config complexity is not justified here. |
| SAQ | Dramatiq | If you want RabbitMQ support or more mature retry/middleware. Dramatiq is sync-first which conflicts with our async stack. |
| HTMX + Jinja2 | React/Vue SPA | If you need offline capability, complex client-side state, or multiple developers on frontend. A single-user admin tool does not need SPA complexity or a separate build pipeline. |
| litellm | Direct OpenAI SDK | If you are committed to a single LLM provider forever. litellm provides flexibility to switch between local/cloud models with zero code changes. |
| mutagen | tinytag | If you only need read-only metadata. We need write capability to update tags after renaming, so mutagen is required. |
| essentia-tensorflow | librosa | If you only need basic BPM/tempo and don't need pre-trained classification models. Essentia provides richer analysis (mood, style, danceability) via TensorFlow models. |
## What NOT to Use
| Avoid | Why | Use Instead |
|-------|-----|-------------|
| ffmpeg-python (pip: `ffmpeg-python`) | Last PyPI release was 2022. Effectively abandoned. 500+ open issues on GitHub. | Use `subprocess.run(["ffprobe", ...])` directly for metadata extraction. Or `python-ffmpeg` (pip: `python-ffmpeg`) which is actively maintained. |
| SQLite | Cannot handle concurrent writes from multiple worker processes analyzing files in parallel. No JSON operators for flexible metadata queries. | PostgreSQL (project constraint). |
| Celery | Massive dependency tree, complex configuration, sync-first design. Overkill for single-user app with Redis already in stack. | SAQ for async task queue. |
| Django | Full MVC framework with ORM, admin, auth -- all unnecessary when you have FastAPI + SQLAlchemy + custom admin UI. Sync-first design conflicts with async processing needs. | FastAPI. |
| LangChain | Enormous abstraction layer for LLM calls. This project just needs "send prompt, get structured response." LangChain adds complexity without benefit for simple classification/naming tasks. | litellm for provider abstraction + raw Pydantic for structured output. |
| React/Next.js | Requires separate build pipeline, Node.js in Docker, npm dependencies. Completely unnecessary for a single-user admin approval UI. | HTMX + Jinja2 + Tailwind CSS via CDN. |
| tinytag | Read-only metadata extraction. Cannot write updated tags back to files after renaming. | mutagen for read+write. |
| psycopg2 | Sync driver. Blocks the event loop. Cannot be used with async SQLAlchemy. | asyncpg for async PostgreSQL access. |
## Version Compatibility
| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| SQLAlchemy >=2.0.52 | asyncpg >=0.31.0 | Use `postgresql+asyncpg://` connection string. Some older asyncpg versions (0.29.x) had issues with `create_async_engine`. |
| essentia-tensorflow >=2.1b6.dev1438 | Python 3.14 | dev1438+ ships cp314 wheels only (macOS arm64/x86_64 + linux x86_64; no linux/arm64). Keep platform marker `sys_platform != 'linux' or platform_machine == 'x86_64'` in dependencies. |
| FastAPI >=0.141.1 | Pydantic 2.x | FastAPI requires Pydantic v2. Do not install Pydantic v1. |
| FastAPI >=0.141.1 | Starlette (resolved through FastAPI) | Do not override FastAPI's Starlette constraint. |
| Alembic >=1.19.1 | SQLAlchemy >=2.0 | Use `alembic init -t async` for async template. Import all models in `env.py` for autogenerate to work. |
| litellm | ALL | **Pin exact minor line.** Supply chain attack on 1.82.7/1.82.8 (March 2026). Pinned `>=1.98.0,<1.99.0`; raise the cap deliberately after vetting. Verify SHA checksums. |
| SAQ >=0.26.4 (`saq[postgres]`) | Postgres (psycopg[binary]>=3.3.4) | Broker migrated from Redis to Postgres in Phase 36. Redis (client >=8.1.0) is used for caching only now. |
| FFmpeg (system) 7.1.x (via the base image) | the essentia-tensorflow wheel's **static** ffmpeg 7.1 (amd64) AND the arm64 agent's from-source essentia, compiled against trixie `libav*-dev` 7.1.x | This is the compatibility that matters and the reason 7.1 is the right line. **amd64:** the wheel's `_essentia.cpython-314-x86_64-linux-gnu.so` statically links ffmpeg 7.1 — no bundled `libav*.so`, no soname refs, so there is no dynamic seam to repoint; trixie's CLI is already on that line. **arm64:** no cp314 aarch64 wheel exists, so essentia is compiled from source against `libavcodec-dev`/`libavformat-dev`/`libavutil-dev`/`libswresample-dev` — the same Debian source package as the CLI, so the base tag moves both together or neither. Reaching 7.1 there required the base move to trixie (bookworm tops out at 5.1.x). **Python stays 3.13 on that image** — TF ships no cp314 aarch64 wheel (dependabot PR #326 proved 3.14 breaks the build); base suite, Python, TF and the pinned essentia commit are one combination, not four independent knobs. |
| chromaprint (system) | no verified consumer | Not consumed via `pyacoustid` (unused) or by any `phaze` source. **Not** an essentia-tensorflow runtime dependency either — `ldd` on the deployed `_essentia` extension shows no chromaprint link and `import essentia` succeeds without it (phaze-0jpe.6 correction; see `docs/design/0002-fingerprint-removal.md`). **Retained permanently by operator decision 2026-07-29** — phaze-0jpe.6 closed as "keep"; stays installed (`chromaprint-tools`) in Docker. |
## Confidence Assessment
| Area | Confidence | Reasoning |
|------|------------|-----------|
| Web framework (FastAPI) | HIGH | Verified current version, massive ecosystem, well-documented async patterns |
| Database (SQLAlchemy + asyncpg + Alembic) | HIGH | Standard production stack, verified versions, extensive async documentation |
| Audio metadata (mutagen) | HIGH | No real alternative for read+write. Stable, zero-dependency, widely used |
| Audio analysis (essentia-tensorflow) | HIGH | Comprehensive MIR library with pre-trained models for BPM, key, mood, style classification |
| Task queue (SAQ) | HIGH | Actively maintained and async-native. The pipeline uses SAQ's PostgreSQL backend; Redis is reserved for cache, rate limiting, execution progress, and counters. |
| LLM integration (litellm) | MEDIUM | Best abstraction layer but recent supply chain incident is concerning. Pin versions aggressively, verify checksums |
| Web UI (HTMX + Jinja2) | HIGH | Well-proven pattern for Python admin tools. No build step, no JS framework complexity |
## Sources
- [mutagen on PyPI](https://pypi.org/project/mutagen/) -- project floor 1.48.1
- [essentia on PyPI](https://pypi.org/project/essentia-tensorflow/) -- version 2.1b6.dev1438, used for audio analysis
- [FastAPI releases](https://github.com/fastapi/fastapi/releases) -- project floor 0.141.1
- [SQLAlchemy on PyPI](https://pypi.org/project/SQLAlchemy/) -- project floor 2.0.52
- [Alembic on PyPI](https://pypi.org/project/alembic/) -- project floor 1.19.1
- [SAQ on PyPI](https://pypi.org/project/saq/) -- project floor 0.26.4
- [litellm security incident](https://docs.litellm.ai/blog/security-update-march-2026) -- supply chain attack March 2026
- [pydantic-settings on PyPI](https://pypi.org/project/pydantic-settings/) -- project floor 2.15.0
- [HTMX + FastAPI patterns](https://johal.in/htmx-fastapi-patterns-hypermedia-driven-single-page-applications-2025/) -- 2025 production patterns
- [Python task queue benchmarks](https://stevenyue.com/blogs/exploring-python-task-queue-libraries-with-load-test) -- arq/dramatiq/huey performance comparison
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

### No local identifiers in tracked files

phaze is developed against a real personal music archive on real hardware. Investigation output — spike docs, planning notes, debug write-ups, benchmark records — is where traces of that archive accumulate, because the honest way to report a measurement is to say what it was measured on.

**Do not commit them.** Scrub as you write, not afterwards.

**Never commit:** filenames, directory names or absolute paths from the real archive (the category that matters most — a release name in a log excerpt, a staging-mount path in a traceback, a directory name in a table of sampled files); content digests and file UUIDs taken from live data.

**Acceptable:** invented example filenames that illustrate a naming format (`Artist - Event - Title (2024).mp3`); synthetic test fixtures (`song.mp3`, `dup.mp3`); host and account names in local instruction material. Committed source, scripts and published docs should refer to hosts by role instead.

**Use these placeholders**, following the vocabulary already established in `docs/spikes/`:

| For | Use |
|-----|-----|
| Individual tracks | `<track-01>`, `<track-02>`, … |
| Concert sets / long recordings | `<set-01>`, `<set-02>`, … |
| Archive mount, host side | `<archive-mount>` |
| Archive mount, in-container | `<archive-mount-in-container>` |
| Local scratch directories | `<scratch>/…` |
| Fingerprint digests | `fp_<hash-1>`, `fp_<hash-2>`, … |
| File UUIDs | `<uuid-1>`, `<uuid-2>`, … |
| Hosts, where a role name will not do | `host-prod`, `host-store` |

**Replace identifiers, never quantities.** This is the rule that gets broken when scrubbing is rushed, and it destroys the value of the document it was meant to protect. Every measured value stays exact — row counts, durations, latencies, sample sizes, percentages. Good: "36 files totalling 42.34 h, stratified across the duration distribution". Bad: "a few dozen files" — scrubbed, but now worthless as evidence. If a scrub changes a number, it is a bug in the scrub; diff the numeric tokens before and after and confirm the only digits lost were part of a removed identifier.

**Scope:** any tracked file — spike and design docs, `.planning/**`, source comments, scripts, SQL. Also commit messages and PR bodies, which are just as permanent and just as public as the files.

**The history caveat:** scrubbing a file does not scrub git history. Once an identifier is committed, removing it from the working tree leaves it fully readable via `git show <old-sha>`, and removing it from history means a rewrite and a force-push — disruptive, and on a shared branch possibly not viable at all. **Prefer never committing the identifier over fixing it later:** use the placeholder in the first draft rather than the real name you intend to replace before pushing.
### Cite ADRs by filename, never by bare number

Write `docs/design/0015-shared-session-gather.md`, not "ADR-0015". Where the prose reads better with the number, keep the number *and* the disambiguator — "ADR-0015 (shared session gather)".

**A bare number is a pointer with no redundancy, so nothing can check it.** ADR numbers are reassignable: renaming a file frees its number, and the next ADR to claim it silently inherits every citation written against the old occupant — correctly formed, greppable, and now resolving to a different, currently-valid document.

**Measured, 2026-08-24 (`phaze-f70y9`).** `4a08e873` renumbered `0004-tracklist-candidate-sets.md` to `0014-...` while `d4f673ac` introduced the shared-session-gather ADR *as* 0014. Session-gather was pushed to 0015; a census of the 3,200-bead corpus found **8 bare "ADR-0014" citations, all in one bead, all meaning session gather, all now resolving to the tracklist ADR**. It was caught exactly once, **by a human reading prose**; no grep, link checker or CI check found it or could have, because "ADR-0014" implies no path — nothing to dereference, nothing to 404.

**`phaze-x2z38`'s duplicate-leading-number guard (`tests/shared/test_adr_numbering.py`) does NOT cover this** and must not be read as if it does: these numbers were never duplicated at any instant, 0014 was legally *reused* after a rename freed it. It is still worth having — it removes the principal *cause* of renumbers.

**When you renumber, sweep the number VACATED and the number newly OCCUPIED.** The second gets missed, and that is a general property of renumbers, not anyone's lapse: at planning time the newly-occupied number is not yet anybody's, so there is nothing to sweep for. `phaze-kbue9` swept 0004 thoroughly and never swept 0014. Likewise **do not cite a number before its file exists** — a forward citation is dangling when written and silently becomes *wrong* once something else claims the number (`f4c39654` cited ADR-0014 when `docs/design/` topped out at `0013-ffmpeg-pin.md`).

*The general form:* a pointer with no redundancy cannot be checked by any tool, so the redundancy must be written into the citation at authoring time — the same shape as [ADR-0012](docs/design/0012-verification-fidelity-and-operator-attribution.md)'s rule that a decision attributed to the operator carries its question, answer, date and durable record.

<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

### Analysis is exhaustive, chunk-bounded, and killed only by lack of progress

`analyze_file` analyzes **every natural fine and coarse window of every file**, whatever its
length. There is no window cap, no even stride, no `sampled` flag, and no `deepen` path — all
removed by `phaze-w55w1`. Two invariants replace what the caps used to provide, and both are
easy to break by accident:

- **Live PCM is bounded by the CHUNK, and since D-09 so is process peak RSS.** Each tier decodes
  and analyzes 60 (fine) / 30 (coarse) windows at a time, so live PCM stays ~317 MB / ~345 MB
  regardless of duration. Do not "simplify" either pass back into a single whole-tier decode: on
  a 12-hour set that is ~7.3 GiB and a cgroup OOMKill. **The process peak did not follow the PCM
  until D-09**, and that gap was a P0: measured on vox against real audio (`phaze-b2qs9`,
  2026-08-12) whole-process peak grew **+0.31 GiB per fine chunk** (R² 0.99959), reaching
  **4.1854 GiB at 4 hours** and **10.2768 at 12h04** against a deployed `memory_limit: 4Gi`, so
  every file past ~3 hours OOMKilled. Re-measured on the same node, same image and the **same
  files** after the fix (`phaze-u1n7j`, 2026-08-13): **1.4985 / 1.6500 / 1.6725 GiB** at 1:00 /
  4:00 / 12:04 — the longest file in the corpus at **41.8%** of the limit, and a residual
  **0.0013 GiB per fine chunk** (99.6% of the slope gone). If you are re-deriving pod sizing,
  read `docs/spikes/phaze-u1n7j-vox-fix-verification.md` for what those numbers do and do not
  license; and if peak ever starts tracking duration again, the teardown below is the first
  suspect, not the buffers. Do not raise the limit to paper over a regression
  (`backends.toml` says so explicitly); duration-linear growth is a bug, not a sizing input.
- **A chunk's streaming network must be DISCONNECTED, not just dropped** (D-09, `phaze-u1n7j`).
  This is the half the chunking originally missed and it cost a deploy: dropping the Python
  proxies leaves essentia's C++ edges — and the `PoolStorage` a Pool sink creates, which Python
  never holds at all — allocated, so every **gated** chunk decode retained ~5 MiB per window
  branch. That turned a per-chunk constant into **+0.31 GiB per 30 minutes of audio** and
  OOMKilled every file past ~3 hours against the 4Gi limit (measured on vox by `phaze-b2qs9`;
  diagnosed and fixed by `phaze-u1n7j`). `gc.collect()` is required alongside the disconnect —
  every streaming algorithm is born into a reference cycle, so refcounting alone never destroys
  it — and `malloc_trim` cannot help, because the pages are live-referenced rather than merely
  un-returned. Two tests hold the line and **neither can be satisfied by a mocked essentia**
  (the original long-file test was mocked, which is why this shipped):
  `test_repeated_gated_chunk_decodes_do_not_grow_peak_rss` and
  `test_the_chunk_decode_leaves_no_connected_network_behind`.
- **Nothing is killed for running long.** A multi-hour concert set is expected to take hours.
  Liveness is progress-based (`analysis_stall_timeout_sec`, default 1800 s of *silence*): the
  child heartbeats window completions, chunk decodes and model sweeps, and only total silence
  kills it. The SAQ `process_file` job runs `timeout=0` + a SAQ `heartbeat`. **Never add a
  wall-clock bound on any lane** — `phaze-1b39` is the incident where one SIGTERM'd legitimate
  2–6 hour analyses and stalled the whole burst lane.

The decision records live with the code: **D-07** (chunking) in `services/analysis.py` and
**D-08** (stall liveness) in `services/analysis_exec.py`.
`docs/design/0007-windowed-analysis.md` has the full rationale and cost analysis (§7 the
operator decision, §8 what shipped); `docs/essentia-analysis.md` has the operational view.
**Measured, 2026-08-12:** `docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md` is the real
peak-RSS / wall-clock measurement on genuine multi-hour corpus files that ADR-0007 §8 was owed.
Read it before touching either invariant: it confirms exhaustive coverage and the chunk gate's
correctness, gives the end-to-end wall clock (**0.56–0.79× the file's own duration**, solo on
vox), and **refuted** the duration-independent-peak claim, which is what opened `phaze-u1n7j`.
**Re-measured, 2026-08-13:** `docs/spikes/phaze-u1n7j-vox-fix-verification.md` is the after side —
same node, same image, the same three files — and it restores that claim, with the mechanism
(D-09) and an equivalence check showing the analysis output is byte-identical across the fix.
Its §3 also **rules glibc arena fragmentation out**, which `phaze-b2qs9` §7 FU-1 had listed as a
prime suspect. The synthetic long-file test
(`tests/analyze/services/pipeline/test_analysis_long_file.py`) proves the claim of a *mocked*
essentia only and always did — the guards that hold it now are in
`test_analysis_streaming_decode.py`, on the real network.

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

## Beadhive Workflow Enforcement

All work in this repo flows through beadhive. Do not make direct repo edits outside this workflow unless the user explicitly asks to bypass it.
The five rules in **Acceptance criteria, attribution, and verification fidelity** directly above bind every step below — they are what the review gate in step 7 is checking, and they are not advisory.

1. **Every piece of work has a bead.** Larger work is an epic with specific stories/tasks/bugs as children. File epics through the planner (`bh plan file`), never by hand — hand-rolled epics fail the molecule convention check.
2. **Exploring a new idea?** Use the planner: invoke the `bh:planner` skill (`/bh:plan <idea>`) to drive ideate → research → decompose → file.
3. **When filing a new bead, ask clarifying questions** — scope, priority, acceptance — before writing the description.
4. **Before starting execution on a bead**, if there is any ambiguity about what must be delivered, keep asking clarifying questions until the work is clear. An acceptance criterion that looks wrong or ambiguous is a question for the operator, never something to reinterpret in prose — see rule 1 above, and `phaze-3ea41` for what reinterpreting one costs.
5. **Once work starts, the dispatching session occupies the dispatcher seat itself** — load the `bh:dispatcher` skill and drive the molecule from that session; do NOT spawn a `bh:dispatcher` sub-agent (a sub-agent surrenders mid-flight visibility and leaves the session inferring state from git, which misreads both uncommitted work and evidence-only spike beads). From that seat, **dispatch a team of developer sub-agents**, each working in its own worktree (`wt/bead/issue/<id>`) branched off the bead's integration branch. Never share a worktree, a test database, or any other writable path between concurrent agents — see "Every writable path shared by concurrent seats is a collision surface".
6. **Fix pushes get the adversarial treatment.** Before a `fixgroup:*` integration branch merges — a molecule whose children are bug beads from a bug hunt — run a **diff-scoped adversarial verification pass over the fix diff**: the same different-model, default-to-refuted verifier the hunt used to confirm its findings, with the fix itself as the claim under refutation. Findings become new beads, never silent edits. Rationale, the measurements behind it, and an explicit list of what the pass does **not** catch: [ADR-0011](docs/design/0011-bug-hunt-cadence.md). The same ADR sets the hunting cadence — routine lens passes are scoped to the diff since the last hunt; whole-tree passes are reduced in frequency, **not** retired.
7. **When all children of the bead are done:** land the molecule (merge commit, never squash), then close the bead(s) with comments explaining the outcome. **A code molecule needs no PR** — `bh work finish <epic>` onto local main, then push `main` to origin directly; CI runs after the fact, so the full `just check` on an isolated seat is what actually gated it. **A docs molecule does**: open a PR, invoke a code review, and wait for green CI. If anything fails, investigate and fix — do not bypass. See "Workflow: Features and PRs" for which changes count as docs.
8. **Periodically push the beads DB** to the Dolt remote: `bd dolt push`.

<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd:profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->

<!-- bv-agent-instructions-v3 -->
---

## Beads Workflow Integration

Work in this repo is tracked as **beads** in a local Dolt database under `.beads/`
(git-ignored) and synced to the Dolt remote `origin`
(`git+ssh://git@github.com/SimplicityGuy/phaze.git`). Beads are driven through
**beadhive** (`bh`); triage and graph analysis come from **beads_viewer**
(`bv`). See `## Beadhive Workflow Enforcement` above for the process rules — this
section is the command reference for them.

> **Editing note.** This block sits between two `bv` marker comments. `bv` detects it
> by marker and version only, never by content, so the prose here is free to diverge
> from `bv`'s stock text — but **do not touch, reword, or renumber the marker lines**,
> or `bv` starts prompting to add the section again at startup. Verify with
> `bv --agents-check`, which should report `blurb v3 — up to date` and exit 0.
> Be aware that `bv --agents-update` would overwrite everything below with `bv`'s
> generic boilerplate; don't run it unless you intend to lose this.

### The toolchain, and what is actually installed

| Tool | Status | Use it for |
|------|--------|------------|
| `bh` | installed | Everything lifecycle: reading, filing, claiming, validating, submitting, merging |
| `bv` | installed (v0.18.0) | Triage and graph analysis — *what to work on*, never mutation |
| `bd` | on `PATH` | Low-level beads CLI. Avoid calling directly (see below); the one sanctioned use is `bd dolt push` |

**The `bh bd` passthrough is disabled** (`passthrough.bd_enabled` defaults off), so
`bh bd <args>` errors out rather than forwarding. Calling `bd` directly is possible but
discouraged — a `PreToolUse` hook warns that it is not hive-aware and can hit the wrong
database. Read through `bh work`, file through `bh plan file` or the `bh` MCP tools, and
reach for `bd` only where this section says to.

### Reading beads

```bash
bh work ready                  # unblocked, dependency-ordered work
bh work list --status open     # filter by state; --json for machine output
bh work issue <id>             # one bead's fields, labels, model:/harness:
bh work brief <id>             # requirements + goals + the validation command
bh work show <id>              # the bead branch's local history before submit
```

All are read-only and accept `--json`.

### Filing beads

- **Epics / molecules** → the planner. Invoke the `bh:planner` skill (`/bh:plan <idea>`)
  or `bh plan file`. Never hand-roll an epic: it fails the molecule convention check.
- **A single bead** → the `bh` MCP tool `bd_create`, which auto-applies the
  provider/org/repo triplet and validates labels.
- Ask clarifying questions on scope, priority and acceptance *before* writing the
  description.

### Driving a bead

The lifecycle is `bh work`; raw `git` is only for the change *inside* the worktree.

```bash
bh work claim <id>       # provision the wt/bead/issue/<id> worktree + identity, → in_progress
bh work check <id>       # run the hive validation (`just check-fast`, phaze-pv3kk — change-
                         # selected, escalating to the full suite) against the worktree
bh work submit <id>      # verify clean conventional history, re-validate with `just check-fast`
                         # (or replay a cached verdict), open the review gate
bh work approve <id>     # resolve the review gate
bh work merge <id>       # serialize a --no-ff merge onto the integration branch, close the bead
                         # (re-validates nothing when landing into a molecule under the default
                         # `validation: relaxed`; landing on `main` DOES validate, via `merge-main`
                         # — see "Which commands are gates" above for the full boundary table)
bh work resume <id>      # re-attach after changes-requested
bh work abandon <id>     # release the claim; --rm also removes the worktree
```

`submit` rejects noisy history — more than `max_commits` over base, or non-conventional
subjects. Use `bh work show <id>` to inspect and `bh work refine <id>` to squash local
checkpoints before resubmitting. `submit` publishes the branch only when the review gate
is `gh:run` / `gh:pr`; with the default in-process human gate it does not push, so open
the PR yourself.

### Triage with bv

`bv` is a graph-aware triage engine: dependency-aware, deterministic output with
precomputed metrics (PageRank, betweenness, critical path, cycles, HITS, eigenvector,
k-core). Use it instead of parsing `.beads/issues.jsonl` or guessing at graph traversal.

**Use only `--robot-*` flags. Bare `bv` launches an interactive TUI that blocks the session.**

```bash
bv --robot-triage                 # THE MEGA-COMMAND: start here
bv --robot-next                   # just the single top pick + claim command
bv --robot-triage --format toon   # token-optimized output for lower context cost
```

`--robot-triage` returns `quick_ref` (counts + top 3 picks), `recommendations` (ranked,
with unblock info), `quick_wins`, `blockers_to_clear`, `project_health` and `commands`.

Before claiming, confirm current state with `bh work issue <id>` or `bh work ready` —
`bv` reads an exported JSONL snapshot, so `bh` is the authority on live state.
`recommendations` can include graph-important work that is blocked or already assigned;
only `quick_ref.top_picks` and non-empty `claim_command` fields are actually claimable.

| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with unblocks lists |
| `--robot-priority` | Priority misalignment detection with confidence |
| `--robot-insights` | PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core |
| `--robot-alerts` | Stale issues, blocking cascades, priority mismatches |
| `--robot-suggest` | Hygiene: duplicates, missing deps, label suggestions, cycle breaks |
| `--robot-diff --diff-since <ref>` | Changes since ref: new/closed/modified |
| `--robot-graph [--graph-format=json\|dot\|mermaid]` | Dependency graph export |

```bash
bv --robot-plan --label backend        # scope to a label's subgraph
bv --robot-insights --as-of HEAD~30    # historical point-in-time
bv --recipe actionable --robot-plan    # pre-filter: ready to work (no blockers)
bv --recipe high-impact --robot-triage # pre-filter: top PageRank scores
```

### Key concepts

- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog. Use the numbers
  `0`–`4`, not words.
- **Types**: `bug`, `feature`, `task`, `epic`, `chore`, `decision`. Aliases:
  `enhancement`/`feat` → `feature`, `dec`/`adr` → `decision`. Anything else is rejected
  with `invalid issue type` unless registered under `types.custom`.
- **Dependencies**: beads can block other beads; `bh work ready` shows only unblocked
  work.
- **Worktrees**: one per bead, `wt/bead/issue/<id>`. Never share a worktree, a test
  database, or a Redis logical DB between concurrent agents — nor any other writable path,
  scratch directories and gate logs included.
- **`review:*` on a CLOSED bead means nothing** (phaze-s3d1u). The label tracks review state
  while a bead is live; `bh` clears it on approve, merge and land, but the clear has holes —
  group/molecule merges have no clear call at all — so 174 of 2412 closed beads carry a stale
  `review:pending`. **Nothing automated reads it after close:** `bv` 0.18.0's binary contains zero
  occurrences of any `review:*` label, and `bh`'s only consumer (`work_next`) filters to non-closed
  beads before reading it. So the practical rule is simply **filter on status when you query that
  label** — `bd list --label review:pending --status open`, never the bare `--label` form. Two
  constraints go with it: **resolving a gate as SUPERSEDED rather than approving a sha nobody
  reviewed is CORRECT and must stay available** — a fix that made `approve` the only tidy path
  would push seats toward false attestations, which is strictly worse than a stale label — and **do
  not mass-edit the existing beads**, which would re-accumulate from the next batch merge. Tracked
  upstream at beadhive/beadhive#15. *The general form:* a record that reads as a status is not one.
- **A repowise health number can be stale, and "trust the index" does not cover it** (phaze-ia4ah).
  The repowise guidance in `.claude/CLAUDE.md` says *"Trust the index — `verified: true` means the
  bytes were checked against the live tree, so never re-read"*, and treats `index_behind: true` as
  informational. That is about **verified BYTES**; it says nothing about **freshly computed
  METRICS**, and the two go stale by different rules.
  **The check, on repowise 0.45.0 — and it is TARGET-AWARE, not a commit equality test.**
  `repowise update` runs the health fold **incrementally, only over the files changed since the last
  index**, stamping each recomputed row's `analyzed_commit` with HEAD-at-fold-time. So rows
  legitimately carry *different* commits, and **an older `analyzed_commit` does not mean stale** — if
  the file has not changed since, that row still describes the current bytes. The question to ask
  per file is whether the file changed after its stamp:

  ```bash
  git diff --quiet "$analyzed_commit" HEAD -- "$path" && echo current || echo STALE
  ```

  **Do NOT write `analyzed_commit != HEAD` and call it stale.** That is the upstream maintainer's
  named trap (repowise-dev/repowise#1864, 2026-08-24): it overstates what the commits prove and
  flags legitimately-current rows. This guidance made exactly that mistake once — see the note below.
  There are **three** states, not two: **known** (a stamp you can compare), **mixed** (a response
  spanning several folds — `get_health` reports `health_analyzed_commits_distinct` for this, and one
  repo-wide boolean cannot represent it), and **unknown** (`NULL`, or a commit git cannot resolve —
  which is *not* the same as current). `get_health` emits `health_analyzed_commit` only when the
  newest row has one, and omits it entirely when every row is `NULL` rather than saying provenance is
  unknown.
  **A targeted call's provenance is not necessarily about your target.** Upstream states the summary
  is currently derived from the repo-wide metric population, so a freshly analyzed *unrelated* file
  can make the metadata look newer than the metric you asked for. Compare per path; do not read the
  response-level stamp as a verdict on your file.
  **Provenance is still being dropped on several write paths** — the full health re-score in
  `update_cmd/persistence.py`, `repowise health`'s own persistence, the fast-index upgrade pass, and
  the `IndexStore` interface/SQL adapter, which cannot forward the commit at all. Any of those can
  replace the table with entirely `NULL` provenance, so a store that once had stamps can lose them.
  `analyzed_commit` is HEAD-at-fold-time, **not** the file's own last-modifying commit (measured:
  0 of 8 sampled rows matched `git log -1 -- <file>`) — it records *when* a row was computed, not
  *what content* it describes.
  **The staleness is not confined to `get_health`.** `get_risk` embeds `health_score`,
  `coverage_pct`, `branch_coverage_pct` and `top_biomarkers` from the same fold rows, and
  `get_context` does via `include=["health"]` — neither warns. Only `get_dead_code` emits
  `_meta.stale_warning`, and it is the tool whose data `update` *does* refresh; `get_health` emits
  none, and the standard message ("run `repowise update`") would be poor advice for it anyway, since
  an update only re-folds changed files.
  **To force a fresh read:** `repowise health --file <path>` recomputes one file in-process
  (`--format json` and `--module <prefix>` are the bulk equivalents). **Do NOT conclude "always use
  the CLI"**: that was measured harmful, because it fixes nothing for bulk reads while leaving agents
  believing they have worked around it. Check `analyzed_commit` instead.
  *Same general form as the entry above:* a record that reads as a status is not one.
  > **This entry was wrong once, and the way it was wrong is the lesson.** As landed on 2026-08-24 it
  > said `repowise update` never runs the health fold and that no cheap staleness check exists. Both
  > were true when measured on repowise **0.44.0** and false on the **0.45.0** that was already
  > installed when the text was written — the prose was faithful to the bead's evidence and stale
  > about the tool. The **first correction then overshot**, asserting `analyzed_commit == HEAD` as a
  > freshness test; the upstream maintainer had already named that exact comparison as a trap hours
  > earlier on `repowise-dev/repowise#1864`, and it was not read before writing. Two lessons, both
  > cheap: **re-measure a tool's behaviour against the version you are running** (the phaze-b62ri
  > shape), and **read the upstream thread before restating what a tool does** — a maintainer's reply
  > outranks our black-box measurement about intent, even when our measurement is correct. #1864
  > remains OPEN and is *not* fixed: provenance stamping is partly implemented, several writers still
  > drop it.
- **`bd label remove` reports success unconditionally** (gastownhall/beads#5988). It prints
  `✓ Removed label 'X' from Y` and exits 0 **even when the bead never had X** — so that line is
  evidence of neither the label's presence nor its removal. Read the label set back with
  `bd label list <id>` if it matters. This is not academic: phaze-s3d1u cited exactly that line as
  its proof and was wrong about the mechanism as a result.

### Syncing the Dolt remote

The JSONL export under `.beads/` is maintained for you — there is no flush step to run.
Periodically push the beads database itself:

```bash
bd dolt push
```

### Git policy

`bh` owns the lifecycle around the change; it does not absolve you of this repo's git
rules. Follow the repository's own instructions before staging, committing or pushing —
"commit only when asked" overrides any generic workflow advice, here or elsewhere.

<!-- end-bv-agent-instructions -->

<!-- bh:agf:start (managed by `bh hive init` — edit outside these markers; `-f` refreshes) -->
## AGF — Agentic Git Flow

This repo is onboarded as a **`bh` hive** and develops via **AGF**: work is tracked in beads
and driven through `bh`, **not** raw `git` / `bd` / `gh`.

- **Is this repo set up for AGF?** → run `bh hive ready` (add `-v` for the line-item breakdown).
- **Lifecycle, roles, conventions:** see `docs/AGF.md` and the bh plugin's role skills.
- Drive beads with `bh work`; load the role skill for your seat (coordinator / developer / merger).
- Batch/collapsed work lives in ONE shared `wt/batch/<group>` worktree and completes as a UNIT:
  `bh work submit --group` then `bh work merge --group` — per-bead `submit`/`check` don't apply.
<!-- bh:agf:end -->
