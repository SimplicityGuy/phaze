# D1 — Audit Log empty: write-side or read-side?

- **Bead:** `phaze-37i1.1` (epic `phaze-37i1` — Audit Log is empty in every tab and shows no
  detail (latest release))
- **Date:** 2026-07-27/28
- **Tree:** branch `wt/bead/issue/phaze-37i1.1`, forked off `wt/bead/epic/phaze-37i1`
- **Scope:** diagnosis only, per the bead's acceptance criteria. **No fix applied in this bead.**
  All database access was read-only (`SELECT` only); no writes were made to `host-prod`'s
  Postgres.

## Verdict: write-side. `execution_log` is empty because nothing has ever reached the
propose/execute stage — this is not a query, filter, or template bug.

## 1 — The settling query, and the row count

Per the brief, the one query that separates write-side from read-side:

```
$ ssh -4 datum@host-prod 'docker exec postgres psql -U phaze -d phaze -c "select count(*) from execution_log;"'
 count
-------
     0
(1 row)
```

**`execution_log` has zero rows on the live database.** Rows absent → this is write-side, per
the brief's own decision rule (rows present + UI empty would be read-side; no rows is write-side).

## 2 — Confirming the read-side code is not the culprit

Before accepting "write-side" at face value, the read path was checked anyway, because the
brief specifically flagged the "all four tabs including All" symptom as suspicious for a
pre-tab-selection filter bug.

- `src/phaze/routers/execution.py:624-658` — the `audit_log` route calls
  `get_execution_logs_page(session, status=status, page=page, page_size=page_size, sort=sort_state)`
  and `get_execution_stats(session)`. `status` defaults to `None` / `"all"` for the All tab.
- `src/phaze/services/execution_queries.py:22-38` (`get_execution_stats`) — a single aggregate
  query with `func.count()` and per-status `case()` counts, `.select_from(ExecutionLog)`, **no
  WHERE clause at all**. It cannot return a nonzero total unless the table has rows.
- `src/phaze/services/execution_queries.py:41-90` (`get_execution_logs_page`) — `stmt =
  select(ExecutionLog)`; the status filter is only applied `if status is not None and status !=
  "all"` (line 79-80). For the All tab this is a plain unfiltered `SELECT * FROM execution_log`
  (via the ORM), so there is no pre-tab-selection predicate that could zero out "All" while
  leaving data in the table for the other tabs.

**Conclusion: there is no read-side defect.** Both the stats aggregate and the paged query read
`execution_log` with no filter for "All", and a table with zero rows produces exactly the
all-tabs-zero symptom the epic describes — no filter bug is needed to explain it.

## 3 — Following the write path upstream: why is `execution_log` empty?

`execution_log` rows are only ever inserted from
`src/phaze/routers/agent_execution.py:91` —
`pg_insert(ExecutionLog).values([payload]).on_conflict_do_nothing(index_elements=["id"])` —
called by an out-of-process execution agent (via `src/phaze/services/agent_client.py:502`
`post_execution_log`) that is itself driven by `src/phaze/tasks/execution.py` on a **per
`RenameProposal`** basis (`ExecutionLogCreate` at `src/phaze/tasks/execution.py:335`). There is
no other code path in `src/phaze`, tests aside, that constructs an `ExecutionLog` row.

So `execution_log` can only ever contain one row per **executed** `RenameProposal`. That pushes
the question upstream: how many proposals exist to execute?

```
$ ssh -4 datum@host-prod 'docker exec postgres psql -U phaze -d phaze -c "select status, count(*) from proposals group by status;"'
 status | count
--------+-------
(0 rows)
```

**`proposals` also has zero rows, of any status.** There has never been anything to execute,
which fully explains the empty `execution_log` without any code defect: no proposals were ever
approved because no proposals were ever generated.

## 4 — Why are there zero proposals? The propose stage has never fired

`RenameProposal` rows are only ever inserted by the `generate_proposals` SAQ task
(`src/phaze/tasks/proposal.py:23`), which is enqueued exclusively from a manually-invoked
convergence-gate endpoint (`src/phaze/routers/pipeline.py:583-596`, and its duplicate call site
at `:2361-2362`) requiring files that have **both** metadata **and** analysis complete
(D-02 convergence gate, `src/phaze/services/pipeline.py:2349-2375`). Unlike `analyze`,
`metadata`, and `fingerprint`, **`propose`/`execute` are not entries in `pipeline_stage_control`
at all**:

```
$ ssh -4 datum@host-prod 'docker exec postgres psql -U phaze -d phaze -c "select * from pipeline_stage_control;"'
    stage    | paused | priority
-------------+--------+----------
 analyze     | f      |       50
 metadata    | f      |       10
 fingerprint | f      |       30
(3 rows)
```

`generate_proposals` is not a stage the automatic pipeline drain advances on its own; it is
gated behind an operator-facing "Generate Proposals" action. Corroborating evidence that this
action has genuinely never been taken, not merely that it ran and produced nothing:

```
$ ssh -4 datum@host-prod 'docker exec postgres psql -U phaze -d phaze -c \
    "select queue, status, count(*) from saq_jobs group by queue, status;"'
          queue          |  status  | count
-------------------------+----------+-------
 phaze-agent-nox-analyze | active   |  1896
 phaze-agent-nox-analyze | complete |    90
 controller              | queued   |     5
 phaze-agent-nox-analyze | queued   |   687
(4 rows)
```

Only the `analyze` agent queue and 5 `controller`-queue jobs are live; there is no
`generate_proposals` job present in any status. `proposals` is a persistent table, not a
transient job record, so if `generate_proposals` had ever succeeded even once there would be at
least one surviving row regardless of any SAQ job-TTL sweep — there is none.

### The archive's actual pipeline position

The archive-activity figures cited when this epic was filed (files ingested, analyses
completed/failed, fingerprint results) are real and were independently reconfirmed here, but
they describe the `analyze`/`fingerprint`/`metadata` stages, not the `propose`/`execute` stages
the Audit Log reports on — these are two different segments of the pipeline:

```
files:                        11428
metadata rows:                 11428   (100% — metadata stage has cleared the whole corpus)
analysis rows total:            2691
  analysis_completed_at set:    1877   (~16% of the corpus)
  failed_at set:                  32
fingerprint_results:           22856
proposals (any status):            0
execution_log (any status):        0
```

(These counts were measured live during this diagnosis and are close to, but not identical to,
the figures quoted in the epic filing — expected, since the corpus is under continuous active
processing between the filing and this measurement.)

The `analyze` stage is only ~16% complete corpus-wide (1896 files actively being analyzed, 687
queued, only 1877 files fully done). Even the D-02 convergence-gate eligible set (metadata done
+ analysis done) has never had `generate_proposals` invoked over it — not once, for any file, at
any point in this archive's history.

## 5 — What is, and is not, broken

- **Not broken:** the Audit Log's read path (`get_execution_stats`,
  `get_execution_logs_page`, `src/phaze/routers/execution.py:624-658`, and the
  `audit_log.html` / `audit_content.html` / `audit_table.html` / `filter_tabs.html` templates
  they feed). All four tabs correctly report zero because `execution_log` genuinely has zero
  rows. There is no predicate, filter, or template binding bug to fix here.
- **The proximate cause:** `execution_log` is empty because `proposals` is empty, because
  `generate_proposals` — the one and only code path that creates a `RenameProposal` — has never
  been enqueued for this archive, for any file, ever (confirmed by both the empty `proposals`
  table and the absence of any `generate_proposals` job in `saq_jobs`).
- **The open question 37i1.2 must resolve first, before writing any fix:** is this working as
  designed, or is it a missing automation? `generate_proposals` is gated behind a manual,
  operator-triggered endpoint (`src/phaze/routers/pipeline.py:583-596`) and is deliberately
  **not** listed in `pipeline_stage_control` alongside `analyze`/`metadata`/`fingerprint`. Two
  readings are both consistent with the evidence gathered here:
  1. **By design** — proposal generation (which calls an LLM and costs money per file, see
     `check_rate_limit` / `settings.llm_max_rpm` in `src/phaze/tasks/proposal.py`) is meant to
     be a deliberate, operator-initiated action, and it simply has never been clicked/invoked on
     this archive. In that reading the Audit Log is not broken at all — it is accurately
     reporting an archive that has not yet reached the rename stage, and there is nothing to
     patch in `execution.py`/`execution_queries.py`.
  2. **A missing trigger** — the convergence gate at `src/phaze/services/pipeline.py:2349-2375`
     already has ~1,877 files eligible (metadata + analysis both complete) and nothing has ever
     drawn from that pool automatically. If the intended UX is for proposals to advance
     automatically once files converge (mirroring how `analyze`/`fingerprint`/`metadata` already
     auto-advance via `pipeline_stage_control`), then the missing piece is wiring
     `generate_proposals` into that same auto-advance mechanism (or surfacing a much more visible
     "N files ready for proposals" affordance in the UI) — not anything inside the audit log
     module itself.

## 6 — What `phaze-37i1.2` should do

1. **Do not touch `src/phaze/routers/execution.py`, `src/phaze/services/execution_queries.py`,
   or the `execution/audit_*` / `filter_tabs.html` templates as a "fix"** — they were verified
   here to correctly reflect an empty `execution_log` table. Changing them would not surface any
   data, because there is no data.
2. **Resolve the design question in §5 with the repo owner first**: should proposal generation
   (and therefore rename execution and Audit Log entries) advance automatically once files
   converge on metadata+analysis, the way `analyze`/`metadata`/`fingerprint` already do via
   `pipeline_stage_control`? Or is the current manual gate intentional, in which case the
   epic's premise ("Audit Log is broken") is not correct and the actual deliverable is
   discoverability/UX (e.g., making the "N files ready — Generate Proposals" affordance more
   prominent, or clarifying empty-state copy on the Audit Log page itself, e.g. "No executions
   yet — nothing has been proposed for rename," rather than a bare zero) rather than a pipeline
   change.
3. **If the answer is "should auto-advance":** wire `generate_proposals` into the same
   auto-advance mechanism the other three stages use (`pipeline_stage_control` +
   whatever currently calls it for `analyze`/`metadata`/`fingerprint`), respecting the existing
   D-02 convergence gate and the LLM rate limit (`settings.llm_max_rpm`) already implemented in
   `src/phaze/tasks/proposal.py`.
4. **If the answer is "manual gate is correct":** the fix is UX-only — an explicit empty state
   on `execution/audit_log.html` distinguishing "no executions have ever run" from "no executions
   match this filter", so the page is not mistaken for broken again. No backend query change is
   needed.
5. **Either way**, re-run the settling query from §1 after any change lands, to confirm
   `execution_log` gains rows once a proposal is actually approved and executed end-to-end.
