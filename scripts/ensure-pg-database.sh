#!/usr/bin/env bash
# Idempotently ensure one or more Postgres databases exist on a running test-harness
# container, tolerating a lost create race against a concurrent invocation (phaze-hk8r).
#
# The naive SELECT-then-CREATE (check if the datname row exists; CREATE only if not) is a
# check-then-act TOCTOU race: two invocations targeting the SAME database (e.g. several
# developers running `just test-db` / `just test-db-for <name>` / `just integration-test`
# at once against a shared container) can both see the SELECT return no row, then both
# attempt CREATE DATABASE. Under `set -e` the loser used to kill the whole recipe on
# Postgres's "already exists" error instead of no-op'ing, even though the database this
# invocation wanted now genuinely exists. Shared by every justfile recipe that provisions
# a Postgres database against "$container", so the tolerant-CREATE fix lives in one place.
#
# Usage: ensure-pg-database.sh <container> <db> [<db> ...]
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <container> <db> [<db> ...]" >&2
  exit 2
fi

container="$1"
shift

for db in "$@"; do
  if docker exec "$container" psql -U phaze -d phaze_test -tc \
    "SELECT 1 FROM pg_database WHERE datname = '${db}'" | grep -q 1; then
    echo "🐘 ${db} already exists"
    continue
  fi
  create_err="$(mktemp)"
  if docker exec "$container" psql -U phaze -d phaze_test \
    -c "CREATE DATABASE ${db} OWNER phaze;" >/dev/null 2>"$create_err"; then
    echo "✅ created ${db}"
    rm -f "$create_err"
  elif grep -q "already exists" "$create_err"; then
    # Lost the create race to a concurrent invocation -- the database is present
    # either way, so this is success, not failure.
    echo "🐘 ${db} already exists (created concurrently)"
    rm -f "$create_err"
  else
    cat "$create_err" >&2
    rm -f "$create_err"
    exit 1
  fi
done
