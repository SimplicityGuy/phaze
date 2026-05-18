---
phase: 28-distributed-execution-dispatch
reviewed: 2026-05-15T00:00:00Z
depth: standard
files_reviewed: 26
files_reviewed_list:
  - src/phaze/config.py
  - src/phaze/main.py
  - src/phaze/routers/agent_exec_batches.py
  - src/phaze/routers/execution.py
  - src/phaze/schemas/agent_exec_batches.py
  - src/phaze/schemas/agent_tasks.py
  - src/phaze/services/agent_client.py
  - src/phaze/services/execution_dispatch.py
  - src/phaze/tasks/execution.py
  - src/phaze/templates/_partials/cross_fs_fingerprint_notice.html
  - src/phaze/templates/duplicates/list.html
  - src/phaze/templates/execution/partials/agents_table.html
  - src/phaze/templates/execution/partials/dispatch_summary_inline.html
  - src/phaze/templates/execution/partials/progress.html
  - src/phaze/templates/execution/partials/progress_row_inline.html
  - tests/test_routers/test_agent_exec_batches.py
  - tests/test_routers/test_execution.py
  - tests/test_routers/test_execution_dispatch.py
  - tests/test_schemas/test_agent_exec_batches.py
  - tests/test_services/test_agent_client_exec_batch_progress.py
  - tests/test_services/test_execution_dispatch_grouping.py
  - tests/test_services/test_fingerprint_locality.py
  - tests/test_tasks/test_execute_approved_batch_progress.py
  - tests/test_template_helpers/test_cross_fs_fingerprint_notice.py
  - tests/test_template_helpers/test_progress_partial.py
findings:
  critical: 1
  warning: 6
  info: 5
  total: 12
status: issues_found
---

# Phase 28: Code Review Report

**Reviewed:** 2026-05-15
**Depth:** standard
**Files Reviewed:** 26
**Status:** issues_found

## Summary

Phase 28's distributed execution dispatch implementation is in good shape. The
locked-decision contract (D-01 .. D-19) is faithfully implemented: handler
ordering on the new progress endpoint matches D-17, the Stripe-style SET NX EX
dedup is wired exactly as specified, dispatch grouping + chunking + Redis-hash
seeding land cleanly, and the SAQ meta-key persistence for retry-stable UUIDs
(D-15 / L6 / L22) closes the retry-idempotency gap. XSS is mitigated by
Jinja2's default autoescape; SQL injection surface is zero (all queries are
parameterized via SQLAlchemy `select()`); cross-tenant 403 fires before any
Redis read; the localhost-only URL validator is enforced at config-construction
time so non-allow-listed hosts cannot reach the audfprint/panako adapters.

The remaining issues are concentrated in two areas: (1) a real but rare race
condition in the multi-sub-job terminal-status promotion path that can produce
the wrong final `status` value on the `exec:{batch_id}` hash, and (2) a
collection of UX / robustness / tech-debt items (test-isolation env-var hacks
documented as such in the plan summaries, an unimplemented `revoked_agents`
breakdown sub-list, a placeholder `href="#"` link in the disclosure banner, and
graceful-degradation gaps when Alpine.js is unavailable or the Redis hash TTL
expires while an SSE consumer is still connected).

No security vulnerabilities found. No SQL injection, no XSS, no
authentication-bypass paths. Idempotency keys are correctly UUID-generated and
the request-id replay window (1h) is appropriate. The schema's `extra="forbid"`
+ cross-field `model_validator` blocks ride-along field attacks and
structurally-valid-but-semantically-broken pairings.

---

## Critical Issues

### CR-01: Terminal-status promotion race produces wrong `status` under concurrent sub-batch terminal POSTs

**File:** `src/phaze/routers/agent_exec_batches.py:189-198`
**Issue:**
The `sub_batch_terminal` block reads three separate Redis fields with
non-atomic awaits and then conditionally writes `status`:

```python
if body.sub_batch_terminal:
    sc = int(await cast(..., redis_client.hget(key, "subjobs_completed")) or 0)
    se = int(await cast(..., redis_client.hget(key, "subjobs_expected")) or 0)
    if sc == se:
        failed = int(await cast(..., redis_client.hget(key, "failed")) or 0)
        new_status = "complete" if failed == 0 else "complete_with_errors"
        await cast(..., redis_client.hset(key, "status", new_status))
```

With three or more sub-jobs running concurrently and one of them failing,
the following interleaving is reachable (verified by tracing redis-py async
pipeline semantics; `transaction=False` does NOT make a pipeline atomic
across connections — Redis serves commands from different connections
interleaved):

1. Successful sub-job A's pipe runs first: subjobs_completed=1, completed+1.
2. Successful sub-job C's pipe runs: subjobs_completed=2, completed+1.
3. Failed sub-job B's pipe begins: HINCRBY subjobs_completed=3 succeeds. Before B's HINCRBY `failed` is processed, other clients' commands can interleave on the Redis server.
4. C's handler enters the `sub_batch_terminal` block: HGET subjobs_completed=3, subjobs_expected=3, **failed=0** (B's HINCRBY failed has not run yet). C HSETs `status="complete"`.
5. B's HINCRBY `failed` runs. failed=1.
6. B's handler: HGET subjobs_completed=3, failed=1. HSETs `status="complete_with_errors"`.

Trace order matters: if C's HSET `status="complete"` lands AFTER B's HSET
`status="complete_with_errors"`, the final state is `status="complete"` —
but `failed=1` is on the hash. The SSE reader sees `status=complete`,
closes the stream with "All N files renamed successfully" — yet the audit
log shows a failure. Operator misses the error.

The window is narrow (between two HGETs on a single connection while a
concurrent connection is mid-pipeline) but real. The SSE close-event copy
at `routers/execution.py:339` only mentions failures when `failed > 0`, so
a wrong-`status` HSET produces a wrong-event-name close
(`event: complete` instead of `event: complete_with_errors`) — which the
operator's HTMX `sse-close` listener uses to determine whether to show the
error-styled completion message.

**Fix:**
Use a server-side Lua script (one round-trip, atomic on the Redis server)
to perform the read-check-write atomically:

```python
_PROMOTE_STATUS_LUA = """
local key = KEYS[1]
local sc = tonumber(redis.call('HGET', key, 'subjobs_completed') or '0')
local se = tonumber(redis.call('HGET', key, 'subjobs_expected') or '0')
if sc ~= se then return 0 end
local failed = tonumber(redis.call('HGET', key, 'failed') or '0')
local new_status = (failed == 0) and 'complete' or 'complete_with_errors'
redis.call('HSET', key, 'status', new_status)
return 1
"""

if body.sub_batch_terminal:
    # Single atomic round-trip on the Redis server.
    await redis_client.eval(_PROMOTE_STATUS_LUA, 1, key)
```

Alternatively, fold the read into the same `transaction=True` pipeline as
the HINCRBYs (with WATCH on the read fields). Lua is simpler. The fix is
~10 lines and closes the race deterministically.

Note: the design (D-16) accepts under-reporting on agent-side POST
failures, but does NOT contemplate this controller-side race. The Phase 28
CONTEXT specifically says "the SSE generator already polls for `status in
{complete, ...}` to close" — the wrong-`status` outcome violates that
polling contract.

---

## Warnings

### WR-01: Idempotency dedup key can be claimed before HINCRBY actually completes — lost-event window

**File:** `src/phaze/routers/agent_exec_batches.py:170-187`
**Issue:**
The handler claims the `exec_progress_req:{request_id}` SET NX EX key
BEFORE the pipelined HINCRBYs run. If `pipe.execute()` raises (Redis
crash, network error, pipeline buffer overflow), the dedup key is left in
place with TTL 3600. Any agent retry with the same `request_id` returns
200 (dedup hit) without HINCRBY ever running. The progress event is
permanently lost; counters under-report by 1.

The agent's tenacity policy in `services/agent_client.py:_request` retries
5xx and persistent network errors three times. If all three attempts hit
the same Redis fault, the agent eventually surfaces
`AgentApiServerError` — which `_execute_one` swallows per D-16. So the
failure is logged WARNING but the operator only sees the discrepancy as
"completed + failed < total" in the SSE.

**Fix:**
Either:
1. Move the SET NX EX claim to AFTER the pipeline succeeds (delete the
   `exec_progress_req:{request_id}` claim on pipeline failure), OR
2. Use a Lua script combining SETNX + HINCRBY in one atomic call so the
   dedup key is only set when the increments commit.

Option 2 also closes a TOCTOU between the cross-tenant HEXISTS check and
the HINCRBY. Lua is the right primitive for this whole handler — see
CR-01 fix.

Documented as acceptable in D-16 only for the AGENT-side failure mode; the
controller-side mid-pipeline failure is not addressed by the design.

---

### WR-02: `_classify_failure_step` brittle string match on "sha256 mismatch"

**File:** `src/phaze/tasks/execution.py:98-111`
**Issue:**
The classifier inspects the exception's `str(exc)` for the substring
`"sha256 mismatch"` and returns `"verify"` regardless of the tracked
`current_step`. This is robust against the documented case (sha256-mismatch
ValueError raised while `current_step="verify"`) but brittle:

1. If a logging chain or wrapper exception ever rebroadcasts text containing
   "sha256 mismatch" while a different step is active, the classification is
   wrong.
2. If the verify error message changes (e.g., translated, reworded for
   operator clarity), the classification silently flips back to
   `current_step`.

The classifier's docstring acknowledges the rule is encoded "so a refactor
that re-orders the body cannot regress the contract" — but the encoding is
fragile against unrelated string-content changes.

**Fix:**
Define a custom exception class for sha256 mismatch and dispatch on type:

```python
class Sha256MismatchError(ValueError):
    """sha256 verify step rejected the file."""

# In _execute_one:
if actual != item.sha256_hash:
    raise Sha256MismatchError(f"sha256 mismatch ...")

# In _classify_failure_step:
def _classify_failure_step(current_step, exc):
    if isinstance(exc, Sha256MismatchError):
        return "verify"
    return current_step
```

Type-based dispatch is mypy-checkable and resistant to error-message
rewording.

---

### WR-03: `revoked_agents` breakdown context never populated — banner sub-list is dead code

**File:** `src/phaze/routers/execution.py:199-213`, `src/phaze/templates/execution/partials/progress.html:41-47`
**Issue:**
The progress.html template renders a per-revoked-agent breakdown if
`revoked_agents` is truthy in the context:

```jinja
{% if revoked_agents %}
<ul class="list-disc ml-4 mt-2">
    {% for agent in revoked_agents %}
    <li>...{{ agent.name }} ({{ agent.agent_id }}) -- {{ agent.count }} proposal{{ 's' if agent.count != 1 else '' }} skipped</li>
    {% endfor %}
</ul>
{% endif %}
```

But `start_execution` never includes `revoked_agents` in the response
context — it only passes `skipped_revoked` (the total count). The Jinja
`{% if %}` is always false in production, the `<ul>` never renders, and
the operator sees only "N proposals skipped" with no per-agent breakdown.

The template-render test
(`tests/test_template_helpers/test_progress_partial.py:225-253`)
exercises the breakdown by passing `revoked_agents=[...]` explicitly, so
the template is correct — only the controller wiring is missing.

The CONTEXT D-09 step 2 says the banner copy is `"Agent <name> revoked;
<N> proposals skipped"` — per-agent attribution is part of the contract.

**Fix:**
Extend `count_revoked_skipped_proposals` (or add a sibling) to return
`list[dict[str, str | int]]` with per-agent rows, then pass it as
`revoked_agents` in the context. Roughly:

```python
async def get_revoked_agent_breakdown(session) -> list[dict[str, object]]:
    stmt = (
        select(Agent.id, Agent.name, func.count(RenameProposal.id))
        .join(FileRecord, FileRecord.agent_id == Agent.id)
        .join(RenameProposal, RenameProposal.file_id == FileRecord.id)
        .where(
            RenameProposal.status == ProposalStatus.APPROVED,
            Agent.revoked_at.is_not(None),
        )
        .group_by(Agent.id, Agent.name)
    )
    result = await session.execute(stmt)
    return [{"agent_id": r[0], "name": r[1], "count": r[2]} for r in result.all()]
```

---

### WR-04: SSE generator leaks connections after `exec:{batch_id}` Redis-hash TTL expires

**File:** `src/phaze/routers/execution.py:285-345`
**Issue:**
The SSE generator's main loop:

```python
while True:
    data = await redis_client.hgetall(f"exec:{batch_id}")
    if not data:
        yield {"event": "progress", "data": "Waiting for execution to start..."}
        await asyncio.sleep(1)
        continue
    ...
```

When the 24h TTL on `exec:{batch_id}` expires, `hgetall` returns `{}` and
the generator falls into the `if not data` branch forever — sending
"Waiting for execution to start..." every second to any browser tab still
subscribed. The loop has no escape on missing hash. A long-lived operator
tab parked on a completed batch (e.g., overnight) will reconnect after the
TTL expires and spin forever, holding an open server connection and a
poll loop on the application server.

The pre-Phase-28 code had the same loop shape, but Phase 28's per-agent
SSE events make the connection more expensive (multiple TemplateResponse
renders per tick) so the leak is more impactful.

**Fix:**
Add a max-wait deadline for "data not yet present" so a hash that never
appears (TTL expired without a dispatch) closes the connection:

```python
async def event_generator():
    no_data_ticks = 0
    while True:
        data = await redis_client.hgetall(f"exec:{batch_id}")
        if not data:
            no_data_ticks += 1
            if no_data_ticks > 60:  # 60s grace period
                yield {"event": "complete", "data": "Batch state unavailable (timed out)."}
                return
            yield {"event": "progress", "data": "Waiting for execution to start..."}
            await asyncio.sleep(1)
            continue
        no_data_ticks = 0
        ...
```

---

### WR-05: `audfprint_url`/`panako_url` validator rejects IPv6 loopback `::1`

**File:** `src/phaze/config.py:64-90`
**Issue:**
The allow-list is `{"localhost", "127.0.0.1", "audfprint", "panako"}`. An
operator who configures `http://[::1]:8001` (IPv6 loopback, the modern
equivalent of `127.0.0.1`) gets a `ValidationError` at boot with a
confusing message. IPv6 is increasingly common on dual-stack deployments.

**Fix:**
Add `"::1"` to the allow-list:

```python
allowed_hosts = {"localhost", "127.0.0.1", "::1", "audfprint", "panako"}
```

`urlparse("http://[::1]:8001").hostname` returns `"::1"` (without
brackets), so the membership check works.

---

### WR-06: Dispatch + revoked-count queries are not in a shared transaction — banner can disagree with reality

**File:** `src/phaze/routers/execution.py:111-112`, `src/phaze/services/execution_dispatch.py:52-111`
**Issue:**
`start_execution` runs two separate queries on the same session without
opening a transaction:

```python
groups = await get_approved_proposals_grouped_by_agent(session)
skipped_revoked = await count_revoked_skipped_proposals(session)
```

Between these queries, an agent's `revoked_at` can be flipped from NULL
to a timestamp by an operator's admin action (or by a separate process).
The first query would have included that agent's proposals in `groups`;
the second query would also count those same proposals in
`skipped_revoked`. Result: the operator sees "N proposals skipped" in the
banner AND those proposals get enqueued anyway. Banner is misleading;
agent receives jobs it can no longer process (its token is revoked at the
auth layer).

For v4.0 single-operator scale the race is unlikely but not impossible —
e.g., a watchdog cron that revokes idle agents could fire mid-dispatch.

**Fix:**
Wrap both queries in a single read-only transaction:

```python
async with session.begin():
    groups = await get_approved_proposals_grouped_by_agent(session)
    skipped_revoked = await count_revoked_skipped_proposals(session)
```

PostgreSQL's default isolation (READ COMMITTED) is sufficient since both
queries are reads of the same snapshot if executed within one transaction.

---

## Info

### IN-01: `PHAZE_TEST_DATABASE_URL_28_02` / `PHAZE_TEST_DATABASE_URL_28_04` worktree-isolation env-vars are tech debt

**File:** `tests/test_routers/test_agent_exec_batches.py:43-60`, `tests/test_routers/test_execution_dispatch.py:17-61`
**Issue:**
Both test modules monkeypatch `tests.conftest.TEST_DATABASE_URL` from an
environment variable scoped to a single Phase 28 wave. The mechanism is
documented inline as a workaround for parallel-pytest sharing the
`legacy-application-server` Agent row insert in `conftest.async_engine`.
Per Phase 28 focus area #10 this was flagged at plan time as
INFO-severity tech debt to clean up post-merge.

The env-var approach is fragile (silent fallback to shared DB when var
unset), couples test code to phase-specific orchestrator state, and leaks
phase numbering into tests that will outlive the phase. A proper fix
lives in `tests/conftest.py` (per-worker DB schema, transactional
rollback, or pytest-xdist worker-id based isolation).

**Fix:**
Remove `_OVERRIDE_DB_URL` / `_override_test_database_url` fixtures from
both test modules; redesign `conftest.async_engine` to use pytest-xdist's
`worker_id` or a transaction-rollback pattern for proper isolation. File
a follow-up issue.

---

### IN-02: `cross_fs_fingerprint_notice.html` "Learn more" link has placeholder `href="#"`

**File:** `src/phaze/templates/_partials/cross_fs_fingerprint_notice.html:12`
**Issue:**
The banner promises operator-facing documentation via a "Learn more" link:

```html
<a href="#" class="text-blue-600 dark:text-blue-400 hover:underline" title="See PROJECT.md">Learn more</a>.
```

The `href="#"` jumps to the top of the page (or nowhere) when clicked. The
`title` attribute hints at PROJECT.md but PROJECT.md is not served at any
operator-facing URL. D-13 / D-14 of the CONTEXT planned for an "inline
link to the docs entry from D-13"; the implementation landed the link
element but not the target.

**Fix:**
Either point `href` at a real route (`/docs/cross-fs-fingerprint` or
similar) once it exists, or remove the link until docs are reachable.
Avoid shipping a no-op link that defaults to scrolling the operator to
the top of their duplicates page.

---

### IN-03: Banner dismissal silently fails when Alpine.js is unavailable

**File:** `src/phaze/templates/_partials/cross_fs_fingerprint_notice.html:1-23`
**Issue:**
`x-data="{ open: true }"` + `x-show="open"` + `@click="open = false"` all
require Alpine.js to be loaded. If the CDN fails or the operator's
browser blocks third-party scripts, the banner renders as a fully-visible
card with a non-functional close button. The operator can't dismiss it
and may not understand why.

This is a graceful-degradation gap, not a defect — the banner is
by-design "per-session dismissible" so a stuck-visible state is
conservative. But the close button is a UX hook the operator will reach
for; making it visibly non-functional is jarring.

**Fix:**
Either gate the close button behind `x-cloak` (so it stays hidden until
Alpine is loaded), or add a fallback CSS-only dismissal (anchor-target
trick) that works without JS. Lower priority than CR/WR items.

---

### IN-04: SSE batch_id path parameter is unauthenticated

**File:** `src/phaze/routers/execution.py:269-270`
**Issue:**
`GET /execution/progress/{batch_id}` accepts any `batch_id: str` without
authentication, returning HGETALL of `exec:{batch_id}`. Anyone with
network access to the admin UI can poll any UUID and see progress data if
they guess. UUIDs are 128-bit and unguessable, so the practical risk is
near-zero, but the endpoint is the only `/execution/*` route without
auth.

The codebase's "private network only" deployment model (CLAUDE.md) makes
this acceptable for v4.0. Flagged for future hardening.

**Fix:**
Defer until the project ships with a public-internet-facing surface.
When that happens, wrap the endpoint in the same admin-auth dep used
elsewhere in `/execution/*`.

---

### IN-05: Terminal-close SSE event builds raw HTML via f-string instead of through Jinja

**File:** `src/phaze/routers/execution.py:339-342`
**Issue:**
The close-event message is constructed inline:

```python
msg = f'Execution complete. All {total} files renamed successfully. <a href="/audit/" class="text-blue-600 hover:underline ml-2">View Audit Log</a>'
```

`total`, `completed`, `failed` are integers (safe), but the inline
raw-HTML string bypasses the project's standard Jinja template chain.
Future edits to this copy require touching Python source instead of a
template; the close-event mark-up has drifted from the rest of the
partials (e.g., dark-mode classes are missing on the anchor:
`dark:text-blue-400` is present in sibling templates but not here).

**Fix:**
Move the close message into a tiny `terminal_message_inline.html`
partial and render it via `_render_partial(request, "...", {...})` for
consistency. Also picks up the dark-mode classes for free.

---

_Reviewed: 2026-05-15_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
