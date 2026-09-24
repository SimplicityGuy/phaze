# Postgres JIT — what it costs and buys phaze, measured (phaze-i252o)

**Status:** measured 2026-09-23 (§§1–9). **Decided 2026-09-24: `jit` stays ON** (§10), by operator
decision on bead `phaze-i252o`. That decision supersedes the operator's earlier "App-wide jit=off
(Recommended)" decision, made the same day. No configuration or code changed. §6's recommendation and mechanisms
are kept as the record of what was proposed. §10.3 shows that both of §6's startup-parameter
mechanisms fail through production's connection pooler.

**Scope, as the operator set it.** Question as put: *"phaze-i252o (Postgres JIT costs 83 of ~298 ms
SQL per /s/analyze render) can't be implemented until you choose its scope. Which?"* Answer as given
(selected option label): *"Measure more first"*. Date: 2026-09-23. Durable record: bead comment on
`phaze-i252o`.

## 1. Question

`phaze-y0upq` measured on host-prod (PostgreSQL 18.6, `jit = on`, `jit_above_cost = 100000`, 25,866
files) that JIT compile time was 83.0 ms of ~298 ms of summed SQL per `/s/analyze` render. Before
choosing a fix scope, three things had to be measured rather than assumed:

1. Which of phaze's queries, admin **and** worker, cross `jit_above_cost`?
2. For each one, what does it cost with JIT on vs off, and how much of that is compilation?
3. Does **any** query benefit from JIT? If one does, app-wide `jit = off` is a regression for it.

## 2. Method

Everything ran locally against a **prod-shaped synthetic database** in this seat's own database on the
shared test Postgres (PostgreSQL 18.6, same minor as host-prod). Nothing touched host-prod.

- **Dataset.** `phaze-y0upq`'s seed reused unchanged: 25,866 `files` (11,428 media + 14,438
  companions), 11,428 `metadata`, 7,127 `analysis` (5,733 complete / 4 failed / 1,390 partial), 8,916
  `cloud_job`, 3,205 `scheduling_ledger`, 620 `saq_jobs`. Added: **315,315 `analysis_window` rows**
  (55 per completed analysis; `phaze-zaf2l` measured 316,159 on host-prod). Schema from
  `alembic upgrade head` on this checkout. The database was dropped afterwards.
- **Planner calibration.** Postgres defaults throughout (`work_mem` 4 MB, `jit_inline_above_cost` and
  `jit_optimize_above_cost` 500,000, `plan_cache_mode` auto), except `random_page_cost = 1.1`. That is
  the value `phaze-y0upq` seeded with, and at 1.1 the local JIT profile matches what host-prod
  reported: emission-only JIT of ~14–20 ms on the stage aggregates. At the stock default of 4.0 the
  same statements cost 2.4–3.4× more, three of the four cross 500,000, and JIT goes to 265–291 ms
  per statement (§5), which host-prod did **not** report. **Host-prod's actual `random_page_cost` is
  unverified** (§7 gives the read-only query). `work_mem` 64 MB changed no plan cost for the five admin statements
  tested.
- **Admin coverage.** Every statement issued by one render of `/s/analyze`, `/pipeline/stats`, `/`,
  `/s/summary`, `/s/metadata`, `/pipeline/tracklist-drain-status` and `/pipeline/files` (plain and
  `?stage=analyze&bucket=failed`), captured in-process with a SQLAlchemy `before_cursor_execute`
  hook.
- **Worker coverage: the whole test suite.** The full suite (9,123 passed, 6 skipped) ran with the same
  hook, recording every distinct statement text with its first parameter tuple: **5,863 distinct
  statements**, 1,198 of them explainable (`SELECT`/`WITH`/`INSERT`/`UPDATE`/`DELETE`). 1,060 were
  planned on the perf database; the 138 that could not be were `executemany` `INSERT … VALUES`
  batches, one per-row `UPDATE … WHERE id =`, and statements that tests aim at deliberately missing
  tables. None of those shapes can reach a cost of 100,000.
- **Measurement per statement.** Plain `EXPLAIN` for cost and whether JIT was planned; `EXPLAIN
  (ANALYZE)` ×3 per variant for execution time and the JIT timing block; the bare statement through
  asyncpg (the app's driver) on an **uncached** connection, 2 warm-up + 7 timed, with variants
  interleaved, reporting the median. Variants: `off`; `on` (deployed thresholds); `forced`
  (`jit_above_cost = 0`); `full` (all three thresholds 0). Every statement ran in a transaction that
  was rolled back, including the `DELETE`s.

## 3. Which statements cross `jit_above_cost`

Of the 1,060 planned statements, **14 cross 100,000** at `random_page_cost = 1.1` (**17** at 4.0).
**None cross 500,000 at 1.1; 14 do at 4.0.** 999 cost under 1,000. They fall into four groups:

| Group | Where | Cost @1.1 | Cost @4.0 |
|---|---|---|---|
| Stage count / status-bucket aggregates | `/pipeline/stats`, `/s/analyze`, `/`, `/s/summary` (4 statements per render) | 148,988 – 317,574 | 480,661 – 1,067,810 |
| Files list with the stage `CASE` column, filtered or sorted | `/pipeline/files?stage=…&bucket=…`, sort by stage | 167,945 – 427,009 | 749,732 – 1,816,185 |
| **Ledger reaper `DELETE`** (worker) | `reap_resolved_ledger_rows`, controller cron `*/5` | 193,708 – 327,346 | 646,799 – 1,284,672 |
| **Cloud drain candidates** (worker) and tracklist candidate queue | `get_cloud_staging_candidates` via `stage_cloud_window`; `/pipeline/tracklist-drain-status` | 32,043 – 37,705 (**below**) | 103,168 – 173,502 (**above**) |

The unfiltered `/pipeline/files` page does not cross; the filtered and sorted variants do.

## 4. JIT on vs off, per statement (`random_page_cost = 1.1`)

Median wall time in ms, bare statement through asyncpg. "JIT" is the compile total from `EXPLAIN
(ANALYZE)` under the deployed thresholds.

| Statement | Cost | off | **on** | JIT | forced | full |
|---|---|---|---|---|---|---|
| Stage aggregate (counts #1) | 316,419 | 16.1 | **38.0** | 19.3 | 38.9 | 256.2 |
| Stage aggregate (counts #2) | 260,669 | 9.6 | **31.0** | 19.0 | 31.0 | 203.5 |
| Stage aggregate (buckets #1) | 199,254 | 10.1 | **29.8** | 19.5 | 30.1 | 187.5 |
| Stage aggregate (buckets #2) | 148,988 | 8.9 | **24.8** | 14.5 | 25.5 | 152.6 |
| Files list, orphaned lens | 427,009 | 65.9 | **122.3** | 53.8 | 130.2 | 660.9 |
| Files list, sort by stage | 226,987 | 51.3 | **112.9** | 58.9 | 112.9 | 662.9 |
| Files list, metadata failed | 224,786 | 14.0 | **66.9** | 54.2 | 70.2 | 565.1 |
| Files list, compact sort | 224,183 | 45.3 | **110.2** | 58.1 | 109.0 | 665.6 |
| Files list, failed-filter empty | 167,945 | 16.3 | **73.4** | 51.1 | 72.5 | 572.4 |
| **Ledger reaper `DELETE`, lane 1** | 327,346 | 20.7 | **56.1** | 27.0 | 57.0 | 290.8 |
| **Ledger reaper `DELETE`, lane 2** | 262,423 | 20.8 | **47.2** | 22.9 | 46.9 | 237.4 |
| **Ledger reaper `DELETE`, lane 3** | 193,708 | 21.0 | **50.4** | 23.9 | 49.6 | 317.6 |
| Migration 067 dominant-style backfill (315,315 `analysis_window` rows) | 61,715 | 138.0 | 141.2 | — | 186.4 | 432.2 |
| **Cloud drain candidates** (keyset page) | 37,705 | 24.2 | 24.4 | — | 34.2 | 135.2 |
| **Cloud drain candidates** (dispatch snapshot) | 37,657 | 26.5 | 26.0 | — | 34.7 | 130.7 |
| Tracklist candidate queue | 32,043 | 28.3 | 28.6 | — | 42.4 | 167.9 |

Two files-list rows are omitted: a repeat of the sort-by-stage statement (226,987; 55.4 off / 120.1
on) and the canonical-route variant (224,181; 14.2 off / 65.4 on).

**Per page.** One `/pipeline/stats` or `/s/analyze` render issues the four stage aggregates:
**71.2 ms** of JIT compilation and **+83.9 ms** of summed statement wall time. The host-prod figure
for `/s/analyze` was 83.0 ms, taken before `phaze-y0upq` landed. Rendered end to end in-process
(2 warm-up + 7 timed, three alternating rounds, `jit` toggled per database): `/pipeline/stats` mean
**181.3–197.3 ms on vs 133.5–138.1 ms off**; `/s/analyze` **248.3–294.5 vs 217.5–234.9 ms**. The
page-level saving is smaller than the summed statement delta because the stats fan-out runs its reads
concurrently.

## 5. Does anything benefit? No, and the cost has a cliff

- **No statement ran faster when JIT actually compiled it,** under any variant. That includes the
  heaviest row-volume statement in the captured population, migration 067's aggregate backfill over
  315,315 `analysis_window` rows: 138.0 ms off, 186.4 ms with JIT forced, 432.2 ms with full JIT. phaze's
  statements are OLTP-sized; runtime is index probes, not expression evaluation over millions of
  rows, which is the only thing JIT speeds up.
- **The worker pays too.** Each ledger-reaper `DELETE` goes from ~21 ms to 47–56 ms. The absolute cost
  is small (cron every 5 min), but it is pure overhead.
- **The 500,000 cliff.** Below 500,000, JIT only emits code: ~14–59 ms per statement. At or above it,
  inlining and optimization switch on and compilation alone takes **~140–700 ms per statement**
  (`full` column; also measured directly at `random_page_cost = 4.0`, where three of the four stage
  aggregates compiled for 265.0, 270.8 and 291.0 ms against 10.6–16.9 ms with JIT off). A render of
  `/pipeline/stats` would then pay about +790 ms of summed statement time, every 5 s, in every visible
  admin tab.
- **What moves a statement across the cliff.** The largest cost today at 1.1 is 427,009 (files list)
  and the largest stage aggregate is 317,574. *Inference, not measured:* these plans are dominated by
  per-row correlated probes, so their cost grows roughly linearly with the rows they range over (media
  files for the stage aggregates). About 1.2× growth takes the files list over 500,000, and about 1.6×
  takes the stage aggregates over. A `random_page_cost` at the stock 4.0 crosses it with no growth at
  all (measured, §3).
- **The drain queue is on the edge.** At 1.1 the cloud drain candidate select costs ~37,700 and never
  JITs. At 4.0 it costs ~173,500 and would.

## 6. Recommendation (implementer's, `phaze-i252o`, 2026-09-23)

**App-wide `jit = off`.** Reasons, all from §3–§5:

1. **No measured beneficiary.** Per-query scoping only pays off if some query needs JIT. None of the
   1,060 planned statements does, including the worker-side ones.
2. **Per-query scoping would already need three call sites today:** the dashboard stats fan-out and
   page session, the `/pipeline/files` list, and the ledger reaper. Two more are one planner setting
   away (drain candidates, tracklist candidate queue). Every new aggregate is a new site, and the
   failure mode is silent: a query that grows past 500,000 starts paying ~0.2–0.7 s with no error.
3. **The downside of app-wide off is bounded by §5:** no statement measured faster with JIT on.

> **Superseded 2026-09-24 (§10).** JIT stays on. Separately, §10.3 measured that neither
> startup-parameter mechanism below works through production's PgBouncer. Read it before reviving
> either.

Mechanisms, for the operator to choose between: asyncpg `server_settings={"jit": "off"}` in the
engine builder (versioned, covers api + worker, does not cover SAQ's own psycopg pool on `saq_jobs`);
or `command: ["postgres", "-c", "jit=off"]` on the compose `postgres` service (versioned, covers every
connection to that server). `SET LOCAL jit = off` does suppress JIT even for a cached prepared plan,
because the executor re-checks the setting. Measured: a statement prepared and first planned with
`jit = on` ran 15–17 ms on every `jit = off` execution, against 288–324 ms on the `jit = on` ones.
So per-query scoping is mechanically sound. The objection to it is coverage.

**Blast radius for the app-wide option (rule 4):** this changes the plan-execution path for every
statement on every phaze connection. By measurement, 14 of 1,060 distinct planned statements are
affected at the calibrated setting (17 at 4.0), and all 14 were faster with JIT off. What could break:
a query that genuinely needs JIT. None exists in the suite's statement population. The test that
proves nothing regressed does not exist yet: the implementation bead needs one that pins `jit = off`
on the engine and re-measures at least the ledger reaper per the bead's acceptance criterion 3.

## 7. What host-prod must confirm (read-only, for the operator)

The conclusions above depend on host-prod's planner settings and current plan costs, which this spike
did not read. Run on host-prod:

```bash
ssh host-prod "docker exec -i postgres psql -U phaze -d phaze" < prod_jit_explain.sql > prod_jit_explain.log
```

The script is in Appendix A. It reads `pg_settings` for the JIT and planner knobs, reads live row
counts, then, inside `BEGIN READ ONLY … ROLLBACK`, runs `EXPLAIN (ANALYZE, SETTINGS)` with JIT on
and then off for the four stage aggregates, the filtered files list and the tracklist candidate queue.
The cloud drain select (`SELECT … FOR UPDATE`) and the three ledger-reaper `DELETE`s get **plain
`EXPLAIN (SETTINGS)` only**: it plans without executing and takes no row locks. The script was
dry-run end to end against the local 18.6 perf database with `ON_ERROR_STOP=1`, exit 0.

What to read out of it:

- the `random_page_cost` row, which confirms or refutes the §2 calibration;
- each statement's top-level `cost=…`, specifically whether any is ≥ 500,000, and each `JIT:` block's
  `Options: Inlining …, Optimization …` line;
- the `Execution Time` pairs, on vs off.

## 8. Limits

- **Synthetic data.** Plan costs track row counts and statistics, not content, and the seed matches
  host-prod's counts. It also reproduces `phaze-y0upq`'s missing `metadata` column statistics, with
  autovacuum held off on `metadata` and `stage_skip`. Local timings are relative evidence; host-prod
  absolute timings were not taken.
- **Suite parameters are synthetic.** Worker statements were planned with the first parameter tuple a
  test supplied, so custom-plan costs for parameter-sensitive predicates can differ from production's.
- **Not captured:** SAQ's own queries on `saq_jobs`, which go through SAQ's psycopg pool rather than
  SQLAlchemy (620 rows here, trivial plans), and any code path no test exercises.
- **Machine load.** Other seats' test suites were running on the same host during page timing. An
  earlier attempt with identical settings read a 562.7 ms mean for `/s/analyze`, then 274.9 ms minutes
  later, so only the alternating on/off rounds are reported.

## 9. The general form (CLAUDE.md rule 5)

*A cost threshold is a data-dependent trigger.* `jit_above_cost` and `jit_inline_above_cost` compare
**estimated** cost, and estimated cost grows with row counts and moves with planner settings. A
configuration that is harmless at today's corpus size can switch behaviour, with a 10× step, at a
size nobody chose, and nothing errors when it does. The same shape applies to any planner decision
gated on estimated cost (parallel query, hash vs nested loop). Where it is written down: here. It is
specific enough to the Postgres planner that no repo-wide rule is proposed.

## 10. Decision: `jit` stays on (`phaze-i252o`, 2026-09-24)

### 10.1 The decisions, in order

1. **Operator decision 2026-09-24: app-wide `jit = off`.** Question as put: *"phaze-i252o, Postgres
   JIT scope?"* Answer as given (selected option label): *"App-wide jit=off (Recommended)"*. Durable
   record: bead comment on `phaze-i252o`, 2026-09-24. **Superseded by decision 2 below.**
2. **Operator decision 2026-09-24: leave JIT on.** This came up while the first decision was being
   implemented, when the mechanism it was dispatched with turned out not to work through
   production's PgBouncer (§10.3). Question as put: *"phaze-i252o: you prescribed asyncpg
   server_settings + psycopg options="-c jit=off". The seat tested both against prod's PgBouncer
   config (edoburu/pgbouncer v1.25.2, pool_mode=session,
   ignore_startup_parameters=extra_float_digits,options). server_settings={jit:off} gets rejected
   ("unsupported startup parameter: jit"), which would take down every connection, and tests that
   connect directly to Postgres would still pass. options=-c jit=off is silently ignored (SHOW jit =
   on). Running SET jit=off on each new connection gives off for both drivers. Which mechanism?"*
   The options offered were SET on connect (Recommended), PgBouncer, server side, and Both. Answer as
   given (free text, verbatim): *"since there is no performance hit to keeping jit on, let's amend
   the ADR with that. leave it on and move to the next bead"*. Durable record: the operator-decision
   comment on bead `phaze-i252o`, 2026-09-24. **This decision supersedes decision 1.** There is no
   numbered ADR for JIT; this document is the decision record the answer refers to.

**Effect.** No configuration or code change: every phaze connection keeps the server's `jit`
setting, `on` with `jit_above_cost = 100000` as of the `phaze-y0upq` reading. The implementation of
decision 1 was written, tested and then dropped unmerged. No production before/after measurement is
owed for this bead.

### 10.2 The rationale, and the measurements beside it

The rationale is the operator's, quoted from the answer above: *"there is no performance hit to
keeping jit on"*. It is recorded here as the operator's reason, not as a result of §§3–5. Those
measurements are unchanged and read as follows:

- **Admin pages.** Per `/pipeline/stats` or `/s/analyze` render, the four stage aggregates cost
  71.2 ms of JIT compilation and +83.9 ms of summed statement wall time (§4). Host-prod's figure was
  83.0 ms. End to end in-process, `/pipeline/stats` rendered in 181.3–197.3 ms with JIT on against
  133.5–138.1 ms off, and `/s/analyze` in 248.3–294.5 ms against 217.5–234.9 ms.
- **Beneficiaries.** No statement ran faster when JIT compiled it (§5).
- **Planner setting.** These figures are at `random_page_cost = 1.1`, and host-prod's value is
  unverified (§2, §7).
- **The cliff.** At plan cost ≥ 500,000, compilation takes ~140–700 ms per statement (§5).
  - **Row growth (inferred):** about 1.6× row growth puts the stats page's stage aggregates past
    it, and about 1.2× does the same for the files list. This is inference, not measurement (§5).
  - **`random_page_cost = 4.0` (measured):** three of the four stage aggregates are already past it
    (§3). A `/pipeline/stats` render would then pay about +790 ms of summed statement time (§5).

A future bead that revisits this decision should start from §7's read-only host-prod confirmation.
That read has not been run.

### 10.3 Durable evidence: how a `jit` setting behaves through production's PgBouncer

Recorded for anyone who revisits this decision. Every phaze connection in production (api, workers,
the in-process migration, and every SAQ broker pool) reaches Postgres through PgBouncer. The
configuration measured here is image `edoburu/pgbouncer:v1.25.2-p0`, with
`pool_mode = session` and `ignore_startup_parameters = extra_float_digits,options`. Startup parameters
are handled by PgBouncer itself, not forwarded to Postgres. That configuration lives in the
deployment-configuration repository, not in this one. It was read from a local checkout of that
repository on 2026-09-24. The deployed file was not read, so check these three values against it
before relying on them.

Measured 2026-09-24 with that image and those two settings, locally, in front of a PostgreSQL 18.6
test database (asyncpg 0.31.0, psycopg 3.3.6, psycopg-pool 3.3.3, SQLAlchemy 2.0.54). Each cell is
`SHOW jit` read after connecting:

| mechanism | direct to Postgres | through PgBouncer 1.25.2 |
| --- | --- | --- |
| asyncpg `server_settings={"jit": "off"}` | `off` | **connection refused**: `asyncpg.exceptions.ProtocolViolationError: unsupported startup parameter: jit` |
| psycopg `options="-c jit=off"` | `off` | **`on`**: connects, and the setting is silently dropped |
| `SET jit = off` on each new connection (asyncpg and psycopg) | `off` | `off` |
| asyncpg `server_settings` with PgBouncer's `track_extra_parameters = IntervalStyle, jit` | not tested | connects, still **`on`**, in session and transaction pooling |

What that means for anyone who revives `jit = off`:

- **`server_settings` would have taken down every SQLAlchemy connection.** The api, the control
  worker and migrations would all fail at connect on the first deploy. The agent worker has no
  SQLAlchemy engine; its only Postgres connection is its SAQ broker pool, covered by the next point.
- **`options` would have shipped a no-op on every SAQ broker pool,** the agent worker's included. It
  raises no error and has no effect.
- **Direct-connection tests cannot tell either failure apart from success.** Every test in the suite
  connects straight to Postgres, so both broken mechanisms pass it (CLAUDE.md rule 3). A test for
  this has to connect through a real PgBouncer configured like production's.
- **`SET jit = off` on connect works only under session pooling.** A `SET` belongs to the server
  connection it ran on. Session pooling pins one server connection to a client for the client's
  whole life. Under transaction pooling, a connect-time `SET` lands on one server connection while
  later transactions run on others, where `jit` is still `on`, and nothing errors. The same
  measurement showed it: a transaction-pooled PgBouncer handed a later transaction a server
  connection that never ran the `SET`, and it read `on`. phaze's queue pool also needs session
  pooling, for SAQ's `LISTEN`/`NOTIFY`.
- **A server-side setting (`postgres -c jit=off`) affects more than phaze.** It is the one mechanism
  the pooler does not interfere with, but production's Postgres also serves other applications.

The implementation that was dropped did three things. It applied `SET jit = off` from a SQLAlchemy
pool `connect` event, run on the raw asyncpg connection so the pool's reset-on-return rollback could
not undo it. For SAQ's queue pools it used a psycopg_pool `configure` callback. It built alembic's
engine through the same code path. Its tests ran each connection factory both directly and through
a PgBouncer container that the test module started itself. Swapping in `server_settings` turned the
PgBouncer run red and left the direct run green.

**The general form (CLAUDE.md rule 5):** *a connection-level setting is only as verified as the
connection path it was tested through.* Anything a client sends at connect time (startup
parameters, TLS options, auth method) is interpreted by whatever terminates the connection, and in
production that is PgBouncer, not Postgres. It is recorded as a transferred-model instance in
`docs/design/0016-transferred-model-verification.md` §3.10: true straight to Postgres, false through
the pooler. It was caught before anything shipped.

## Appendix A — `prod_jit_explain.sql`

Literals are the constants the application binds (stage names, statuses, file types). The single
UUID and timestamp are the synthetic keyset cursor from `tests/analyze/tasks/test_drain_head_of_line.py`.

```sql
-- phaze-i252o: read-only JIT evidence. Nothing here writes: ANALYZE runs only on SELECTs, inside
-- BEGIN READ ONLY ... ROLLBACK; the DELETEs get plain EXPLAIN, which plans without executing.
SELECT name, setting, source FROM pg_settings WHERE name IN ('jit','jit_above_cost','jit_inline_above_cost',
  'jit_optimize_above_cost','random_page_cost','seq_page_cost','effective_cache_size','work_mem','plan_cache_mode') ORDER BY name;
SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE relname IN ('files','metadata','analysis','analysis_window','cloud_job','scheduling_ledger','stage_skip') ORDER BY relname;
BEGIN READ ONLY;
\echo ===== Q1 /pipeline/stats + /s/analyze stage aggregate -- jit on
EXPLAIN (ANALYZE, SETTINGS) SELECT anon_1.status, count(*) AS count_1 FROM (SELECT CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'metadata'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS status FROM files WHERE files.file_type IN ('mp3'::VARCHAR, 'm4a'::VARCHAR, 'ogg'::VARCHAR, 'flac'::VARCHAR, 'wav'::VARCHAR, 'aiff'::VARCHAR, 'wma'::VARCHAR, 'aac'::VARCHAR, 'opus'::VARCHAR, 'mp4'::VARCHAR, 'mkv'::VARCHAR, 'avi'::VARCHAR, 'webm'::VARCHAR, 'mov'::VARCHAR, 'wmv'::VARCHAR, 'flv'::VARCHAR)) AS anon_1 GROUP BY anon_1.status;
SET LOCAL jit = off;
\echo ===== Q1 /pipeline/stats + /s/analyze stage aggregate -- jit off
EXPLAIN (ANALYZE, SETTINGS) SELECT anon_1.status, count(*) AS count_1 FROM (SELECT CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'metadata'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS status FROM files WHERE files.file_type IN ('mp3'::VARCHAR, 'm4a'::VARCHAR, 'ogg'::VARCHAR, 'flac'::VARCHAR, 'wav'::VARCHAR, 'aiff'::VARCHAR, 'wma'::VARCHAR, 'aac'::VARCHAR, 'opus'::VARCHAR, 'mp4'::VARCHAR, 'mkv'::VARCHAR, 'avi'::VARCHAR, 'webm'::VARCHAR, 'mov'::VARCHAR, 'wmv'::VARCHAR, 'flv'::VARCHAR)) AS anon_1 GROUP BY anon_1.status;
SET LOCAL jit = on;
\echo ===== Q2 /pipeline/stats + /s/analyze stage aggregate -- jit on
EXPLAIN (ANALYZE, SETTINGS) SELECT anon_1.status, count(*) AS count_1 FROM (SELECT CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS status FROM files WHERE files.file_type IN ('mp3'::VARCHAR, 'm4a'::VARCHAR, 'ogg'::VARCHAR, 'flac'::VARCHAR, 'wav'::VARCHAR, 'aiff'::VARCHAR, 'wma'::VARCHAR, 'aac'::VARCHAR, 'opus'::VARCHAR, 'mp4'::VARCHAR, 'mkv'::VARCHAR, 'avi'::VARCHAR, 'webm'::VARCHAR, 'mov'::VARCHAR, 'wmv'::VARCHAR, 'flv'::VARCHAR)) AS anon_1 GROUP BY anon_1.status;
SET LOCAL jit = off;
\echo ===== Q2 /pipeline/stats + /s/analyze stage aggregate -- jit off
EXPLAIN (ANALYZE, SETTINGS) SELECT anon_1.status, count(*) AS count_1 FROM (SELECT CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS status FROM files WHERE files.file_type IN ('mp3'::VARCHAR, 'm4a'::VARCHAR, 'ogg'::VARCHAR, 'flac'::VARCHAR, 'wav'::VARCHAR, 'aiff'::VARCHAR, 'wma'::VARCHAR, 'aac'::VARCHAR, 'opus'::VARCHAR, 'mp4'::VARCHAR, 'mkv'::VARCHAR, 'avi'::VARCHAR, 'webm'::VARCHAR, 'mov'::VARCHAR, 'wmv'::VARCHAR, 'flv'::VARCHAR)) AS anon_1 GROUP BY anon_1.status;
SET LOCAL jit = on;
\echo ===== Q3 /pipeline/stats + /s/analyze stage aggregate -- jit on
EXPLAIN (ANALYZE, SETTINGS) SELECT count(*) AS count_1 FROM files WHERE files.file_type IN ('mp3'::VARCHAR, 'm4a'::VARCHAR, 'ogg'::VARCHAR, 'flac'::VARCHAR, 'wav'::VARCHAR, 'aiff'::VARCHAR, 'wma'::VARCHAR, 'aac'::VARCHAR, 'opus'::VARCHAR, 'mp4'::VARCHAR, 'mkv'::VARCHAR, 'avi'::VARCHAR, 'webm'::VARCHAR, 'mov'::VARCHAR, 'wmv'::VARCHAR, 'flv'::VARCHAR) AND (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) AND NOT ((EXISTS (SELECT saq_jobs.key FROM saq_jobs WHERE saq_jobs.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)) AND saq_jobs.status IN ('queued'::VARCHAR, 'active'::VARCHAR))) OR (EXISTS (SELECT cloud_job.id FROM cloud_job WHERE cloud_job.file_id = files.id AND cloud_job.status IN ('uploading'::VARCHAR, 'uploaded'::VARCHAR, 'submitted'::VARCHAR, 'running'::VARCHAR, 'awaiting'::VARCHAR)))) AND NOT ((EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) OR (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) OR (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)));
SET LOCAL jit = off;
\echo ===== Q3 /pipeline/stats + /s/analyze stage aggregate -- jit off
EXPLAIN (ANALYZE, SETTINGS) SELECT count(*) AS count_1 FROM files WHERE files.file_type IN ('mp3'::VARCHAR, 'm4a'::VARCHAR, 'ogg'::VARCHAR, 'flac'::VARCHAR, 'wav'::VARCHAR, 'aiff'::VARCHAR, 'wma'::VARCHAR, 'aac'::VARCHAR, 'opus'::VARCHAR, 'mp4'::VARCHAR, 'mkv'::VARCHAR, 'avi'::VARCHAR, 'webm'::VARCHAR, 'mov'::VARCHAR, 'wmv'::VARCHAR, 'flv'::VARCHAR) AND (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) AND NOT ((EXISTS (SELECT saq_jobs.key FROM saq_jobs WHERE saq_jobs.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)) AND saq_jobs.status IN ('queued'::VARCHAR, 'active'::VARCHAR))) OR (EXISTS (SELECT cloud_job.id FROM cloud_job WHERE cloud_job.file_id = files.id AND cloud_job.status IN ('uploading'::VARCHAR, 'uploaded'::VARCHAR, 'submitted'::VARCHAR, 'running'::VARCHAR, 'awaiting'::VARCHAR)))) AND NOT ((EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) OR (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) OR (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)));
SET LOCAL jit = on;
\echo ===== Q4 /pipeline/stats + /s/analyze stage aggregate -- jit on
EXPLAIN (ANALYZE, SETTINGS) SELECT count(*) AS count_1 FROM files WHERE files.file_type IN ('mp3'::VARCHAR, 'm4a'::VARCHAR, 'ogg'::VARCHAR, 'flac'::VARCHAR, 'wav'::VARCHAR, 'aiff'::VARCHAR, 'wma'::VARCHAR, 'aac'::VARCHAR, 'opus'::VARCHAR, 'mp4'::VARCHAR, 'mkv'::VARCHAR, 'avi'::VARCHAR, 'webm'::VARCHAR, 'mov'::VARCHAR, 'wmv'::VARCHAR, 'flv'::VARCHAR) AND (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)))) AND NOT (EXISTS (SELECT saq_jobs.key FROM saq_jobs WHERE saq_jobs.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)) AND saq_jobs.status IN ('queued'::VARCHAR, 'active'::VARCHAR))) AND NOT (((EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NULL)) OR (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'metadata'::VARCHAR)) OR (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NOT NULL))) AND NOT (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger, metadata WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)) AND metadata.file_id = files.id AND metadata.failed_at IS NOT NULL AND scheduling_ledger.enqueued_at > metadata.failed_at)));
SET LOCAL jit = off;
\echo ===== Q4 /pipeline/stats + /s/analyze stage aggregate -- jit off
EXPLAIN (ANALYZE, SETTINGS) SELECT count(*) AS count_1 FROM files WHERE files.file_type IN ('mp3'::VARCHAR, 'm4a'::VARCHAR, 'ogg'::VARCHAR, 'flac'::VARCHAR, 'wav'::VARCHAR, 'aiff'::VARCHAR, 'wma'::VARCHAR, 'aac'::VARCHAR, 'opus'::VARCHAR, 'mp4'::VARCHAR, 'mkv'::VARCHAR, 'avi'::VARCHAR, 'webm'::VARCHAR, 'mov'::VARCHAR, 'wmv'::VARCHAR, 'flv'::VARCHAR) AND (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)))) AND NOT (EXISTS (SELECT saq_jobs.key FROM saq_jobs WHERE saq_jobs.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)) AND saq_jobs.status IN ('queued'::VARCHAR, 'active'::VARCHAR))) AND NOT (((EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NULL)) OR (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'metadata'::VARCHAR)) OR (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NOT NULL))) AND NOT (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger, metadata WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)) AND metadata.file_id = files.id AND metadata.failed_at IS NOT NULL AND scheduling_ledger.enqueued_at > metadata.failed_at)));
SET LOCAL jit = on;
\echo ===== Q5 /pipeline/files?stage=analyze&bucket=failed list -- jit on
EXPLAIN (ANALYZE, SETTINGS) SELECT files.id, files.sha256_hash, files.original_path, files.original_filename, files.original_filename_repaired, files.current_path, files.file_type, files.file_size, files.batch_id, files.agent_id, files.created_at, files.updated_at, CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'metadata'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_1, CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_2, CASE WHEN false THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT proposals.id FROM proposals WHERE proposals.file_id = files.id)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT proposals.id FROM proposals WHERE proposals.file_id = files.id AND proposals.status = 'failed'::VARCHAR)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_3, CASE WHEN false THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT proposals.id FROM proposals WHERE proposals.file_id = files.id)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT proposals.id FROM proposals WHERE proposals.file_id = files.id AND proposals.status = 'failed'::VARCHAR)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_4, CASE WHEN false THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT execution_log.id FROM execution_log JOIN proposals ON execution_log.proposal_id = proposals.id WHERE proposals.file_id = files.id AND execution_log.status = 'completed'::VARCHAR)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT execution_log.id FROM execution_log JOIN proposals ON execution_log.proposal_id = proposals.id WHERE proposals.file_id = files.id AND execution_log.status = 'failed'::VARCHAR)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_5 FROM files WHERE CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END = 'failed'::VARCHAR ORDER BY files.current_path ASC, files.id LIMIT 51::INTEGER OFFSET 0::INTEGER;
SET LOCAL jit = off;
\echo ===== Q5 /pipeline/files?stage=analyze&bucket=failed list -- jit off
EXPLAIN (ANALYZE, SETTINGS) SELECT files.id, files.sha256_hash, files.original_path, files.original_filename, files.original_filename_repaired, files.current_path, files.file_type, files.file_size, files.batch_id, files.agent_id, files.created_at, files.updated_at, CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'metadata'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_1, CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_2, CASE WHEN false THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT proposals.id FROM proposals WHERE proposals.file_id = files.id)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT proposals.id FROM proposals WHERE proposals.file_id = files.id AND proposals.status = 'failed'::VARCHAR)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_3, CASE WHEN false THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT proposals.id FROM proposals WHERE proposals.file_id = files.id)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT proposals.id FROM proposals WHERE proposals.file_id = files.id AND proposals.status = 'failed'::VARCHAR)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_4, CASE WHEN false THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT execution_log.id FROM execution_log JOIN proposals ON execution_log.proposal_id = proposals.id WHERE proposals.file_id = files.id AND execution_log.status = 'completed'::VARCHAR)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT execution_log.id FROM execution_log JOIN proposals ON execution_log.proposal_id = proposals.id WHERE proposals.file_id = files.id AND execution_log.status = 'failed'::VARCHAR)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END AS anon_5 FROM files WHERE CASE WHEN (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) THEN 'in_flight'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) THEN 'done'::VARCHAR WHEN (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) THEN 'skipped'::VARCHAR WHEN (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)) THEN 'failed'::VARCHAR ELSE 'not_started'::VARCHAR END = 'failed'::VARCHAR ORDER BY files.current_path ASC, files.id LIMIT 51::INTEGER OFFSET 0::INTEGER;
SET LOCAL jit = on;
\echo ===== Q6 tracklist candidate queue (tracklist-drain-status) -- jit on
EXPLAIN (ANALYZE, SETTINGS) SELECT files.id, coalesce(files.original_filename_repaired, files.original_filename) AS filename, files.sha256_hash, files.original_path, files.file_type, files.file_size, metadata.duration, metadata.bitrate, metadata.track_number, metadata.artist, metadata.title, metadata.album, metadata.raw_tags, EXISTS (SELECT file_companions.id FROM file_companions JOIN files AS files_1 ON files_1.id = file_companions.companion_id WHERE file_companions.media_id = files.id AND files_1.file_type = 'cue'::VARCHAR) AS has_cue, EXISTS (SELECT tracklists.id FROM tracklists WHERE tracklists.file_id = files.id AND tracklists.status IN ('approved'::VARCHAR, 'pending'::VARCHAR)) AS has_tracklist FROM files LEFT OUTER JOIN metadata ON metadata.file_id = files.id WHERE files.file_type IN ('aac'::VARCHAR, 'aiff'::VARCHAR, 'avi'::VARCHAR, 'flac'::VARCHAR, 'flv'::VARCHAR, 'm4a'::VARCHAR, 'mkv'::VARCHAR, 'mov'::VARCHAR, 'mp3'::VARCHAR, 'mp4'::VARCHAR, 'ogg'::VARCHAR, 'opus'::VARCHAR, 'wav'::VARCHAR, 'webm'::VARCHAR, 'wma'::VARCHAR, 'wmv'::VARCHAR) ORDER BY files.id;
SET LOCAL jit = off;
\echo ===== Q6 tracklist candidate queue (tracklist-drain-status) -- jit off
EXPLAIN (ANALYZE, SETTINGS) SELECT files.id, coalesce(files.original_filename_repaired, files.original_filename) AS filename, files.sha256_hash, files.original_path, files.file_type, files.file_size, metadata.duration, metadata.bitrate, metadata.track_number, metadata.artist, metadata.title, metadata.album, metadata.raw_tags, EXISTS (SELECT file_companions.id FROM file_companions JOIN files AS files_1 ON files_1.id = file_companions.companion_id WHERE file_companions.media_id = files.id AND files_1.file_type = 'cue'::VARCHAR) AS has_cue, EXISTS (SELECT tracklists.id FROM tracklists WHERE tracklists.file_id = files.id AND tracklists.status IN ('approved'::VARCHAR, 'pending'::VARCHAR)) AS has_tracklist FROM files LEFT OUTER JOIN metadata ON metadata.file_id = files.id WHERE files.file_type IN ('aac'::VARCHAR, 'aiff'::VARCHAR, 'avi'::VARCHAR, 'flac'::VARCHAR, 'flv'::VARCHAR, 'm4a'::VARCHAR, 'mkv'::VARCHAR, 'mov'::VARCHAR, 'mp3'::VARCHAR, 'mp4'::VARCHAR, 'ogg'::VARCHAR, 'opus'::VARCHAR, 'wav'::VARCHAR, 'webm'::VARCHAR, 'wma'::VARCHAR, 'wmv'::VARCHAR) ORDER BY files.id;
SET LOCAL jit = on;
\echo ===== Q7 cloud drain candidates (get_cloud_staging_candidates / stage_cloud_window; SELECT ... FOR UPDATE) -- plain EXPLAIN only
EXPLAIN (SETTINGS) SELECT files.id, files.sha256_hash, files.original_path, files.original_filename, files.original_filename_repaired, files.current_path, files.file_type, files.file_size, files.batch_id, files.agent_id, files.created_at, files.updated_at, cloud_job.updated_at AS updated_at_1 FROM files JOIN cloud_job ON cloud_job.file_id = files.id WHERE cloud_job.status = 'awaiting'::VARCHAR AND NOT (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) AND NOT ((EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) OR (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) OR (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL))) AND (files.created_at, files.id) > ('2026-07-27T03:00:02+00:00'::TIMESTAMP WITH TIME ZONE, '9da2a86a-6152-4afc-be29-5eb7224ae34a'::UUID) ORDER BY files.created_at ASC, files.id ASC LIMIT 100::INTEGER FOR UPDATE OF cloud_job SKIP LOCKED;
\echo ===== Q8 ledger reaper DELETE lane 1 (reap_resolved_ledger_rows) -- plain EXPLAIN only, never ANALYZE
EXPLAIN (SETTINGS) DELETE FROM scheduling_ledger WHERE scheduling_ledger.key IN (SELECT concat('push_file:'::VARCHAR, CAST(files.id AS VARCHAR)) AS concat_1 FROM files WHERE (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('push_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) AND NOT ((EXISTS (SELECT saq_jobs.key FROM saq_jobs WHERE saq_jobs.key = concat('push_file:'::VARCHAR, CAST(files.id AS VARCHAR)) AND saq_jobs.status IN ('queued'::VARCHAR, 'active'::VARCHAR))) OR (EXISTS (SELECT cloud_job.id FROM cloud_job WHERE cloud_job.file_id = files.id AND cloud_job.status IN ('uploading'::VARCHAR, 'uploaded'::VARCHAR, 'submitted'::VARCHAR, 'running'::VARCHAR, 'awaiting'::VARCHAR)))) AND ((EXISTS (SELECT cloud_job.id FROM cloud_job WHERE cloud_job.file_id = files.id AND cloud_job.status = 'succeeded'::VARCHAR)) OR (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) OR (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) OR (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)))) RETURNING scheduling_ledger.key;
\echo ===== Q9 ledger reaper DELETE lane 2 (reap_resolved_ledger_rows) -- plain EXPLAIN only, never ANALYZE
EXPLAIN (SETTINGS) DELETE FROM scheduling_ledger WHERE scheduling_ledger.key IN (SELECT concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)) AS concat_1 FROM files WHERE (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)))) AND NOT ((EXISTS (SELECT saq_jobs.key FROM saq_jobs WHERE saq_jobs.key = concat('process_file:'::VARCHAR, CAST(files.id AS VARCHAR)) AND saq_jobs.status IN ('queued'::VARCHAR, 'active'::VARCHAR))) OR (EXISTS (SELECT cloud_job.id FROM cloud_job WHERE cloud_job.file_id = files.id AND cloud_job.status IN ('uploading'::VARCHAR, 'uploaded'::VARCHAR, 'submitted'::VARCHAR, 'running'::VARCHAR, 'awaiting'::VARCHAR)))) AND ((EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.analysis_completed_at IS NOT NULL)) OR (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'analyze'::VARCHAR)) OR (EXISTS (SELECT analysis.id FROM analysis WHERE analysis.file_id = files.id AND analysis.failed_at IS NOT NULL)))) RETURNING scheduling_ledger.key;
\echo ===== Q10 ledger reaper DELETE lane 3 (reap_resolved_ledger_rows) -- plain EXPLAIN only, never ANALYZE
EXPLAIN (SETTINGS) DELETE FROM scheduling_ledger WHERE scheduling_ledger.key IN (SELECT concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)) AS concat_1 FROM files WHERE (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)))) AND NOT (EXISTS (SELECT saq_jobs.key FROM saq_jobs WHERE saq_jobs.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)) AND saq_jobs.status IN ('queued'::VARCHAR, 'active'::VARCHAR))) AND ((EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NULL)) OR (EXISTS (SELECT stage_skip.id FROM stage_skip WHERE stage_skip.file_id = files.id AND stage_skip.stage = 'metadata'::VARCHAR)) OR (EXISTS (SELECT metadata.id FROM metadata WHERE metadata.file_id = files.id AND metadata.failed_at IS NOT NULL))) AND NOT (EXISTS (SELECT scheduling_ledger.key FROM scheduling_ledger, metadata WHERE scheduling_ledger.key = concat('extract_file_metadata:'::VARCHAR, CAST(files.id AS VARCHAR)) AND metadata.file_id = files.id AND metadata.failed_at IS NOT NULL AND scheduling_ledger.enqueued_at > metadata.failed_at))) RETURNING scheduling_ledger.key;
ROLLBACK;
```
