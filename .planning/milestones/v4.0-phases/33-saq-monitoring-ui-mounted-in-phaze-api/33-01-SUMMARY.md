---
phase: 33-saq-monitoring-ui-mounted-in-phaze-api
plan: "01"
subsystem: web-mount
tags: [saq, dashboard, starlette, config, mount-helper]
requires:
  - "saq.web.starlette.saq_web (the embeddable dashboard factory; installed saq==0.26.4)"
  - "tests/_queue_fakes.py::FakeQueue.info — Wave 0 Redis-free QueueInfo double"
provides:
  - "src/phaze/web/saq_mount.py::build_saq_app(queues) — pure Starlette wrapper around the single saq_web('/saq', queues) call"
  - "src/phaze/web::phaze.web package marker"
  - "src/phaze/config.py::Settings.enable_saq_ui — default-True, env-overridable mount toggle"
affects:
  - "Wave 2 (33-02) lifespan mount: calls build_saq_app once, gated on settings.enable_saq_ui"
tech-stack:
  added: []
  patterns:
    - "Testable mount helper: isolate the single globals-clobbering saq_web call behind a pure function so the lifespan stays thin and the mount is unit-testable over FakeQueue with no DB/Redis"
    - "AST-walk a function body (not raw substring scan) to assert an implementation contract when the docstring legitimately names the forbidden tokens"
key-files:
  created:
    - src/phaze/web/__init__.py
    - src/phaze/web/saq_mount.py
    - tests/test_web/__init__.py
    - tests/test_web/test_saq_mount.py
  modified:
    - src/phaze/config.py
decisions:
  - "Annotation-only imports (Queue, Starlette) kept under TYPE_CHECKING to satisfy ruff TC002; from __future__ import annotations makes the string annotations resolve safely without a runtime import"
  - "enable_saq_ui placed adjacent to auto_migrate so all api/lifespan toggles cluster; lives on the shared Settings class (both roles parse it, only the api process acts on it)"
  - "The no-pool-construction test AST-walks build_saq_app and asserts the only Call is saq_web, rather than substring-scanning the source (the docstring names Queue.from_url / Redis / connect while documenting what the helper does NOT do)"
metrics:
  duration: "~18 min"
  completed: "2026-06-11"
  tasks: 3
  files: 5
---

# Phase 33 Plan 01: build_saq_app Mount Helper + enable_saq_ui Flag Summary

Created the testable `phaze/web/saq_mount.py::build_saq_app(queues)` pure wrapper around the single `saq_web("/saq", queues)` call (constructs no Queue/Redis pool, reads passed instances via `.info()`, documents the once-per-process globals clobber) plus the default-True `settings.enable_saq_ui` flag Wave 2 will gate the lifespan mount on — all unit-tested over Wave 0's `FakeQueue` doubles with no DB, Redis, or app boot.

## What Was Built

- **`src/phaze/web/__init__.py`** — `phaze.web` package marker (one-line docstring).
- **`src/phaze/web/saq_mount.py`::`build_saq_app(queues: list[Queue]) -> Starlette`** — body is exactly `return saq_web("/saq", queues=queues)` (mount-path literal and `root_path` both pinned to `"/saq"` so the baked `/saq/static/...` asset URLs resolve — RESEARCH Pitfall 3). The docstring warns that `saq_web` stores its queue registry in module globals (`saq.web.starlette.QUEUES`/`ROOT_PATH`) and CLEARS `QUEUES` on every call (`saq/web/starlette.py:135`), so it must be called exactly once per process, and that the app reads queue state via the passed instances' `.info()` and never opens a second Redis pool (LOCKED no-second-pool decision). No new top-level dependency added. Annotation-only `Queue`/`Starlette` imports sit under `TYPE_CHECKING` (ruff TC002); `saq_web` is the only runtime import.
- **`src/phaze/config.py`::`Settings.enable_saq_ui`** — `Field(default=True, validation_alias=AliasChoices("PHAZE_ENABLE_SAQ_UI", "enable_saq_ui"), description=...)` placed next to `auto_migrate`. Reuses the existing `Field`/`AliasChoices` imports; no new import. Default-on (dashboard mounts with zero operator action); `PHAZE_ENABLE_SAQ_UI=false` disables it.
- **`tests/test_web/test_saq_mount.py`** (+ package `__init__.py`) — four tests over a throwaway `FastAPI()` + sync `TestClient` + `FakeQueue` doubles (the default `client` conftest fixture skips the lifespan where the real mount lives, so these deliberately wire their own — RESEARCH Pitfall 2):
  - `test_build_saq_app_routes_and_root_renders` (`-k build`): `.routes` include `/` and `/api/queues`; `GET /saq/` → 200 with `/saq/static/` in the body.
  - `test_api_queues_reuses_passed_instances_no_pool` (`-k reuse`): `GET /saq/api/queues` lists both `controller` and `phaze-agent-nox`; an AST-walk of `build_saq_app` asserts the only call is `saq_web` (no pool/Redis/from_url/connect construction).
  - `test_enable_saq_ui_flag_defaults_true` (`-k flag`): `settings.enable_saq_ui is True`.
  - `test_saq_web_single_call_contract`: a second `build_saq_app` leaves `saq.web.starlette.QUEUES == {"b"}` — the globals-clobber, documenting mount-once-per-process.

## Verification

- `uv run pytest tests/test_web/test_saq_mount.py tests/test_queue_fakes.py -q` — **7 passed**.
- `-k build` / `-k reuse` / `-k flag` each select and pass **exactly 1** test (VALIDATION rows 33-01-01/02/03).
- `uv run ruff check src/phaze/web/ src/phaze/config.py tests/test_web/` — clean.
- `uv run mypy src/phaze/web/saq_mount.py src/phaze/config.py` — clean.
- `PHAZE_ENABLE_SAQ_UI=false` parses to `False` (alias wired); default `True` confirmed.
- Config regression: `uv run pytest tests/ -k config -q` — **65 passed** (the new Settings field broke no existing config parsing).
- No `Queue`/Redis pool constructed in the helper (AST-asserted); no new top-level dependency; `saq[web]` deliberately NOT added.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Annotation-only imports moved under `TYPE_CHECKING` (ruff TC002)**
- **Found during:** Task 1 verification.
- **Issue:** The plan specified top-level `from saq import Queue` / `from starlette.applications import Starlette`, but ruff `TC002` flagged both (they are used only in annotations) and Task 1's acceptance requires `ruff check src/phaze/web/` clean.
- **Fix:** Moved `Queue` and `Starlette` into a `TYPE_CHECKING` block; `from __future__ import annotations` (already required by the plan) makes the string annotations resolve without a runtime import. `saq_web` remains the only runtime import. Behavior unchanged — `build_saq_app([])` still returns a `Starlette`.
- **Files modified:** `src/phaze/web/saq_mount.py`. **Commit:** `b9eb555`.

**2. [Rule 1 - Bug in planned test] no-pool assertion changed from substring scan to AST-walk**
- **Found during:** Task 3 verification.
- **Issue:** The plan's Task 3 step (2) said to "assert via source that the helper file contains no `Queue.from_url`/Redis construction" by reading the module text and asserting the substring is absent. That assertion fails as written: the helper's docstring legitimately contains the literal tokens `Queue.from_url`, `Redis`, and `connect` while documenting what the helper does NOT do (and Task 1's own `grep` acceptance only passed by accident — BSD `grep` treats `\|` as literal, not alternation, so it never matched).
- **Fix:** The no-pool-construction contract is now asserted by AST-walking the `build_saq_app` function body (docstrings are string constants, not `Call` nodes) and asserting the only call is `saq_web` — never a pool/Redis/from_url/connect construction. This expresses the real contract robustly regardless of docstring prose.
- **Files modified:** `tests/test_web/test_saq_mount.py`. **Commit:** `a7775d4`.

**3. [Rule 3 - Blocking] queue-listing test renamed to contain "reuse"**
- **Found during:** Task 3 verification against VALIDATION.
- **Issue:** VALIDATION row 33-01-02 selects the no-pool test via `-k reuse`; the initial name (`..._lists_passed_instances_with_no_constructed_pool`) contained no "reuse" substring and would not be selected.
- **Fix:** Renamed to `test_api_queues_reuses_passed_instances_no_pool` so `-k reuse` resolves to exactly one test. **Commit:** `a7775d4`.

## Self-Check: PASSED

- `src/phaze/web/__init__.py` — FOUND
- `src/phaze/web/saq_mount.py` — FOUND (`build_saq_app` present, TYPE_CHECKING imports)
- `src/phaze/config.py` — FOUND (modified, `enable_saq_ui` present)
- `tests/test_web/__init__.py` — FOUND
- `tests/test_web/test_saq_mount.py` — FOUND (4 tests, all pass)
- `b9eb555`, `67a5648`, `a7775d4` — all FOUND in git log
