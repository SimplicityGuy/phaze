# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**phaze** — A music collection organizer. Python 3.14, MIT licensed. Ingests music files (mp3, m4a,
ogg) and concert video streams, analyzes them, uses AI to propose better filenames and destination
paths, and provides an admin web UI to review and approve the renames/moves. Single operator, large
personal archive (primarily full sets from events like Coachella).

**Core value:** messy music and concert files properly named, organized into logical folders,
deduplicated, with rich metadata in Postgres — plus a human-in-the-loop approval workflow so nothing
moves without review.

## Development Setup

- **Python**: 3.14 exclusively
- **Package manager**: `uv` only — never bare `pip`, `python`, `pytest`, or `mypy`. Always `uv run`.
- **Pre-commit**: installed and active. All hooks pass before commits.

### Key Commands

```bash
uv sync                    # Install dependencies
uv run pytest              # Run tests
uv run pytest tests/test_foo.py::test_bar  # Run a single test
uv run ruff check .        # Lint
uv run ruff format .       # Format
uv run mypy .              # Type check
uv run pre-commit run --all-files

just test                  # Fast LOCAL ITERATION only: -x -q. Not a gate
just check-fast            # THE per-bead gate: lint + typecheck + the tests repowise says the
                           # change touches, escalating to the full suite when it can't tell
just check                 # lint + typecheck + full suite WITH coverage (95% LINE floor)
just check-all             # THE molecule gate: every pre-commit hook + the full suite
just branch-check          # Per-bead BRANCH-coverage gate. Free after any `check`
just test-db               # Bring up the shared test Postgres (5433) + Redis (6380) harness
just test-db-for <name>    # Carve an isolated seat — REQUIRED for concurrent worktrees
```

> Bare `uv run pytest` needs the harness up (`just test-db`); without it the integration tests skip,
> so a green run means less than it looks like. Check the database line in the pytest header.

### Which commands are gates, and which only look like one

**The boundary tables, the measured evidence behind every row, the regeneration commands, and the
bh-version archaeology live in [`docs/gates-and-isolation.md`](docs/gates-and-isolation.md).** Read
it before citing any command as evidence in a bead. The operational summary:

| Command | ruff | mypy | tests | coverage | Where it runs |
|---|---|---|---|---|---|
| `just test` | — | — | `-x -q`, stops at first failure | no | local iteration — **not a gate** |
| `just check-fast` | yes | yes | change-selected subset, escalating to the full suite | only on an escalated run | **the per-bead gate** — `bh work check` / `submit` / `merge` / `merge-main` |
| `just check` | yes | yes | full suite | 95% line floor | `postland`, `union`, and the manual escape hatch |
| `just check-all` | via pre-commit | via pre-commit | full suite | 95% line floor | **the molecule gate** — `bh work finish` |
| `just branch-check` | — | — | — | per-bead branch coverage | free after any `check` |

**No boundary an ad-hoc bead traverses runs the full suite.** `check`, `submit`, `merge` and
`merge-main` all resolve to `just check-fast`; an ad-hoc bead (parent = NONE) never reaches
`molecule` / `postland` / `union`. So post-push CI is often the **first** complete run of the suite
such a bead ever gets — treat a red post-merge CI run as a fix-forward P0, not a routine failure.

`just test` is deliberately retained and deliberately not a gate: `-x` gives a tight edit/run loop
and `-q` gives dot-density, and both cost evidence (a red `-x` run characterises only the prefix
before the first failure; `-q` suppresses the pytest header). **Do not "optimise" `-x` into `just
check` or `check-all`** — those exist to characterise the whole suite, and a truncated prefix tells
you nothing about the 7,000 tests after the first failure.

Two gate boundaries are configured **outside this repo**, in `~/.beadhive/config.yaml`, which is
shared by every hive on the machine and whose global default runs **no tests at all**. A dropped
override key does not error — the boundary silently inherits a weaker command and still prints
green. `tests/shared/test_validation_gate_recipes.py` guards the justfile half; nothing in this repo
can guard the config half.

#### Reading a gate result — five rules, each bought with an incident

1. **A gate is green only if its OWN pytest summary line says so.** Never a wrapper's exit code,
   never a background-task "completed", never the absence of visible errors. Read the pytest summary
   **and** the coverage line, and confirm the pytest header names **your own** seat's database.
2. **A gate's verdict is a property of a RUN, never of a COMMAND.** `bh work submit` consults a
   verdict ledger keyed on `(tree hash, validate-cmd hash)` and on a hit skips the checkout entirely,
   printing `validation verdict reused (…)`. That submit ran no tests. A replay prints **no** pytest
   summary and **no** coverage line — if you cannot see those lines, you did not measure anything,
   whatever the command was called.
3. **A count is a property of a RUN, never of a SUITE.** `just check-fast` prints ~417 passed on a
   selected run and ~8,000 on an escalated one — both green, and the selected run leaves no
   `deselected` and no trace whatever in its summary line. Cite a count together with the recipe that
   produced it, and never compare one across runs. (The suite itself also grows: two green full-suite
   runs in this repo's records differ by 11 tests.)
4. **A RED with no pytest summary is UNMEASURED, not failing.** An exit 1 whose transcript says
   `🎯 selector: fail` and `no verdict was produced` is a harness problem to escalate, not a
   regression to hunt — even though the last line printed reads `main is RED in combination`.
5. **A gate measures a TREE, and the base may move under it.** Before spending a slot:
   `git fetch origin && git rev-list --left-right --count HEAD...origin/main`.

#### Capturing a gate's status

A shell **pipeline**'s status is its last command's, so `| tail`, `| head`, `| grep`, `| jq` and even
`| cat` all report 0 over a command that died. `set -o pipefail` fixes that in both bash and zsh.
**Do not reach for `${PIPESTATUS[0]}`** — this repo's shell is zsh, where it is not an error but
silently expands to the **empty string**, which reads as benign. `grep` is sharper still: it inverts
as well as masks.

`pipefail` governs `a | b` and does **nothing** for a sequential list `a; b`, whose status is simply
its last command's — a trailing `tail` to trim output is the idiomatic thing to write and has
already reported success over a check that exited 1 and ran zero tests.

**Redirection order** is the same family and fails more quietly: `cmd > file 2>&1` captures stderr;
`cmd 2>&1 > file` re-points stdout *after* duplicating it, leaving stderr on the terminal. For a
command that writes to stderr, the wrong order leaves the file **empty, with exit 0** — invisible in
a transcript.

What survives all three mechanisms is **reading the log body**. Append the tell **before** any
trimming:

```bash
just check > gate.log 2>&1; GATE_EXIT=$?; echo "GATE_EXIT=$GATE_EXIT" >> gate.log
```

| what the log holds | verdict |
|---|---|
| `GATE_EXIT=0` + pytest summary + coverage line | green |
| `GATE_EXIT=1` + pytest summary | genuinely red — a verdict |
| **no `GATE_EXIT` line at all**, log truncated | **KILLED. Not a verdict.** |

The third row is what earns the technique: SIGTERM kills the shell before the appended `echo`, so a
killed gate cannot forge it. **It also makes any enclosing wrapper's status permanently meaningless**
— the `echo` always succeeds, so the wrapper reports 0 by construction. Append it **and** read the
log; the two halves are one instruction.

Name gate logs `*.log`. `.gitignore` carries `*.log`, so the file stays invisible to `git status`;
any other name leaves the worktree dirty and `check-fast`'s selector refuses to run anything at all.

#### Before launching a gate: check headroom, not a gate count

```bash
sysctl vm.swapusage                                                 # hard NO-GO if "used" is non-zero
memory_pressure | tail -1                                           # the headroom read
ps -eo args= | grep -cE '^[^ ]*/\.venv/bin/python[0-9.]* .*pytest'  # context, not a verdict
```

Swap is **necessary, not sufficient**: it answers *"is this machine thrashing now"*, while a launch
decision needs *"will it thrash if I add ~1.3 GB"*. Non-zero swap is a hard no-go; `0.00M` establishes
only that nothing is thrashing *now*. There is deliberately **no calibrated threshold** on the
headroom read — none has been derived, and inventing one would be exactly the unmeasured arithmetic
this rule replaced. **Record the swap reading and the wait into the gate log** so the next kill either
confirms or refutes the discriminator.

A **gate count is context, never a verdict** — it may make you more conservative, never less, and is
**never** a reason to kill a running gate. Per-gate cost is unstable in three independent ways (the
suite grows, RSS grows ~20% within a single run, and a count is blind to everything else on the
machine), and the process-matching command miscounts in both directions — including missing a gate
entirely for its first 30–60 s while ruff and mypy run. **Do not use `vm_stat`'s `Pages free`** as a
headroom read: on Darwin a low value is the healthy steady state (measured: 0.12 GB free against
9.87 GB inactive, i.e. ~10 GB actually available).

Waiting is cheap — one seat lost ~1,190 s to a kill, then waited 390 s and ran clean in 19:52:

```bash
waited=0
while [ "$(ps -eo args= | grep -cE '^[^ ]*/\.venv/bin/python[0-9.]* .*pytest')" -gt 1 ] \
      && [ "$waited" -lt 3600 ]; do sleep 30; waited=$((waited+30)); done
echo "WAITED_SEC=$waited SWAP=$(sysctl -n vm.swapusage)"   # into the gate log, before the gate
```

### Test databases and seat isolation

`TEST_DATABASE_URL` is validated by a single guard in `tests/db_guard.py`. Two enforced rules:

- **The database name must contain a `test` segment** — `phaze_test`, `phaze_test_<bead>` and
  `phaze_<bead>_test` pass; `phaze` and `phaze_prod` do not. A failing name **errors the run**. It
  does not skip: a skip would silently drop ~18 integration tests while pytest still reported green.
- **Port 5433, never 5432.** 5433 is the ephemeral harness; 5432 is the developer's own database, and
  the fixtures create and drop schema — a default pointing there is a live-data-loss shape.

Unset defaults to `postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test`. Every run prints its
resolved target in the pytest header — **check it before trusting a green run**. `exclusive` means
this process holds the session lock; `unlocked (…)` has three causes (Postgres unreachable at session
start, `PHAZE_TEST_DB_ALLOW_SHARED=1`, or `--collect-only`), and in the first two the run is not
protected and its failures are not trustworthy under any concurrency.

**Never share Postgres OR Redis between concurrent seats.** Both are stateful, both are shared by
default. Saying only "test database" was a real defect: it taught agents to isolate Postgres and left
every seat on the same logical Redis, where two test modules run a global `scan_iter`+`delete` sweep
over `exec:*`, `exec_progress_req:*` and `execdispatch:*` in fixture setup *and* teardown. One seat's
fixture then deletes another's live keys mid-test — a failure indistinguishable from a real
regression that passes on isolated re-run, which is the worst possible shape because it trains
reviewers to dismiss red runs.

```bash
just test-db-for <name>    # creates phaze_<derived>_test + phaze_<derived>_migrations_test,
                           # allocates a dedicated Redis logical DB, prints all three exports
```

`<name>` is **not** used verbatim: it is normalized (hyphens → underscores) with a short hash of the
original appended, so `my-seat` and `my_seat` cannot silently collide onto one seat. **Always copy
the exports the recipe prints** rather than hand-constructing a DSN — the two agree only when
`<name>` has no hyphens. Redis indices come from an atomic registry (DB 0 holds it, seats get 1
upward, 64 available), so re-running for the same worktree is idempotent and allocation past the cap
fails loudly rather than wrapping onto a shared index. Leaving `PHAZE_REDIS_URL` unset is still valid
for single-agent and CI runs.

`just check` / `check-fast` **self-provision** an `auto_<branch>_<hash>` seat when
`TEST_DATABASE_URL` is unset, so a solo worktree is genuinely isolated rather than landing on shared
`phaze_test`. That is a floor, not a substitute: export your own seat anyway — a derived name is
opaque in `just test-db-seats`, and a seat you did not name is one you will not think to release. An
exported `TEST_DATABASE_URL` is honoured **verbatim** and provisions nothing, which CI depends on.

**One database, one pytest process.** `TEST_DATABASE_URL` isolates a *worktree*; it never isolated a
*process*. `tests/conftest.py`'s session-scoped engine runs `create_all` at session start and
`drop_all` at teardown, so whichever process finishes first drops the schema out from under the other
(measured: 238 failed + 12 errors, all `UndefinedTableError`, all green on isolated re-run). A
session-level Postgres advisory lock now refuses the second process **before collection**, naming the
holder's pid. The most natural way to hit this is re-running a subset "to check something" in a second
terminal while the full suite is going. `PHAZE_TEST_DB_ALLOW_SHARED=1` bypasses it; pytest-xdist
against one database *is* this defect and is not a reason to set it.

**Scope every `pg_locks` / `pg_stat_activity` query with `current_database()`.** A per-worktree
database isolates table data completely and the system catalogues not at all, so any such query sees
every seat's backends — two correctly-isolated suites both went red on an advisory-lock count that
had picked up the other seat's copy of the same key. `SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT
granted)` is the nastier version: satisfied by any blocked backend in the cluster, so the barrier
returns before the test's own waiter has queued. `tests/db_guard.BLOCKED_WAITER_SQL` is the shared
correct form; `tests/shared/test_cluster_wide_catalog_scoping.py` fails the build on an unscoped
query.

**Give a seat back when a worktree is done — and never tear the harness down to free one.**

```bash
just test-db-seats                # who holds which logical DB, and the evidence per verdict
just test-db-release <name>       # hand ONE finished seat's index back
just test-db-reclaim [--apply]    # dry run / free every seat no longer in use
```

A seat is protected while **any** of three signals holds: a Redis client is connected (L1); a Postgres
backend is on its databases (L2 — pytest holds one all session and often holds no Redis connection, so
this is what protects an idle-looking suite, and it is **mandatory**: if Postgres is unreachable the
sweep *refuses* rather than reading unknown as free); or its lease is unexpired (L3, default 72 h).
Two conditions override the lease but **never** L1/L2: its origin worktree is gone (the normal end of
`bh work merge`, so this is the common case), or its index is past the container's DB count. A freed
index is not first in line for the next seat — a shell with a stale `PHAZE_REDIS_URL` still exported
is the one hazard no liveness check can see. Full rule set: `scripts/redis-seat-registry.sh`.

**`just test-db-down` refuses while any client is connected to a `phaze%test` database**, listing the
seats it is protecting; `PHAZE_TEST_DB_FORCE_DOWN=1` overrides for genuinely stale connections. On
2026-07-29 a down + recreate mid-round destroyed 89 per-worktree databases and reset the Redis
registry with five suites in flight. **If you got here because Redis indices ran out, this is the
wrong tool** — use `just test-db-reclaim`, which touches neither container.

**Every writable path shared by concurrent seats is a collision surface.** Postgres was the first
instance and Redis the second, so carry the general form rather than a list: **anything a concurrent
seat can open for writing must carry the bead id in its path** — databases, cache namespaces, log
files, transcripts, scratch directories, report files. The surface is "writable and shared", not
"stateful service".

**Evidence-bearing paths are the costly ones, because they fail silently.** A clobbered database
raises; a clobbered gate log yields a *confident wrong citation* — a pass/fail count, a coverage
figure or a database header belonging to some other run. **The scratchpad is per-SESSION, not
per-seat**, and its "session-specific" label is the whole trap: it isolates you from other *sessions*,
never from your sibling *seats*, so every sub-agent of one dispatch gets the identical path. Measured
on a six-way dispatch, three seats redirected `just check` into the same `check.log`, `>` truncated at
open, and one seat read another's database name out of it and killed a healthy gate — right reasoning,
wrong evidence, ~20 minutes lost. Write scratch under `<scratchpad>/<bead-id>/`, or simply inside the
bead's own worktree, which is per-bead by construction.

> **Operator decision 2026-08-22.** Question as put: *"the shared-scratchpad-as-'session-specific'
> trap will recur on every future fan-out, and it produced a wasted gate run and two near-misses in
> one wave. The durable fix is either a per-seat scratchpad convention or a line in CLAUDE.md's
> dispatch guidance. Want me to file that as a bead?"* Answer as given, verbatim: *"yes, always use
> per-bead scratchpads."* Durable record: bead `phaze-rlshw`. The operator said **per-bead**, not
> per-seat as the question offered — the bead id outlives a seat being reassigned or resumed.

## Code Quality

### Ruff

Line length **150**. Lint `target-version` is **`py313`** — intentionally one minor behind the 3.14
runtime. Python 3.14's PEP 649 deferred annotations make ruff's `TC`/`UP037` rewrites want to move
type-only imports into `TYPE_CHECKING` blocks and unquote annotations, which breaks
Pydantic/SQLAlchemy/FastAPI (they resolve annotations at runtime via `get_type_hints`). Keep `py313`
until those rewrites are safe.

- **Enabled**: `ARG`, `B`, `C4`, `E`, `F`, `I`, `PLC`, `PTH`, `RUF`, `S`, `SIM`, `T20`, `TCH`, `UP`, `W`, `W191`
- **Ignored**: `B008`, `C901`, `E501`, `S101`
- **Per-file**: `__init__.py` → `F401`; `T201` (print) allowed in `scripts/parity/**`,
  `src/phaze/cli/**`, `src/phaze/main.py` and tests; `services/**` → `S603`/`S607`; `tests/**` also
  → `PLC`, `S105`, `ARG001`
- **isort**: `lines-after-imports = 2`, `combine-as-imports`, `split-on-trailing-comma`,
  `force-sort-within-sections`; `known-first-party` = the project package
- **Format**: double quotes, space indent, `docstring-code-format = false`

### Mypy

Strict — `disallow_untyped_defs`, `disallow_incomplete_defs`, `check_untyped_defs`,
`disallow_untyped_decorators`, `no_implicit_optional`, `warn_return_any`, `warn_redundant_casts`,
`warn_unused_ignores`, `warn_no_return`, `warn_unreachable`, `strict_equality`,
`warn_unused_configs` — with `python_version = "3.14"` and `mypy_path = ["src"]`. **Tests are
excluded entirely** (`exclude`), not run under a relaxed override, along with `prototype/`,
`services/` and `vulture_whitelist.py`. `[tool.mypy]` in `pyproject.toml` is authoritative.

### Pre-commit

**Frozen SHAs, not tags,** for every hook. pre-commit-hooks (large files, executable shebangs, merge
conflicts, TOML, YAML, JSON check + pretty-format, AWS credentials, private keys, EOF fixer, trailing
whitespace, mixed line endings) · ruff `--fix` + ruff-format · bandit (`-x tests,services -s B608`) ·
check-jsonschema (GitHub workflows/actions) · hadolint · actionlint · yamllint strict · shellcheck-py
(`--shell=bash --severity=warning`) · shfmt (`--indent=2 --case-indent --language-dialect=bash
--write`) · a local `uv run mypy .` hook with `pass_filenames: false`.

## Testing

- **95% LINE coverage repo-wide, plus a 90% per-module line floor** — both enforced by
  `scripts/coverage_floor.py`, which `just test-cov` and `just coverage-combine` run.
- Coverage uploads to Codecov with service-specific flags. Codecov config: precision 2, round down,
  range 70–100%, project target auto with 1% threshold, patch target 80% with 5% threshold.

### Branch coverage: measured everywhere, gated per bead

> **Operator decision 2026-08-21.** Question as put: *"Branch coverage is off, and runs 4-8 points
> under line coverage on the refactor targets. What should this epic do about it?"* Answer as given
> (selected option label, verbatim): *"Enable it, gate the refactor targets only (Recommended)"*.
> Durable record: bead `phaze-bk9el.21`.

`branch = true` in `[tool.coverage.run]`, so **every** coverage run measures branches and the number
is visible everywhere. The **gate** is deliberately narrow:

- **Repo-wide, the floors stay on LINES** — 95% total, 90% per module. Branch coverage sits below the
  line figure on most files here, so a repo-wide branch floor would fail on day one and the backfill
  would dwarf the work it was meant to protect. **Do not raise `fail_under` against branches.**
- **Per bead, `just branch-check`** reads the `coverage.json` any gate run leaves behind, checks only
  the `src/phaze/**.py` files that bead changed against `--base-ref` (committed, staged *and*
  unstaged, so it is useful mid-flight), names every file it checked, and prints the **uncovered
  branch line numbers** rather than a bare percentage. Raising is welcome, holding steady is fine,
  **lowering fails the bead**. Files the bead did not touch are out of scope.
- **It fails closed on a missing baseline.** Only `phaze-bk9el.1` may pass
  `--allow-missing-baseline` — it cannot be blocked by a check consuming the artifact it exists to
  produce. **No other bead should pass that flag**; seeing it is a signal something is wrong. An
  exemption at the call site is auditable; a lenient default is invisible to every bead downstream.

Per-bead is where the value is: decomposing a function, flattening a nest or splitting a file are all
operations where **every line still executes** and only the branch combinations change. Line coverage
is structurally unable to see that regression, and a repo-wide average is far too coarse to.

**The trap if you ever change a coverage floor.** With `branch = true`, coverage.py's own
`fail_under` measures the **combined** `(covered_lines + covered_branches) / (statements + branches)`
and offers no option to select the metric. Enabling branch measurement therefore silently re-points
every floor left on that knob. That is why both repo-wide floors read `percent_statements_covered`
explicitly in `scripts/coverage_floor.py`; why the two artifact **writers** (`coverage json`,
`coverage xml`) carry `--fail-under=0` in `coverage-combine` while `coverage report --fail-under=95`,
last and on purpose, is where the run actually fails; and why every line the gate prints names the
metric it measured. A writer that fails its own floor still **writes its file first, then exits** —
which once aborted the run at `coverage xml` so `coverage.json`, the file `branch-check` reads, was
never written at all. `tests/shared/test_coverage_gate.py` pins the report floor to `pyproject.toml`
so the two must move together.

## Workflow: Features and PRs

- **Every feature gets its own git worktree** — no cross-contamination between features.
- **Code changes may go straight to main.** A bead taken through the beadhive lifecycle — validated,
  resolved at its review gate, merged `--no-ff` — may push `main` to origin directly. No PR is
  required for a code bead, and none should be opened for one.
- **Docs changes require a PR.** Anything under `docs/**`, any root-level `*.md` (`CLAUDE.md` and
  `README.md` included), and any other prose-only change gets its own branch and a PR for human
  review. Prose is where decisions and rationale are recorded, and a wrong line reads as authoritative
  to every future agent for as long as it stands — that is what the review is for, and CI cannot
  supply it. **A change touching both is a docs change.**
- **One PR per feature**, wherever a PR is opened at all.
- **`bh work merge` / `bh work finish` are LOCAL — they never push.** `✓ merged … and closed it`
  describes your clone only. **Verify a landing against the REMOTE**: `git fetch origin && git
  merge-base --is-ancestor <sha> origin/main`. Never against local `main` — the merge just wrote that
  ref, so the check cannot fail, and a verification that cannot fail is not one.
- **On a direct push, CI runs after the fact — nothing gates `main`.** Since the per-bead gate became
  change-selected, `merge-main` no longer runs the full suite either, so post-push CI is often the
  first complete run of the suite a bead ever gets. **Treat a red post-merge CI run as a fix-forward
  P0, not a routine failure to triage later.**

### The three topology questions

One tool, three directions. Each has a green that is **produced by** the condition it was meant to
catch, so run all of them rather than trusting any one.

```bash
git fetch origin
git rev-list --left-right --count main...origin/main   # BASE SKEW — before provisioning a worktree
git log --oneline origin/main..HEAD                    # CONTAMINATION — do I carry others' commits?
git log --oneline HEAD..origin/main                    # STALENESS — what landed since I branched?
git diff --stat origin/main...HEAD                     # the AUTHORITATIVE diff
```

- **Base skew, before any `bh work claim`.** Zero/zero is the only safe state: claim reports
  `start_point: "main"`, the **local** ref, so every worktree cut while local `main` is ahead inherits
  those commits and the PR renders foreign files. Fix with `git rebase --onto origin/main <old-base>`.
  **Pushing `main` does not always clear it** — content that reached origin by a different route has a
  different sha and no shared ancestor. *Content equality does not imply a shared ancestor.*
- **Staleness, before you SUBMIT and again before the seat that MERGES lands it.** The contamination
  check is **clean precisely when you are behind**, so it reports healthy in exactly the state that
  produces a red merge. Before submit is the cheap moment — the rebase *replaces* the gate you were
  about to spend rather than adding to it. Before merge is the load-bearing one: `bh work merge`
  writes the merge commit **before** validating, and then declines to roll it back. The merge-side
  check belongs to whoever runs the merge, and a non-empty result there means **bounce the bead
  before spending the merge validation**, not after.
- **Non-empty → read the DIFF, do not count the commits.** How far behind you are is not the question;
  whether what landed touches what you touch is. Rebase-looping against a main moving at ~2.7
  commits/h never converges.
- **Intersecting → rebase, re-run the guards it implicates, and state both in the submit report.** A
  rebase reported alongside what landed and what was re-run is the documented remedy; a *silent* one
  is what "do not rebase unbidden" forbids. **Escalate instead of rebasing** when the rebase is not
  mechanical — conflicts, or a landing that invalidates the bead's premise.
- **Two shapes make a disjoint file list the WRONG answer**: a landed change to a **shared wire
  contract or schema your tests construct** (two beads sharing no file still went red on a renamed
  field), and a landed **whole-tree guard** under `tests/shared/`, which intersects every diff by
  construction — 78 of 585 test files read a tracked repo file and assert on its content.
- **Never `git reset --soft origin/main`.** `--soft` keeps its promise about the working tree but
  moves HEAD **forward** onto a ref that has advanced, staging every intervening commit as its
  **inverse** — measured, 105 commits of other seats' work staged as a revert. This is worse than
  foreign additions, which announce themselves: a `D` line is indistinguishable from a deliberate
  removal. **Both documented checks go green on it.** Reset to a commit you own —
  `git reset --soft HEAD~<n>` or `git reset --soft "$(git merge-base origin/main HEAD)"` — and read
  `git diff --cached --stat` before committing a squash.
- **On a red gate, establish provenance BEFORE debugging.** If the failing file is not in
  `git diff --stat origin/main...HEAD`, the red is probably not yours; check `git log --oneline -5
  origin/main` for what landed while the gate ran.

*The general form:* ask of any check **"what does its GREEN look like while the thing I am worried
about is happening?"** If the answer is "exactly the same as always", it is answering a different
question from the one you are asking it.

Incidents, measured costs, and why no mechanical guard is practical:
[`docs/git-topology-and-verification.md`](docs/git-topology-and-verification.md).

## CI (GitHub Actions)

Follows the discogsography pattern: reusable `workflow_call` workflows with separate jobs for **code
quality** (all pre-commit hooks), **tests** (pytest with coverage, uploaded to Codecov with flags and
`disable_search: true`) and **security** (pip-audit, bandit, osv-scanner, Semgrep, TruffleHog secret
scanning, Trivy container scanning). Concurrency groups with `cancel-in-progress` on PR workflows.
Emoji prefixes on all step names.

## Code Style

150-character lines · type hints on all functions · double quotes · PEP 8 · `pyproject.toml` section
order `[build-system]` → `[project]` → `[project.scripts]` → `[tool.*]` → `[dependency-groups]`, with
alphabetically sorted dependencies.

## Project constraints

- **Language**: Python 3.14 exclusively
- **Package manager**: uv only
- **Deployment**: Docker Compose on a home server, private network
- **Database**: PostgreSQL
- **Scale**: must handle large file counts efficiently — batch processing and parallelization
- **Existing code**: must integrate with the provided analysis prototypes and respect their per-file
  interface
- **Naming format**: AI filename proposals — specific format TBD

## Technology Stack

`pyproject.toml` is authoritative for versions; this section is for the *reasons*.

**FastAPI** + **Jinja2 / HTMX 2 / Alpine.js / Tailwind 4** — server-rendered admin UI, no JS build
step (Tailwind compiles at image-build time from a pinned standalone binary; HTMX and Alpine from
CDN). **SQLAlchemy 2 async + asyncpg + Alembic** on **PostgreSQL 18**. **SAQ** (`saq[postgres]`) for
the task queue — the broker moved from Redis to Postgres in Phase 36, so **Redis 8** is now cache,
LLM rate limiting, execution progress and counters only. **mutagen** for tag read *and* write.
**essentia-tensorflow** for BPM/key/mood/style. **litellm** + **pydantic** for LLM proposals.
**pydantic-settings**, **uvicorn**, **Docker Compose 2**.

**The pins that are load-bearing, and why they cannot drift:**

| Pin | Why |
|---|---|
| **Python 3.14** (app) / **3.13** (arm64 agent) | essentia-tensorflow dev1438+ ships cp314 wheels only (macOS arm64/x86_64 + linux x86_64; no linux/arm64) — keep the `sys_platform != 'linux' or platform_machine == 'x86_64'` marker. No cp314 aarch64 TF wheel exists, so the agent image stays on 3.13: base suite, Python, TF and the pinned essentia commit are **one combination, not four independent knobs**. |
| **litellm `>=1.98.0,<1.99.0`** | Supply-chain attack on 1.82.7/1.82.8 (March 2026). Pin the exact minor line, raise the cap only after vetting, verify checksums. |
| **FFmpeg 7.1.x via the base image tag — deliberately NOT `ffmpeg=<version>`** | Debian locks the upstream MAJOR.MINOR line per release, so `trixie` **is** the pin; an explicit version would select *away* from security updates. 7.1 is also the line the essentia wheel statically links (amd64) and the arm64 agent compiles against (`libav*-dev`, same Debian source package as the CLI) — so base and analysis library move together or not at all. Reaching it required moving the agent base bookworm → trixie, not adding a pin. **CI still tests 8.1** — a settled operator decision, `docs/design/0013-ffmpeg-pin.md` §7. |
| **chromaprint (system)** | Retained permanently with **no known consumer**. It is *not* an essentia runtime dependency (`ldd` on the deployed `_essentia` extension shows no link; `import essentia` succeeds without it) and no phaze source calls `fpcalc`/`acoustid`. **Operator decision 2026-07-29: keep** — a runtime `dlopen` path was never exhaustively ruled out and the install cost is trivial. **Do not re-open as a cleanup task.** See `docs/design/0002-fingerprint-removal.md`. |
| **pyacoustid** | Never a dependency and never used. The fingerprinting feature it would have served was removed from the product on 2026-07-28 (epic `phaze-0jpe`). |

**Known gap:** the test harness runs `redis:7-alpine` while production runs `redis:8-alpine` —
version skew the suite does not cover.

**Do not introduce:** `ffmpeg-python` (abandoned since 2022 — shell out to `ffprobe`, or use
`python-ffmpeg`) · SQLite (no concurrent writes from parallel workers, no JSON operators) · Celery or
Dramatiq (SAQ is async-native and sufficient for a single-user app) · Django · LangChain (litellm +
Pydantic covers "send prompt, get structured response") · React/Next.js (would add a Node build
pipeline for a single-user admin UI) · tinytag (read-only; renaming needs tag writes) · psycopg2
(sync driver, blocks the event loop — asyncpg).

## Conventions

Full text: [`CONVENTIONS.md`](CONVENTIONS.md).

### No local identifiers in tracked files

phaze is developed against a real personal music archive on real hardware, and investigation output —
spike docs, planning notes, debug write-ups, benchmark records — is where traces of it accumulate,
because the honest way to report a measurement is to say what it was measured on. **Scrub as you
write, not afterwards.**

**Never commit:** filenames, directory names or absolute paths from the real archive (the category
that matters most — a release name in a log excerpt, a staging-mount path in a traceback); content
digests and file UUIDs taken from live data. **Acceptable:** invented example filenames that
illustrate a format, synthetic fixtures (`song.mp3`), host and account names in *local* instruction
material — committed source and published docs refer to hosts by role.

**Placeholders:** `<track-01>` · `<set-01>` · `<archive-mount>` · `<archive-mount-in-container>` ·
`<scratch>/…` · `fp_<hash-1>` · `<uuid-1>` · `host-prod` / `host-store`.

**Replace identifiers, never quantities.** This is the rule that breaks when scrubbing is rushed, and
it destroys the value of the document it was meant to protect. Every measured value stays exact.
Good: *"36 files totalling 42.34 h, stratified across the duration distribution"*. Bad: *"a few dozen
files"* — scrubbed, and now worthless as evidence. **If a scrub changes a number, it is a bug in the
scrub.**

**Scope:** any tracked file, plus commit messages and PR bodies, which are just as permanent.
**Scrubbing a file does not scrub git history** — prefer never committing the identifier over fixing
it later, since a history rewrite may not be viable at all.

### Cite ADRs by filename, never by bare number

Write `docs/design/0015-shared-session-gather.md`, not "ADR-0015". Where the prose reads better with
the number, keep the number **and** the disambiguator — "ADR-0015 (shared session gather)".

**A bare number is a pointer with no redundancy, so nothing can check it.** ADR numbers are
reassignable: renaming a file frees its number, and the next ADR to claim it silently inherits every
citation written against the old occupant — correctly formed, greppable, and now resolving to a
different, currently-valid document. Measured 2026-08-24: one renumber left **8 bare "ADR-0014"
citations** all pointing at the wrong document. It was caught exactly once, **by a human reading
prose** — no grep or link checker could have, because a bare number implies no path.

`tests/shared/test_adr_numbering.py` catches *duplicate* leading numbers and must not be read as
covering this: those numbers were never duplicated at any instant, 0014 was legally **reused** after
a rename freed it.

**When you renumber, sweep the number VACATED and the number newly OCCUPIED.** The second gets
missed, and that is a property of renumbers rather than anyone's lapse — at planning time the
newly-occupied number is not yet anybody's, so there is nothing to sweep for. Likewise **do not cite
a number before its file exists**: a forward citation is dangling when written and silently becomes
*wrong* once something else claims the number.

## Architecture

Not fully mapped — follow existing patterns found in the codebase. `.claude/CLAUDE.md` carries the
Repowise index, module map and tool guidance.

### Analysis is exhaustive, chunk-bounded, and killed only by lack of progress

`analyze_file` analyzes **every natural fine and coarse window of every file**, whatever its length.
There is no window cap, no even stride, no `sampled` flag and no `deepen` path. Two invariants replace
what the caps used to provide, and both are easy to break by accident:

- **Live PCM is bounded by the CHUNK, and since D-09 so is process peak RSS.** Each tier decodes and
  analyzes 60 (fine) / 30 (coarse) windows at a time, holding live PCM at ~317 MB / ~345 MB regardless
  of duration. **Do not "simplify" either pass back into a single whole-tier decode** — on a 12-hour
  set that is ~7.3 GiB and a cgroup OOMKill. The process peak did not follow the PCM until D-09, and
  that gap was a P0: measured against real audio, peak grew **+0.31 GiB per fine chunk** (R² 0.99959),
  reaching **10.28 GiB at 12h04** against a deployed `memory_limit: 4Gi`, so every file past ~3 hours
  OOMKilled. Re-measured after the fix on the same node, image and files: **1.50 / 1.65 / 1.67 GiB**
  at 1:00 / 4:00 / 12:04. **Duration-linear growth is a bug, not a sizing input — do not raise the
  limit to paper over a regression.**
- **A chunk's streaming network must be DISCONNECTED, not just dropped** (D-09). This is the half the
  original chunking missed, and it cost a deploy: dropping the Python proxies leaves essentia's C++
  edges — and the `PoolStorage` a Pool sink creates, which Python never holds at all — allocated, so
  every **gated** chunk decode retained ~5 MiB per window branch. `gc.collect()` is required
  alongside the disconnect, because every streaming algorithm is born into a reference cycle and
  refcounting alone never destroys it; `malloc_trim` cannot help, because the pages are
  live-referenced rather than merely un-returned. Two tests hold the line and **neither can be
  satisfied by a mocked essentia** (the original long-file test was mocked, which is why this
  shipped): `test_repeated_gated_chunk_decodes_do_not_grow_peak_rss` and
  `test_the_chunk_decode_leaves_no_connected_network_behind`.
- **Nothing is killed for running long.** A multi-hour concert set is expected to take hours.
  Liveness is progress-based (`analysis_stall_timeout_sec`, default 1800 s of *silence*): the child
  heartbeats window completions, chunk decodes and model sweeps, and only total silence kills it. The
  SAQ `process_file` job runs `timeout=0` plus a SAQ `heartbeat`. **Never add a wall-clock bound on
  any lane** — `phaze-1b39` is the incident where one SIGTERM'd legitimate 2–6 hour analyses and
  stalled the whole burst lane.

Decision records live with the code: **D-07** (chunking) in `services/analysis.py`, **D-08** (stall
liveness) in `services/analysis_exec.py`. Rationale and cost analysis:
`docs/design/0007-windowed-analysis.md` (§7 the operator decision, §8 what shipped); operational view:
`docs/essentia-analysis.md`. Measurements: **2026-08-12**, `docs/spikes/phaze-b2qs9-exhaustive-analysis-measurement.md`
(before — confirms exhaustive coverage and end-to-end wall clock at **0.56–0.79× the file's own
duration**, and *refuted* the duration-independent-peak claim) and
**2026-08-13**, `docs/spikes/phaze-u1n7j-vox-fix-verification.md` (after — restores it, with the
mechanism, a byte-identical equivalence check, and §3 ruling out glibc arena fragmentation).

## Acceptance criteria, attribution, and verification fidelity

Five rules that bind every bead changing a production path. They exist because three production
incidents — `phaze-1b39`, `phaze-b2qs9`/`phaze-u1n7j` and `phaze-3ea41` — shipped through a green
suite by the same mechanism: a change is justified by an **argument** about equivalence or bounds;
the argument is verified against a **proxy that structurally cannot exhibit the failure**; green CI
is then read as confirming the argument rather than the proxy; and production is the first place the
real input class ever meets the code.

[ADR-0012](docs/design/0012-verification-fidelity-and-operator-attribution.md) argues each rule
against all three incidents with an explicit would-have-caught / would-**not**-have-caught verdict.
**Read the verdicts before applying a rule: none of the five catches all three**, and knowing which
one is doing the work on a given change is the point.

1. **An acceptance criterion is discharged by a test or by the operator — never by prose.** For every
   criterion on the bead, name the test that exercises it, or the recorded operator amendment that
   changed it. A criterion you reasoned about in a decision record is not met; ambiguity goes back to
   the operator as a question. **Narrowing a criterion is allowed and sometimes right** — as an
   operator action, recorded before submit, with the remainder filed as its own bead. Narrowing it
   *silently* is what broke `phaze-3ea41`: *"existing audio-file analysis is unchanged"* was replaced
   in prose with the narrower *"the audio stream is bit-identical"*, which was true, verified, and
   **about the wrong quantity** — the container had changed to Matroska, the deployed
   `es.MetadataReader` reads no duration from it, and zero duration produced zero windows for all
   11,428 files in the corpus.
2. **"Operator decision" is a citation, not an emphasis marker.** Any text claiming one carries **the
   question as it was put, the answer as it was given (quoted), the date, and a pointer to the durable
   record**. The durable record is a bead comment or an ADR section — a commit message and a PR body
   are neither, because both are written by the implementer at submit time and read by nobody
   afterwards. The attribution extends no further than the question asked: a second decision found
   inside the first is a **new question for the operator, not a corollary**, and symmetrically a
   decision may not be narrowed past the conditions attached to it. A claim failing any of the four is
   not deleted, it is **relabelled as the implementer's decision** — a perfectly good thing for a
   decision to be, and one that invites the review the operator label suppresses.
   - **Before writing one from memory, check `scripts/recover_operator_decisions.py`.** The
     question-as-put and answer-as-given are frequently *not* in anything a normal grep finds: an
     `AskUserQuestion` exchange is an assistant `tool_use` matched to a `tool_result`, so neither is a
     user turn and both are invisible to a grep of bead comments or user messages. **Quote only the
     selected option LABEL** — an option's *description* is the assistant's framing of the choice, not
     the operator's words, and that exact conflation has produced real citation defects here.
3. **Verify with the artifact's real consumer, not with the tool that produced it.** A change
   producing a new artifact names its real consumer, and the test calls that consumer; validating an
   artifact with the tool that produced it proves round-tripping, not compatibility. `phaze-3ea41`
   **did** ship real-`ffmpeg`, real-container tests — they asserted the extracted `.mka` was
   *"decodable by ffprobe"*, and ffprobe reads Matroska duration correctly. `es.MetadataReader`, the
   consumer that could not, was never handed the file. So: a claim about **real essentia** is not
   discharged by a mocked one; about a **container format**, not by probing with the muxer's own
   tooling; about **real multi-hour durations**, not by a short synthetic fixture; about **the
   archive's distribution**, by a query over `files.duration` — one query would have stopped
   `phaze-1b39`.
   - **An INDEPENDENT consumer is not automatically a DISCRIMINATING one.** The question is not *"is
     this a different implementation?"* but *"would this tool have **rejected** the wrong artifact?"*,
     answered by feeding it one. Measured: `ffprobe`, the obvious independent reader for a `.wma` tag
     write, reported `TAG:artist=…` for the **wrong** file too, while `es.MetadataReader` returned
     every field empty.
4. **A change to a working production path owes a blast-radius statement.** Three sentences in the
   bead or PR before submit, with the population **measured, not adjectival**: *"This changes the path
   for `<population>`. What currently works that this could break: `<X>`. The test that proves it
   still works: `<T>`."* "Some files" does not satisfy it; "all 11,428 files in the corpus" does. **If
   no test `T` exists, that is the finding — escalate rather than write a weaker sentence.**
   `phaze-3ea41` was scoped as "analyze video containers" and silently rewrote the analysis path for
   the whole archive.
5. **A lesson recorded at one site states its general form, or states why it has none.** When a fix's
   decision record or test docstring says *"X cannot be verified by Y"*, the merging seat either names
   the general form and where it is now written down, or says why the lesson is genuinely specific to
   that call site. One sentence, not optional. This exists because the most expensive component of the
   pattern was not a missing lesson but a **captured, correct, un-generalized** one.

## A belief that is true in a neighbouring system is a claim, not knowledge

The five rules above assume a check gets scheduled. This is the failure where none is, and it sits one
layer up from all of them.

**A belief carried in from a neighbouring system — another OS, another version of the same tool,
another project with the same tool in it — presents as something you already know rather than as
something you are claiming. Verification fires on claims, so nothing fires.** The defining property is
that a transferred model has **no referent**: nothing to dereference, nothing to 404, nothing to
notice going stale.

> **When a belief about a tool's behaviour is load-bearing for a decision, and you did not read it or
> run it IN THIS ENVIRONMENT, run it. Thirty seconds, every time.**

**"Load-bearing"** is what keeps this from being paralysis — it applies when the belief changes what
you *do* (a design conclusion, a gate you hold or release, a number you cite), not to every incidental
assumption. **"In this environment"** means the installed version, this OS, this checkout — not the
version documented upstream.

**There is a second form, and it is harder to see:** a **verified mechanism vouching for an unobserved
instance**. A seat confirmed a real mechanism, then asserted an instance it had never observed and
labelled it measured, while holding a gate slot on it — its own account: *"the mechanism felt like it
carried the instance with it."* **A mechanism you verified does not vouch for an instance you did not
observe.** Verify the mechanism *and* look at the case.

**The catalogue lives in [ADR-0016](docs/design/0016-transferred-model-verification.md) §8, not here**
— `CLAUDE.md` hosts the fixed-size half (the name, the property, the trigger) and the list that
accretes goes where it is read on demand. **Add new instances to the ADR.** Every entry there was
produced by a careful seat reasoning correctly from a sound model, every one was true somewhere else,
and every one was caught within minutes.

*Its sibling is "cite ADRs by filename", and the relationship is a strict ordering rather than a
repetition:* a bare number is a pointer with **no redundancy**, so a reader can still try to
dereference it and a link check (`tests/shared/test_adr_citation_resolution.py`) catches the dangling
case; a transferred model has **no pointer at all**, which is why its mitigation cannot be a checker
and has to be the condition above.

## Beadhive Workflow Enforcement

All work in this repo flows through beadhive. Do not make direct repo edits outside this workflow
unless the user explicitly asks to bypass it. The five rules above bind every step — they are what
the review gate in step 7 is checking, and they are not advisory.

1. **Every piece of work has a bead.** Larger work is an epic with children. File epics through the
   planner (`bh plan file`), never by hand — hand-rolled epics fail the molecule convention check.
2. **Exploring a new idea?** Use the planner: `/bh:plan <idea>`.
3. **When filing a new bead, ask clarifying questions** — scope, priority, acceptance — before writing
   the description.
4. **Before starting execution**, keep asking clarifying questions until the work is clear. An
   acceptance criterion that looks wrong or ambiguous is a question for the operator, never something
   to reinterpret in prose.
5. **Once work starts, the dispatching session occupies the dispatcher seat itself** — load the
   `bh:dispatcher` skill and drive the molecule from that session; do **not** spawn a `bh:dispatcher`
   sub-agent, which surrenders mid-flight visibility and leaves the session inferring state from git
   (misreading both uncommitted work and evidence-only spike beads). From that seat, dispatch developer
   sub-agents, each in its own worktree. **Never share a worktree, test database, Redis logical DB or
   any other writable path between concurrent agents.**
6. **Fix pushes get the adversarial treatment.** Before a `fixgroup:*` branch merges, run a
   diff-scoped adversarial verification pass over the fix diff — the same different-model,
   default-to-refuted verifier the hunt used, with the fix as the claim under refutation. Findings
   become new beads, never silent edits. Rationale, measurements, and what the pass does **not** catch:
   [ADR-0011](docs/design/0011-bug-hunt-cadence.md), which also sets the hunting cadence.
7. **When all children are done:** land the molecule (merge commit, never squash), then close the
   bead(s) with comments explaining the outcome. **A code molecule needs no PR** — `bh work finish
   <epic>` onto local main, then push. **A docs molecule does**: open a PR, invoke a code review, wait
   for green CI. If anything fails, investigate and fix — do not bypass.
8. **Periodically push the beads DB**: `bd dolt push`.

<!-- bv-agent-instructions-v3 -->
## Beads Workflow Integration

Work is tracked as **beads** in a local Dolt database under `.beads/` (git-ignored), synced to the
Dolt remote `origin`. Beads are driven through **beadhive** (`bh`); triage and graph analysis come
from **beads_viewer** (`bv`). See **Beadhive Workflow Enforcement** above for the process rules —
this section is the command reference for them.

> **Editing note.** This block sits between two `bv` marker comments. `bv` detects it by marker and
> version only, never by content, so the prose here is free to diverge from `bv`'s stock text — but
> **do not touch, reword or renumber the marker lines**, or `bv` starts prompting to add the section
> again at startup. Verify with `bv --agents-check` (expect `blurb v3 — up to date`, exit 0).
> `bv --agents-update` would overwrite everything below with generic boilerplate; don't run it.

### The toolchain

| Tool | Use it for |
|------|------------|
| `bh` | Everything lifecycle: reading, filing, claiming, validating, submitting, merging |
| `bv` | Triage and graph analysis — *what to work on*, never mutation |
| `bd` | Low-level beads CLI. Avoid; the one sanctioned use is `bd dolt push` |

The `bh bd` passthrough is disabled. Calling `bd` directly is possible but discouraged — a
`PreToolUse` hook warns that it is not hive-aware and can hit the wrong database.

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

- **Epics / molecules** → the planner (`/bh:plan <idea>` or `bh plan file`). Never hand-roll an epic:
  it fails the molecule convention check.
- **A single bead** → the `bh` MCP tool `bd_create`, which auto-applies the provider/org/repo triplet
  and validates labels.
- Ask clarifying questions on scope, priority and acceptance *before* writing the description.

### Driving a bead

```bash
bh work claim <id>       # provision the wt/bead/issue/<id> worktree + identity, → in_progress
bh work check <id>       # run the hive validation (just check-fast) against the worktree
bh work submit <id>      # verify clean conventional history, re-validate, open the review gate
bh work approve <id>     # resolve the review gate
bh work merge <id>       # serialize a --no-ff merge onto the integration branch, close the bead
bh work resume <id>      # re-attach after changes-requested
bh work abandon <id>     # release the claim; --rm also removes the worktree
```

`submit` rejects noisy history (more than `max_commits` over base, or non-conventional subjects) —
inspect with `bh work show <id>` and squash with `bh work refine <id>`. It publishes the branch only
when the review gate is `gh:run` / `gh:pr`; with the default in-process human gate it does not push,
so open the PR yourself. `merge` re-validates nothing when landing into a molecule under the default
`validation: relaxed`; landing on `main` **does** validate, via `merge-main`. **`merge` has no `--as`
flag** — passing one is a usage error (exit 2), not a no-op, even though `submit` requires it.

### Triage with bv

`bv` is a graph-aware triage engine with precomputed metrics (PageRank, betweenness, critical path,
cycles, HITS, eigenvector, k-core). **Use only `--robot-*` flags — bare `bv` launches an interactive
TUI that blocks the session.**

```bash
bv --robot-triage                 # THE MEGA-COMMAND: start here
bv --robot-next                   # the single top pick + claim command
bv --robot-triage --format toon   # token-optimized output
```

| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with unblocks lists |
| `--robot-priority` | Priority misalignment detection with confidence |
| `--robot-insights` | PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core |
| `--robot-alerts` | Stale issues, blocking cascades, priority mismatches |
| `--robot-suggest` | Hygiene: duplicates, missing deps, label suggestions, cycle breaks |
| `--robot-diff --diff-since <ref>` | Changes since ref |
| `--robot-graph [--graph-format=json\|dot\|mermaid]` | Dependency graph export |

Scope with `--label <l>`, `--as-of <ref>`, or `--recipe actionable|high-impact`.

Before claiming, confirm live state with `bh work issue <id>` or `bh work ready` — `bv` reads an
exported JSONL snapshot, so **`bh` is the authority**. `recommendations` can include work that is
blocked or already assigned; only `quick_ref.top_picks` and non-empty `claim_command` fields are
actually claimable.

### Key concepts

- **Priority**: P0=critical … P4=backlog. Use the numbers `0`–`4`, not words.
- **Types**: `bug`, `feature`, `task`, `epic`, `chore`, `decision` (aliases: `enhancement`/`feat` →
  feature, `dec`/`adr` → decision). Anything else is rejected unless registered under `types.custom`.
- **Worktrees**: one per bead, `wt/bead/issue/<id>`. Never shared — see the isolation rules above.
- **`review:*` on a CLOSED bead means nothing.** The label tracks review state while a bead is live
  and `bh`'s clear has holes (group/molecule merges have no clear call), so 174 of 2412 closed beads
  carry a stale `review:pending`. Nothing automated reads it after close. **Filter on status when you
  query it** — `bd list --label review:pending --status open`, never the bare form. Do **not**
  mass-edit the existing beads, and keep resolving a gate as SUPERSEDED available: forcing `approve`
  as the only tidy path would push seats toward false attestations. Tracked at beadhive/beadhive#15.
- **A repowise health number can be stale, and "trust the index" does not cover it.** That guidance is
  about **verified BYTES**; a freshly computed **METRIC** goes stale by different rules. `repowise
  update` folds health **incrementally, only over changed files**, stamping each row's
  `analyzed_commit` with HEAD-at-fold-time — so rows legitimately carry different commits and an older
  stamp does **not** mean stale. Ask per file whether the file changed after its stamp:
  ```bash
  git diff --quiet "$analyzed_commit" HEAD -- "$path" && echo current || echo STALE
  ```
  **Do not write `analyzed_commit != HEAD` and call it stale** — that is the upstream maintainer's
  named trap (repowise-dev/repowise#1864). There are **three** states: known, mixed (a response
  spanning several folds — see `health_analyzed_commits_distinct`), and unknown (`NULL`, or a commit
  git cannot resolve, which is *not* the same as current). Staleness is not confined to `get_health`:
  `get_risk` and `get_context(include=["health"])` embed the same fold rows and neither warns. To force
  a fresh read, `repowise health --file <path>`; **do not conclude "always use the CLI"** — that fixes
  nothing for bulk reads while leaving agents believing they have worked around it.
- **`bd label remove` reports success unconditionally** (gastownhall/beads#5988). It prints
  `✓ Removed label 'X' from Y` and exits 0 **even when the bead never had X**, so that line is evidence
  of neither presence nor removal. Read the label set back with `bd label list <id>` if it matters.

*The general form behind the last three:* **a record that reads as a status is not one.**

### Syncing the Dolt remote

The JSONL export under `.beads/` is maintained for you. Periodically push the database: `bd dolt push`.

### Git policy

`bh` owns the lifecycle around the change; it does not absolve you of this repo's git rules. Follow
the repository's own instructions before staging, committing or pushing — "commit only when asked"
overrides any generic workflow advice.
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
