# Phase 75: Engineering Hygiene — Guard Hardening, Tech-Debt & Stale-Tracking Cleanup - Research

**Researched:** 2026-07-06
**Domain:** Cross-milestone engineering-hygiene reconciliation (docs/tracking) + one force-local regression test
**Confidence:** HIGH (every file:line claim verified against the live worktree this session)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** HYG-01 — **Close as already-satisfied; no new code, no new test.** The FileNotFoundError-on-absent-REQUIREMENTS.md fix already landed in PR #207 (`ec80a53a`). `test_requirements_traceability.py` defines `_NO_ACTIVE_MILESTONE = not _REQUIREMENTS.exists()` and skipif-gates the active-milestone tests. The between-milestones `git rm REQUIREMENTS.md` close path already stays green.
- **D-02:** HYG-01's premise ("reads REQUIREMENTS.md with no existence check") is **stale**. Reconcile HYG-01's requirement text + traceability status to reflect that PR #207 satisfies it. The "regression test covers the archived/no-active-milestone case" clause is met by the existing module-level skip + `test_archived_milestones_internally_consistent` — no additional test.
- **D-03:** HYG-02 — **Delete both stale `cloud_target` breadcrumb comments** (`api` service + `worker` service). Keep the surrounding backends.toml mount explainer intact.
- **D-04:** HYG-02 premise correction: there is **no `PHAZE_CLOUD_TARGET` env line** anywhere in the repo — only the two comments exist. The executor removes comments only.
- **D-05:** HYG-03 — **Drop as superseded; make NO code change.** The `>1`-compute fail-fast was deleted outright by Phase 72 (D-03) to enable the N-compute capability. Re-adding a `>1`-compute boot reject would break Phases 72-74's shipped-and-verified behavior.
- **D-06:** The correct boot guard **already exists**: `config.py:_validate_registry` boot-rejects a *duplicate `agent_ref`*; `resolved_non_local_kind` returns `"compute"` for any N; single-/zero-compute paths byte-identical.
- **D-07:** Reconcile HYG-03's requirement text + STATE.md deferred row to **SUPERSEDED**, citing Phase 72 D-03. Documentation/tracking reconciliation, not implementation.
- **D-08:** WR-01 (`_probe_availability` concurrent shared-session probe) is **NOT fixed in this phase**. Stays a tracked deferred item. Bounded impact (flaps one lane for one 5s poll, self-heals).
- **D-09:** HYG-04 — **Add a real-route regression test** in `tests/shared/routers/test_pipeline.py` covering the three force-local gate sites (`pipeline.py:396`, `:718`, `:793`). Drive the actual endpoints (two duration-router triggers + the backfill route) with the persisted `get_route_control` toggle.
- **D-10:** HYG-04 assertions: with force-local **True**, every file routes local — **zero** `AWAITING_CLOUD` rows held, byte-identical to an all-local registry; with force-local **False**, long files held for the cloud drain. Backfill under force-local is a clean zero-mutation no-op. Exact fixture mechanics are planner discretion so long as the real `effective_cloud_enabled` fold is exercised.
- **D-11:** HYG-05 — Flip `63-UAT` to complete (0 pending scenarios). Mark quick-tasks `260628-wzq` + `260629-eev` complete (both already committed). Status reconciliation only.

### Claude's Discretion
- Exact wording of the reconciled requirement text for HYG-01 (satisfied) and HYG-03 (superseded), and the exact STATE.md deferred-row edits.
- HYG-04 test fixture mechanics (helper reuse, session setup) within the real-route constraint of D-09/D-10.

### Deferred Ideas (OUT OF SCOPE)
- **WR-01** — serialize the N-compute probe fan-out (`_probe_availability`, `backends.py`). Keep tracked; do NOT fix in Phase 75.
- **PROV-02 / PROV-03** — capability-aware routing + on-demand compute provisioning. Future milestone.
- Reviewed Todos: None matched.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| HYG-01 | Traceability guard no longer raises FileNotFoundError when REQUIREMENTS.md absent; regression covers the no-active-milestone case | VERIFIED already satisfied by PR #207 (`ec80a53a`). Reconcile-only — see Verification Finding 1. No code/test. |
| HYG-02 | Remove the stale `PHAZE_CLOUD_TARGET` env + comment lines from docker-compose | VERIFIED no env line exists; two breadcrumb comments present. 2-line (functionally) comment deletion — see Verification Finding 2. |
| HYG-03 | `>1`-compute fail-fast fires at boot rather than lazily | VERIFIED superseded — fail-fast was *deleted* by Phase 72; correct boot guard (duplicate-`agent_ref`) already exists. Reconcile-to-SUPERSEDED only — see Verification Finding 3. |
| HYG-04 | Force-local duration-router gate covered by a committed regression test (3 gate sites) | VERIFIED all three gate sites + endpoints + toggle plumbing; NO existing coverage. The one genuine deliverable — see Verification Finding 4 + Validation Architecture. |
| HYG-05 | Reconcile stale 2026.7.0 tracking (63-UAT, quick-tasks 260628-wzq/260629-eev) | VERIFIED both SUMMARY.md files exist, both commits present, 63-UAT partial with 0 pending. Bookkeeping only — see Verification Finding 5. |
</phase_requirements>

## Summary

This is a **reconciliation-heavy** milestone-close hygiene sweep, not an implementation phase. Four of the five HYG requirements are docs/tracking edits under `.planning/` (HYG-01, HYG-03, HYG-05) or a comment deletion in `docker-compose.yml` (HYG-02). **Only HYG-04 writes executable code** — a regression test in `tests/shared/routers/test_pipeline.py`. There is **no `src/` behavior change anywhere in the phase**, no new dependencies, and no user-facing change.

Every file:line claim in CONTEXT.md was verified against the live worktree. **All locked decisions remain valid** — nothing surfaced that would invalidate D-01..D-11. A handful of line numbers have drifted by a few lines (documented in the Verification Findings table below), and one helper named in CONTEXT (`set_route_control`) does not exist as a service function — the persisted toggle is written either by directly adding a `RouteControl(id="global", force_local=True)` row (recommended for the test) or via `POST` to the `routers/routing.py` force-local endpoint. Neither drift changes any decision.

**Primary recommendation:** Plan HYG-01/02/03/05 as pure reconciliation tasks (no verification beyond `just docs-drift` + `git grep` checks). Plan HYG-04 as a single new test module region driving the three real endpoints with a persisted `RouteControl` row, modeled byte-for-byte on the existing `test_backfill_disabled_when_cloud_local` / `test_backfill_enabled_resets_and_holds` pair — swapping the "all-local registry" driver for a "cloud-ON registry + force_local=True row" driver and asserting identical zero-mutation / zero-`AWAITING_CLOUD` outcomes.

## Verification Findings (CONTEXT.md claims vs. live code)

| # | CONTEXT claim | Live reality | Drift | Decision impact |
|---|---------------|--------------|-------|-----------------|
| 1 | HYG-01: `_NO_ACTIVE_MILESTONE` L64; skipif `:257/:266/:276` (D-01) / `L256/265/275` (refs) | `_NO_ACTIVE_MILESTONE` at **L64** ✓; skipif at **L256, L265, L275** (refs correct; D-01's 257/266/276 off-by-one). A **4th** skipif at **L327** (`test_inflight_phase_with_unmarked_requirements_passes`) not listed in CONTEXT. Skip evaluates *before* body runs, so `_read(_REQUIREMENTS)` never fires when absent → FileNotFoundError provably prevented. | Minor line drift; 1 extra skipif undocumented | **None** — D-01/D-02 hold. Already satisfied. |
| 2 | HYG-02: comments at `docker-compose.yml:24` (api) + `:52` (worker); no `PHAZE_CLOUD_TARGET` env (D-04) | `git grep PHAZE_CLOUD_TARGET -- 'docker-compose*.yml'` → **CLEAN** ✓. api breadcrumb spans **L24-25** (two physical lines); worker breadcrumb is **L52** (one line). backends.toml explainer to KEEP: api L21-23, worker L49-51. | api comment is 2 physical lines, not 1 | **None** — D-03/D-04 hold. See Pitfall 1 for the L25 boundary nuance. |
| 3 | HYG-03: `resolved_non_local_kind:573` returns "compute"; `_validate_registry:437` dup-`agent_ref` reject | `resolved_non_local_kind` def at **L550**, compute-branch `return non_local[0].kind` at **L574** (L573 is the comment) ✓. `_validate_registry` at **L415**, dup-`agent_ref` reject at **L437-450** ✓. `_probe_availability` at **L651** (refs said `:665`). Docstrings explicitly state Phase 72 D-03 retired the `>1`-compute raise. | `_probe_availability` line drift (651 vs 665); return one line below stated | **None** — D-05/D-06/D-07 hold. Re-adding a `>1` reject WOULD break shipped N-compute (confirmed). |
| 4 | HYG-04: gate sites `pipeline.py:396/718/793`; `get_route_control`/`set_route_control` toggle; no existing coverage | Gate sites **exact**: L396 (`trigger_analysis`, `POST /api/v1/analyze`), L718 (`trigger_analysis_ui`, `POST /pipeline/analyze`), L793 (`trigger_backfill_cloud`, `POST /pipeline/backfill-cloud`) ✓. `get_route_control` exists (`services/route_control.py`). **`set_route_control` does NOT exist** — writer is `routers/routing.py::force_local` (POST) or a direct `RouteControl` row insert. `grep force_local/route_control` in `test_pipeline.py` → **no existing coverage** ✓. | `set_route_control` helper is fictional | **None** — D-09/D-10 hold; use a direct `RouteControl` row insert (see Validation Architecture). |
| 5 | HYG-05: quick-task SUMMARY files exist + committed; 63-UAT 0 pending | Both `SUMMARY.md` files exist. Commits **present**: `5f43aa7` (260628-wzq), `267109b` (260629-eev). STATE.md L234 shows `63-UAT partial` with "0 pending scenarios". | None | **None** — D-11 holds. |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| HYG-01 traceability reconcile | Planning docs (`.planning/`) | Test infra (verify-only) | The guard test already exists; only REQUIREMENTS.md text + traceability row change. |
| HYG-02 compose comment delete | Deployment config (`docker-compose.yml`) | — | Comment-only; no service/env semantics touched. |
| HYG-03 N-compute reconcile | Planning docs (`.planning/`) | API/Backend (read-only confirm) | Confirm `backends.py`/`config.py` already correct; edit only REQUIREMENTS.md + STATE.md. |
| HYG-04 force-local gate test | Test infra (`tests/shared/routers/`) | API/Backend (routes under test) | Drives the real FastAPI pipeline routes; asserts the `effective_cloud_enabled` fold + `route_control` persistence. |
| HYG-05 tracking reconcile | Planning docs (`.planning/STATE.md` + quick SUMMARY) | — | Status flips only; underlying work already committed. |

## Standard Stack

**No new dependencies.** Zero-dependency phase (parity with the whole 2026.7.2 milestone). All work uses the existing test + docs toolchain.

| Tool | Version (pyproject) | Purpose | Why Standard |
|------|--------------------|---------|--------------|
| pytest | >=9.1.1 | HYG-04 test runner | Project standard; `uv run pytest`. |
| pytest-asyncio | >=1.4.0 | async route tests | All `test_pipeline.py` tests are `@pytest.mark.asyncio`. |
| httpx `AsyncClient` | (transitive) | drives the FastAPI routes | The `client` fixture (conftest L213) is FastAPI's recommended async test client. |
| pytest-cov | >=7.1.0 | coverage (90% floor) | Enforced per CLAUDE.md; HYG-04 raises `pipeline.py` force-local branch coverage. |

**Installation:** none — `uv sync` already provides everything.

## Package Legitimacy Audit

Not applicable — **this phase installs zero packages**. No `slopcheck` / registry verification required.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Persisting the force-local toggle in the HYG-04 test | A bespoke monkeypatch of `get_route_control` | A real `RouteControl(id="global", force_local=True)` row added via the shared `session` | D-09 requires the *real* `effective_cloud_enabled` fold be exercised; the `client` fixture overrides `get_session` to the same `session`, so a seeded row is visible to the endpoint. Monkeypatching the reader would bypass the fold under test. |
| Zero-mutation / zero-`AWAITING_CLOUD` assertions | A new assertion harness | The existing `test_backfill_disabled_when_cloud_local` pattern (`session.refresh` + state assert + `SchedulingLedger` empty-select) | Proven idiom in the same file; keeps the new test at the same altitude. |
| Driving the routes | Unit-calling `_route_discovered_by_duration` directly | `await client.post("/api/v1/analyze" | "/pipeline/analyze" | "/pipeline/backfill-cloud")` | D-09 mandates real-route/endpoint altitude so the gate site (not the helper) is covered. |

**Key insight:** the entire HYG-04 deliverable is a *composition* of existing test helpers (`_make_file`, `_persist_files_with_duration`, `_persist_failed_with_duration`, `seed_active_agent`, `wire_fakes`, `install_fake_queues`, `_drain_background`, `_LONG`/`_SHORT`) plus one new row insert. No new fixtures or harness code should be built.

## Runtime State Inventory

> This is a rename/refactor-adjacent (reconciliation) phase, but it edits **only planning docs, one compose comment, and a test file**. There is no string-rename, no datastore key change, no service reconfiguration. Each category is answered explicitly:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | **None** — no database keys, collection names, or user_ids are renamed or touched. HYG-04 seeds an ephemeral `RouteControl` row only in the test DB. | None |
| Live service config | **None** — no n8n/Datadog/Tailscale/Cloudflare config. HYG-02 deletes an *inert* comment; there was never a live `PHAZE_CLOUD_TARGET` env consumed (`extra=ignore` drops nothing because the key is unset — verified `git grep` clean). | None |
| OS-registered state | **None** — no Task Scheduler / pm2 / systemd / launchd registrations reference anything in scope. | None |
| Secrets/env vars | **None** — no secret keys or env var names change. HYG-02's target is a comment, not an env line (D-04 verified). | None |
| Build artifacts | **None** — no `pyproject.toml`/package rename; no egg-info/wheels/images affected. HYG-04 adds test code that ships in the same wheel-excluded `tests/` tree. | None |

**Canonical question answered:** After the planning-doc + comment + test edits land, **no runtime system holds any old string cached, stored, or registered** — because nothing is renamed and no runtime config is changed.

## Common Pitfalls

### Pitfall 1: Over-deleting the backends.toml explainer in HYG-02
**What goes wrong:** The `api` service breadcrumb spans **two physical lines (L24-25)** and the stale sentence continues into "…Mount a backends.toml to enable cloud backends." on L25. Deleting too much removes the legitimate explainer (L21-23) that D-03 says to KEEP; deleting too little leaves the stale "Phase 67 / removed cloud_target" reference.
**Why it happens:** CONTEXT says "L24" (singular) but the api breadcrumb is 2 lines; the worker breadcrumb is 1 line (L52).
**How to avoid:** Delete exactly the "Replaces the removed cloud_target selector + flat …fields (Phase 67…)" sentence in both services. Keep the three-line "Backend execution registry … declared in a backends.toml mounted at PHAZE_BACKENDS_CONFIG_FILE … zero-config implicit all-local registry" explainer. The trailing "Mount a backends.toml to enable cloud backends." (api L25) is executor discretion — it is arguably explainer, not breadcrumb; the safe read is to drop only the Phase-67/cloud_target reference and keep operator-useful guidance.
**Warning signs:** `git grep -n "cloud_target\|Phase 67" docker-compose.yml` must return clean after the edit; the backends.toml explainer must still be present.

### Pitfall 2: Asserting a routing *count* instead of `AWAITING_CLOUD` *absence* in HYG-04
**What goes wrong:** A test that only checks `enqueued == N` can pass even if a long file was silently held in `AWAITING_CLOUD` under force-local (the exact regression the T-71-08 backfill comment guards).
**Why it happens:** The subtle bug is a *state mutation*, not a routing count.
**How to avoid:** Assert (a) **zero** files in `FileState.AWAITING_CLOUD` after force-local analyze of a long file, (b) the long file routed local (or was skipped only for no-agent reasons, never held), and (c) for backfill, the `ANALYSIS_FAILED` row is **never** reset to `DISCOVERED` and **no** `SchedulingLedger` row is seeded — byte-identical to `test_backfill_disabled_when_cloud_local`.
**Warning signs:** the test still passes if you comment out the `and not await get_route_control(session)` clause — that means it isn't actually gating on the fold.

### Pitfall 3: Reintroducing a `>1`-compute boot reject for HYG-03
**What goes wrong:** Taking the requirement text literally and adding a boot-time `>1`-compute raise breaks Phases 72-74's shipped-and-verified N-compute capability, its tests, and its docs.
**Why it happens:** HYG-03 was authored from a stale snapshot; its premise was overtaken by Phase 72 D-03.
**How to avoid:** Make **no code change**. Reconcile the requirement + STATE.md row to SUPERSEDED, citing Phase 72 D-03. The correct boot guard (duplicate-`agent_ref`) already lives in `_validate_registry`.
**Warning signs:** any diff under `src/` for HYG-03 is a red flag.

## Validation Architecture

> Nyquist validation is ENABLED. This section covers **HYG-04 only** — the sole testable deliverable. HYG-01/02/03/05 are reconciliation edits validated by `just docs-drift` (already wired) + `git grep` guards, not by new tests.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.1.1 + pytest-asyncio 1.4.0 (async routes via httpx `AsyncClient`) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (L136) |
| Target file | `tests/shared/routers/test_pipeline.py` (2299 lines; add a new force-local region) |
| Bucket | `shared` (per `tests/buckets.json`) |
| Quick run command | `uv run pytest tests/shared/routers/test_pipeline.py -x` |
| Full suite command | `uv run pytest` (or `just test-bucket shared` for the isolated bucket per the CI-isolation memory) |

### The three gate sites (what each proposed case must exercise)

| Gate site | Endpoint | Fold under test | Duration-router trigger? |
|-----------|----------|-----------------|--------------------------|
| `pipeline.py:396` (`trigger_analysis`) | `POST /api/v1/analyze` | `effective_cloud_enabled = settings.cloud_enabled and not await get_route_control(session)` → passed to `_route_discovered_by_duration` | Yes (trigger #1) |
| `pipeline.py:718` (`trigger_analysis_ui`) | `POST /pipeline/analyze` | same fold → same router | Yes (trigger #2) |
| `pipeline.py:793` (`trigger_backfill_cloud`) | `POST /pipeline/backfill-cloud` | `if not settings.cloud_enabled or await get_route_control(session): return zero-mutation no-op` | No (backfill early-return) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| HYG-04 | Force-local True on `/api/v1/analyze`: a `_LONG` file routes local, **zero** `AWAITING_CLOUD` held (gate L396) | route/integration | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_analyze_api -x` | ❌ Wave 0 |
| HYG-04 | Force-local True on `/pipeline/analyze`: same — long file not held (gate L718) | route/integration | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_analyze_ui -x` | ❌ Wave 0 |
| HYG-04 | Force-local True on `/pipeline/backfill-cloud`: zero-mutation no-op — `ANALYSIS_FAILED` unchanged, no ledger seed (gate L793) | route/integration | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local_backfill -x` | ❌ Wave 0 |
| HYG-04 | Force-local False (control): long file IS held for the cloud drain (registry honored) — proves the gate is the *only* difference | route/integration | `uv run pytest tests/shared/routers/test_pipeline.py -k force_local -x` | ❌ Wave 0 |

### Observable signals (the assertions)

**Force-local = True (cloud-ON registry `[_COMPUTE_BACKEND]` from the autouse fixture + a persisted `RouteControl(force_local=True)` row):**
1. `/api/v1/analyze` and `/pipeline/analyze` with a `_LONG` (6000s ≥ 5400s threshold) DISCOVERED file + an online fileserver agent → the long file routes **local** (`process_file` enqueued to `phaze-agent-<id>`, never the `default` queue); **`SELECT ... WHERE state == AWAITING_CLOUD` returns zero rows** — byte-identical to what an all-local `[_LOCAL_BACKEND]` registry produces.
2. `/pipeline/backfill-cloud` with an `ANALYSIS_FAILED ∧ _LONG` candidate → response reports `count=0, disabled=True`; the row **stays `ANALYSIS_FAILED`** (never reset to `DISCOVERED`); **no `SchedulingLedger` row** for `process_file:<id>` is seeded; `capture == []` (nothing enqueued). Zero-mutation no-op per the T-71-08 comment (`pipeline.py:789-793`).

**Force-local = False (control, same cloud-ON registry, no/False `RouteControl` row):**
3. The same `_LONG` file on `/api/v1/analyze` **IS held** in `AWAITING_CLOUD` (registry honored) — this is the existing `test_analyze_long_file_held_awaiting_cloud_even_with_compute_online` behavior, re-asserted as the control so the *only* variable is the toggle.

### Sampling adequacy
- Gate **L396** covered by the `/api/v1/analyze` force-local case (trigger #1).
- Gate **L718** covered by the `/pipeline/analyze` force-local case (trigger #2) — distinct endpoint, distinct handler, same fold; must be exercised separately because it is a physically separate gate line.
- Gate **L793** covered by the `/pipeline/backfill-cloud` force-local case (backfill early-return).
- No gate site left uncovered. A True case + a False control per trigger gives 2-sided evidence that the toggle (not some other condition) drives the behavior.

### Test altitude & fixtures (D-09)
- **Altitude:** real-route/endpoint via the `client` `AsyncClient` fixture — never unit-call `_route_discovered_by_duration`. This is what makes the gate line (the `... and not await get_route_control(session)` fold) the thing under test.
- **Persisted toggle (not a mock):** add `RouteControl(id="global", force_local=True)` to the shared `session` and `await session.commit()` before the POST. The `client` fixture overrides `get_session` to that same `session` (conftest L216), so the endpoint's `get_route_control(session)` reads the seeded row. The `RouteControl` table is created via `Base.metadata.create_all` in conftest (L193) and the model is registered in `phaze.models` — no migration wiring needed for the test.
- **Registry fixture:** keep the autouse `_cloud_compute_registry` (pins `settings.backends = [_COMPUTE_BACKEND]`, cloud ON) so the ONLY thing forcing local is the toggle — this is what proves the fold, versus the existing all-local `[_LOCAL_BACKEND]` tests which force local via the registry.
- **Reused helpers:** `_make_file`, `_persist_files_with_duration([_LONG])`, `_persist_failed_with_duration([_LONG])`, `seed_active_agent(session, "nox", kind="fileserver")` / `seed_active_agent(session, "cloud", kind="compute")`, `wire_fakes(client)` / `install_fake_queues(client)`, `_drain_background()`, `_LONG`/`_SHORT`.

### Wave 0 gaps
- [ ] New force-local test region in `tests/shared/routers/test_pipeline.py` (3-4 cases per the map above) — covers HYG-04 / gate sites L396, L718, L793. No new fixtures, no framework install (all present).

*(No `conftest.py` changes required — the shared `session`/`client`/queue-fake harness already supports the persisted-row + real-route pattern.)*

## Environment Availability

Not applicable — the phase has **no external dependencies**. HYG-04 runs against the in-process FastAPI app + an in-memory/ephemeral test DB created by conftest; HYG-01/02/03/05 are file edits. No CLIs, services, runtimes, or databases beyond the existing `uv run pytest` toolchain are needed.

## Security Domain

`security_enforcement` is not disabled, but this phase introduces **no new attack surface**: no new endpoints, no input parsing, no auth/session/crypto changes, no `src/` behavior change. HYG-04 adds a test; HYG-01/02/03/05 edit docs/comments/tracking. STRIDE/ASVS review is N/A for this scope — the force-local toggle it *tests* (not adds) is an internal-realm operator control already shipped in Phase 71 behind the reverse-proxy internal boundary.

| ASVS Category | Applies | Standard Control |
|---------------|---------|------------------|
| V5 Input Validation | no | No new inputs; HYG-04 drives existing validated routes |
| V6 Cryptography | no | No crypto touched |

## State of the Art

Not applicable — no libraries, framework versions, or ecosystem patterns are being adopted or changed. The phase reconciles tracking to reflect code that already shipped in Phases 67-74.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| — | (none) | — | — |

**All claims in this research were verified against the live worktree this session (grep/read/git-log) or cited to CONTEXT.md decisions.** No `[ASSUMED]` claims — no user confirmation needed beyond the already-locked D-01..D-11.

## Open Questions

1. **HYG-02 L25 trailing sentence boundary**
   - What we know: the `api` breadcrumb (L24-25) ends with "…Mount a backends.toml to enable cloud backends."
   - What's unclear: whether that trailing operator-guidance sentence counts as "the stale breadcrumb" (delete) or "the explainer" (keep).
   - Recommendation: keep operator-useful guidance; delete only the "Replaces the removed cloud_target selector … (Phase 67 …)" reference. Executor discretion per D-03 ("keep the surrounding backends.toml mount explainer intact"). Low stakes — it's a comment.

2. **HYG-04 case count (3 vs 4)**
   - What we know: three gate sites; a False control strengthens the proof.
   - What's unclear: whether the planner wants one control case or a control per trigger.
   - Recommendation: 3 force-local-True cases (one per gate site) + at least 1 force-local-False control. Planner discretion per D-10.

## Sources

### Primary (HIGH confidence — verified this session)
- `tests/shared/core/test_requirements_traceability.py` (L54-73, L256-339) — `_NO_ACTIVE_MILESTONE` skip mechanism (HYG-01).
- `docker-compose.yml` (L18-58) + `git grep PHAZE_CLOUD_TARGET -- 'docker-compose*.yml'` (clean) — HYG-02.
- `src/phaze/services/backends.py` (L532-574 `resolve_compute_backend`/`resolved_non_local_kind`, L651 `_probe_availability`) — HYG-03.
- `src/phaze/config.py` (L415-450 `_validate_registry` dup-`agent_ref`) — HYG-03.
- `src/phaze/routers/pipeline.py` (L279-348 router, L372-408 `trigger_analysis`, L693-724 `trigger_analysis_ui`, L763-813 `trigger_backfill_cloud`) — HYG-04 gate sites.
- `src/phaze/services/route_control.py` + `src/phaze/models/route_control.py` + `src/phaze/routers/routing.py` + `alembic/versions/031_add_route_control.py` — force-local toggle plumbing.
- `tests/shared/routers/test_pipeline.py` (L40-120 fixtures, L636-1016 backfill tests, L882-926 zero-mutation pattern) + `tests/conftest.py` (L193, L205-216) — HYG-04 test harness.
- `.planning/quick/260628-wzq-…/SUMMARY.md`, `.planning/quick/260629-eev-…/SUMMARY.md` + `git log 5f43aa7 267109b` — HYG-05.
- `.planning/{CONTEXT,REQUIREMENTS,STATE,ROADMAP,MILESTONES}.md` — scope + reconciliation targets.

### Secondary / Tertiary
- None — no web/Context7 lookups needed (in-repo verification only).

## Metadata

**Confidence breakdown:**
- HYG-01/02/03/05 reconciliation targets: **HIGH** — all file:line claims verified; only cosmetic line drift.
- HYG-04 test architecture: **HIGH** — gate sites exact, endpoints confirmed, harness pattern already proven in the same file, RouteControl table confirmed created in the test DB.
- No-new-dependency / no-`src`-behavior-change claim: **HIGH** — verified across all five items.

**Research date:** 2026-07-06
**Valid until:** 2026-07-13 (line numbers may drift with any intervening commit to `pipeline.py` / `test_pipeline.py` / `docker-compose.yml`; re-grep the four gate-site markers before planning if the worktree advances).
