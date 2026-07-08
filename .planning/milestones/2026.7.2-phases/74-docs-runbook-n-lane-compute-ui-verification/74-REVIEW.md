---
phase: 74-docs-runbook-n-lane-compute-ui-verification
reviewed: 2026-07-06T05:37:21Z
depth: standard
files_reviewed: 9
files_reviewed_list:
  - docker-compose.cloud-agent.yml
  - docs/README.md
  - docs/cloud-burst.md
  - docs/configuration.md
  - docs/multi-compute.md
  - docs/runbook.md
  - src/phaze/services/backends.py
  - tests/agents/deployment/test_cloud_agent_compose.py
  - tests/shared/services/test_lane_snapshot.py
findings:
  critical: 0
  warning: 1
  info: 2
  total: 3
status: issues_found
---

# Phase 74: Code Review Report

**Reviewed:** 2026-07-06T05:37:21Z
**Depth:** standard
**Files Reviewed:** 9
**Status:** issues_found

## Summary

Phase 74 (MCOMP-07) is a documentation + test + docstring-correction phase, and the diff is
correspondingly low-risk. I verified the load-bearing mechanical claims rather than trusting them:

- **Compose parametrization is sound.** `docker-compose.cloud-agent.yml` wraps the previously
  hardcoded `image:` and `command:` in `${PHAZE_CLOUD_AGENT_IMAGE:-…}` / `${PHAZE_CLOUD_AGENT_CMD:-…}`.
  The image default nests a second substitution (`${…:-ghcr.io/…:${PHAZE_IMAGE_TAG:-latest}-arm64}`),
  which Compose v2 (compose-go) resolves recursively; YAML parses cleanly (no bare `": "`), confirmed
  by the guard tests reading the file through `yaml.safe_load`. With neither override set the arm64
  default renders byte-identically to the prior file — the guard tests assert exactly this.
- **All new doc cross-references resolve.** `multi-compute.md`'s links to
  `configuration.md#backend-registry-backendstoml`, `deployment.md#step-4--populate-the-file-server-env`,
  `cloud-burst.md#step-5--bring-up-the-compute-agent-docker-composecloud-agentyml`,
  `runbook.md#reading-the-n-lanes`, `runbook.md#spillover-behavior`, and `k8s-burst.md` all match
  live headings/files. The new index/runbook/config/cloud-burst back-links are correct.
- The `backends.py` change is docstring-only, as scoped.

One genuine robustness concern remains in the new test + the docstring it certifies (WR-01). Two
minor doc-quality items (IN-01, IN-02).

Pre-existing defects outside this phase's diff (e.g. the duplicated `# MKUE-01/D-04:` comment block at
`src/phaze/services/backends.py:494-496`) were left unflagged per scope.

## Warnings

### WR-01: Arbiter test asserts the ABSENCE of a documented-unsafe concurrent-session race — flaky-CI risk; docstring certifies it as "race-free" on empirical grounds only

**File:** `tests/shared/services/test_lane_snapshot.py:498-527` (test), `src/phaze/services/backends.py:651-666` (docstring)

**Issue:**
The new `test_compute_probe_real_fanout_keeps_both_lanes_online` deliberately runs the **real**
`_probe_availability` fan-out (no monkeypatch) over two online `ComputeAgentBackend`s bound to a single
shared `AsyncSession`, and asserts `availability == {"a1-arm64": True, "x86-spill": True}`.

Under the hood each compute probe path is
`ComputeAgentBackend.is_available` → `select_agent_by_id` → `await session.execute(...)`
(confirmed at `src/phaze/services/enqueue_router.py:155`). `_probe_availability`
(`backends.py:665`) launches these via `asyncio.gather`, so for N≥2 online compute lanes two
`session.execute` calls are in flight on **one** `AsyncSession`. SQLAlchemy explicitly forbids
concurrent operations on a single `AsyncSession` and guards it with
`InvalidRequestError`/`IllegalStateChangeError` — but whether that guard fires depends on whether the
first `execute` suspends into I/O before the second is scheduled, i.e. it is **timing-dependent**. If
it fires under CI load (the repo already has documented DB-flake-under-VM-pressure and
bucket-isolation sensitivities), `_probe_one` catches it and degrades the raced lane to
`available=False`, and the equality assertion fails → intermittent CI failure.

The test's own docstring frames itself as an "arbiter … only settles whether the race manifests" and
notes the fix would be "serializing the compute probes" — i.e. the phase knowingly shipped a test that
green-lights a happy-path outcome of a race it did not fix. The corrected `_probe_availability`
docstring (`backends.py:655-663`) then certifies the pattern as "proven race-free in practice …
deterministically both `available=True` across repeated runs." That is an **empirical** claim about a
structurally concurrency-unsafe pattern; the code does not serialize the probes, and the docstring
itself hedges by relying on the post-fan-out `session.rollback` to "clear any single-probe DB poison,"
which concedes a probe can poison the session.

(The unserialized concurrent-session use in `_probe_availability` is pre-existing from Phase 71/72 and
was correctly left out of scope; production impact is bounded — a raced probe only flaps one lane's
`available` flag for a single 5s poll and self-heals, no data loss. The reviewable defect here is the
new test's fragility plus the new docstring blessing the pattern as guaranteed-safe.)

**Fix:**
Make the arbiter deterministic instead of asserting a race does not occur. Either serialize the compute
probes so the property is structural rather than timing-dependent, e.g. in `_probe_availability`:

```python
async def _probe_availability(session, backends):
    # Compute probes share `session`; serialize them so concurrent session.execute
    # can never race (SQLAlchemy forbids concurrent ops on one AsyncSession).
    results = [await _probe_one(session, b) for b in backends]
    return dict(results)
```

(the fan-out latency bound still holds — each `_probe_one` is individually `wait_for`-bounded), or, if
concurrency must be retained, give each compute probe its own session/connection. Then reword the
`_probe_availability` docstring to state the *structural* guarantee rather than "proven race-free in
practice … across repeated runs." If neither is in scope, at minimum drop the real-fan-out assertion or
mark the test with a stability guard so it cannot flake the shared suite.

## Info

### IN-01: Grammar error in `multi-compute.md` "See also"

**File:** `docs/multi-compute.md:176`

**Issue:** "the canonical `[[backends]]` field reference (do not restated here)." — "do not restated"
is ungrammatical.

**Fix:** "(not restated here)" or "(do not restate it here)".

### IN-02: `cloud-agent` compose guard doc-comment references a moved test path

**File:** `docker-compose.cloud-agent.yml:22`

**Issue:** The header comment says invariants are "asserted by
`tests/test_deployment/test_cloud_agent_compose.py`", but the test now lives at
`tests/agents/deployment/test_cloud_agent_compose.py` (its own module docstring still cites the old
`tests/test_deployment/…` path too). The stale pointer is cosmetic but will misdirect a maintainer.
This comment line is outside the phase's diff hunks; noted only because the phase touches this file and
the same file's parametrization is the phase's subject.

**Fix:** Update the path to `tests/agents/deployment/test_cloud_agent_compose.py` (and the mirrored
reference in the test's own docstring) when convenient.

---

_Reviewed: 2026-07-06T05:37:21Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
