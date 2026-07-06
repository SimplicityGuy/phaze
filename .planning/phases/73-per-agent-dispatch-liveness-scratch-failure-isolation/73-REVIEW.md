---
phase: 73-per-agent-dispatch-liveness-scratch-failure-isolation
reviewed: 2026-07-05T21:59:52Z
depth: standard
files_reviewed: 6
files_reviewed_list:
  - src/phaze/config_backends.py
  - src/phaze/config.py
  - src/phaze/routers/agent_push.py
  - src/phaze/schemas/agent_tasks.py
  - src/phaze/services/backends.py
  - src/phaze/tasks/push.py
findings:
  critical: 1
  warning: 4
  info: 1
  total: 6
status: issues_found
---

# Phase 73: Code Review Report

**Reviewed:** 2026-07-05T21:59:52Z
**Depth:** standard
**Files Reviewed:** 6
**Status:** issues_found

## Summary

Reviewed the six Phase 73 source files (per-agent dispatch/liveness/scratch/failure-isolation:
`ComputeBackend.push_host` + `resolve_compute_backend`, the `PushFilePayload.dest_*` per-file
destination fields, the payload-driven rsync `remote_dest`, and the `/pushed`+`/mismatch`
callback re-keying off `cloud_job.backend_id`). The `config.py`/`config_backends.py` registry
changes are sound and well fail-fast (duplicate `agent_ref` guard, per-variant required-field
validators, clean retirement of `active_compute_scratch_dir`).

**Security note requested in the phase intent (rsync `remote_dest` argv-injection):** verified.
The `--` argv terminator in `_build_rsync_argv` (`tasks/push.py`) blocks flag-smuggling through
`original_path`/`remote_dest` as *rsync's own* top-level flags, `dest_host`/`dest_ssh_user` are
schema-validated against whitespace and shell metacharacters, and — critically — the remote spec
is always rendered as `"<ssh_user>@<dest_host>:..."` with `ssh_user` guaranteed non-empty
(`_require_push_config` requires `push_ssh_user is not None`, and `ComputeBackend` requires a
non-empty `push_host`), so the combined token handed to the local `ssh` argv can never itself
begin with `-`, which is the precondition for the classic "hostname-as-ssh-option" injection
(e.g. `-oProxyCommand=...`). Combined with `dest_host`/`dest_scratch_dir`/`dest_ssh_user` being
server-stamped from trusted operator config (never attacker/network input), I did not find an
exploitable residual path through this specific vector. I *did* find an adjacent, real gap in the
`/mismatch` callback's authorization/idempotency discipline (below) that is unrelated to the rsync
argv question but sits in the same phase surface, and a couple of defense-in-depth gaps in the new
`dest_*` validation that are worth closing even though not currently reachable by an attacker.

## Critical Issues

### CR-01: `report_push_mismatch` can force an arbitrary file to `AWAITING_CLOUD` with no reporter check and no state guard

**File:** `src/phaze/routers/agent_push.py:214-259`
**Issue:**

The new D-07 reporter-identity gate is skipped entirely whenever `resolve_compute_backend` returns
`None` (no `cloud_job` row exists for the file, or its `backend_id` names a removed/non-compute
backend):

```python
cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one_or_none()
backend = resolve_compute_backend(settings, cloud_job.backend_id if cloud_job else None)
if backend is not None and agent.id != backend.agent_ref:
    raise HTTPException(status_code=403, ...)
```

`get_authenticated_agent` (`routers/agent_auth.py`) only verifies the bearer token belongs to
*some* non-revoked `Agent` row — it does not check the agent's `kind`, nor any relationship
between that agent and the `file_id` in the URL. So when `backend is None`, **any** authenticated
agent (any fileserver, any of the N compute agents) can call
`POST /api/internal/agent/push/{file_id}/mismatch` for **any** `file_id`, including one it has no
legitimate relationship to.

Worse, the over-cap "spill" branch mutates `FileRecord`/`CloudJob` **unconditionally**, with no
precondition that the file is actually `PUSHING` — unlike `report_pushed`'s WR-02 CAS guard
(`.where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING)`):

```python
if next_attempt > settings.push_max_attempts:
    await session.execute(update(FileRecord).where(FileRecord.id == file_id).values(state=FileState.AWAITING_CLOUD))
    await session.execute(update(CloudJob).where(CloudJob.file_id == file_id).values(status=CloudJobStatus.FAILED.value, attempts=settings.cloud_submit_max_attempts))
    await clear_ledger_entry(session, ledger_key)
    await session.commit()
    ...
```

Any authenticated agent can drive the `push_attempt` counter in the `push_file:<file_id>`
`SchedulingLedger` row past `push_max_attempts` (default 3) by POSTing `/mismatch` repeatedly for
that `file_id`, then on the next call trip the unconditional `UPDATE FileRecord ... SET state =
AWAITING_CLOUD` — regardless of the file's actual current state (e.g. `ANALYZED`, `PROPOSED`,
`EXECUTED`) and regardless of whether that agent has any real connection to the file. This reverts
an already-completed file back into `AWAITING_CLOUD`, causing lost/duplicated downstream work and
violating the state-machine invariants the rest of the pipeline (proposal generation, execution)
assumes.

This is not a hypothetical: the project's own
`tests/agents/routers/test_agent_push.py::test_push_mismatch_over_cap_spills_to_awaiting_cloud_and_clears_ledger`
exercises *exactly* this "no `cloud_job` row → no reporter gate → unconditional spill" path and
asserts it succeeds — proving the gap is real and currently untested against a
wrong-state/wrong-reporter combination (the test conveniently seeds the file at
`state=FileState.PUSHING` before spilling, so the missing precondition never surfaces).

**Fix:** Mirror `report_pushed`'s WR-02 discipline — gate the spill (and, ideally, the re-drive)
`UPDATE FileRecord` on the current state actually being `PUSHING`, checking `rowcount` before
proceeding to touch `CloudJob`/the ledger, e.g.:

```python
res = await session.execute(
    update(FileRecord)
    .where(FileRecord.id == file_id, FileRecord.state == FileState.PUSHING)
    .values(state=FileState.AWAITING_CLOUD)
)
if res.rowcount == 0:
    await session.commit()
    return PushMismatchResponse(file_id=file_id, cleared=False)
```

Additionally, consider not skipping the reporter check entirely when `backend is None` — at
minimum require the calling agent's `kind == "compute"` (mirroring the docstring's own claim that
"/mismatch is reported by the COMPUTE agent"), so a fileserver agent's token cannot be used to
manipulate an unrelated file's push-retry/backend state at all.

## Warnings

### WR-01: `dest_scratch_dir` is missing the same shell-metacharacter validation as `dest_host`/`dest_ssh_user`

**File:** `src/phaze/schemas/agent_tasks.py:97-124`
**Issue:** `_dest_scratch_absolute` only checks that `dest_scratch_dir` starts with `/`; it does
not run it through `_DEST_HOST_FORBIDDEN` the way `_dest_host_safe`/`_dest_ssh_user_safe` do, even
though `dest_scratch_dir` is interpolated into the same remote spec
(`"<user>@<dest_host>:<dest_scratch_dir>/<file_id>.<file_type>"`, `tasks/push.py:107`). Because
`ssh` (absent `-N`) normally hands its remote command to the target's login shell for execution,
the *path* half of the remote spec arguably has a stronger injection surface on the remote host
than the *host* half — yet it is the one field left unguarded. Currently mitigated only by the
fact that `dest_scratch_dir` is operator-supplied (`backends.toml`), never attacker input, but the
inconsistency undercuts the stated "defense-in-depth at the schema layer (T-73-01)" intent and
should be closed to match `dest_host`/`dest_ssh_user`.

**Fix:**
```python
@field_validator("dest_scratch_dir")
@classmethod
def _dest_scratch_absolute(cls, v: str | None) -> str | None:
    if v is not None:
        if not v.startswith("/"):
            raise ValueError("dest_scratch_dir must be an absolute path")
        if any(ch in cls._DEST_HOST_FORBIDDEN for ch in v):
            raise ValueError("dest_scratch_dir must not contain whitespace or shell metacharacters")
    return v
```

### WR-02: `_build_rsync_argv` builds a literal `"...@None:None/..."` remote spec if `dest_host`/`dest_scratch_dir` are ever `None`

**File:** `src/phaze/tasks/push.py:106-107`
**Issue:**
```python
ssh_user = payload.dest_ssh_user or cfg.push_ssh_user
remote_dest = f"{ssh_user}@{payload.dest_host}:{payload.dest_scratch_dir}/{payload.file_id}.{payload.file_type}"
```
`PushFilePayload.dest_host`/`dest_scratch_dir` are `Optional[str] = None` at the schema level (by
design, per the Phase 73 comment: "a four-field construction must still validate until then"), but
nothing in `push_file`/`_build_rsync_argv` guards against them actually being `None` at the point
of use. Every *current* producer (`services/backends.py::_enqueue_push_file`,
`routers/agent_push.py::report_push_mismatch`) does populate them, so this isn't reachable today —
but a future producer, a test helper, or a code-path regression that constructs a bare
`PushFilePayload(file_id=..., original_path=..., file_type=..., agent_id=...)` would silently
produce the exact `"None:..."` remote-spec bug this phase's own comments elsewhere explicitly
guard against fail-fast (`ComputeBackend._require_dispatch_fields`, `ComputeAgentBackend._destination`).
Here there is no equivalent guard.

**Fix:** Raise a clear `RuntimeError` in `push_file`/`_build_rsync_argv` when `dest_host` or
`dest_scratch_dir` is `None`, e.g.:
```python
if payload.dest_host is None or payload.dest_scratch_dir is None:
    raise RuntimeError(f"push_file: payload for file_id={payload.file_id} is missing dest_host/dest_scratch_dir")
```

### WR-03: `ComputeBackend._require_dispatch_fields` only fails fast on missing `push_host`, not on an unsafe one

**File:** `src/phaze/config_backends.py:99-116`
**Issue:** `_require_dispatch_fields` checks only `if not self.push_host: raise ValueError(...)` —
it never runs `push_host` (or `ssh_user`) through the stricter shape check
`PushFilePayload._dest_host_safe` applies later. A malformed `backends.toml` entry (e.g.
`push_host` containing a stray space or shell metacharacter from a copy-paste error) passes
config-load-time validation cleanly and only surfaces as a `pydantic.ValidationError` deep inside
the first `push_file` dispatch — a much worse failure mode than the "fail fast at construction,
id-tagged" discipline this same module otherwise applies everywhere else (see the
`agent_ref`/`scratch_dir`/`push_host` presence checks immediately above).

**Fix:** Either import and reuse the same forbidden-character check in
`_require_dispatch_fields`, or factor it into a shared helper both `config_backends.py` and
`schemas/agent_tasks.py` call, so a malformed `push_host`/`ssh_user` in `backends.toml` fails at
boot with the `backend {id!r}` message instead of at the first dispatch.

### WR-04: Ledger `push_attempt` read-modify-write in `report_push_mismatch` is not concurrency-safe

**File:** `src/phaze/routers/agent_push.py:225-230, 313-318`
**Issue:** The `push_attempt` counter is read (`SELECT ... SchedulingLedger`), incremented in
Python, and written back (`UPDATE ... SET payload = merged`) without a `SELECT ... FOR UPDATE` or
equivalent locking. Two concurrent `/mismatch` calls for the same `file_id` (plausible: a fileserver
retry racing a genuinely duplicate report) can both read the same `current_attempt`, both compute
the same `next_attempt`, and the second write clobbers the first — silently under-counting attempts
and delaying (or in a pathological interleave, indefinitely postponing) the `push_max_attempts`
spill. This pattern pre-dates Phase 73 but is exercised by the same code path this phase modified;
worth closing alongside CR-01 since both are about `/mismatch` mutation safety.

**Fix:** Use `select(...).with_for_update()` on the `SchedulingLedger` row (or an advisory lock
keyed on `ledger_key`) before the read-increment-write sequence.

## Info

### IN-01: `_require_push_config`/`ComputeBackend._require_dispatch_fields` treat `""` as valid (only `None` fails)

**File:** `src/phaze/tasks/push.py:131`, `src/phaze/config_backends.py:110-115`
**Issue:** Both fail-fast checks use `is None` (`push.py`) or truthiness against `None` only via
`not self.push_host` (which *does* catch `""` in `config_backends.py`, but `_require_push_config`'s
`getattr(cfg, name) is None` does **not** catch an operator-set empty string, e.g.
`PHAZE_PUSH_SSH_USER=""`). An empty `push_ssh_user` would pass `_require_push_config` and then
silently fall through as the `dest_ssh_user or cfg.push_ssh_user` fallback source, producing a
scarcely-diagnosable `"@host:..."` remote spec instead of the intended fail-fast error.
**Fix:** Change `_require_push_config`'s predicate to `not getattr(cfg, name)` so an empty string
is treated the same as `None`.

---

_Reviewed: 2026-07-05T21:59:52Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
