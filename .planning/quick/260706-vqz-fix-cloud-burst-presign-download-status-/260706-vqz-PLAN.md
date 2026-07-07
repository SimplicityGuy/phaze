---
phase: quick-260706-vqz
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/routers/agent_files.py
  - tests/agents/routers/test_agent_presign_download.py
autonomous: true
requirements: [WR-03]
must_haves:
  truths:
    - "A running analyze pod whose cloud_job.status is SUBMITTED can fetch a presigned download URL (200)."
    - "A running analyze pod whose cloud_job.status is RUNNING can fetch a presigned download URL (200)."
    - "An UPLOADED cloud_job still returns 200 (unchanged behavior)."
    - "An UPLOADING cloud_job still returns 409 (object not yet fully staged)."
    - "A terminal SUCCEEDED or FAILED cloud_job returns 409 (object may be cleaned up)."
    - "A file with no cloud_job row returns 409."
  artifacts:
    - path: "src/phaze/routers/agent_files.py"
      provides: "presign_download readiness guard keyed on a downloadable/staged status set"
      contains: "frozenset"
    - path: "tests/agents/routers/test_agent_presign_download.py"
      provides: "boundary regression tests across the full CloudJobStatus set"
  key_links:
    - from: "src/phaze/routers/agent_files.py"
      to: "phaze.models.cloud_job.CloudJobStatus"
      via: "module-level downloadable-status frozenset"
      pattern: "CloudJobStatus\\.(UPLOADED|SUBMITTED|RUNNING)"
---

<objective>
Fix the cloud-burst presign-download readiness guard so a running analyze pod can actually fetch
its staged file. `presign_download` in `src/phaze/routers/agent_files.py` currently 409s unless
`cloud_job.status == UPLOADED`, but `submit_cloud_job.py:117` advances status to SUBMITTED at Kueue
Job creation BEFORE the pod runs. The pod therefore always observes SUBMITTED (or later RUNNING) and
can never fetch its bytes — cloud analysis can never complete (found in the first live k8s E2E,
2026-07-07, image 2026.7.3).

Purpose: unblock the k8s cloud-burst path end-to-end.
Output: a status-set boundary fix + full boundary regression coverage.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

<interfaces>
<!-- Executor uses these directly — no codebase exploration needed. -->

From src/phaze/models/cloud_job.py — CloudJobStatus (enum.StrEnum, string-backed):
```python
UPLOADING = "uploading"   # bytes NOT yet fully staged -> NOT downloadable
UPLOADED  = "uploaded"    # object staged in bucket    -> downloadable
FAILED    = "failed"      # terminal, may be cleaned up -> NOT downloadable
SUBMITTED = "submitted"   # Kueue Job created, pod pending/running -> downloadable
RUNNING   = "running"     # pod executing -> downloadable
SUCCEEDED = "succeeded"   # terminal, may be cleaned up -> NOT downloadable
```
The `status` column is a plain string; the query in the handler selects `CloudJob.status`
(a `str`), so the guard compares against `.value` strings.

Current guard (src/phaze/routers/agent_files.py:176-182), the ONLY lines to change:
```python
cloud_job_row = (await session.execute(select(CloudJob.status, CloudJob.staging_bucket).where(CloudJob.file_id == file_id))).first()
cloud_job_status = cloud_job_row.status if cloud_job_row is not None else None
if cloud_job_row is None or cloud_job_status != CloudJobStatus.UPLOADED.value:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"staged object not ready (cloud_job status={cloud_job_status!r})",
    )
```

Timing proof — src/phaze/tasks/submit_cloud_job.py:117 stamps `status=CloudJobStatus.SUBMITTED.value`
at Kueue Job creation, BEFORE the analyze pod runs and calls presign-download.

Do NOT reuse `services/backends.py` `IN_FLIGHT` — that set includes `UPLOADING`, which is NOT
downloadable. Define a dedicated set in this router.

Existing test fixtures (tests/agents/routers/test_agent_presign_download.py):
- `s3_env` — one-kueue-backend backends.toml + moto server + bucket created.
- `_seed_file(session, agent, *, sha256=_SHA)` — inserts the FileRecord.
- `_seed_cloud_job(session, file_id, *, status=CloudJobStatus.UPLOADED)` — inserts a CloudJob with
  `staging_bucket="staging"` (resolvable in the registry).
- Test signature pattern: `(s3_env, authenticated_client, session, seed_test_agent)`;
  `agent, _token = seed_test_agent`.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Widen the presign readiness guard to the downloadable/staged status set</name>
  <files>src/phaze/routers/agent_files.py</files>
  <behavior>
    - status == UPLOADED  -> passes guard (200 path, unchanged)
    - status == SUBMITTED -> passes guard (200 path) — THE BUG FIX
    - status == RUNNING   -> passes guard (200 path) — THE BUG FIX
    - status == UPLOADING -> 409 "staged object not ready" (object not fully staged)
    - status == SUCCEEDED -> 409 (terminal, may be cleaned up)
    - status == FAILED    -> 409 (terminal, may be cleaned up)
    - no cloud_job row    -> 409
    - 409 detail still includes the actual status via `{cloud_job_status!r}`
  </behavior>
  <action>
    Add a module-level `frozenset[str]` near the top of the module (after imports) named
    `_PRESIGN_DOWNLOADABLE_STATUSES`, built from `{CloudJobStatus.UPLOADED.value,
    CloudJobStatus.SUBMITTED.value, CloudJobStatus.RUNNING.value}`. Add an explanatory comment:
    the staged object lives in the bucket from UPLOADED through RUNNING until post-success cleanup,
    so all three are downloadable; UPLOADING is not yet fully staged and terminal SUCCEEDED/FAILED
    may already be cleaned up — reference bug 260706-vqz (submit_cloud_job stamps SUBMITTED at Job
    creation, before the pod calls presign-download, so an UPLOADED-only guard is unreachable for a
    live pod). Deliberately NOT the backends.py IN_FLIGHT tuple (that includes UPLOADING).
    Change the guard condition from `cloud_job_status != CloudJobStatus.UPLOADED.value` to
    `cloud_job_status not in _PRESIGN_DOWNLOADABLE_STATUSES`. Keep `cloud_job_row is None` as the
    first disjunct and preserve the existing 409 detail message with `{cloud_job_status!r}`.
    Leave everything else in the handler untouched: 404 on unknown file, the bucket-resolvability
    409, the SERVER-side `expected_sha256` from FileRecord, and AUTH-01 path-only file_id.
    Type hints on the new binding; double quotes; line length 150.
  </action>
  <verify>
    <automated>uv run ruff check src/phaze/routers/agent_files.py &amp;&amp; uv run mypy src/phaze/routers/agent_files.py &amp;&amp; grep -q "_PRESIGN_DOWNLOADABLE_STATUSES" src/phaze/routers/agent_files.py</automated>
  </verify>
  <done>Guard admits {UPLOADED, SUBMITTED, RUNNING} via a dedicated documented frozenset; ruff + mypy clean.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Boundary regression tests across the full CloudJobStatus set</name>
  <files>tests/agents/routers/test_agent_presign_download.py</files>
  <behavior>
    - New: SUBMITTED -> 200 with a presigned download_url containing the file_id and expected_sha256 == _SHA
    - New: RUNNING   -> 200 with a presigned download_url containing the file_id
    - New: SUCCEEDED -> 409 with "not ready" in detail
    - New: FAILED    -> 409 with "not ready" in detail
    - Existing UPLOADING -> 409 and no-cloud_job -> 409 remain green (do not weaken them)
  </behavior>
  <action>
    Extend the existing module using its fixtures (`s3_env`, `authenticated_client`, `session`,
    `seed_test_agent`) and helpers (`_seed_file`, `_seed_cloud_job`). Add positive tests seeding
    `_seed_cloud_job(session, file.id, status=CloudJobStatus.SUBMITTED)` and `...RUNNING`, asserting
    `resp.status_code == 200`, `str(file.id) in body["download_url"]`, and
    `body["expected_sha256"] == _SHA` (mirror `test_presign_download_returns_url_and_server_sourced_sha256`).
    Add negative tests seeding `SUCCEEDED` and `FAILED`, asserting `409` and `"not ready" in
    resp.json()["detail"]`. Prefer a `pytest.mark.parametrize` over the four statuses if it keeps the
    module tidy, but plain per-status tests are acceptable — match the surrounding style. Do not modify
    the existing UPLOADING/no-cloud_job/unknown-file/unresolvable-bucket tests.
  </action>
  <verify>
    <automated>uv run pytest tests/agents/routers/test_agent_presign_download.py -q</automated>
  </verify>
  <done>All presign tests pass, including new SUBMITTED/RUNNING 200 and SUCCEEDED/FAILED 409 cases.</done>
</task>

</tasks>

<verification>
- `uv run pytest tests/agents/routers/test_agent_presign_download.py -q` — all green.
- `uv run ruff check src/phaze/routers/agent_files.py tests/agents/routers/test_agent_presign_download.py` — clean.
- `uv run mypy src/phaze/routers/agent_files.py` — clean.
- `uv run pytest --cov=phaze.routers.agent_files --cov-report=term-missing tests/agents/routers/test_agent_presign_download.py` — touched module ≥ 90%.
</verification>

<success_criteria>
- presign_download returns 200 for cloud_job.status in {UPLOADED, SUBMITTED, RUNNING}.
- presign_download returns 409 for UPLOADING, SUCCEEDED, FAILED, and missing cloud_job row.
- The downloadable set is a dedicated documented module-level frozenset (not backends.py IN_FLIGHT).
- All other handler behavior (404, bucket-resolvability 409, server-sourced sha256, AUTH-01) unchanged.
- ruff, mypy strict, and ≥90% coverage on the touched module all satisfied.
</success_criteria>

<output>
Create `.planning/quick/260706-vqz-fix-cloud-burst-presign-download-status-/260706-vqz-SUMMARY.md` when done
</output>
