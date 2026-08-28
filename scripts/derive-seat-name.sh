#!/usr/bin/env bash
# Derive the safe, collision-free Postgres/Redis identifier for one `just test-db-for` seat
# name (phaze-fmfk).
#
# A raw Postgres unquoted identifier can't contain a hyphen, but this repo's own worktree
# convention IS hyphenated (`wt/bead/issue/phaze-fq9h-1`), so naming a seat after the worktree --
# the obvious, natural thing to do -- used to blow up `test-db-for` with a raw
# `syntax error at or near "-"` and no hint at the cause or the fix.
#
# This script is the single place that decides what a raw seat name normalizes to, shared by the
# `test-db-for` justfile recipe and this module's own regression tests (which exercise it directly
# via subprocess, without needing the Postgres/Redis harness up).
#
# Two rules:
#
# 1. Reject anything that is not a lowercase, hyphen/underscore identifier up front, with a
#    message stating the rule, rather than letting a bad name reach `CREATE DATABASE` as a raw
#    Postgres parse error. Lowercase-only avoids Postgres silently folding an unquoted mixed-case
#    identifier to lowercase behind our back, which would otherwise desync the case a caller
#    prints/exports from the case Postgres actually created.
#
# 2. Normalize hyphens AND DOTS to underscores for the SQL identifier -- dots are accepted because
#    every beadhive epic child id contains one (`phaze-o8sie.3`), and CLAUDE.md's own isolation
#    instruction is `just test-db-for <bead-id>`, so the id must be usable verbatim. But '-', '_'
#    and '.' now all collapse onto the same text, so 'my-seat', 'my_seat' and 'my.seat' would
#    otherwise silently land on ONE seat: the exact shared-database defect this whole mechanism
#    exists to prevent (phaze-fwo7). Disambiguate by appending a short, DETERMINISTIC hash of the
#    raw, pre-normalization name. Deterministic (not random) is what keeps `test-db-for` idempotent
#    -- CLAUDE.md and the Redis allocation registry both rely on the same raw name always resolving
#    to the same seat, database, and Redis index; a fresh random suffix per invocation would mint a
#    new database and burn a new Redis index on every re-run.
#
# A THIRD rule, easy to miss because Postgres never reports it as an error: unquoted identifiers
# are capped at 63 bytes (NAMEDATALEN-1) and Postgres SILENTLY TRUNCATES anything longer instead
# of refusing it. Left unbudgeted, the 9 characters this script's own disambiguating suffix adds
# make that MORE likely to bite than before this script existed: two long seat names that agree
# on their first 63 characters land on one database -- the exact collision the hash exists to
# prevent, reintroduced at the tail end. The three names this script's output feeds
# (`phaze_<name>_test`, `phaze_<name>_migrations_test`, and the Redis registry field, which has
# no such limit) are not the same length -- `_migrations_test` (16 chars) overflows before `_test`
# (5 chars) does, so an unbudgeted long name could plausibly get a working primary database and a
# silently-shared migrations database. Budget backwards from the LONGEST of the three consumers
# (see below), and truncate only the normalized part, never the hash -- the hash is what carries
# the uniqueness, so trimming it would defeat the point of adding it.
#
# Usage: derive-seat-name.sh <raw-seat-name>
# On success: prints the derived identifier (e.g. "my_seat_a1b2c3d4") to stdout, nothing else.
# On failure: prints a message naming the constraint to stderr, exits 1.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <raw-seat-name>" >&2
  exit 2
fi

raw_name="$1"

if ! [[ "$raw_name" =~ ^[a-z][a-z0-9_.-]*$ ]]; then
  echo "" >&2
  echo "❌ invalid seat name '${raw_name}': must be lowercase, start with a letter, and contain" >&2
  echo "   only letters, digits, '-', '_', and '.' (e.g. 'laqf', 'review-polite', 'phaze-fq9h-1'," >&2
  echo "   'phaze-o8sie.3')." >&2
  exit 1
fi

# Hash the RAW (untruncated) name -- the uniqueness a long name needs comes from the full name,
# not from whatever prefix survives truncation below.
if command -v sha256sum >/dev/null 2>&1; then
  name_hash="$(printf '%s' "$raw_name" | sha256sum | cut -c1-8)"
elif command -v shasum >/dev/null 2>&1; then
  name_hash="$(printf '%s' "$raw_name" | shasum -a 256 | cut -c1-8)"
else
  echo "❌ neither sha256sum nor shasum available to derive the seat discriminator" >&2
  exit 1
fi

# Longest consumer of this script's output: `phaze_<derived>_migrations_test`. `<derived>` is
# `<normalized>_<hash>`, so the normalized part's budget is the 63-byte Postgres cap minus the
# fixed-length prefix, suffix, hash, and the "_" joining normalized and hash.
postgres_identifier_max=63
db_prefix="phaze_"
longest_db_suffix="_migrations_test"
max_normalized_len=$((postgres_identifier_max - ${#db_prefix} - ${#longest_db_suffix} - ${#name_hash} - 1))

normalized="${raw_name//[-.]/_}"
normalized="${normalized:0:max_normalized_len}"

printf '%s_%s\n' "$normalized" "$name_hash"
