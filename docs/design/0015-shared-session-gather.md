# ADR-0015 — `asyncio.gather` over a shared `AsyncSession`, and what `dataflow_verified` does not say

| | |
| --- | --- |
| **Status** | Accepted — 2026-08-23 |
| **Date** | 2026-08-23 |
| **Bead** | `phaze-4tch9` (amends the reason recorded by `phaze-p2p6u`; general form owed to `phaze-bk9el.25`/`.26`) |
| **Applies to** | every `serial_await_in_loop` / `io_in_loop` triage decision, and every `asyncio.gather` over database work |
| **Supersedes** | `src/phaze/services/proposal.py`'s claim that gathering over one `AsyncSession` "is a correctness bug" |
| **Related** | [ADR-0011](0011-bug-hunt-cadence.md) (findings become beads), [ADR-0012](0012-verification-fidelity-and-operator-attribution.md) rule 5 (a lesson states its general form) |

## The general form

> **No-data-dependence — repowise's `dataflow_verified: true` — is NECESSARY AND NEVER SUFFICIENT
> for an `asyncio.gather`. The shared session is the binding constraint, and on the pinned
> SQLAlchemy 2.0.52 the failure mode is SILENT SERIALIZATION, not an exception.**

That sentence is the whole ADR. Everything below is the evidence for it and the two ways it is
misread.

This document exists because the same claim was written three ways in one tree, one of them false,
and because a static-analysis flag makes these findings look actionable when it is silent on the
thing that actually decides them. It is filed as a design note rather than left at a call site so
that a future bead triaging `serial_await_in_loop` findings can find it by grepping for
`dataflow_verified`, `serial_await_in_loop`, `asyncio.gather` or `AsyncSession`.

## 1. Two facts are established, and both are reasons not to gather

**Fact A — upstream does not support it. (Documented contract; verified against the SQLAlchemy
docs 2026-08-23.)** SQLAlchemy states it in two places, and both name `asyncio.gather` or asyncio
tasks explicitly:

> The `AsyncSession` object is a thin proxy over a `Session`, and the same rules apply regarding
> concurrency. It is an unsynchronized, mutable, stateful object, and thus **not** safe to use a
> single instance in multiple asyncio tasks concurrently.
> — `doc/build/orm/session_basics.rst`

> When using concurrent tasks with asyncio, such as with `asyncio.gather()`, it is important to use
> a **separate** `AsyncSession` for each individual task.
> — `doc/build/orm/extensions/asyncio.rst`

This is sufficient on its own. Nothing below weakens it.

**Fact B — on 2.0.52 it does not raise; it silently serializes. (M — measured four times, by four
different seats, on three different databases.)**

| Seat / bead | Date | Serial | Gathered | Ratio | Raised? |
| --- | --- | --- | --- | --- | --- |
| `phaze-bk9el.25` | 2026-08-22 | 0.636 s | 0.628 s | 1.01× | no |
| dispatcher seat, `bk9el.25` worktree DB | 2026-08-22 | — (reproduced) | — | — | no |
| dev/w3-26, `phaze-bk9el.26` | 2026-08-22 | 0.745 s | 0.659 s | 1.13× | no |
| dev/4tch9-gather, `phaze-4tch9` (this ADR) | 2026-08-23 | 0.632 s | 0.655 s | **0.96×** | no |

Each run is six `select pg_sleep(0.1)` statements on **one** `AsyncSession`, against an
ideal-parallel **0.10 s** and an ideal-serial **0.60 s**. Every measured gathered time sits on the
ideal-*serial* figure: the queries still went down the wire one at a time. The 1.01–1.13× spread is
per-call Python overhead, not overlap — and this bead's own run came out at **0.96×**, i.e. the
gather was *slower* than the loop. `phaze-bk9el.26` and this bead additionally issued **eight
concurrent ORM `session.execute()` calls on one session and got eight results back with no
exception**.

The reproduction script is trivial and is worth re-running rather than trusting this table if the
SQLAlchemy pin ever moves: six `pg_sleep(0.1)` on one `AsyncSession`, timed serially and under
`asyncio.gather`, catching `BaseException`.

**In-suite corroboration (M — run 2026-08-23).**
`tests/review/routers/test_agent_exec_batches.py::test_concurrent_sub_batch_terminals_keep_status_consistent_with_failed`
drives 25 rounds of **three concurrent POSTs** through one ASGI client whose app overrides
`get_session` with `lambda: session` — one shared `AsyncSession` — and every request's
`get_authenticated_agent` dependency does `await session.execute(...)` on it. It passes
(`1 passed in 0.77s`). That test is not *about* session concurrency, but it could not be green if
concurrent use of one `AsyncSession` raised. It is therefore the repo's own standing evidence for
Fact B — and, by the next section, a site that is quietly depending on undocumented behaviour.

## 2. Fact B is not a licence — this is the half that gets dropped

The temptation, once the measurement is in hand, is to conclude that a gather over a shared session
is harmless because it "just serializes". **It is not, and the corrected text at every call site
must say so explicitly.**

- Silent serialization is **undocumented** behaviour. Fact A is upstream's stated contract; Fact B
  is an observation about one pinned version. A release that started raising would turn every such
  gather into a live defect at once, and it would be an upstream *bug fix*, not a regression.
- Silent serialization is **worse than an exception** for the reader. An exception tells you the
  optimisation is invalid. Silence lets a "performance fix" ship that measurably bought nothing
  while looking like it worked — and, per the table above, sometimes cost 4%.
- "Unsupported" and "does not raise today" are both true. **Neither licenses a gather.**

The practical guidance is therefore unchanged from what it always was — *do not gather over a
shared session* — and only the stated reason changes: from "it is a correctness bug that fails
loudly" to "upstream does not support it and you get no speedup anyway."

This ADR is itself an instance of the failure mode it describes, which is why it is worded this
carefully. `phaze-p2p6u` recorded a right conclusion behind a wrong premise; the premise then
propagated into a later seat's first draft, where only a measurement caught it before commit. Per
[ADR-0012](0012-verification-fidelity-and-operator-attribution.md) rule 2, a wrong premise carrying
a right conclusion is not a harmless imprecision: it is what the M/I convention exists to stop.

## 3. The `dataflow_verified: true` trap

repowise's `serial_await_in_loop` finding carries a `dataflow_verified: true` flag when no
iteration reads what a previous one wrote. The flag is **correct** and it is **not a green light**.

A dataflow check answers one question — *is there a data dependence between iterations?* — and a
gather needs at least two more answered:

1. **Do the branches share a session?** A dataflow check cannot see this. It is the binding
   constraint at almost every flagged site in this repo, because the flagged loops are loops over
   one request's or one job's session.
2. **Does concurrency change the transaction, the snapshot, or the degrade path?** Splitting
   branches onto their own sessions is the *fix* for (1), and it is not free: it drops the shared
   read snapshot, multiplies the connection-pool draw, and changes which exception surfaces when
   more than one branch fails. `services/cue_review.py` and `services/companion.py` record all
   three costs at their own call sites.

So a `dataflow_verified: true` finding means "this specific objection does not apply", never "this
is safe to parallelise". **That flag is the thing that routes beads to these call sites, so it is
the thing most likely to mislead the next reader.**

## 4. When a gather over database work IS right

This ADR forbids one shape, not concurrency. The supported pattern is upstream's own: **one
`AsyncSession` per task**. This repo already does it correctly in three places, and they are the
model to copy rather than exceptions to it:

- `services/pipeline/stages.py::get_stage_progress` — every independent count fans out through
  `_read_in_own_session`, bounded by the shared `_STATS_FANOUT` semaphore.
- `routers/pipeline/dashboard_stats.py` — the ~12 dashboard reads, same helper, same cap; the one
  read with a true value dependency (`_build_dag_context`) deliberately stays a sequential await
  *after* the gather.
- `routers/shell/summary.py::_build_summary_context` — same helper, same cap.

Each pays the snapshot cost knowingly and says so in a comment. That is the bar: a gather over
database work is fine when every branch owns its session **and** the transaction/snapshot/degrade
consequences are stated.

## 5. The sweep (`phaze-4tch9`, 2026-08-23)

`grep -rn "gather" src/ tests/` — **73 mentions across 30 files**, resolving to **28 executable
`asyncio.gather(...)` call sites** (10 in `src/`, 18 in `tests/`) plus the prose that reasons about
them. **All 28 call sites and every prose mention were read.**

**Call sites gathering over a SHARED `AsyncSession`: 1 of 28.**

| | |
| --- | --- |
| `tests/review/routers/test_agent_exec_batches.py:734` | Three concurrent POSTs on one ASGI client; `app.dependency_overrides[get_session] = lambda: session` gives every request the same session, and `get_authenticated_agent` executes on it. **Left as-is**: the test's subject is Redis atomicity under a real Redis, its DB touch is one authentication SELECT per request, and it passes. Recorded here because it *depends on* Fact B rather than on Fact A, so it is where a future SQLAlchemy bump would surface first. |

The other 27 are clean, and for the same two reasons in every case: the branches touch no database
(`job_runner.py:643`, `tasks/functions.py:224`, `analysis_exec.py:294,328`, `video_audio.py:600,690`
are HTTP-POST drains and subprocess stream pumps; `tasks/discogs.py:68` closes its session *before*
the network gather by construction, per `phaze-xdu1`), or each branch opens its own session
(`stages.py:273`, `dashboard_stats.py:615`, `summary.py:508` via `_read_in_own_session`; every
`tests/integration/*_concurrency.py` race via `session_factory()` per branch; the paired-client
router races via a per-request `_override_session`).

**Prose sites recording the FALSE premise: 1.** `src/phaze/services/proposal.py`, reason 1 of the
`store_proposals` upsert loop — the subject of this bead, amended with the code untouched. (It sat
at lines 415-421 when the bead was filed and at 719-745 when it landed, an unrelated change having
grown the file in between. Cite the *symbol*; if you must cite a line, pin the commit the way
`cue_review.py` does with "lines 178 and 184 as measured at a3fd169a".)

Every other prose site was already correct or already measured: `services/companion.py` (the `phaze-bk9el.25` ruling, which caught and
corrected the same false phrasing in its own text), `services/cue_review.py` (the `phaze-bk9el.26`
measurement), `services/agent_task_router.py:314`, `services/backends/lane_snapshot.py:173`,
`services/pipeline/stages.py:134,186`, `routers/pipeline/dashboard_stats.py:345,582-591`.

**No behaviour changed by this bead.** `proposal.py`'s loop stays sequential, and it would stay
sequential even if this ADR did not exist: reason 2 at that site is independently disqualifying —
folding the N per-proposal upserts into one multi-row `INSERT ... ON CONFLICT DO UPDATE` turns a
duplicate `file_index` from the LLM (today tolerated, last-write-wins) into Postgres error 21000
and fails the whole batch write.
