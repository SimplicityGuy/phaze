#!/usr/bin/env bash
# Emit shell exports for one derived local seat, or preserve a complete caller-owned triplet.
# Stdout is eval-safe by contract; all diagnostics go to stderr.
set -euo pipefail

vars=(TEST_DATABASE_URL MIGRATIONS_TEST_DATABASE_URL PHAZE_REDIS_URL)
set_count=0
for name in "${vars[@]}"; do
  [[ -n "${!name:-}" ]] && ((set_count += 1))
done

if ((set_count > 0)); then
  if ((set_count != ${#vars[@]})); then
    echo "❌ Caller-owned test configuration must set TEST_DATABASE_URL, MIGRATIONS_TEST_DATABASE_URL, and PHAZE_REDIS_URL together." >&2
    exit 1
  fi
  echo "↩️  Preserving caller-owned Postgres and Redis settings." >&2
  exit 0
fi

[[ $# -eq 7 ]] || {
  echo "usage: $0 SEAT PG_CONTAINER PG_PORT REDIS_CONTAINER REDIS_PORT REDIS_CAPACITY ORIGIN" >&2
  exit 1
}

seat="$1"
pg_container="$2"
pg_port="$3"
redis_container="$4"
redis_port="$5"
redis_capacity="$6"
origin="$7"

just test-db >&2
bash scripts/provision-test-seat.sh \
  --seat "$seat" \
  --pg-container "$pg_container" \
  --pg-port "$pg_port" \
  --redis-container "$redis_container" \
  --redis-port "$redis_port" \
  --redis-capacity "$redis_capacity" \
  --origin "$origin"
