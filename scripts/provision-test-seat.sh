#!/usr/bin/env bash
# Provision ONE isolated test seat (Postgres pair + Redis logical DB) and print the three
# exports that select it -- and NOTHING else -- on stdout (phaze-bk9el.23).
#
# WHY THIS EXISTS AS A SCRIPT RATHER THAN INLINE IN `test-db-for`. There are now TWO callers
# that must provision an identical seat:
#
#   * `just test-db-for <name>` -- the operator-driven path. A human reads the exports off the
#     terminal and pastes them into their shell.
#   * `just test-validate` -- the gate. When the caller exported no TEST_DATABASE_URL it has to
#     provision a seat for itself and `eval` the exports, because before phaze-bk9el.23 it fell
#     back to the SHARED `phaze_test` + Redis DB 0, which is the one thing CLAUDE.md says never
#     to do ("Never share Postgres OR Redis between concurrent agents") for the one command
#     every bead is now required to run.
#
# Those two must agree EXACTLY -- same normalization, same database names, same registry
# allocation -- or the gate's seat and the operator's seat for one worktree diverge and the
# isolation is imaginary. phaze-bk9el.23 criterion 5 says so explicitly: "Do not hand-construct
# the DSN". A second copy of the recipe body is exactly how that drift happens, so there is one
# copy, here, and both callers run it.
#
# STDOUT/STDERR CONTRACT, which is the whole reason this is a separate file: stdout carries the
# three `export KEY="value"` lines and nothing else, so `eval "$(provision-test-seat.sh ...)"`
# is safe. Every human-readable line -- including the output of `ensure-pg-database.sh` and
# `redis-seat-registry.sh` -- goes to stderr, where it still reaches the terminal.
#
# Usage:
#   provision-test-seat.sh --seat NAME --pg-container C --pg-port P \
#                          --redis-container C --redis-port P --redis-capacity N \
#                          [--origin PATH]
#
# `--seat NAME` is the RAW seat name; it is normalized through `derive-seat-name.sh` (phaze-fmfk),
# never used verbatim. `--origin PATH` is recorded in the Redis registry and is what
# `just test-db-reclaim`'s O1 rule ("its origin worktree is gone") reads; default `$PWD`.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

seat=""
pg_container=""
pg_port=""
redis_container=""
redis_port=""
redis_capacity=""
origin="$PWD"

usage() {
  echo "usage: $0 --seat NAME --pg-container C --pg-port P --redis-container C --redis-port P --redis-capacity N [--origin PATH]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --seat)
      seat="${2:-}"
      shift 2
      ;;
    --pg-container)
      pg_container="${2:-}"
      shift 2
      ;;
    --pg-port)
      pg_port="${2:-}"
      shift 2
      ;;
    --redis-container)
      redis_container="${2:-}"
      shift 2
      ;;
    --redis-port)
      redis_port="${2:-}"
      shift 2
      ;;
    --redis-capacity)
      redis_capacity="${2:-}"
      shift 2
      ;;
    --origin)
      origin="${2:-}"
      shift 2
      ;;
    *) usage ;;
  esac
done

[ -n "$seat" ] || usage
[ -n "$pg_container" ] || usage
[ -n "$pg_port" ] || usage
[ -n "$redis_container" ] || usage
[ -n "$redis_port" ] || usage
[ -n "$redis_capacity" ] || usage

# phaze-fmfk: the single place that decides what a raw name normalizes to (hyphens -> underscores
# plus a short hash of the RAW name, so `my-seat` and `my_seat` cannot collide onto one seat).
name="$(bash "${script_dir}/derive-seat-name.sh" "$seat")"
echo "Seat '${seat}' -> identifier '${name}' (stable across re-runs of the same seat name)." >&2

main_db="phaze_${name}_test"
migrations_db="phaze_${name}_migrations_test"

# phaze-hk8r: ensure-pg-database.sh tolerates a lost create race -- see its header for why a plain
# SELECT-then-CREATE is a check-then-act TOCTOU that used to kill this path under `set -e` when a
# concurrent invocation won the CREATE first. Now that `test-validate` provisions too, concurrent
# invocations are the NORMAL case rather than the exotic one.
bash "${script_dir}/ensure-pg-database.sh" "$pg_container" "$main_db" "$migrations_db" >&2

# Redis isolation. Postgres-only isolation was the phaze-fwo7 defect: every worktree landed on the
# same logical Redis DB 0, where fixtures run global `scan_iter`+`delete` sweeps over `exec:*` /
# `tracklist_req:*` and assertions count the global keyspace. Allocation is an atomic registry in
# DB 0, NOT a hash of the name. Keyed on the DERIVED `name`, so the registry, the Postgres pair
# above and the exports below can never disagree about what this seat is called.
redis_db="$(bash "${script_dir}/redis-seat-registry.sh" allocate \
  --redis-container "$redis_container" \
  --seat "$name" \
  --capacity "$redis_capacity" \
  --origin "$origin")"

# The three exports, and nothing else, on stdout.
echo "export TEST_DATABASE_URL=\"postgresql+asyncpg://phaze:phaze@localhost:${pg_port}/${main_db}\""
echo "export MIGRATIONS_TEST_DATABASE_URL=\"postgresql+asyncpg://phaze:phaze@localhost:${pg_port}/${migrations_db}\""
echo "export PHAZE_REDIS_URL=\"redis://localhost:${redis_port}/${redis_db}\""
