#!/usr/bin/env bash
# Re-hash the test-harness role's password with ONE SCRAM iteration, so each fresh test connection
# stops paying ~12 ms of client-side key derivation (phaze-1dc8g).
#
# THE COST THIS REMOVES
#
# The test engine uses NullPool, so every DB test opens a fresh asyncpg connection -- ~3,500 per
# full suite (phaze-6qhh2's SPIKE RESULT). Measured 2026-09-24 from the macOS host against the
# 5433 harness: a connect takes a median ~17-18 ms, of which ~12 ms is the CLIENT between the
# server's SASL-continue and its own SASL-final. asyncpg 0.31 derives the SCRAM salted password as
# a Python-level loop of `iterations` hmac.new() calls (asyncpg/protocol/scram.pyx,
# `_generate_salted_password`), and the server's default `scram_iterations` is 4096. Re-hashing the
# role with `scram_iterations = 1` took the same connect to a median ~5 ms on the same database.
# The iteration count lives in the stored verifier, so it is a property of the ROLE, not of any
# connection option the client could send.
#
# WHY NOT REUSE CONNECTIONS INSTEAD: see `async_engine` in tests/conftest.py.
#
# WHAT THIS CAN TOUCH, AND WHY THAT IS SAFE
#
# It reaches Postgres ONLY through `docker exec` into the named container, over its trust-auth unix
# socket -- it takes no host, port or DSN, so it is structurally unable to reach the developer's own
# 5432 database or anything in production. It additionally refuses any container whose environment
# is not the ephemeral harness's (POSTGRES_DB=phaze_test, POSTGRES_USER=phaze,
# POSTGRES_PASSWORD=phaze): re-setting the password to `phaze` is a no-op only where `phaze` already
# IS the password, and anywhere else it would be a credential change.
#
# It is idempotent (a role already at one iteration is left alone) and restarts nothing.
# `ALTER ROLE ... PASSWORD` keeps the password itself unchanged and does not touch existing
# sessions; only connections authenticated afterwards see the new verifier.
#
# Callers: `just test-db` (the local 5433 harness) and .github/workflows/tests.yml (the job's own
# `services.postgres` container). Pinned by tests/shared/test_harness_scram_iterations.py.
#
# Usage: harness-scram-iterations.sh <container>
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <container>" >&2
  exit 2
fi

container="$1"
role="phaze"
want_iterations=1

env_dump="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container")"
for required in POSTGRES_DB=phaze_test POSTGRES_USER=phaze POSTGRES_PASSWORD=phaze; do
  if ! printf '%s\n' "$env_dump" | grep -qx "$required"; then
    echo "❌ ${container} is not the ephemeral test harness (missing ${required}); refusing to re-hash any password there." >&2
    exit 1
  fi
done

iterations() {
  docker exec "$container" psql -U "$role" -d postgres -tAc \
    "SELECT split_part(split_part(rolpassword, '\$', 2), ':', 1) FROM pg_authid WHERE rolname = '${role}'"
}

current="$(iterations)"
if [ "$current" = "$want_iterations" ]; then
  echo "🔑 ${container}: role ${role} already hashed with ${want_iterations} SCRAM iteration"
  exit 0
fi

docker exec "$container" psql -U "$role" -d postgres -v ON_ERROR_STOP=1 -q \
  -c "SET scram_iterations = ${want_iterations}" \
  -c "ALTER ROLE ${role} PASSWORD 'phaze'" >/dev/null

after="$(iterations)"
if [ "$after" != "$want_iterations" ]; then
  echo "❌ ${container}: role ${role} still reports ${after:-<none>} SCRAM iterations after the re-hash" >&2
  exit 1
fi
echo "🔑 ${container}: re-hashed role ${role} from ${current:-<none>} to ${want_iterations} SCRAM iteration (phaze-1dc8g)"
