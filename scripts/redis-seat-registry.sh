#!/usr/bin/env bash
# The Redis logical-DB seat registry behind `just test-db-for` (phaze-68wky).
#
# WHAT THIS OWNS
#
# `just test-db-for <name>` gives one worktree ("seat") an exclusive Redis logical database on the
# SHARED `phaze-test-redis` container, because two redis-backed test modules run global
# `scan_iter`+`delete` sweeps in fixture setup AND teardown -- on a shared logical DB one seat's
# fixture deletes another seat's live keys mid-test (phaze-fwo7). This script is the single place
# that decides which index a seat gets, and -- new in phaze-68wky -- when an index may be taken
# back.
#
# THE DEFECT THIS REPLACES
#
# Allocation used to be `INCR phaze:test:redis-db-counter` with no reclaim: a monotonic counter.
# Every seat that ever ran `test-db-for` burned an index forever, so the counter walked past the
# 64-database cap and then refused every new seat -- with indices 68, 73, 74 and 80 observed in the
# wild, mostly held by worktrees that no longer existed. Refusing was right (wrapping onto a shared
# index silently restores phaze-fwo7), but the only remedies offered were `just test-db-down`
# variants, and `test-db-down` removes the ONE Postgres+Redis pair every concurrent seat shares --
# the phaze-ieqg incident, where a mid-round teardown destroyed 89 per-worktree databases and this
# very registry under five in-flight suites. Five independent fleet reports (hq-6hb, hq-8zr, hq-el5,
# hq-ew2, hq-ij4) hit that wall and each invented a different workaround, one of which was dropping
# Redis isolation altogether -- i.e. straight back into phaze-fwo7.
#
# So: allocation now takes the LOWEST FREE index rather than the next one, and there are two
# ways to make an index free again (`release`, `reclaim`) that never touch the shared CONTAINERS.
#
# CONTRACT CHANGE (phaze-robzi.1): `reclaim --apply` is no longer Postgres-non-destructive. A
# freed seat's REDIS entries are all that named its `phaze_<name>_test` /
# `phaze_<name>_migrations_test` pair -- once `free_seat` deletes them, those two databases have no
# owner left at all, which is exactly how 652 orphaned databases (6974 MB) accumulated on the
# shared harness with no tool able to reap them short of a full `test-db-down`. So `reclaim --apply`
# now drops a freed seat's OWN two databases in the same operation that frees its Redis index (see
# `drop_seat_databases`, called from `free_seat`'s success path). `release` is UNCHANGED and stays
# the non-destructive-of-Postgres counterpart it always was -- see its own header comment.
#
# THE LIVENESS TEST (the part that must not be guessed at)
#
# "Is this seat still in use?" cannot be answered by "is something connected right now" alone: a
# developer who ran `test-db-for` and is editing code for an hour has no connections at all, and
# handing their index to a second seat is precisely the bug. So a seat is considered IN USE if ANY
# of these hold, and only reclaimable when NONE do:
#
#   L1  A Redis client is connected to its logical database (`CLIENT LIST` -> `db=<index>`).
#   L2  A Postgres client backend is connected to `phaze_<seat>_test` or
#       `phaze_<seat>_migrations_test` on the shared harness. This is the strong one: pytest holds
#       a session-level advisory-lock connection for the WHOLE run (tests/db_guard.py), so an
#       idle-looking suite is still visible here. It is the same signal `just test-db-down` already
#       trusts to refuse a teardown. Postgres evidence is MANDATORY for `reclaim`: if the Postgres
#       container cannot be reached, the sweep refuses rather than treating unknown as free.
#
#       L2 therefore has THREE states, and collapsing them is the phaze-gmkua defect: the query
#       used to end `2>/dev/null || true`, so an unreachable Postgres returned an EMPTY list --
#       byte-identical to "no seat has a backend attached", i.e. unknown silently read as free.
#
#         obtained     the query answered. `pg_live` is authoritative, empty or not.
#         unavailable  a container was named but did not answer. NOT a finding: every seat's L2
#                      state is unknown, so classification keeps them all -- EXCEPT O2 (below),
#                      whose evidence is structural rather than observational and so does not need
#                      L2 at all (phaze-hrnww): `list` still reports an out-of-range legacy seat as
#                      stale even when Postgres cannot be reached. `reclaim` is unaffected, since it
#                      refuses outright on unavailable L2 before classify_seats ever runs.
#         waived       the operator passed `--no-postgres-check`, accepting a sweep on L1/L3/O1/O2
#                      alone. `reclaim` always says so on stderr -- see `cmd_reclaim`, and note the
#                      warning is gated on the EVIDENCE, never on whether `--pg-container` happened
#                      to be passed: the justfile recipe always passes it, so the old
#                      `-z "$pg_container"` gate silenced the warning on the only shape an operator
#                      ever types.
#   L3  Its lease has not expired -- `test-db-for` stamps `phaze:test:redis-db-seen` on every call,
#       and a seat stamped within PHAZE_TEST_REDIS_SEAT_LEASE_HOURS (default 72) is left alone.
#       This is what protects the claimed-but-idle seat that L1 and L2 cannot see.
#
# A registry entry with NO stamp predates phaze-68wky, so its age is genuinely unknown. It is
# reported as `unknown`, NOT as stale, and a sweep leaves it alone unless asked with
# `--include-unstamped` -- backfilling a plausible age for it would be precisely the guessed-at
# liveness test this design refuses. Each such seat stamps itself the next time its worktree runs
# `test-db-for`, so the backlog drains on its own as seats are used.
#
# Two conditions override L3 (never L1/L2 -- nothing reclaims a seat that is demonstrably in use).
# O2 also overrides unavailable L2 evidence in `list`'s reporting (phaze-hrnww) -- see the L2 note
# above; O1 does not, and stays behind unavailable L2 as the weaker case.
#
#   O1  The seat's recorded origin directory (`phaze:test:redis-db-origin`, the worktree that ran
#       `test-db-for`) no longer exists. The worktree is gone; the seat cannot come back.
#   O2  The seat's index is >= the container's logical-database count. No client can be using it --
#       Redis refuses `SELECT` past `databases`, so such a seat is already broken, not live. These
#       are exactly the 68/73/74/80 allocations that wedged the harness.
#
# `release <seat>` is the operator-driven path and deliberately ignores L3/O1/O2: naming a seat IS
# the assertion that it is finished. It still refuses on L1/L2 (use --force to override).
#
# Every registry VALUE is data and is sanitized before it is used as an index -- `classify_seats`
# always did this, `cmd_release` did not, and the gap was phaze-nbfuc: a value of `.*` reached the
# L1 grep as a regex, matched every `CLIENT LIST` line, and left the seat reporting in use forever.
# A value that is not an index is now reported and released: no logical database can correspond to
# it, so there is nothing a client could be holding and nothing to clear.
#
# ATOMICITY AND IDEMPOTENCE
#
# Allocation is one server-side Lua script: read-existing / scan-used / claim happen inside a
# single Redis execution, so two shells racing on two names can never be handed the same index, and
# two shells racing on the SAME name both read back one winner. Re-running for an already-allocated
# seat returns the same index and only refreshes the lease -- CLAUDE.md and every agent recipe
# depend on that (a fresh index per invocation would strand the previous one and orphan its keys).
# The scan is what makes released indices reusable; `hash(name) % N` would not do, it collides ~35%
# of the time across 8 seats and would reintroduce phaze-fwo7 intermittently. Within the scan, an
# index nobody has ever held is preferred over a recycled one (see the comment on the two loops).
#
# Keys, all on logical DB 0 (never allocated to a seat):
#   phaze:test:redis-db-index   hash seat -> index   (unchanged shape; pre-existing entries load)
#   phaze:test:redis-db-seen    hash seat -> unix seconds of last `test-db-for` (lease stamp)
#   phaze:test:redis-db-origin  hash seat -> absolute path of the worktree that claimed it
#   phaze:test:redis-db-counter The ALL-TIME high-water mark: the largest index ever handed out on
#                               this container, which is exactly what the old monotonic counter
#                               already held. It is no longer the allocator (the scan is), but it
#                               is what makes "prefer an index nobody has ever held" true --
#                               deriving that mark from the CURRENTLY REGISTERED entries collapses
#                               it the moment the top seat is freed (phaze-08sww). It only ever
#                               RISES: `allocate` raises it when it hands out a fresh index; a
#                               non-`--force` `release` raises it atomically inside the SAME EVAL
#                               that frees the seat, immediately before the delete, so a REFUSED
#                               release (L1/L2 live, or free_seat's own compare-and-delete losing
#                               the race) leaves this key untouched (phaze-4xsbe; see FREE_SEAT_LUA's
#                               comment) -- `--force` and `reclaim --apply` raise it via a separate,
#                               prior EVAL instead, which is safe for them because neither has a
#                               refusal path left to reach once it starts freeing. Keeping the same
#                               key also keeps an older checkout's
#                               `INCR`-based `test-db-for` behaving sanely against the same
#                               container -- and a mark left at 68/73/80 by the counter era is not
#                               clamped, because it is telling the truth: in a 64-database space
#                               every allocatable index really has been handed out at some point, so
#                               there is nothing fresh left to prefer and recycling is all there is.
#
# Seat names are the DERIVED identifiers from `scripts/derive-seat-name.sh` (phaze-fmfk); this
# script does not normalize anything itself, so the registry key, the Postgres database pair and
# the printed exports can never disagree about what a seat is called.
#
# Freeing is atomic in the same way and for a sharper reason. `reclaim` classifies every seat once
# and then destroys them from that snapshot, so the snapshot is stale by construction -- a 5-seat
# sweep measured 5.46s. `free_seat` therefore re-reads L1 and compare-and-deletes the registry
# fields against the lease stamp classification saw, and declines if either has moved. See its
# comment for the failure it closes (phaze-r311e).
#
# The compare-and-delete and the FLUSHDB of the freed logical DB are themselves ONE server-side
# script (FREE_SEAT_LUA), not two round trips -- see its comment for the narrower failure THAT
# closes (phaze-atu2e) and the one residual window it documents rather than closes (the L1
# `CLIENT LIST` re-verification, which cannot run from Lua at all).
#
# Usage:
#   redis-seat-registry.sh allocate --redis-container C --seat S --capacity N [--origin PATH]
#   redis-seat-registry.sh release  --redis-container C --seat S [--pg-container C] [--force]
#   redis-seat-registry.sh list     --redis-container C [--capacity N] [--pg-container C]
#   redis-seat-registry.sh reclaim  --redis-container C --capacity N
#                                   (--pg-container C | --no-postgres-check)
#                                   [--apply] [--include-unstamped]
#
# Exit codes: 0 ok, 1 error, 2 usage, 3 capacity exhausted (allocate), 4 refused because the seat
# is in use (release), 5 nothing to release -- the named seat holds no Redis logical DB (release).
# 5 is deliberately its own code rather than folded into 0: `release` already gives every other
# outcome (a real free, a refusal, exhaustion) a distinct code, and a caller that only checked "was
# it 0" is exactly the phaze-o8sie shape -- a wrong-guessed name read the honest "nothing to
# release" prose as success. Scripted callers can `case $? in 5) ... ;; esac` without parsing
# English; a human reading the terminal still sees the same prose, on stderr like every other
# non-zero release outcome.
set -euo pipefail

readonly REGISTRY_KEY="phaze:test:redis-db-index"
readonly SEEN_KEY="phaze:test:redis-db-seen"
readonly ORIGIN_KEY="phaze:test:redis-db-origin"
readonly HIGHWATER_KEY="phaze:test:redis-db-counter"

lease_hours="${PHAZE_TEST_REDIS_SEAT_LEASE_HOURS:-72}"

subcommand="${1:-}"
shift || true

redis_container=""
pg_container=""
seat=""
capacity=""
origin=""
apply=0
force=0
skip_postgres_check=0
include_unstamped=0

usage() {
  echo "usage:" >&2
  echo "  $0 allocate --redis-container C --seat S --capacity N [--origin PATH]" >&2
  echo "  $0 release  --redis-container C --seat S [--pg-container C] [--force]" >&2
  echo "  $0 list     --redis-container C [--capacity N] [--pg-container C]" >&2
  echo "  $0 reclaim  --redis-container C --capacity N (--pg-container C | --no-postgres-check) [--apply] [--include-unstamped]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --redis-container)
      redis_container="${2:-}"
      shift 2
      ;;
    --pg-container)
      pg_container="${2:-}"
      shift 2
      ;;
    --seat)
      seat="${2:-}"
      shift 2
      ;;
    --capacity)
      capacity="${2:-}"
      shift 2
      ;;
    --origin)
      origin="${2:-}"
      shift 2
      ;;
    --apply)
      apply=1
      shift
      ;;
    --force)
      force=1
      shift
      ;;
    --no-postgres-check)
      skip_postgres_check=1
      shift
      ;;
    --include-unstamped)
      include_unstamped=1
      shift
      ;;
    *)
      echo "❌ unknown argument '$1'" >&2
      usage
      ;;
  esac
done

[ -n "$redis_container" ] || usage

# ---------------------------------------------------------------------------------------------
# Redis plumbing. Everything goes through `docker exec` -- the same path the justfile recipe used
# before this script existed -- so no redis-cli on the host is required and the tests can drive a
# THROWAWAY container by name without publishing a port anywhere near the shared harness on 6380.
# ---------------------------------------------------------------------------------------------

registry_cli() {
  docker exec "$redis_container" redis-cli -n 0 "$@"
}

# A container that is genuinely absent stays absent, so re-asking costs a few hundred milliseconds
# once and settles the question. A SINGLE `docker exec` does not settle it: under load the daemon
# can fail one exec on a container that is up and answering, and this probe then reports the one
# thing guaranteed to be unhelpful -- "not reachable, start the harness" about a harness already
# running. Observed on a machine with four suites in flight, where it turned an `allocate` that
# should have refused with the exhaustion message (exit 3) into exit 1.
#
# This is a probe, not a guard: it decides whether we can TALK to Redis, never what the registry
# says. Nothing below retries, and no allocation, refusal or reclaim verdict is affected by how many
# times we asked -- in particular the exhaustion refusal is reached through the allocator's own EVAL
# and is untouched by this loop.
redis_reachable=0
for _ in 1 2 3; do
  if docker exec "$redis_container" redis-cli PING >/dev/null 2>&1; then
    redis_reachable=1
    break
  fi
  sleep 0.3
done
if [ "$redis_reachable" -eq 0 ]; then
  echo "❌ Redis container '${redis_container}' is not reachable. Start the shared harness with \`just test-db\`." >&2
  exit 1
fi

server_now() {
  docker exec "$redis_container" redis-cli TIME | head -n1
}

# Logical databases actually configured on the container. `--capacity` is what the caller intends;
# this is what Redis will really accept a SELECT for, and the smaller of the two is the real cap.
container_databases() {
  docker exec "$redis_container" redis-cli CONFIG GET databases 2>/dev/null | tail -n1
}

effective_capacity() {
  local configured
  configured="$(container_databases)"
  if [ -z "${capacity:-}" ]; then
    echo "${configured:-16}"
  elif [ -n "$configured" ] && [ "$configured" -lt "$capacity" ]; then
    echo "$configured"
  else
    echo "$capacity"
  fi
}

# Indices with at least one client connected right now (liveness signal L1). redis-cli's own
# connection lands on whatever `-n` selected, so this is read on DB 0, which is never allocated.
live_redis_indices() {
  docker exec "$redis_container" redis-cli CLIENT LIST 2>/dev/null |
    sed -n 's/.*[[:space:]]db=\([0-9]*\).*/\1/p' | sort -u
}

# Databases with a live client backend on the shared Postgres (liveness signal L2). Same query
# shape `just test-db-down` uses to refuse a teardown; pytest holds its advisory-lock connection
# for the whole session, so a suite that is merely thinking still shows up.
readonly PG_LIVE_SQL="SELECT DISTINCT datname FROM pg_stat_activity
      WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() AND datname LIKE 'phaze%'"

# Set by collect_postgres_evidence, read by classify_seats / cmd_release / cmd_reclaim.
# `pg_evidence` is one of obtained | unavailable | waived (see the L2 note in the header); `pg_live`
# is meaningful ONLY when it is `obtained`, which is the whole point of keeping the two apart.
pg_evidence="waived"
pg_live=""

collect_postgres_evidence() {
  pg_live=""
  if [ -z "$pg_container" ]; then
    pg_evidence="waived"
    return 0
  fi
  # Asked for even under --no-postgres-check: that flag waives the REFUSAL, not evidence that is
  # there for the taking. A reachable Postgres still protects a running suite either way.
  if pg_live="$(docker exec "$pg_container" psql -U phaze -d postgres -tAc "$PG_LIVE_SQL" 2>/dev/null)"; then
    pg_evidence="obtained"
    return 0
  fi
  # The query failed. An empty answer would be indistinguishable from "nobody is connected", so
  # say so instead of returning one (phaze-gmkua defect B).
  pg_live=""
  if [ "$skip_postgres_check" -eq 1 ]; then
    pg_evidence="waived"
  else
    pg_evidence="unavailable"
  fi
}

# `-F` in both, because both needles are DATA: the index comes from the registry hash and the seat
# name from its field. Without it a registry value of `.*` is a regex that matches every CLIENT LIST
# line, which reported the seat permanently in use and made it unreleasable (phaze-nbfuc). Callers
# sanitize the index as well -- this is the second line of defence, not the only one.
seat_is_redis_live() {
  local index="$1" live
  live="$2"
  printf '%s\n' "$live" | grep -qxF "$index"
}

seat_is_postgres_live() {
  local name="$1" live
  live="$2"
  printf '%s\n' "$live" | grep -qxF -e "phaze_${name}_test" -e "phaze_${name}_migrations_test"
}

# ---------------------------------------------------------------------------------------------
# allocate
# ---------------------------------------------------------------------------------------------

# One atomic server-side transaction: return the seat's existing in-range index (refreshing its
# lease), else claim the LOWEST FREE index in [1, capacity), else report exhaustion. Index 0 is
# reserved for this registry itself and is never handed out.
readonly ALLOCATE_LUA='
local reg, seen, origin_key, highwater_key = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local seat, capacity, origin_path = ARGV[1], tonumber(ARGV[2]), ARGV[3]
local now = redis.call("TIME")[1]

local function stamp()
  redis.call("HSET", seen, seat, now)
  if origin_path ~= "" then redis.call("HSET", origin_key, seat, origin_path) end
end

local existing = redis.call("HGET", reg, seat)
local existing_index = existing and tonumber(existing) or nil
if existing_index and existing_index >= 1 and existing_index < capacity then
  stamp()
  return "existing " .. existing_index
end

local used = {}
local flat = redis.call("HGETALL", reg)
for i = 1, #flat, 2 do
  if flat[i] ~= seat then
    local value = tonumber(flat[i + 1])
    if value then used[value] = true end
  end
end

-- The ALL-TIME high-water mark, persisted rather than derived. Deriving it from the entries above
-- is what phaze-08sww reported: freeing the top seat collapsed the mark, so the very next
-- allocation recycled an index that had been in use seconds earlier -- weakest precisely after a
-- sweep, which is the moment recycling happens at all. The registered entries are still folded in
-- as a FLOOR, so a container whose mark predates this key (or was written by an older checkout)
-- cannot start below what it has already handed out.
local stored = tonumber(redis.call("GET", highwater_key)) or 0
local highwater = stored
for i = 1, #flat, 2 do
  local value = tonumber(flat[i + 1])
  if value and value < capacity and value > highwater then highwater = value end
end
-- Write the floor back rather than recomputing it every call. Otherwise the entries that raised it
-- can be released before any above-mark allocation persists it, and the mark collapses after all --
-- the same defect, just one allocation later. This makes the FIRST call against a container whose
-- key predates this change durable, which is the only case where the floor does any work.
if highwater > stored then redis.call("SET", highwater_key, highwater) end

local function claim(index)
  redis.call("HSET", reg, seat, index)
  if index > highwater then redis.call("SET", highwater_key, index) end
  stamp()
  if existing then return "reallocated " .. index end
  return "allocated " .. index
end

-- Prefer an index no seat has ever held (above the all-time high-water mark) before recycling a
-- freed one. Reclaim can only ever be as good as its evidence, and one hazard survives every
-- check: a shell that still has a stale PHAZE_REDIS_URL exported keeps using its old index even
-- after the registry hands it back. Recycling last, rather than first, means that hazard is only
-- taken when the space genuinely demands it.
for index = highwater + 1, capacity - 1 do
  if not used[index] then return claim(index) end
end
for index = 1, capacity - 1 do
  if not used[index] then return claim(index) end
end

return "exhausted 0"
'

# stdout is the allocated index and NOTHING else, so the caller can capture it with a plain
# command substitution; every human-facing line goes to stderr and reaches the terminal unread.
cmd_allocate() {
  [ -n "$seat" ] && [ -n "$capacity" ] || usage
  local cap result verdict index
  cap="$(effective_capacity)"

  result="$(registry_cli EVAL "$ALLOCATE_LUA" 4 "$REGISTRY_KEY" "$SEEN_KEY" "$ORIGIN_KEY" "$HIGHWATER_KEY" "$seat" "$cap" "$origin")"
  verdict="${result%% *}"
  index="${result##* }"

  case "$verdict" in
    existing)
      echo "🟥 '${seat}' already holds Redis logical DB ${index}" >&2
      ;;
    allocated | reallocated)
      if [ "$verdict" = "reallocated" ]; then
        # Only reachable when the recorded index was out of range -- an allocation from the old
        # monotonic counter that Redis would refuse to SELECT. Moving it into range is a repair,
        # not a reassignment: nothing can have been using it.
        echo "♻️  '${seat}' held an out-of-range index; moved it to Redis logical DB ${index}" >&2
      else
        echo "✅ allocated Redis logical DB ${index} to '${seat}'" >&2
      fi
      # A freed index keeps whatever keys its previous seat left behind unless that seat was
      # released through this script. Handing those to a new seat is the phaze-fwo7 shape from the
      # other direction -- foreign keys in a "private" database -- so clear it at handover. Safe by
      # construction: only an index no live seat holds can be allocated here.
      local leftover
      leftover="$(docker exec "$redis_container" redis-cli -n "$index" DBSIZE 2>/dev/null || echo 0)"
      if [ "${leftover:-0}" -gt 0 ]; then
        echo "🧹 logical DB ${index} held ${leftover} leftover key(s) from a previous seat; cleared" >&2
        docker exec "$redis_container" redis-cli -n "$index" FLUSHDB >/dev/null
      fi
      ;;
    exhausted)
      echo "" >&2
      echo "❌ All $((cap - 1)) allocatable Redis logical DBs on ${redis_container} are held; no index left for '${seat}'." >&2
      echo "   (${cap} logical DBs exist; DB 0 is the allocation registry, so seats get 1..$((cap - 1)).)" >&2
      echo "   Refusing to wrap onto a shared index -- that would silently reintroduce the cross-worktree" >&2
      echo "   Redis interference this registry exists to prevent (phaze-fwo7)." >&2
      echo "" >&2
      echo "   Reclaim first -- neither of these stops or clears the shared harness:" >&2
      echo "     just test-db-seats                  # who holds what, and which seats look stale" >&2
      echo "     just test-db-reclaim                # dry run: what a sweep would free" >&2
      echo "     just test-db-reclaim --apply        # free every seat no longer in use" >&2
      echo "     just test-db-release <other-seat>   # hand back one named seat you know is done" >&2
      echo "" >&2
      echo "   Only if the sweep frees nothing and every seat is genuinely live, raise the space:" >&2
      echo "     PHAZE_TEST_REDIS_DATABASES=$((cap * 2)) just test-db" >&2
      echo "   (that resize recreates the Redis container, so it refuses while any seat is connected;" >&2
      echo "   do NOT reach for \`just test-db-down\` -- it removes the Postgres+Redis pair every" >&2
      echo "   concurrent worktree shares and destroys their databases mid-run, phaze-ieqg.)" >&2
      exit 3
      ;;
    *)
      echo "❌ unexpected allocator response: ${result}" >&2
      exit 1
      ;;
  esac

  printf '%s\n' "$index"
}

# ---------------------------------------------------------------------------------------------
# release / reclaim
# ---------------------------------------------------------------------------------------------

# Hand an index back: wipe the seat's keys so the next holder starts clean, then drop the three
# registry fields. Touches nothing outside logical DB `index` and the registry hashes -- the
# containers and every OTHER seat's keys and databases are untouched. As of phaze-robzi.1, a
# `drop_pg=1` caller (only `cmd_reclaim`'s `--apply` loop passes it) ALSO drops this seat's own two
# Postgres databases on the success path below (see `drop_seat_databases`) -- `cmd_release`, force
# or not, never passes it, so a plain `release` still touches no Postgres database at all.
#
# Freeing is CONDITIONAL, and that is the whole of phaze-r311e. `reclaim` classifies every seat
# ONCE and then destroys them one at a time from that snapshot; a 5-seat sweep measured 5.46s
# end to end, and a 38-seat registry is 20s+ of exposure. An agent running `just test-db-for` inside
# that window is handed its existing index back, correctly and atomically -- and the sweep, acting
# on a snapshot taken before that happened, used to flush the database anyway and hand the index to
# the next allocate. Two seats, one logical DB: exactly the phaze-fwo7 shape this registry exists to
# prevent, from a snapshot that was merely stale rather than wrong.
#
# So the destruction re-derives its own evidence rather than trusting the snapshot:
#
#   1. Re-read L1 immediately before touching anything. A client that has connected since
#      classification means a suite is running in that seat right now.
#   2. Compare-and-delete the registry fields against the lease stamp classification saw, AND flush
#      the freed logical DB, in the SAME server-side script.
#
# Both destructive effects -- the registry delete and the FLUSHDB -- run inside FREE_SEAT_LUA, as
# one Redis EVAL. This closes phaze-atu2e: earlier, the delete and the FLUSHDB were two separate
# `docker exec` round trips, so between them the freed index was registry-free and a concurrent
# `allocate` could legitimately claim it, clear what it saw as leftover data, and start writing --
# and the second round trip, a FLUSHDB decided on stale information, then wiped the NEW holder's
# live keys. Folding both into one EVAL removes the gap entirely: Redis is single-threaded and a
# script's effects are atomic and invisible to every other client until the whole script returns,
# so no other client can ever observe "registry entry gone, logical DB not yet flushed" -- there is
# no state left to race into. The delete-before-flush ORDERING inside the script still matters for
# the reason it always did: if the CAS declines (the seat moved or was restamped), nothing must be
# destroyed, so the flush is reached only after the delete has already succeeded.
#
# The high-water raise (phaze-4xsbe) is folded in here too, not issued as a prior separate EVAL: it
# runs AFTER the CAS checks above and BEFORE the HDELs below, so it is reached if and only if this
# script is about to actually free the seat. `cmd_release` used to call `raise_highwater` before any
# of this -- including before the L1/L2 liveness checks that can still refuse the release outright
# -- so a release that declined to do anything had nonetheless written to the registry. The mark
# must still be raised BEFORE the delete and not after: once the HDELs below run, this entry no
# longer appears in the HGETALL scan, and a registry entry written before the mark existed (the
# `INCR`-era counter, see the header) is the one case this raise exists to cover at all. Folding it
# into the same atomic script gets both properties at once -- raised before the entry disappears,
# but only when the entry is genuinely about to disappear.
#
# What this does NOT close (documented per phaze-atu2e review finding 2, not fixed): the L1
# `CLIENT LIST` re-verification a few lines below is its OWN separate `docker exec`, issued BEFORE
# this EVAL, so a client that connects to the freed index in that sub-second gap is still flushed.
# It cannot be folded into FREE_SEAT_LUA -- `CLIENT LIST` (and `CLIENT` generally) is flagged
# `noscript` and Redis refuses to run it from EVAL at all ("This Redis command is not allowed from
# script"), confirmed against a live container while implementing this fix. This residual window is
# real, is not covered by any test, and is narrower than the one this bead closes -- but it is not
# nothing, so do not assume `free_seat` is race-free end to end.
readonly FREE_SEAT_LUA='
local reg, seen, origin_key, highwater_key = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local seat, expected_index, expected_seen, cap = ARGV[1], ARGV[2], ARGV[3], tonumber(ARGV[4])

local current_index = redis.call("HGET", reg, seat)
if current_index == false then return "vanished" end
if current_index ~= expected_index then return "moved" end

local current_seen = redis.call("HGET", seen, seat)
if current_seen == false then current_seen = "" end
if current_seen ~= expected_seen then return "restamped" end

local stored_highwater = tonumber(redis.call("GET", highwater_key)) or 0
local highwater = stored_highwater
local flat = redis.call("HGETALL", reg)
for i = 1, #flat, 2 do
  local value = tonumber(flat[i + 1])
  if value and value < cap and value > highwater then highwater = value end
end
if highwater > stored_highwater then redis.call("SET", highwater_key, highwater) end

redis.call("HDEL", reg, seat)
redis.call("HDEL", seen, seat)
redis.call("HDEL", origin_key, seat)

local index = tonumber(expected_index)
if index and index >= 1 and cap and index < cap then
  redis.call("SELECT", index)
  redis.call("FLUSHDB")
end

return "freed"
'

# Raise the persisted all-time high-water mark to cover everything CURRENTLY registered. Both
# freeing paths call this before they destroy anything, and the ordering is the entire point: once
# an entry is gone the allocator can no longer infer that its index was ever handed out, and the
# "prefer an index no seat has ever held" preference collapses exactly when it is needed most
# (phaze-08sww). `allocate` folds the same floor in, so a container that has allocated under this
# script already carries a correct mark -- this is what covers a registry written before the key
# existed, whose first operation under this script is a release or a sweep rather than an allocate.
#
# Purely additive: the mark only ever rises, and nothing reads it except the allocator's preference.
readonly RAISE_HIGHWATER_LUA='
local highwater_key, reg = KEYS[1], KEYS[2]
local capacity = tonumber(ARGV[1])
local stored = tonumber(redis.call("GET", highwater_key)) or 0
local highwater = stored
local flat = redis.call("HGETALL", reg)
for i = 1, #flat, 2 do
  local value = tonumber(flat[i + 1])
  if value and value < capacity and value > highwater then highwater = value end
end
if highwater > stored then redis.call("SET", highwater_key, highwater) end
return tostring(highwater)
'

raise_highwater() {
  local cap="$1"
  registry_cli EVAL "$RAISE_HIGHWATER_LUA" 2 "$HIGHWATER_KEY" "$REGISTRY_KEY" "$cap" >/dev/null
}

# Drop a freed seat's own two Postgres databases (phaze-robzi.1). Reached ONLY from free_seat's
# success path below, and only when the caller asked for it (`drop_pg=1`) -- in practice only
# `cmd_reclaim`'s `--apply` loop ever passes that, never `cmd_release`. `name` is already the
# DERIVED identifier (the registry key, straight from `derive-seat-name.sh`), so this composes the
# two database names directly rather than re-deriving anything.
#
# Best-effort per database rather than all-or-nothing: a failure to drop one must not be reported
# as though the seat were still allocated (the Redis EVAL above already succeeded, atomically, by
# the time this runs), and must not stop the sweep from moving on to the next seat.
drop_seat_databases() {
  local name="$1" db
  for db in "phaze_${name}_test" "phaze_${name}_migrations_test"; do
    if docker exec "$pg_container" psql -U phaze -d postgres -tAc "DROP DATABASE IF EXISTS \"${db}\"" >/dev/null 2>&1; then
      echo "   dropped Postgres database '${db}'"
    else
      echo "⚠️  could not drop Postgres database '${db}' for freed seat '${name}' -- check ${pg_container}'s logs; its Redis index was freed regardless." >&2
    fi
  done
}

# Set by free_seat when it declines, so the caller can say which guard objected.
free_seat_refusal=""

# Returns 0 having freed the seat, or non-zero having touched nothing at all.
free_seat() {
  local name="$1" index="$2" cap="$3" expected_seen="$4" raw="$5" drop_pg="${6:-0}"
  free_seat_refusal=""

  # Index 0 is the registry's own connection, so it is always "live" and must never be tested here;
  # classify_seats reports a corrupt registry value as index 0 for exactly that reason. An
  # out-of-range index cannot have a client either -- Redis refuses SELECT past `databases` -- so
  # the check is a no-op there rather than a special case.
  local redis_live
  redis_live="$(live_redis_indices)"
  if [ "$index" -ge 1 ] && seat_is_redis_live "$index" "$redis_live"; then
    free_seat_refusal="a Redis client connected to DB ${index} since classification (L1)"
    return 1
  fi

  # phaze-robzi.1: when this free will also drop the seat's own Postgres databases, re-derive the
  # L2 evidence FRESH here too -- the identical re-read-before-acting shape as L1 just above, and
  # for the identical reason: `reclaim` classifies every seat ONCE from a stale snapshot (a 5-seat
  # sweep already measures 5.46s end to end), and a suite that started running in that gap must not
  # have its databases dropped out from under it.
  #
  # `pg_evidence` can land on `obtained` (a container answered) or on `unavailable`/`waived`
  # (`--no-postgres-check` with no reachable Postgres, or no `--pg-container` passed at all --
  # `cmd_reclaim`'s own top-level `require_postgres_evidence` already refuses the WHOLE sweep
  # outright on `unavailable` evidence before classify_seats ever runs, unless the operator opted
  # out with `--no-postgres-check`; this is only reached once that gate has already been satisfied
  # or waived). A LIVE backend on `obtained` evidence refuses the whole free -- the Redis EVAL below
  # is never reached -- because leaving the Redis index allocated while the database it names has
  # already vanished would be the exact orphan-accumulation mechanism this bead exists to close.
  # Anything short of `obtained` evidence does NOT refuse the free: it only means the drop step
  # below is skipped for this seat (never a blind `DROP DATABASE`), and the Redis half proceeds
  # exactly as `reclaim --no-postgres-check` has always behaved.
  local drop_databases_this_seat=0
  if [ "$drop_pg" -eq 1 ]; then
    collect_postgres_evidence
    if [ "$pg_evidence" = "obtained" ]; then
      if seat_is_postgres_live "$name" "$pg_live"; then
        free_seat_refusal="a Postgres backend connected to phaze_${name}_test or its migrations database since classification (L2)"
        return 1
      fi
      drop_databases_this_seat=1
    fi
  fi

  # `cap` rides along as a 4th ARGV so the script itself can decide, atomically alongside the
  # delete, whether the freed index is in range to flush (phaze-atu2e). Nothing outside this one
  # EVAL touches logical DB `index` on the success path any more.
  local verdict
  verdict="$(registry_cli EVAL "$FREE_SEAT_LUA" 4 "$REGISTRY_KEY" "$SEEN_KEY" "$ORIGIN_KEY" "$HIGHWATER_KEY" "$name" "$raw" "$expected_seen" "$cap")"
  case "$verdict" in
    freed) ;;
    vanished)
      free_seat_refusal="its registry entry was already gone"
      return 1
      ;;
    moved)
      free_seat_refusal="it moved to a different index since classification"
      return 1
      ;;
    restamped)
      free_seat_refusal="it was re-claimed since classification (\`test-db-for\` re-stamped its lease)"
      return 1
      ;;
    *)
      free_seat_refusal="unexpected registry response '${verdict}'"
      return 1
      ;;
  esac

  if [ "$drop_databases_this_seat" -eq 1 ]; then
    drop_seat_databases "$name"
  elif [ "$drop_pg" -eq 1 ]; then
    echo "⚠️  freed '${name}''s Redis index without dropping its Postgres databases: L2 evidence was not obtained (${pg_evidence}). Its two databases are now orphaned; a later, evidenced sweep will still find them." >&2
  fi
}

# Unconditional. Only `release --force` reaches this: naming a seat AND passing --force is the
# operator asserting both that it is finished and that they have read why the guards objected.
#
# This one is NOT phaze-atu2e's bug even though it is also two round trips: it flushes BEFORE it
# deletes the registry fields, so the registry still shows the seat holding `index` for the whole
# FLUSHDB. `allocate`'s scan skips indices still present in the registry, so nothing can claim
# `index` until the HDELs below run -- there is no window where a new holder's keys are exposed to
# a stale flush the way there was in `free_seat`.
free_seat_unconditionally() {
  local name="$1" index="$2" cap="$3"
  if [ "$index" -ge 1 ] && [ "$index" -lt "$cap" ]; then
    docker exec "$redis_container" redis-cli -n "$index" FLUSHDB >/dev/null
  fi
  registry_cli HDEL "$REGISTRY_KEY" "$name" >/dev/null
  registry_cli HDEL "$SEEN_KEY" "$name" >/dev/null
  registry_cli HDEL "$ORIGIN_KEY" "$name" >/dev/null
}

cmd_release() {
  [ -n "$seat" ] || usage
  local cap raw index seen_at
  cap="$(effective_capacity)"
  raw="$(registry_cli HGET "$REGISTRY_KEY" "$seat")"
  if [ -z "$raw" ]; then
    # Exit 5, not 0 (phaze-robzi.5): a name that resolves to no allocated seat is not a release,
    # and the observed failure was exactly a wrong-guessed name -- hyphen vs. underscore -- read as
    # success because this used to return 0. Point at the two non-destructive ways to find the real
    # identifier or free what is actually stale, same as the exhaustion message does.
    echo "🟡 '${seat}' holds no Redis logical DB; nothing was released." >&2
    echo "   If this name was a guess, \`just test-db-seats\` lists every real identifier and the" >&2
    echo "   evidence behind it. \`just test-db-reclaim\` (dry run) / \`just test-db-reclaim --apply\`" >&2
    echo "   frees whatever is genuinely stale by reading liveness, not by trusting a name." >&2
    exit 5
  fi

  # Sanitize exactly as classify_seats does, and for the same reason (phaze-nbfuc). The registry
  # value is data, and release used to hand it straight to `seat_is_redis_live`, which greps for it
  # -- so a value of `.*` was read as a REGEX, matched every `CLIENT LIST` line, and the seat
  # reported permanently in use and could not be released without --force. `raw` stays byte-exact
  # for the compare-and-delete; `index` is the sanitized number the liveness check and FLUSHDB use,
  # and it is 0 when the value is not an index, because 0 is the registry's own connection and is
  # never tested for liveness.
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    index="$raw"
  else
    index=0
    echo "⚠️  '${seat}' has a corrupt registry value '${raw}', which is not an index. No Redis logical" >&2
    echo "    DB can correspond to it, so there is nothing to clear; releasing the entry itself." >&2
  fi
  seen_at="$(registry_cli HGET "$SEEN_KEY" "$seat")"

  # What the success line calls the thing that was freed. A corrupt value has no logical DB to name,
  # and reporting the sanitized 0 would name the registry's own database.
  local freed_what="Redis logical DB ${index}"
  [ "$index" -ge 1 ] || freed_what="corrupt registry entry '${raw}'"

  if [ "$force" -eq 1 ]; then
    # Before the free, not after: releasing the top seat must not lose the record that its index
    # was handed out, or the next allocation recycles it instead of taking a fresh one
    # (phaze-08sww). --force always reaches the free below, so raising here is never observed on a
    # refusal path -- there is no refusal path once --force is given.
    raise_highwater "$cap"
    free_seat_unconditionally "$seat" "$index" "$cap"
    echo "✅ released ${freed_what} from '${seat}' (keys cleared, index free for the next seat)"
    return 0
  fi

  local redis_live
  redis_live="$(live_redis_indices)"
  collect_postgres_evidence
  # `-ge 1` for the same reason free_seat has it: index 0 is the registry's own connection and is
  # always "live", so a sanitized-to-0 corrupt value must never reach the L1 check.
  if [ "$index" -ge 1 ] && seat_is_redis_live "$index" "$redis_live"; then
    echo "❌ Refusing to release '${seat}': a client is connected to Redis logical DB ${index} right now." >&2
    echo "   Releasing it would clear keys a running suite is using. Wait for it, or --force." >&2
    exit 4
  fi
  if [ "$pg_evidence" = "unavailable" ]; then
    # Same rule as the sweep: a named container that will not answer leaves L2 unknown, and
    # unknown is never read as free (phaze-gmkua).
    echo "❌ Refusing to release '${seat}': Postgres container '${pg_container}' did not answer, so a" >&2
    echo "   suite running in this seat would be invisible. Start the harness (\`just test-db\`), or --force." >&2
    exit 4
  fi
  if [ "$pg_evidence" = "obtained" ] && seat_is_postgres_live "$seat" "$pg_live"; then
    echo "❌ Refusing to release '${seat}': a client backend is connected to its Postgres database." >&2
    echo "   pytest holds that connection for the whole session, so a suite is running in this seat." >&2
    echo "   Wait for it to finish, or --force." >&2
    exit 4
  fi

  # Conditional even here: the checks above are a snapshot too, just a much fresher one. The
  # compare-and-delete gets `raw`, not `index` -- it has to match the registry byte for byte, and a
  # corrupt value sanitized to 0 would never match itself.
  # `free_seat` raises the high-water mark itself, atomically, inside FREE_SEAT_LUA, immediately
  # before the HDELs it performs on success (see that script's comment) -- so a refusal here, exactly
  # like a refusal from L1/L2 above or reclaim's dry run (`[ "$apply" -ne 1 ] || raise_highwater
  # "$cap"` below), leaves the registry untouched. There is deliberately no separate raise_highwater
  # call on this path (phaze-4xsbe): a second, non-atomic EVAL here would reopen the exact window
  # this fix closes -- refuse first, mutate never.
  # drop_pg=0, explicitly: `release` -- force or not -- never drops a Postgres database. Only
  # `cmd_reclaim`'s `--apply` loop passes 1 (phaze-robzi.1's operator-approved contract change is
  # scoped to reclaim alone).
  if ! free_seat "$seat" "$index" "$cap" "$seen_at" "$raw" 0; then
    echo "❌ Did not release '${seat}': ${free_seat_refusal}." >&2
    echo "   Nothing was cleared. Re-run \`just test-db-seats\` to see where the seat stands now." >&2
    exit 4
  fi
  echo "✅ released ${freed_what} from '${seat}' (keys cleared, index free for the next seat)"
}

# Shared by `list` and `reclaim`: classify every registry entry against the liveness rules above.
# Emits one TAB-separated record per seat:
#
#   name  index  raw  seen  verdict  reason
#
# `index` is sanitized (0 when the registry value is not an index, so arithmetic and the L1 check
# stay safe); `raw` is the byte-exact registry value and `seen` the lease stamp AS OBSERVED HERE.
# Those last two are what free_seat compares against before it destroys anything -- the record is a
# snapshot, and carrying what the snapshot saw is what lets the destruction notice it went stale
# (phaze-r311e). `reason` stays last because it is the only field that contains spaces.
#
# `raw` and `seen` are emitted as EMPTY_FIELD when they are empty, and the readers translate it
# back. TAB is an IFS *whitespace* character, so `read` collapses runs of tabs and strips leading
# and trailing ones: an empty field in the middle of a record silently shifts every field after it,
# which is how an unstamped seat first arrived at free_seat wearing another field's verdict. Lease
# stamps are digits and a registry value is never a bare '-', so the sentinel cannot collide.
#
readonly EMPTY_FIELD="-"

decode_field() {
  [ "$1" != "$EMPTY_FIELD" ] || return 0
  printf '%s' "$1"
}

# Reads `pg_evidence`/`pg_live`, which the CALLER must have filled with collect_postgres_evidence.
# Not collected here: every caller captures this function with `$(...)`, and a command substitution
# runs in a subshell, so anything assigned inside would be discarded on the way out.
classify_seats() {
  local cap="$1"
  local now redis_live lease_seconds
  now="$(server_now)"
  redis_live="$(live_redis_indices)"
  lease_seconds=$((lease_hours * 3600))

  # Each hash is fetched ONCE and looked up locally. A per-seat `HGET` would be three `docker exec`
  # round trips per seat; on a real 38-seat registry that turned `just test-db-seats` into a
  # multi-second wait, which is a poor property for the command whose whole job is to be the easy
  # thing to reach for. (Local awk rather than a bash 4 associative array: /bin/bash on macOS is
  # still 3.2, and this script has to run there.)
  local pairs seen_pairs origin_pairs name index seen_at origin_path age verdict reason
  pairs="$(registry_cli HGETALL "$REGISTRY_KEY" | paste - - 2>/dev/null || true)"
  [ -n "$pairs" ] || return 0
  seen_pairs="$(registry_cli HGETALL "$SEEN_KEY" | paste - - 2>/dev/null || true)"
  origin_pairs="$(registry_cli HGETALL "$ORIGIN_KEY" | paste - - 2>/dev/null || true)"

  while IFS=$'\t' read -r name index; do
    [ -n "$name" ] || continue
    seen_at="$(printf '%s\n' "$seen_pairs" | awk -F'\t' -v key="$name" '$1 == key { print $2 }')"
    origin_path="$(printf '%s\n' "$origin_pairs" | awk -F'\t' -v key="$name" '$1 == key { print $2 }')"
    verdict="keep"
    reason=""

    if ! [[ "$index" =~ ^[0-9]+$ ]]; then
      verdict="reclaim"
      reason="registry value '${index}' is not an index"
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "0" "${index:-$EMPTY_FIELD}" "${seen_at:-$EMPTY_FIELD}" "$verdict" "$reason"
      continue
    fi

    if seat_is_redis_live "$index" "$redis_live"; then
      reason="a Redis client is connected to DB ${index} (L1)"
    elif [ "$pg_evidence" = "obtained" ] && seat_is_postgres_live "$name" "$pg_live"; then
      reason="a Postgres backend is connected to phaze_${name}_test (L2)"
    elif [ "$index" -ge "$cap" ]; then
      # Ahead of the `unavailable` branch below on purpose (phaze-hrnww): O2's evidence is
      # structural, not observational -- an index past the cap has no logical DB for any client to
      # `SELECT`, so Postgres being unreachable tells this branch nothing it needs. Reporting it
      # "in-use" anyway (the pre-fix ordering) hid exactly the wedged 68/73/74/80-shaped entries an
      # operator running `just test-db-seats` with Postgres down is most likely hunting for. This is
      # `list`-only reporting: `reclaim --apply` still refuses outright on unavailable L2 evidence
      # before it ever reaches classify_seats (see `require_postgres_evidence`), so nothing here
      # weakens that refusal.
      verdict="reclaim"
      reason="index ${index} is past the ${cap}-database cap, so no client can be using it (O2)"
    elif [ "$pg_evidence" = "unavailable" ]; then
      # A container was named and did not answer. Its silence says nothing about this seat, so it
      # must not fall through to O1/L3 and be read as absence of life (phaze-gmkua defect B). This
      # branch is only reachable from `list`; `reclaim` refuses outright before classifying.
      reason="Postgres container '${pg_container}' did not answer, so L2 is unknown for this seat and unknown is never read as free"
    elif [ -n "$origin_path" ] && [ ! -d "$origin_path" ]; then
      verdict="reclaim"
      reason="origin worktree ${origin_path} no longer exists (O1)"
    elif [ -z "$seen_at" ]; then
      # Allocated before phaze-68wky added lease stamps, so its age is genuinely unknown -- and
      # inventing an age for it would be exactly the guessed-at liveness test this design refuses.
      # A sweep leaves these alone unless the operator asks for them with --include-unstamped.
      verdict="unstamped"
      reason="predates lease stamping, so its age is unknown; re-running \`just test-db-for\` in that seat stamps it"
    else
      age=$((now - seen_at))
      if [ "$age" -gt "$lease_seconds" ]; then
        verdict="reclaim"
        reason="lease expired: last claimed $((age / 3600))h ago, over the ${lease_hours}h lease (L3)"
      else
        reason="lease live: last claimed $((age / 3600))h ago, within the ${lease_hours}h lease (L3)"
      fi
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$index" "$index" "${seen_at:-$EMPTY_FIELD}" "$verdict" "$reason"
  done <<<"$pairs"
}

require_postgres_evidence() {
  if [ -n "$pg_container" ]; then
    if docker exec "$pg_container" psql -U phaze -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
      return 0
    fi
    echo "❌ Postgres container '${pg_container}' is not reachable, so a running suite in a seat would be invisible." >&2
  else
    echo "❌ No --pg-container given, so a running suite in a seat would be invisible." >&2
  fi
  echo "   pytest holds a Postgres connection for its whole session and often none to Redis, so without" >&2
  echo "   that evidence a live seat looks idle and the sweep could clear a suite's keys mid-run." >&2
  echo "   Start the harness (\`just test-db\`), or pass --no-postgres-check to sweep on the remaining" >&2
  echo "   evidence alone -- only do that when you know no suite is running." >&2
  exit 1
}

cmd_list() {
  local cap records
  cap="$(effective_capacity)"
  collect_postgres_evidence
  if [ "$pg_evidence" = "unavailable" ]; then
    echo "⚠️  Postgres container '${pg_container}' did not answer, so the L2 evidence is missing." >&2
    echo "    Every seat is reported in-use below on that basis alone; unknown is not free." >&2
  fi
  records="$(classify_seats "$cap")"
  if [ -z "$records" ]; then
    echo "No Redis logical DBs are allocated on ${redis_container} (capacity ${cap}, index 0 reserved for the registry)."
    return 0
  fi
  echo "Redis seat allocations on ${redis_container} (capacity ${cap}, lease ${lease_hours}h):"
  echo ""
  printf '  %-6s  %-40s  %-9s  %s\n' "DB" "SEAT" "STATE" "EVIDENCE"
  local state
  while IFS=$'\t' read -r name index _raw _seen verdict reason; do
    [ -n "$name" ] || continue
    case "$verdict" in
      keep) state="in-use" ;;
      unstamped) state="unknown" ;;
      *) state="stale" ;;
    esac
    printf '  %-6s  %-40s  %-9s  %s\n' "$index" "$name" "$state" "$reason"
  done <<<"$records"
  echo ""
  echo "  in-use  left alone by \`just test-db-reclaim\`."
  echo "  stale   what \`just test-db-reclaim --apply\` frees."
  echo "  unknown pre-phaze-68wky allocations; left alone unless you add --include-unstamped."
}

cmd_reclaim() {
  [ -n "$capacity" ] || usage
  [ "$skip_postgres_check" -eq 1 ] || require_postgres_evidence

  local cap records freed=0 kept=0 unstamped=0 raced=0
  cap="$(effective_capacity)"
  collect_postgres_evidence

  # Report on the evidence that was actually OBTAINED, not on which flags were passed. The old
  # gate was `--no-postgres-check AND no --pg-container`, but the justfile recipe always passes
  # `--pg-container` -- so `just test-db-reclaim --no-postgres-check`, the only shape an operator
  # ever types, swept with L2 missing and said nothing at all (phaze-gmkua defect A).
  if [ "$skip_postgres_check" -eq 1 ]; then
    if [ "$pg_evidence" = "obtained" ]; then
      echo "ℹ️  --no-postgres-check: the refusal was waived, but '${pg_container}' answered, so the Postgres" >&2
      echo "    evidence (L2) was collected and used anyway. Seats with a live backend are still protected." >&2
    else
      echo "⚠️  --no-postgres-check: sweeping without the Postgres evidence (L2). A suite that is running" >&2
      echo "    but not currently touching Redis is invisible to this sweep." >&2
    fi
  elif [ "$pg_evidence" != "obtained" ]; then
    # require_postgres_evidence passed its probe, then the liveness query itself failed. Reading
    # that as "no seat has a backend attached" is exactly what defect B did.
    echo "❌ The Postgres liveness query against '${pg_container}' failed, so the L2 evidence this sweep" >&2
    echo "   requires is unavailable. Refusing rather than reading unknown as free." >&2
    echo "   Re-run once the harness is answering, or pass --no-postgres-check to sweep on the remaining" >&2
    echo "   evidence alone -- only do that when you know no suite is running." >&2
    exit 1
  fi

  records="$(classify_seats "$cap")"
  if [ -z "$records" ]; then
    echo "No Redis logical DBs are allocated on ${redis_container}; nothing to reclaim."
    return 0
  fi

  # A prediction for the messages below, not a gate: `free_seat` re-derives this FRESH per seat
  # (immediately before it would drop anything) and is the actual authority. This is `obtained`
  # precisely when `free_seat`'s own drop step will run at all -- see its comment -- so it is what
  # tells the dry run and the per-seat success line whether to mention dropping databases.
  local will_drop_databases=0
  [ "$pg_evidence" != "obtained" ] || will_drop_databases=1

  # Before the first entry is destroyed, not after: a sweep is the one moment that can erase the
  # evidence of which indices have been handed out, and an index freed by a sweep is precisely the
  # one a shell somewhere is most likely to still have exported (phaze-08sww). A dry run mutates
  # nothing, including this.
  [ "$apply" -ne 1 ] || raise_highwater "$cap"

  while IFS=$'\t' read -r name index raw seen verdict reason; do
    [ -n "$name" ] || continue
    if [ "$verdict" = "keep" ]; then
      kept=$((kept + 1))
      continue
    fi
    if [ "$verdict" = "unstamped" ] && [ "$include_unstamped" -ne 1 ]; then
      unstamped=$((unstamped + 1))
      continue
    fi
    if [ "$apply" -ne 1 ]; then
      freed=$((freed + 1))
      if [ "$will_drop_databases" -eq 1 ]; then
        echo "•  would reclaim DB ${index} from '${name}' (would also drop phaze_${name}_test, phaze_${name}_migrations_test) — ${reason}"
      else
        echo "•  would reclaim DB ${index} from '${name}' (its 2 Postgres databases would NOT be dropped: L2 evidence is ${pg_evidence}) — ${reason}"
      fi
      continue
    fi
    raw="$(decode_field "$raw")"
    seen="$(decode_field "$seen")"
    # The verdict above was reached before any of this loop's other seats were touched. free_seat
    # re-derives its own evidence and declines if the seat moved underneath the snapshot; a decline
    # counts as kept, because the seat is still held (phaze-r311e). `1` here is drop_pg: reclaim's
    # `--apply` is the only caller that ever asks free_seat to also drop the seat's own databases
    # (phaze-robzi.1) -- `release`'s call site above always passes 0.
    if free_seat "$name" "$index" "$cap" "$seen" "$raw" 1; then
      freed=$((freed + 1))
      if [ "$will_drop_databases" -eq 1 ]; then
        echo "✅ reclaimed DB ${index} from '${name}' (and dropped phaze_${name}_test, phaze_${name}_migrations_test) — ${reason}"
      else
        echo "✅ reclaimed DB ${index} from '${name}' — ${reason}"
      fi
    else
      raced=$((raced + 1))
      echo "🟥 left '${name}' alone: ${free_seat_refusal}. Nothing was cleared."
    fi
  done <<<"$records"

  echo ""
  if [ "$apply" -eq 1 ]; then
    if [ "$will_drop_databases" -eq 1 ]; then
      echo "🧹 reclaimed ${freed} seat(s) (Redis index freed and its own 2 Postgres databases dropped); left ${kept} in-use seat(s) alone."
    else
      echo "🧹 reclaimed ${freed} seat(s) (Redis index freed; Postgres databases NOT dropped -- L2 evidence is ${pg_evidence}); left ${kept} in-use seat(s) alone."
    fi
    echo "   Every other Postgres database, and both containers, were left alone."
    if [ "$raced" -gt 0 ]; then
      echo "${raced} seat(s) classified as stale came back to life during the sweep and were left alone."
    fi
  else
    echo "Dry run: ${freed} seat(s) would be reclaimed, ${kept} left alone. Re-run with --apply to free them."
  fi
  if [ "$unstamped" -gt 0 ]; then
    echo ""
    echo "${unstamped} seat(s) predate lease stamping (phaze-68wky) and were left alone: their age is unknown,"
    echo "and a worktree that still has PHAZE_REDIS_URL exported would keep writing to an index handed back"
    echo "underneath it. Each one stamps itself the next time its worktree runs \`just test-db-for\`. Add"
    echo "--include-unstamped to clear the backlog once you know those worktrees are gone."
  fi
}

case "$subcommand" in
  allocate) cmd_allocate ;;
  release) cmd_release ;;
  list) cmd_list ;;
  reclaim) cmd_reclaim ;;
  *) usage ;;
esac
