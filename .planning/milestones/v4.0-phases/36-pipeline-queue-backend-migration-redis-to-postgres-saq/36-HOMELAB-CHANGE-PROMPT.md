<!-- generated-by: gsd-executor -->
# Homelab Change Prompt — Phaze SAQ Queue Backend: Redis → Postgres

> **Paste the section below into the homelab repo agent.** It is a ready-to-apply
> change request for the homelab deployment of Phaze. This is the **migration-specific**
> portion only (Phase 36). A final env/secret consolidation pass may follow **after
> Phase 38** if the pipeline-control UI work adds further deployment variables — do not
> treat this as the last word on Phaze's compose/env surface.

---

## Context for the homelab agent

Phaze's SAQ task queue has moved its **broker** from Redis to **PostgreSQL**. As of this
release:

- **PostgreSQL is the queue broker.** SAQ enqueues/dequeues jobs from Postgres tables
  (`saq_jobs`, `saq_stats`, `saq_versions`) and uses `LISTEN`/`NOTIFY` for wakeups. This
  enables native per-job `priority` and `scheduled` job control (Postgres-only in SAQ),
  which is the substrate for the upcoming per-stage pause/priority work.
- **Redis is now cache-only.** It still runs and is still required — it backs LLM
  rate-limiting and the pipeline counters — but it is **no longer the queue broker**.
- A new secret-backed setting, **`PHAZE_QUEUE_URL`**, carries the Postgres broker DSN
  (a **raw libpq** `postgresql://…` string, **not** the SQLAlchemy `postgresql+asyncpg://`
  form). It is a credential-bearing secret and supports the `<VAR>_FILE` convention.
- **New network edge:** the file-server agents now open a psycopg3 connection to the
  control-host Postgres. They previously had **no** direct Postgres reachability (Phase 26
  D-25 deliberately kept agents Postgres-free at the network level). That boundary is now
  intentionally relaxed for the broker move.

Apply the changes below to the homelab Phaze deployment (control host + each file-server
agent host, `datum@nox` and `datum@lux`).

---

## 1. Add `PHAZE_QUEUE_URL` to BOTH the control and agent compose services

Add `PHAZE_QUEUE_URL` to the environment of **every** Phaze service that runs a SAQ queue —
the control-host `api` + `worker` (control role) **and** each file-server `worker` +
`watcher` (agent role).

- **Form:** a **raw libpq DSN** — `postgresql://<user>:<password>@<postgres-host>:5432/<db>`.
  Do **NOT** use the `postgresql+asyncpg://` dialect form here; psycopg3's connection pool
  cannot parse the `+driver` suffix. (Phaze normalizes an `+asyncpg`/`+psycopg` form if one
  slips through, but configure the libpq form directly.)
- **On agent hosts**, `<postgres-host>` must be the control-host's private LAN address/DNS —
  the agents reach Postgres over the network now (see §4).
- **Treat it as a secret** (it carries DB credentials). Use the `<VAR>_FILE` convention
  instead of inlining the password, mirroring how `DATABASE_URL` is already handled:

  ```yaml
  # control-host docker-compose (api + worker), and each agent compose (worker + watcher)
  secrets:
    phaze_queue_url:
      file: ./secrets/phaze_queue_url        # contents: postgresql://USER:PASSWORD@HOST:5432/DB
  services:
    worker:
      secrets: [phaze_queue_url]
      environment:
        PHAZE_QUEUE_URL_FILE: /run/secrets/phaze_queue_url
  ```

  The direct `PHAZE_QUEUE_URL` env var wins if both are set; the file's trailing newline is
  stripped; a missing/unreadable `_FILE` path fails fast at startup.

> Placeholders only — never commit a real password into the compose file or the secret
> file's tracked example. Use the existing homelab secret-management path (SOPS / mounted
> Docker secret) you already use for `DATABASE_URL`.

---

## 2. Image dependency swap: `saq[redis]` → `saq[postgres]` (rebuild required)

The Phaze image's dependency changed from `saq[redis]` to `saq[postgres]`, which pulls in
**psycopg3** (`psycopg` + `psycopg-pool`). `redis` remains an explicit dependency (the cache
plane still uses `redis.asyncio` directly).

- **Rebuild / re-pull** the Phaze image so the new dependency set is present. A stale image
  with only `saq[redis]` will fail to construct the Postgres queue.
- **libpq-on-slim-base verification:** psycopg3 ships binary wheels (`psycopg[binary]`), but
  confirm the runtime image can actually load `libpq` on the `python:3.14-slim` base. Mirror
  the Phase-30 essentia apt-layer lesson: if `import psycopg` fails at runtime for a missing
  `libpq.so`, add the `libpq5` apt package to the image (or ensure the binary wheel is the
  resolved variant). Verify with a quick `python -c "import psycopg; print(psycopg.__version__)"`
  inside the built image before rolling it out.

---

## 3. First-boot auto-DDL (`saq_jobs` / `saq_stats` / `saq_versions`) + DB-role grants

On its first `connect()`, the PostgresQueue runs `init_db()`, which takes a
`pg_try_advisory_lock` and then idempotently runs `CREATE TABLE IF NOT EXISTS` for
**`saq_jobs`**, **`saq_stats`**, and **`saq_versions`** (plus their indexes). There is **no
Alembic migration** for these — SAQ owns its own schema, guarded by the advisory lock so
concurrent boots don't collide.

**DB-role requirements** — the Postgres role in `PHAZE_QUEUE_URL` must be able to:

- `CREATE TABLE` / `CREATE INDEX` **in its own schema** (for the first-boot auto-DDL), and
- use `LISTEN` / `NOTIFY` (SAQ wakes consumers on channel `saq:<queue-name>`).

The homelab `phaze` role already owns its database, so this is satisfied by default. If you
run a least-privilege role instead, grant exactly the above and nothing broader — keep the
broker role scoped to its own schema (do not hand it cross-schema or superuser rights).

---

## 4. New firewall rule: agents → control-host Postgres:5432 (relaxes D-25)

The agents (`datum@nox`, `datum@lux`) must now reach the **control-host Postgres on TCP
5432**. This is a new network edge — Phase 26's D-25 boundary kept agents Postgres-free, and
that is intentionally relaxed here for the broker move.

- **Open** the firewall path agent-host → control-host:5432 on the private LAN only. Postgres
  must **not** be exposed to the public internet.
- **Optional production credential guard:** Phaze does **not** yet enforce a
  production-credential guard on `PHAZE_QUEUE_URL` (analogous to the existing
  `_enforce_redis_password_in_production` guard for Redis). If you want the same belt-and-
  suspenders posture, note it as a follow-up — for now, the protection is the LAN-scoped
  firewall + a non-trivial DB password in the secret.

---

## 5. Connection budget vs Postgres `max_connections`

Each PostgresQueue opens its **own** psycopg3 pool, *separate* from the SQLAlchemy/asyncpg
engine pool. Phaze ships conservative per-queue sizes:

| Pool | `min` / `max` | Where |
|------|---------------|-------|
| Controller queue | **2 / 8** | control-host `api` + `worker` |
| Agent consume queue + per-agent router/monitor queues | **1 / 4** each | per agent; the API also holds one per non-revoked agent for the `/saq` monitor mount |
| SQLAlchemy engine pools | (existing) | control `worker` + `api`, unchanged |

**Budget to stay under:**

```
total ≈ (controller pool) + (Σ per-agent pools, incl. /saq monitor) + (SQLAlchemy engine pools)
        ≤ Postgres max_connections
```

**Action:** confirm the homelab Postgres `max_connections` (commonly `100`). With two agents
the SAQ pools alone are well within budget, but verify the sum of SAQ pools + the SQLAlchemy
engine pools fits, and raise `max_connections` (or lower pool maxes) if you add more agents.

---

## 6. Redis stays — as cache only

Keep the Redis service running. It is still required for:

- LLM rate-limiting (`generate_proposals`), and
- the pipeline counters (the `cache_redis` handle / `ctx["redis"]`).

Do **not** remove Redis, its password (`REDIS_PASSWORD`), or its LAN binding
(`REDIS_BIND_IP`). The only change is conceptual: it is no longer the SAQ broker. The
existing `PHAZE_REDIS_URL` stays exactly as it is.

---

## 7. Cutover / redeploy ordering

No job-data migration is needed. In-flight jobs that were sitting on the **Redis** broker at
cutover may be dropped — that is acceptable: Phase-32 boot re-enqueue + the cron self-heal
rebuild `saq_jobs` from **DB-truth** (the `DISCOVERED`/stage state of files), so work is not
lost, only re-derived.

**Sequence:**

1. **Roll out the new image + env to the CONTROL host first.** On first boot the control
   worker's PostgresQueue creates `saq_jobs`/`saq_stats`/`saq_versions` (auto-DDL, §3) and the
   boot re-enqueue self-heals the queue from DB-truth.
2. **Then redeploy each agent host** (`datum@nox`, `datum@lux`) with the new image + the
   `PHAZE_QUEUE_URL` env (§1) and confirmed Postgres reachability (§4).
3. Verify the `/saq` monitor (control-host `phaze-api` at `/saq`) shows the Postgres-backed
   queues, and that an enqueue→dequeue smoke (e.g. trigger an analysis) lands and drains on
   the new broker.

---

## Done-when checklist

- [ ] `PHAZE_QUEUE_URL` (libpq form, `<VAR>_FILE` secret) set on control `api`+`worker` AND each agent `worker`+`watcher`
- [ ] Image rebuilt/re-pulled with `saq[postgres]`; `import psycopg` verified on the slim base (libpq present)
- [ ] DB role can `CREATE TABLE`/`CREATE INDEX` in its schema and use `LISTEN`/`NOTIFY`; `saq_jobs`/`saq_stats`/`saq_versions` created on first boot
- [ ] Firewall opened agent-host → control-host:5432 (LAN only); Postgres not public
- [ ] Connection budget confirmed against Postgres `max_connections`
- [ ] Redis still running as cache-only (broker role removed, password + binding unchanged)
- [ ] Control host redeployed first, then agents; `/saq` + enqueue→dequeue smoke pass

---

*Migration-specific homelab change for Phase 36 (Redis→Postgres SAQ broker). Final
env/secret consolidation may follow after Phase 38.*
