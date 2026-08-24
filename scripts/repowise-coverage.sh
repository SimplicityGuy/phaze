#!/usr/bin/env bash
# Refresh the repowise index AND the coverage folded into its health scores, as ONE paired
# operation. Driven by `just repowise-coverage [seat]`; see that recipe's comment for the WHY.
#
# The five steps this encodes were measured by hand on 2026-08-18 (phaze-2rgq2). Two of them are
# not discoverable from the tools' own output, which is the entire reason this is a script and not
# a README paragraph:
#
#   1. `repowise update`                     reindex (structural; this repo indexes docs_enabled:false)
#   2. pytest --cov --cov-context=test       ~21 min, writes .coverage WITH per-test contexts
#   3. `repowise coverage add .coverage`     -> the test-to-code map ONLY
#   4. `coverage xml` + `coverage add ...`   -> per-file line coverage (REQUIRED, see gate)
#      then `coverage json`                  -> the report `just branch-check` reads (phaze-jktlb)
#   5. `repowise health`                     folds the coverage into the defect scores
#
# Step 3 alone is a SILENT PARTIAL SUCCESS: it prints "Built the test-to-code map: N test->file
# record(s)", `repowise coverage status` then shows a populated map, and yet line_coverage_pct
# stays null and the untested_hotspot biomarker keeps firing on 96-100%-covered files. So this
# script never exits 0 on that state -- `scripts/repowise_coverage_gate.py` re-reads repowise's own
# status at the end and fails closed on it, on any index/coverage commit skew, and on a report whose
# files did not all map into the index (which `repowise coverage add` reports as a note, not an
# error -- a 1-of-249 ingest otherwise looks entirely healthy).
#
# Usage: repowise-coverage.sh [seat-name]
#   with a seat name    provisions an isolated test DB + Redis index via `just test-db-for <seat>`
#                       and uses the exports that recipe prints, verbatim.
#   without one         requires TEST_DATABASE_URL, MIGRATIONS_TEST_DATABASE_URL and
#                       PHAZE_REDIS_URL to already be exported, and refuses if any is missing.
#                       Running the full suite against the shared default while another worktree
#                       is testing is the phaze-ieqg / phaze-fwo7 concurrency defect.
#
# Env overrides:
#   PHAZE_REPOWISE_ALLOW_DIRTY=1   downgrade the modified-tracked-files refusal to a warning.
set -euo pipefail

seat="${1:-}"

if ! command -v repowise >/dev/null 2>&1; then
  echo "❌ repowise is not on PATH — install it, or run this from a shell that has it." >&2
  exit 1
fi

# The index lives in .repowise/ in the DURABLE checkout and is gitignored, so a bh worktree
# (wt/bead/...) never has one. Running there is not a no-op: `repowise coverage status` creates an
# empty stub .repowise/wiki.db and then dies with a raw SQLAlchemy traceback about a missing table.
# Refuse early and say where to go instead.
if [ ! -d .repowise ]; then
  echo "❌ no .repowise/ index in $PWD." >&2
  echo "   This recipe refreshes an EXISTING index, so it must run in the durable checkout that" >&2
  echo "   holds one (a bh worktree does not). Run it there, or \`repowise init\` here first." >&2
  exit 1
fi

# --- commit pairing, part 1: the tree must not move under us -------------------------------------
# Health findings carry line numbers and coverage carries line numbers, so the two are only
# comparable when they describe the same tree. Modified TRACKED files break that directly: the
# lines pytest measured are not the lines the index recorded, and the sha stamped on the ingest
# names a commit that was never what ran. Untracked files cannot shift a line number in a tracked
# file, so they warn rather than refuse -- a durable checkout usually carries some.
tracked_dirt="$(git status --porcelain --untracked-files=no)"
if [ -n "$tracked_dirt" ]; then
  if [ "${PHAZE_REPOWISE_ALLOW_DIRTY:-}" = "1" ]; then
    echo "⚠️  WORKING TREE IS DIRTY and PHAZE_REPOWISE_ALLOW_DIRTY=1 is set — continuing anyway." >&2
    echo "⚠️  The commit sha stamped on this refresh will NOT describe what was measured:" >&2
    printf '%s\n' "$tracked_dirt" | sed 's/^/      /' >&2
  else
    echo "❌ refusing to run: tracked files are modified, so the index and the coverage would" >&2
    echo "   describe a tree that no commit names." >&2
    printf '%s\n' "$tracked_dirt" | sed 's/^/      /' >&2
    echo "   Commit or stash them, or set PHAZE_REPOWISE_ALLOW_DIRTY=1 to accept a mismatched run." >&2
    exit 1
  fi
fi

untracked="$(git ls-files --others --exclude-standard)"
if [ -n "$untracked" ]; then
  untracked_count="$(printf '%s\n' "$untracked" | wc -l | tr -d ' ')"
  echo "⚠️  ${untracked_count} untracked file(s) present; they are indexed but belong to no commit:" >&2
  printf '%s\n' "$untracked" | head -n 5 | sed 's/^/      /' >&2
fi

head_before="$(git rev-parse HEAD)"
# repowise records ingest times in UTC. The gate compares both ingests against this to prove THIS
# run produced them -- without it, a refresh whose ingests silently no-op'd is indistinguishable
# from one that worked, because the previous run's rows sit there looking perfectly healthy.
run_started_utc="$(date -u +"%Y-%m-%d %H:%M:%S")"

# --- test isolation ------------------------------------------------------------------------------
# CLAUDE.md: never share Postgres OR Redis between concurrent agents, and always copy the exports
# `test-db-for` prints rather than hand-building the DSN (the derived seat name is hashed, so a
# hand-built `phaze_<seat>_test` only agrees when <seat> has no hyphens).
if [ -n "$seat" ]; then
  echo "🔧 provisioning an isolated test seat for '${seat}'..."
  seat_output="$(just test-db-for "$seat")"
  printf '%s\n' "$seat_output"
  for var in TEST_DATABASE_URL MIGRATIONS_TEST_DATABASE_URL PHAZE_REDIS_URL; do
    value="$(printf '%s\n' "$seat_output" | sed -n "s/^[[:space:]]*export ${var}=\"\\(.*\\)\"\$/\\1/p" | head -n 1)"
    if [ -z "$value" ]; then
      echo "❌ \`just test-db-for ${seat}\` did not print an export for ${var}." >&2
      exit 1
    fi
    export "${var}=${value}"
  done
else
  missing=""
  for var in TEST_DATABASE_URL MIGRATIONS_TEST_DATABASE_URL PHAZE_REDIS_URL; do
    [ -n "${!var:-}" ] || missing="${missing} ${var}"
  done
  if [ -n "$missing" ]; then
    echo "❌ no seat name given and these are unset:${missing}" >&2
    echo "   Either pass a seat name (\`just repowise-coverage <name>\`, which runs test-db-for for" >&2
    echo "   you) or export the three values \`just test-db-for <name>\` prints. Running the full" >&2
    echo "   suite against the shared default database is a concurrency defect, not a shortcut." >&2
    exit 1
  fi
fi

tmpdir="$(mktemp -d)"
# Keep the raw status JSON when the run ends badly — it is the evidence for WHICH of the gate's
# conditions failed, and re-deriving it means another 21 minutes.
cleanup() {
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    rm -rf "$tmpdir"
  else
    echo "ℹ️  repowise status JSON kept for debugging in ${tmpdir}" >&2
  fi
}
trap cleanup EXIT

echo ""
echo "⏱️  This takes about 21 minutes (7043 tests, measured 2026-08-18). It runs the FULL suite;"
echo "    coverage contexts make it slower than \`just test\`. Nothing below is skippable."
echo "    HEAD ${head_before}"
echo "    DB   ${TEST_DATABASE_URL}"
echo "    Redis ${PHAZE_REDIS_URL}"
echo ""

# --- 1. reindex ----------------------------------------------------------------------------------
echo "1/5 🔁 repowise update (reindex at ${head_before})"
repowise update

# --- 2. the suite, WITH per-test contexts --------------------------------------------------------
# --cov-context=test is what builds the per-test map behind `get_risk`'s tests_to_run and the
# impacted-tests skill. Without it those come back empty, and empty there reads as "no tests" when
# it means "unknown" — the worst possible answer for a reviewer deciding what to re-run.
# --cov-fail-under=0 mirrors `just test-bucket`: the 95% floor is enforced by `just coverage-combine`
# and CI, not here. This recipe's job is to MEASURE and ingest; failing the ingest because coverage
# dipped would withhold the data exactly when it is most wanted.
# phaze-jktlb: flags are alphabetical here and in every justfile coverage recipe, `--cov` carries no
# `=phaze` because `source = ["phaze"]` in pyproject already says so, and `--cov-report=` stays --
# like `just test-bucket`, this run wants the binary .coverage and nothing else, and step 4 below
# writes the xml itself once the map is safely ingested. Its `-o coverage.xml` is gone for the same
# one-place reason: `[tool.coverage.xml] output` in pyproject now supplies that path.
echo ""
echo "2/5 🧪 pytest --cov --cov-context=test (~21 min)"
rm -f .coverage coverage.xml
if ! uv run pytest tests/ --cov --cov-context=test --cov-fail-under=0 --cov-report= -q; then
  echo "❌ the suite failed — NOTHING was ingested, so the previously ingested coverage is intact." >&2
  echo "   Fix the failures and re-run; ingesting a red run would fold wrong data into every score." >&2
  exit 1
fi

if [ ! -f .coverage ]; then
  echo "❌ pytest finished but no .coverage was written — cannot ingest." >&2
  exit 1
fi

# --- 3. ingest the per-test map ------------------------------------------------------------------
echo ""
echo "3/5 📥 repowise coverage add .coverage   (test-to-code map)"
if ! repowise coverage add .coverage; then
  echo "❌ ingesting the test-to-code map failed." >&2
  exit 1
fi

# --- 4. ingest per-file line coverage — NOT optional ---------------------------------------------
# This is the half that step 3 only LOOKS like it did. See the gate script's docstring.
echo ""
echo "4/5 📥 coverage xml + repowise coverage add coverage.xml   (per-file line coverage)"
if ! uv run coverage xml --fail-under=0; then
  echo "❌ \`coverage xml\` failed — per-file line coverage was NOT ingested." >&2
  echo "   The test-to-code map from step 3 IS in the database and \`repowise coverage status\`" >&2
  echo "   will happily show it. That state is the trap: line_coverage_pct stays null and every" >&2
  echo "   untested_hotspot finding stays wrong. Re-run once \`coverage xml\` works." >&2
  exit 1
fi
if ! repowise coverage add coverage.xml; then
  echo "❌ ingesting coverage.xml failed — per-file line coverage is missing while the" >&2
  echo "   test-to-code map is present. This is the silent-partial state; do not treat the" >&2
  echo "   populated \`repowise coverage status\` map as success." >&2
  exit 1
fi

# phaze-jktlb, AC4: this script runs the WHOLE suite, so "no consumer is coupled to one recipe"
# reads onto it exactly as it reads onto `just test-cov`. It already emitted coverage.xml above
# (for repowise); it emitted no coverage.json at all, so a developer who spent 21 minutes here and
# then ran `just branch-check` got a missing file, or worse a stale one from some earlier run.
#
# It is NOT covered by the operator decision of 2026-08-22 recorded on bead phaze-jktlb, which
# excluded `just test-bucket` from AC4 and stops there. That decision rests on a shard measuring a
# FRACTION of phaze; this run measures all of it, so the reasoning does not transfer, and extending
# it here would be stretching an attribution past the conditions attached to it. Fixed on its own
# merits instead, which is why no second narrowing was needed.
#
# Emitted AFTER the ingest, deliberately: the ingest is what this script exists for, and a report
# written for someone else's benefit must not be able to abort it. The destination comes from
# pyproject, same as everywhere else.
#
# DO NOT "SIMPLIFY" THIS INTO THE PYTEST STEP ABOVE. The obvious tidy-up is to delete these lines
# and pass the justfile's `cov_reports` through as a script argument, so that "which reports" is
# stated in exactly one place. It was considered and rejected (dispatcher ruling, 2026-08-22).
# A shell script cannot read a `just` variable, so the plumbing is real, and worse, it forces the
# pytest step to emit reports BEFORE the ingest -- inverting the measure -> ingest -> report order
# this script exists to encode and handing a report writer the power to abort the ingest.
#
# What is duplicated is only the "which reports" answer, and by a DIFFERENT mechanism (`coverage`
# subcommands here, `--cov-report` flags in the justfile). The destination is still single-sourced
# in pyproject, which is the half that silently rots when it is duplicated.
echo ""
echo "    📄 coverage json (for \`just branch-check\`; not used by repowise)"
if ! uv run coverage json --fail-under=0; then
  echo "⚠️  \`coverage json\` failed — the repowise ingest above SUCCEEDED and is intact, but" >&2
  echo "   coverage.json was not refreshed, so \`just branch-check\` has no report to read." >&2
  exit 1
fi

# --- 5. fold it into the health scores ------------------------------------------------------------
echo ""
echo "5/5 🩺 repowise health (fold coverage into the defect scores)"
repowise health

# --- commit pairing, part 2: verify, do not assume -----------------------------------------------
echo ""
head_after="$(git rev-parse HEAD)"
if [ "$head_after" != "$head_before" ]; then
  echo "❌ HEAD MOVED DURING THE RUN: ${head_before} -> ${head_after}" >&2
  echo "   The index, the coverage and the tree now disagree, and findings carry line numbers." >&2
  echo "   The ingests DID land — re-run this recipe on a settled HEAD to correct them." >&2
  exit 1
fi

repowise coverage status --format json >"${tmpdir}/coverage-status.json"
repowise status --format json >"${tmpdir}/index-status.json"
uv run python scripts/repowise_coverage_gate.py \
  --coverage-status "${tmpdir}/coverage-status.json" \
  --index-status "${tmpdir}/index-status.json" \
  --coverage-xml coverage.xml \
  --started-at "$run_started_utc" \
  --expected-commit "$head_before"
