"""The ONE stranded-``saq_jobs``-row DELETE both control-side key reapers issue (phaze-e57w / phaze-o0n6).

CONTROL-ONLY (Phase 26 D-03 / control-vs-agent DB boundary). Imported by
:mod:`phaze.tasks.aborting_reaper` and :mod:`phaze.tasks.active_reaper` ONLY; like them it must never
be reached from ``phaze.tasks.agent_worker`` or anything under ``phaze.tasks._shared`` (the agent path
is deliberately Postgres-free -- ``tests/shared/core/test_task_split.py`` enforces this).

WHY ONE STATEMENT AND NOT TWO
-----------------------------
SAQ's ``_enqueue`` upsert only overwrites a conflicting key whose status is in
``('aborted','complete','failed')`` (``saq/queue/postgres.py``). EVERY other status holds the
deterministic key ``'<function>:<file_id>'`` hostage, so every re-enqueue of that file collapses to a
``None`` return and the file becomes silently un-requeueable by any path -- including the recovery CLI.

``'aborting'`` was the first such status found (phaze-e57w, ``aborting_reaper``). ``'active'`` is the
second (phaze-o0n6: 2,413 rows on one live queue against worker concurrency 4, 2,411 of them files
that had NEVER been analyzed). The two reapers differ ONLY in which status they target and how much
slack that status earns; the three load-bearing guards are IDENTICAL, so they are written once here
rather than copied. A guard fixed in one reaper and not the other is the exact drift this prevents.

WHAT IS DELIBERATELY *NOT* SHARED, AND WHY (phaze-bk9el.27)
-----------------------------------------------------------
The two reaper modules still read as ~24% / ~18% duplicated, and that residue is examined and KEPT.
Diffed line-for-line, the flagged clone (``aborting_reaper`` 64-86 against ``active_reaper`` 95-116) is
``from __future__ import annotations``, ``from typing import Any``, ``import structlog``, the two
shared imports off THIS module, the ``logger = structlog.get_logger(__name__)`` line, the module-local
SQL alias and the ``async def`` signature. Nineteen of those twenty-three lines are identical because
they are Python module boilerplate; the four that differ are the two alias names and the two function
names, i.e. exactly the lines that MUST differ. There is no import block to extract.

The part that genuinely had to stay in lockstep -- the statement and its four guards -- is already
extracted, into this module, by phaze-o0n6, for the stated reason at the top of this docstring. What is
left in each reaper is ~10 lines of body that differ in four places (the settings attribute, the SQL
alias, the ``:status`` bind, and the log event/message). Folding THAT into a shared
``_reap(ctx, sql, status, slack, log_event, log_message)`` would trade a duplication finding for a
six-argument primitive-obsession one, hand ten lines of body six lines of configuration, and break the
property both reapers document and rely on: the module-local alias exists so each reaper's degrade test
can monkeypatch ITS statement without touching the sibling's. The log messages are also not copy-paste
-- the ``'active'`` one carries the ledger-rows-kept clause that is the whole phaze-o0n6 acceptance
item 3 -- and the clone pair's co-change count in this repo's history is ZERO: these two files have
never had to change together. Structural similarity by design is not debt, and coupling them to move a
metric would make a genuinely healthy pair worse.

FOUR GUARDS, all load-bearing:

- **Age bound on the FROZEN ``started``, NOT ``touched``.** SAQ's sweeper bumps ``touched`` on every
  pass (``Queue.update`` sets ``touched = now()``), so a touched-based bound would never fire on a
  repeatedly-swept row -- and 773 of the phaze-o0n6 population carried ``error: "swept"``, i.e. had been
  swept at least once. ``started`` is frozen at (last) dequeue/process start, so ``now - started`` is
  the true age of the stuck state (spike phaze-qmc2.1, fact 4). A row whose blob carries no ``started``
  is EXCLUDED rather than reaped on incomplete data.
- **The bound is PER-ROW, derived from that row's own serialized ``timeout``** (phaze-lqkz), never one
  fixed constant. The predicate measures the ATTEMPT'S RUNTIME, so a fixed bound is only correct for
  jobs whose timeout happens to match it. The motivating case was ``process_file``'s then-7200s job
  timeout -- 8x the old 900s constant -- which a fixed bound would have made INSTANTLY reap-eligible,
  racing the owning worker's finalization. (Since phaze-w55w1 ``process_file`` runs ``timeout=0`` and
  is excluded by the next guard entirely; the per-row derivation still governs every other job.)
  ``Job.to_dict`` always serializes ``timeout`` once it differs from the SAQ dataclass default (10s),
  true for every phaze producer via ``apply_project_job_defaults``, so the row's OWN blob already
  carries the bound; no side table is needed. A bare/legacy row without ``timeout`` falls back to
  :data:`SAQ_DEFAULT_TIMEOUT_SECONDS`.
- **``timeout: 0`` rows are EXCLUDED, not bounded at 0 (phaze-mllxc).** In SAQ, ``timeout=0`` means
  UNBOUNDED -- ``Job.stuck`` is ``(self.timeout and ...)``, so 0 is falsy and the job is never
  sweep-eligible. ``scan_directory`` enqueues with ``timeout=0`` deliberately, because a full
  SHA-256 walk of a large network-mounted archive legitimately takes 1-2h
  (``routers/pipeline_scans.py``). ``Job.to_dict`` serializes ANY field that differs from its
  dataclass default, so ``timeout: 0`` is always present in the blob -- the old
  ``COALESCE(...::bigint, :default_timeout_seconds)`` only substitutes the default when the key is
  ABSENT, so a present ``0`` produced a bound of ``0 + slack`` and reaped a healthy 1-2h scan
  ~15 minutes in. The exclusion conjunct below is an ``<> 0`` guard, deliberately NOT a
  ``NULLIF(...,0)`` substitution -- ``NULLIF`` would silently convert 0 to the 10s default and still
  reap at 910s. A ``timeout: 0`` row's liveness is owned by the progress-based scan stall reaper
  instead.
- **CAS on the status inside the DELETE's WHERE.** A row a live worker is mid-finalizing must never be
  stolen. Under READ COMMITTED the DELETE re-checks the ``status = :status`` qualification after
  locking a concurrently-updated row, so a row that flips to a terminal status first wins the race and
  is left alone.

WHY EACH CALLER'S ``except Exception`` IS BROAD (phaze-bk9el.27)
----------------------------------------------------------------
Both reapers wrap this statement in a SAVEPOINT under a bare ``except Exception``, and both are kept
that way. The contract is categorical -- a reaper hiccup must never abort the controller cron tick that
runs it -- and it is enforced at a CRON BOUNDARY, where a raise takes down a tick that also carries
unrelated work. The failure modes are open-ended by nature: a pre-migration environment with no
``saq_jobs`` table, a malformed ``job`` blob failing the ``convert_from(...)::jsonb`` cast, a driver- or
pool-level error. Narrowing to an enumerated set would honour the never-raise contract only for the
modes someone thought of in advance and would convert every other one into the tick-aborting raise the
contract exists to forbid. Each handler is already narrow where it counts: it wraps one statement,
catches ``Exception`` rather than ``BaseException`` (cancellation still propagates), logs with
``exc_info=True``, and returns the explicit ``{"reaped": 0}`` rather than swallowing silently.

The status is a BIND PARAMETER, not interpolated -- one prepared statement serves both callers and no
caller can widen the reaper's blast radius by passing SQL.
"""

from __future__ import annotations

from sqlalchemy import text


# SAQ's Job dataclass default timeout (saq/job.py) -- the fallback bound for a row whose blob has no
# explicit ``timeout`` key (mirrors ``phaze.tasks._shared.queue_defaults._SAQ_DEFAULT_TIMEOUT``, that
# module's single source of truth; duplicated here as a plain int so this SQL-bound module stays free
# of a cross-import into the agent-shared package for one constant).
SAQ_DEFAULT_TIMEOUT_SECONDS = 10

# Read+DELETE in ONE atomic statement. ``status = :status`` IS the CAS. Age is computed from the blob's
# FROZEN ``started`` (ms) vs now; ``touched`` is deliberately NOT used (sweeper-bumped). The bound is
# per-row (the row's own ``timeout``, falling back to the bare SAQ default) plus the caller's slack.
#
# Deliberately NOT scoped by ``queue``: a held key blocks re-enqueue of its file on EVERY queue (the
# ``saq_jobs`` PK is the key alone), so a queue-scoped reaper would leave the block in place for any
# row whose queue the caller forgot to name.
REAP_STRANDED_SQL = text(
    """
    DELETE FROM saq_jobs
    WHERE status = :status
      AND (convert_from(job, 'UTF8')::jsonb ? 'started')
      AND COALESCE((convert_from(job, 'UTF8')::jsonb->>'timeout')::bigint, :default_timeout_seconds) <> 0
      AND (EXTRACT(EPOCH FROM NOW()) * 1000 - (convert_from(job, 'UTF8')::jsonb->>'started')::bigint)
          / 1000.0 > COALESCE((convert_from(job, 'UTF8')::jsonb->>'timeout')::bigint, :default_timeout_seconds) + :slack_seconds
    RETURNING key
    """
)


__all__ = ["REAP_STRANDED_SQL", "SAQ_DEFAULT_TIMEOUT_SECONDS"]
