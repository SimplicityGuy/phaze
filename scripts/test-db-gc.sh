#!/usr/bin/env bash
# Reap the STOCK of `phaze%test%` databases that have no registry seat, no Postgres backend, and
# are past an age floor (phaze-robzi.2 -- the sibling of phaze-robzi.1's FLOW fix in
# scripts/redis-seat-registry.sh).
#
# THE PROBLEM THIS CLOSES
#
# `test-db-reclaim --apply` (phaze-robzi.1) now drops a freed seat's own two databases in the same
# operation that frees its Redis index, going forward. It cannot reach databases ALREADY orphaned
# before that fix existed: once a seat was reclaimed under the OLD contract, its registry entry --
# the only thing that ever named `phaze_<seat>_test` / `phaze_<seat>_migrations_test` -- was gone,
# and nothing pointed at those two databases again. That is how 652 such databases (6974 MB) were
# measured on the shared harness on 2026-08-25, with the only tool that could remove them being a
# full `test-db-down` container teardown -- the exact instrument CLAUDE.md records destroying 89
# per-worktree databases and the Redis seat registry mid-round on 2026-07-29.
#
# THREE INDEPENDENT SIGNALS, all required before a drop (operator decision 2026-08-25, quoted
# verbatim: "Unregistered + no connections + age floor (Recommended)" -- the "(Recommended)" suffix
# is the assistant's framing, not the operator's words; durable record: bead phaze-robzi.2):
#
#   1. UNREGISTERED -- no seat currently in `phaze:test:redis-db-index` (the Redis registry behind
#      `redis-seat-registry.sh`) names this database.
#   2. NO BACKENDS -- zero Postgres client backends connected to it right now.
#   3. PAST AN AGE FLOOR -- default 24h, overridable with `--age-floor-hours`. Covers the race the
#      first two signals structurally cannot: a seat mid-provision that has already run `CREATE
#      DATABASE` for its pair but has not yet reached the Redis `allocate` call that registers it.
#
# WHY THIS MATCHES SEAT NAMES FORWARD, NEVER PARSES THEM BACKWARD
#
# The obvious design reads a database name like `phaze_<x>_test`, strips the fixed prefix/suffix,
# and asks whether `<x>` is a live seat. That is fragile in exactly the way
# `scripts/derive-seat-name.sh` warns about: its own budgeted truncation caps the identifier well
# below the NAMEDATALEN-1 (63 byte) limit for the *current*, truncation-safe implementation, but the
# 652-database stock this script exists to reap predates that safety net and offers no guarantee
# every name in it was produced the same way. `_migrations_test` (16 bytes) overflows before `_test`
# (5 bytes) does, so a parser that assumes a `_test` / `_migrations_test` pair always shares an
# intact common prefix can silently mismatch on legacy names.
#
# So this script goes the OTHER direction, which sidesteps the ambiguity entirely: for every seat
# CURRENTLY in the registry, compute its two expected database names exactly the way
# `provision-test-seat.sh` does (`phaze_<seat>_test`, `phaze_<seat>_migrations_test` -- `<seat>` is
# already the derived, registry-stored identifier, never re-derived here) and mark those two exact
# strings PROTECTED. Any `phaze%test%` database not in that protected set is, by definition,
# unregistered -- no string parsing, truncation reasoning, or pairing assumption required.
#
# THE AGE CLOCK, MEASURED RATHER THAN ASSUMED (phaze-robzi.2, 2026-08-28, against postgres:18-alpine
# in a throwaway container -- the same image CLAUDE.md pins for this harness)
#
# `pg_database` carries no creation timestamp column at all (confirmed via `\d pg_database` and
# `information_schema.columns` -- neither lists one). The usual next reach is `pg_stat_file` on the
# database's own directory (`base/<oid>`), which DOES return a row -- but its `creation` field came
# back NULL on this image (birth-time is not exposed through this filesystem stack), so "creation"
# is not an available clock at all here, confirming the header's own caveat about that half.
# `modification`, however, IS populated and moves on real writes: measured immediately after
# `CREATE DATABASE`, then again after `CREATE TABLE` + `INSERT` + `CHECKPOINT` inside that database
# 5 seconds later, `modification` jumped forward by exactly that gap. So this is NOT a creation
# clock -- it is a "time since the directory was last touched" clock, and that is a stricter, safer
# choice for THIS purpose than a true creation timestamp would have been: a database that is
# somehow still receiving writes despite being unregistered keeps resetting its own age and stays
# protected by the floor, where a creation-based clock would eventually age it out from under an
# active (if buggy) writer. The alternative floated at planning time -- a creation stamp recorded in
# the Redis registry at provision time -- cannot help the STOCK problem this script targets: by
# construction, every database this script considers a candidate has NO registry entry left at all,
# which is exactly why phaze-robzi.1 could not reach it either.
#
# SAFETY
#
#   * Dry run by default, `--apply` to act -- matching `test-db-reclaim`'s shape.
#   * `phaze_test` / `phaze_migrations_test`, the shared canonical pair, are NEVER candidates,
#     unconditionally, regardless of registry or backend state.
#   * Fails CLOSED: if Postgres or the Redis registry does not answer, this refuses outright rather
#     than reading "unknown" as "safe to drop" (mirrors `require_postgres_evidence` in
#     redis-seat-registry.sh).
#   * Every verdict is re-derived immediately before a drop, not trusted from the classification
#     snapshot -- the same re-read-before-acting shape `free_seat` uses (phaze-r311e / phaze-robzi.1).
#   * Never stops, removes, or recreates a container on any path -- only `docker exec`.
#
# Usage:
#   test-db-gc.sh --pg-container C --redis-container C [--age-floor-hours H] [--apply]
#
# Exit codes: 0 ok, 1 error/refused, 2 usage.
set -euo pipefail

pg_container=""
redis_container=""
age_floor_hours="${PHAZE_TEST_DB_GC_AGE_FLOOR_HOURS:-24}"
apply=0

usage() {
  echo "usage: $0 --pg-container C --redis-container C [--age-floor-hours H] [--apply]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pg-container)
      pg_container="${2:-}"
      shift 2
      ;;
    --redis-container)
      redis_container="${2:-}"
      shift 2
      ;;
    --age-floor-hours)
      age_floor_hours="${2:-}"
      shift 2
      ;;
    --apply)
      apply=1
      shift
      ;;
    *)
      echo "❌ unknown argument '$1'" >&2
      usage
      ;;
  esac
done

[ -n "$pg_container" ] || usage
[ -n "$redis_container" ] || usage
[[ "$age_floor_hours" =~ ^[0-9]+$ ]] || {
  echo "❌ --age-floor-hours must be a non-negative integer, got '${age_floor_hours}'" >&2
  exit 2
}

# The two databases this script must never, under any signal combination, treat as a candidate.
readonly SHARED_MAIN_DB="phaze_test"
readonly SHARED_MIGRATIONS_DB="phaze_migrations_test"

# Fails closed exactly like `require_postgres_evidence` in redis-seat-registry.sh: an unreachable
# Postgres means backend evidence (signal 2) is unknown, and unknown is never read as free.
if ! docker exec "$pg_container" psql -U phaze -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "❌ Postgres container '${pg_container}' is not reachable, so no backend evidence is available." >&2
  echo "   Refusing rather than reading unknown as safe to drop. Start the harness (\`just test-db\`)." >&2
  exit 1
fi

# Fails closed the same way for the registry: an unreachable Redis means signal 1 (unregistered) is
# unknown for every database, and a database that IS registered but looks unreachable-unregistered
# would be dropped out from under a live seat.
if ! docker exec "$redis_container" redis-cli PING >/dev/null 2>&1; then
  echo "❌ Redis container '${redis_container}' is not reachable, so registration status (signal 1) is" >&2
  echo "   unknown for every database. Refusing rather than reading unknown as unregistered. Start the" >&2
  echo "   harness (\`just test-db\`)." >&2
  exit 1
fi

# Signal 2: zero Postgres client backends on `db`, scoped to exactly that database by name -- the
# same shape `seat_is_postgres_live` in redis-seat-registry.sh uses, re-run fresh wherever this
# script is about to act rather than trusted from classification.
readonly BACKENDS_SQL="SELECT DISTINCT datname FROM pg_stat_activity
      WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() AND datname LIKE 'phaze%'"

backends_on() {
  docker exec "$pg_container" psql -U phaze -d postgres -tAc "$BACKENDS_SQL" 2>/dev/null | grep -qxF "$1"
}

# Signal 1: the forward-derived protected set (seat -> its two database names), never the reverse.
# One name per line on stdout; empty when the registry holds no seats at all.
protected_database_names() {
  local seat
  docker exec "$redis_container" redis-cli -n 0 HKEYS phaze:test:redis-db-index 2>/dev/null | while IFS= read -r seat; do
    [ -n "$seat" ] || continue
    printf 'phaze_%s_test\n' "$seat"
    printf 'phaze_%s_migrations_test\n' "$seat"
  done
}

is_protected() {
  printf '%s\n' "$protected" | grep -qxF "$1"
}

# Signal 3: age, from `pg_stat_file`'s `modification` field on the database's own directory --
# see the header for why this, not `creation`, is the clock this script uses. One `datname|seconds`
# record per candidate row; `age_seconds` is empty when `pg_stat_file` itself fails (the directory
# vanished between listing and stat-ing it, e.g. a concurrent drop) -- treated as "too young to
# tell" rather than "old enough to drop", i.e. left alone, never the reverse.
readonly CANDIDATE_SQL="SELECT d.datname || '|' || COALESCE(extract(epoch from now() - (pg_stat_file('base/' || d.oid, true)).modification)::bigint::text, '')
  FROM pg_database d
  WHERE d.datname LIKE 'phaze%test%'
  ORDER BY d.datname"

candidates="$(docker exec "$pg_container" psql -U phaze -d postgres -tAc "$CANDIDATE_SQL")"
protected="$(protected_database_names)"

if [ -z "$candidates" ]; then
  echo "No phaze%test% databases found on ${pg_container}; nothing to collect."
  exit 0
fi

age_floor_seconds=$((age_floor_hours * 3600))
kept=0
dropped=0

echo "Postgres databases matching phaze%test% on ${pg_container} (age floor ${age_floor_hours}h):"
echo ""
printf '  %-9s  %-50s  %s\n' "STATE" "DATABASE" "EVIDENCE"

while IFS='|' read -r dbname age_seconds; do
  [ -n "$dbname" ] || continue

  verdict="keep"
  reason=""
  if [ "$dbname" = "$SHARED_MAIN_DB" ] || [ "$dbname" = "$SHARED_MIGRATIONS_DB" ]; then
    reason="the shared canonical pair is never a candidate"
  elif is_protected "$dbname"; then
    reason="a live seat in the Redis registry names this database"
  elif backends_on "$dbname"; then
    reason="a Postgres backend is connected to it right now"
  elif [ -z "$age_seconds" ] || [ "$age_seconds" -lt "$age_floor_seconds" ]; then
    age_h=$((${age_seconds:-0} / 3600))
    reason="${age_h}h old, under the ${age_floor_hours}h age floor (or its age could not be read)"
  else
    verdict="reclaim"
    age_h=$((age_seconds / 3600))
    reason="unregistered, no backends, ${age_h}h old (past the ${age_floor_hours}h floor)"
  fi

  printf '  %-9s  %-50s  %s\n' "$([ "$verdict" = keep ] && echo keep || echo stale)" "$dbname" "$reason"

  if [ "$verdict" != "reclaim" ]; then
    kept=$((kept + 1))
    continue
  fi

  if [ "$apply" -ne 1 ]; then
    dropped=$((dropped + 1))
    continue
  fi

  # Re-derive every signal immediately before dropping -- the same re-read-before-acting shape
  # `free_seat` uses in redis-seat-registry.sh (phaze-r311e / phaze-robzi.1). A sweep over many
  # databases takes real wall-clock time, and a database mid-provision or freshly re-registered
  # since classification must not be destroyed from a stale snapshot.
  fresh_protected="$(protected_database_names)"
  if printf '%s\n' "$fresh_protected" | grep -qxF "$dbname"; then
    echo "🟥 left '${dbname}' alone: a seat registered it since classification. Nothing was dropped."
    kept=$((kept + 1))
    continue
  fi
  if backends_on "$dbname"; then
    echo "🟥 left '${dbname}' alone: a Postgres backend connected to it since classification. Nothing was dropped."
    kept=$((kept + 1))
    continue
  fi
  if docker exec "$pg_container" psql -U phaze -d postgres -tAc "DROP DATABASE IF EXISTS \"${dbname}\"" >/dev/null 2>&1; then
    dropped=$((dropped + 1))
    echo "✅ dropped '${dbname}' — ${reason}"
  else
    echo "⚠️  could not drop '${dbname}' -- check ${pg_container}'s logs." >&2
    kept=$((kept + 1))
    continue
  fi
done <<<"$candidates"

echo ""
if [ "$apply" -eq 1 ]; then
  echo "🧹 dropped ${dropped} database(s); left ${kept} alone. No container was stopped, removed, or recreated."
else
  echo "Dry run: ${dropped} database(s) would be dropped, ${kept} left alone. Re-run with --apply to drop them."
fi
