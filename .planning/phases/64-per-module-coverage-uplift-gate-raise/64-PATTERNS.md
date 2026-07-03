# Phase 64: Per-Module Coverage Uplift & Gate Raise - Pattern Map

**Mapped:** 2026-07-02
**Files analyzed:** 7 (2 new scripts/tests, 3 new test files, 2 modified config, 1 optional test extension)
**Analogs found:** 7 / 7 (every work item has a strong in-repo analog — this phase is nearly all "copy an existing pattern")

> **Scoping note the planner MUST honor (from RESEARCH §Re-Baselining):** the authoritative COMBINED coverage is **96.89%**; the ONLY sub-floor module is `services/review.py` at **83.16%** (needs ~2 lines). The stale "worst offender" list (shell 39.7%, pipeline 65.5%, tracklists ~69%, agent_liveness 12.5%) is a no-DB measurement artifact — those modules are all ≥90% combined. Do NOT schedule redundant test waves for them. The real engineering is the floor-enforcement machinery + gate number.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `scripts/coverage_floor.py` (NEW) | utility / CI-build tool | transform (parse `coverage.json` → exit code) | `scripts/classify-changed-files.sh` (Phase 63 CI script) + `scripts/parity/*.py` (Python-in-scripts conventions) | role-match (CI gate script; language differs from the .sh analog but the RESEARCH-supplied shape is authoritative) |
| `tests/shared/test_coverage_floor.py` (NEW) | test (script unit test) | transform | `tests/shared/test_change_gate.py` | exact (tests a load-bearing CI script) |
| `tests/shared/test_coverage_gate.py` (NEW) | test (config guard) | request-response (read config files, assert) | `tests/shared/test_ci_workflow_wiring.py` | exact (reads justfile + pyproject, asserts consistency) |
| `tests/review/services/test_review_degrade.py` (NEW) | test (service) | CRUD / degrade-path | `tests/shared/services/test_pipeline.py` + `tests/review/services/test_proposal_queries.py` | exact (service-layer, `session` fixture, seeded rows) |
| `tests/agents/services/test_agent_liveness.py` (MODIFIED, optional margin) | test (service) | CRUD / degrade-path | itself (extend) | exact |
| `pyproject.toml` (MODIFIED) | config | — | `[tool.coverage.report]` L67-70 + `[tool.ruff.lint.per-file-ignores]` L181-187 | exact edit sites |
| `justfile` (MODIFIED) | config (recipe) | — | `coverage-combine` recipe L107-110 | exact edit site |

---

## Pattern Assignments

### `scripts/coverage_floor.py` (NEW — utility / CI-build tool, transform)

**Analog (role — CI gate script):** `scripts/classify-changed-files.sh`. That Phase 63 script is the precedent for "extract a small, independently-testable gate decision into `scripts/`, invoke it from a `just` recipe, unit-test it from `tests/shared/`." Copy its **docstring discipline** (what it reads, what it prints/exits, why it fails-safe) and its **fail-closed posture** (a missing/empty input must fail, never silently pass — see RESEARCH §Security threat "fail-open").

**Analog (language — Python script under `scripts/`):** `scripts/parity/*.py` are the existing Python scripts in the tree. Note they are covered by the ruff ignore `"scripts/parity/**" = ["T201"]` (pyproject L183) — the new script needs the equivalent `# noqa: T201` on its `print` calls OR a new per-file-ignore entry (see pyproject edit below).

**Authoritative implementation shape:** RESEARCH.md lines 138-181 give the exact ~40-line stdlib-only script (`json` + `sys` + `pathlib`, `FLOOR = 85.0`, iterate `data["files"]`, skip `num_statements == 0`, compare raw float `percent_covered`, `EXEMPT: dict[str,str]` for D-09, exit 1 on any failure). The executor should implement that verbatim shape. Key correctness details the analog + RESEARCH pin:
- Compare the **raw float** `percent_covered`, NOT `percent_covered_display` (which rounds) — so 84.995% correctly fails an 85.0 floor (pyproject `precision = 2` is set).
- Skip zero-statement files (`__init__.py`) to avoid false failures.
- Tracked set = the `files` dict keys (self-maintaining; a new `phaze/**` module auto-appears). No hand-maintained allowlist (D-03).

**Docstring/comment pattern to copy** (from `classify-changed-files.sh` L1-31 — states inputs, outputs, exit semantics, the security invariant, and "invoked from CI via `just …`, unit-tested by `tests/shared/…`"):
```
"""Fail if any tracked phaze module is below the per-module coverage floor (COV-01, D-01/D-02/D-03).

Reads `coverage json` output and enforces a single uniform floor (D-04=85) over every
tracked source file. Runs in `just coverage-combine` AFTER `coverage combine`, so it sees
the authoritative COMBINED coverage (Phase 63 D-02) — never a partial per-bucket shard.
"""
```

---

### `tests/shared/test_coverage_floor.py` (NEW — test, unit-tests the script)

**Analog:** `tests/shared/test_change_gate.py` (the Phase 63 test that exercises `classify-changed-files.sh`). This is the direct precedent: "a load-bearing CI script gets its own `tests/shared/` unit test." Placement in `tests/shared/` is deliberate — it rides the `shared` bucket (partition guard, RESEARCH Pitfall 6).

**Repo-root idiom to copy** (`test_change_gate.py` L30-32):
```python
# tests/shared/test_coverage_floor.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "coverage_floor.py"
```

**Test-invariant pattern to copy** (`test_change_gate.py` L52-83): a parametrized table of crafted inputs → expected outcome, PLUS one explicit non-parametrized positive test for the load-bearing security case. For the floor script, feed a **synthetic `coverage.json`** (write it to a `tmp_path`, `chdir` or point the script at it) with:
- a sub-floor module → assert exit code **1** (and the module name printed);
- all-pass modules → assert exit **0**;
- a zero-statement file → assert it is skipped (not a false failure);
- an `EXEMPT`-listed sub-floor module → assert honored (exit 0);
- **fail-closed:** a missing/empty `coverage.json` must NOT exit 0 (RESEARCH §Security threat "fail-open" — mirror `test_change_gate.py`'s empty-input → conservative case at L64-67).

Note the analog runs the .sh via `subprocess.run([...], check=True)`. Since `coverage_floor.py` is Python, prefer importing `main()` directly (faster, no subprocess) OR run via `subprocess.run([sys.executable, str(_SCRIPT)], ...)` — either matches the analog's "exercise the real script over its real interface" intent.

---

### `tests/shared/test_coverage_gate.py` (NEW — test, config-consistency guard)

**Analog:** `tests/shared/test_ci_workflow_wiring.py`. This is the exact pattern for "read `justfile` + a config file as text/structured data and assert an invariant that would otherwise silently drift." It is DB-free and subprocess-free — same as this new guard needs to be.

**Recipe-extraction helper to REUSE/copy** (`test_ci_workflow_wiring.py` L46-55, `_extract_recipe(justfile_text, name)`): anchors on `re.MULTILINE ^` so a recipe *name* mentioned in a comment is not mistaken for the header. The new guard reads the `coverage-combine` recipe body and greps the `--fail-under=<N>` value.

**Repo-root + file-path idiom to copy** (`test_ci_workflow_wiring.py` L39-43):
```python
_REPO_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _REPO_ROOT / "justfile"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
```

**The invariant to assert (D-05, RESEARCH Pitfall 3):** parse `pyproject.toml [tool.coverage.report] fail_under` (use `tomllib`, stdlib) and the `justfile coverage-combine` `--fail-under=` number; assert **they are equal** AND **both > 90.38** (strictly above the baseline). This is the "CLI `--fail-under` silently overrides config" tripwire.

---

### `tests/review/services/test_review_degrade.py` (NEW — test, service / degrade-path)

**Analog (structure):** `tests/shared/services/test_pipeline.py` (service fns exercised via the `session` fixture) and the co-located `tests/review/services/test_proposal_queries.py` (same bucket, same service-test conventions — `from __future__ import annotations`, `TYPE_CHECKING` guard on `AsyncSession`, `@pytest.mark.asyncio`, a local `_create_*` seed helper).

**Placement:** `tests/review/services/` — the dir already exists and holds the review-bucket service tests. Correct bucket per RESEARCH Pitfall 6 (review.py tests → `tests/review/`).

**Target module facts (from `src/phaze/services/review.py`, read this session — exact lines + log keys):**

| Function | Sub-floor lines | Degrade log key (assert via `caplog`) |
|----------|-----------------|----------------------------------------|
| `get_pending_proposal_rows` | 74-76 | `pending_proposal_rows_degraded` |
| `get_tagwrite_review_rows` | 134-136 | `tagwrite_review_rows_degraded` |
| `get_dedupe_groups` | 197-199 | `dedupe_groups_degraded` |
| `get_cue_review_cards` | 267-269 | `cue_review_cards_degraded` |
| `_format_size` (139) | 142, 148 | pure return-value (no log) |
| `_format_quality` (151) | 156 | pure return-value (no log) |

Each degrade branch is `except Exception → logger.warning("<key>", exc_info=True) → return []`.

**Degrade-path test pattern (D-07 — assert observable outcome, NOT "no exception"):** inject a session whose accessed method raises, then assert BOTH `result == []` AND the warning was emitted. The `caplog` capture works because the autouse `_route_structlog_through_stdlib` fixture (`tests/conftest.py` L96-97) routes structlog → stdlib. RESEARCH.md L314-338 gives the concrete template:
```python
import logging
from types import SimpleNamespace  # or a tiny raising stub class

@pytest.mark.asyncio
async def test_get_dedupe_groups_degrades_to_empty_and_logs(caplog):
    class _Boom:
        def begin_nested(self):          # first thing the fn touches inside try
            raise RuntimeError("db down")
    with caplog.at_level(logging.WARNING):
        result = await get_dedupe_groups(_Boom())  # type: ignore[arg-type]
    assert result == []                                       # observable return
    assert any("dedupe_groups_degraded" in r.getMessage() for r in caplog.records)  # observable side-effect
```
> Note: `session.begin_nested()` in review.py is called as `async with session.begin_nested():` — the stub's `begin_nested` (or its `__aenter__`) must raise. RESEARCH used a `begin_nested` that raises synchronously; verify against the real `async with` call site (review.py L61/104/173/220) and make the stub raise at the point control first enters the `try`.

**Pure-formatter test pattern (trivial return-value assertions, no seam):** RESEARCH.md L340-356:
```python
from phaze.services.review import _format_quality, _format_size

def test_format_size_edges():
    assert _format_size(None) == "unknown size"
    assert _format_size(0) == "unknown size"          # covers L142 (falsy guard)
    assert _format_size(22_400_000).endswith(" MB")
    assert _format_size(2**60).endswith(" PB")         # covers L148 (loop-exhaustion branch)

def test_format_quality_with_and_without_bitrate():
    assert _format_quality({"file_size": 22_400_000, "bitrate": 320}).startswith("320 kbps · ")
    assert "kbps" not in _format_quality({"file_size": 22_400_000})   # covers L156 (no-bitrate branch)
```

---

### `tests/agents/services/test_agent_liveness.py` (MODIFIED — optional margin, D-05)

**Analog:** itself. The file today (L1-197) covers `classify` + `sort_key` (pure functions, no DB — `_make_agent` builder, parametrized boundary tables). It has NO `session`-fixture tests yet.

**The margin target** (`services/agent_liveness.py` L174-180 — the only miss, 85.42% → buys margin): the `classify_compute_lanes` `SQLAlchemyError` degrade branch (`rollback → return ("IDLE", 0)`, rollback-failure log key `compute_lane_liveness_rollback_failed` at L179). Test pattern (RESEARCH L224): inject a session whose `.execute` raises `SQLAlchemyError` and assert the return tuple is `("IDLE", 0)`. This is a NEW `@pytest.mark.asyncio` DB-style test appended to the existing pure-function file — follow the degrade-path pattern from `test_review_degrade.py` above. **Scope this as optional margin, not floor-clearing** (the module already passes 85%).

---

## Shared Patterns

### Repo-root path idiom (all config/script-reading tests)
**Source:** `tests/shared/test_change_gate.py` L30-32, `tests/shared/test_ci_workflow_wiring.py` L39-43
**Apply to:** `test_coverage_floor.py`, `test_coverage_gate.py`
```python
# tests/shared/<file>.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
```

### Degrade-path assertion (service degrade branches)
**Source:** RESEARCH template + autouse `_route_structlog_through_stdlib` (`tests/conftest.py` L96-97)
**Apply to:** `test_review_degrade.py`, the `agent_liveness` margin test
Assert BOTH the default return (`[]` / `("IDLE", 0)`) AND the emitted `logger.warning(...)` key via `caplog.records` → `r.getMessage()`. Never "call it and assert no exception" (D-07; RESEARCH Pitfall 4 flags that as a defect).

### Service-test file header conventions (every new test file)
**Source:** `tests/review/services/test_proposal_queries.py` L1-25, `tests/shared/services/test_pipeline.py` L1-56
```python
"""<one-line purpose>."""
from __future__ import annotations
from typing import TYPE_CHECKING
import pytest
if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
```
`asyncio_mode = auto`, so `@pytest.mark.asyncio` is used explicitly on async tests per the analogs.

### Reusable fixtures (from `tests/conftest.py`, apply to every bucket)
**Source:** `tests/conftest.py` — `session` (L173-179, `AsyncSession` on test DB), `client` (L181-189, `httpx.AsyncClient` over ASGITransport with `get_session` override), `authenticated_client` (L211), `make_file` (L366), and domain seeders directly relevant to review.py: `seed_pending_proposal` (L396), `seed_executed_file_with_metadata` (L428), `seed_duplicate_group` (L461), `seed_cue_set` (L475), `seed_cloud_jobs` (L624, for agent_liveness happy paths).
**Apply to:** any HAPPY-path test (the degrade tests use raising stubs, not the real `session`). RESEARCH: "Do NOT hand-roll app bootstrapping — use the harness."

### Isolation hazards (all new tests)
**Source:** RESEARCH Pitfall 5/6; MEMORY [[reference_ci_bucket_isolation]]
- Autouse `_isolate_pydantic_settings_from_env_file` (`conftest.py` L32-33) `cache_clear()`s `get_settings` per test — new tests must not cache settings module-globally.
- New tests must pass **in isolation** via `just test-bucket <bucket>`, not only in the full suite.
- Every `test_*.py` MUST land under a known `tests/<bucket>/` dir or the partition guard (`tests/shared/test_partition_guard.py`) fails CI.

---

## Config edit sites (exact, keep consistent)

### `justfile` — `coverage-combine` recipe (L107-110, current)
```make
coverage-combine:
    uv run coverage combine
    uv run coverage xml
    uv run coverage report --fail-under=85
```
**Edit (RESEARCH L182-191):** add `uv run coverage json` (the floor script needs `coverage.json` — the recipe currently emits only xml), raise `--fail-under=85` → `--fail-under=<NEW_GLOBAL>`, and append `uv run python scripts/coverage_floor.py`. D-02 discretion: a dedicated `just coverage-floor` recipe called from `coverage-combine` is equally valid.
**Do NOT touch** `test-bucket` (L102-103, `--cov-fail-under=0` is deliberate — a shard is partial; RESEARCH Pitfall 1 + the L95-99 comment).

### `pyproject.toml`
- `[tool.coverage.report] fail_under = 85` (L68) → `<NEW_GLOBAL>`. **Must equal the justfile number** (the `test_coverage_gate.py` guard enforces this).
- `[tool.ruff.lint.per-file-ignores]` (L181-187): the new script's `print` calls need `T201` allowed. Either add `"scripts/coverage_floor.py" = ["T201"]` (mirrors the existing `"scripts/parity/**" = ["T201"]` at L183) OR keep inline `# noqa: T201` (RESEARCH L421 flags this exact choice).

### `<NEW_GLOBAL>` value (D-05 — pin at execute time)
Measured combined overall today = **96.89%**; post-uplift ≥ that. Set to the integer floor of (achieved − ~1), e.g. **95**. Strictly > 90.38, low-90s-or-higher, ~1.5-2pt headroom. RESEARCH §Gate Wiring gives the CI-faithful reproduce commands (per-bucket shards → `coverage combine` → `coverage json`/`report`) to pin the exact digit.

### CI: NO workflow YAML edit needed
`.github/workflows/tests.yml` `combine` job (L145) already runs `just coverage-combine`. Because the floor check lands *inside* that recipe, it runs automatically on the combined `.coverage`, before the Codecov upload (L148). RESEARCH L244-246 verified this.

---

## No Analog Found

None. Every work item maps to a strong in-repo analog. The `scripts/coverage_floor.py` language differs from its closest CI-script analog (`.sh` → `.py`), but (a) the CI-gate-script *role* is an exact Phase 63 precedent, (b) `scripts/parity/*.py` establishes the Python-in-`scripts/` conventions, and (c) RESEARCH.md supplies the authoritative implementation shape — so there is no "invent from scratch" surface.

## Metadata

**Analog search scope:** `scripts/`, `tests/shared/`, `tests/review/services/`, `tests/agents/services/`, `tests/shared/services/`, `tests/shared/core/`, `tests/conftest.py`, `src/phaze/services/{review,agent_liveness}.py`, `justfile`, `pyproject.toml`, `.github/workflows/tests.yml`
**Files scanned:** ~14 read in full/part; bucket dirs enumerated
**Pattern extraction date:** 2026-07-02
</content>
</invoke>
