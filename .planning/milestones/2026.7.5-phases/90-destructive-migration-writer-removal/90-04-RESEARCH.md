# Phase 90-04: Model/Enum Retirement + shadow_compare Cleanup — Research

**Researched:** 2026-07-12
**Domain:** Python/SQLAlchemy ORM deletion, dead-subsystem removal, large test-suite migration, mutation-tested source guard
**Confidence:** HIGH (all claims grounded in repo grep/read at phase HEAD; no external deps)
**Scope:** 90-04 ONLY. Plans 90-01 (readers→derived), 90-02 (writers removed), 90-03 (migration 039 landed) are SHIPPED/immutable and are NOT re-litigated here.

## Summary

90-04 is a large-but-mechanical structural deletion with exactly one judgement call (shadow_compare removal), and that call resolves cleanly to **full removal, no relocation**. All four open questions are answerable from the repo with high confidence.

The critical architectural question the original phase planning missed — "does anything at runtime depend on `shadow_compare`?" — has a definitive answer: **no live app code imports it.** The apparent couplings in `routers/pipeline.py`, `services/backends.py`, and `services/stage_status.py` are **100% comments/docstrings**. The true dependency direction is the *reverse* of what the objective feared: `shadow_compare.py` imports `done_clause`/`failed_clause` FROM `stage_status.py` (`shadow_compare.py:54`), not vice-versa. Removing shadow_compare cannot break the derived readers PR-A built.

**Primary recommendation:** Execute in this order — (1) delete the shadow_compare subsystem + its dedicated tests, (2) fix the central `make_file` lever + sibling factories in `conftest.py` and delete the model column/enum/index in the SAME sweep, (3) sweep the ~90 test files bucket-by-bucket (mechanical kwarg-drop for incidental seeds, marker-seed for derived-status tests per the PR-A/PR-B pattern), (4) add the mutation-tested guard. Two draft-plan corrections are load-bearing: `test_drain_double_dispatch.py` must be **migrated, not deleted** (it is a ROADMAP hard gate whose core is state-independent), and the Task-2 `grep -v '#'` acceptance check is **wrong** (it will not filter triple-quoted docstring mentions).

---

## User Constraints (from CONTEXT.md — the decisions 90-04 executes)

- **D-08 (guard):** mypy/ruff/import are the PRIMARY anti-drift guard; add ONE thin source-grep test, **mutation-tested** (add a fake `.state=`, watch RED, restore). No full behavioral schema-absence suite.
- **D-07 (frozen invariants):** 039's guard transcribed the shadow HARD invariants INLINE — it does NOT import `services/shadow_compare.py`. Confirmed at `039:52` ("NO `phaze.*` imports") and `039:96-113` (the 13 hard invariants inline). **This is why full removal loses no safety.**
- **Do NOT touch migration 039** (landed in 90-03).
- Python 3.14, `uv run` prefix, per-bucket isolation at `:5433` with `+asyncpg`, never `--no-verify`.

---

## Q1 — shadow_compare coupling & safe-removal boundary

### VERDICT: FULL REMOVAL. No symbol relocation required.

Delete `src/phaze/services/shadow_compare.py` and `src/phaze/cli/shadow_compare.py` wholesale. Nothing needs to be moved into a constants module or into `stage_status.py`.

### Per-file reference classification (the key evidence)

`git grep -n "shadow_compare\|shadow-compare" -- src/phaze` returns references in five files. Classified:

| File | Line(s) | Reference kind | Breaks on removal? |
|------|---------|----------------|--------------------|
| `services/shadow_compare.py` | — | the subsystem itself | deleted |
| `cli/shadow_compare.py` | `:32` `from phaze.services.shadow_compare import run_shadow_compare`; `:38` (TYPE_CHECKING `Report`) | **runtime import** | deleted together |
| `routers/pipeline.py` | `:1353`, `:1378` | **comment prose** ("the Phase-79 shadow-compare gate stays green") | NO |
| `services/backends.py` | `:97` | **docstring prose** (documents the `AWAITING_CLOUD => cloud_job` invariant #131) | NO |
| `services/stage_status.py` | `:12`, `:101` | **docstring prose** (`:101` notes `_dedup_exists` is "identical in body" to `dedup_resolved_clause`) | NO |

**Dependency direction (the fear inverted):** `stage_status.py` does NOT import `shadow_compare` — `grep -n "shadow" stage_status.py` returns only the two docstring lines. Conversely `shadow_compare.py:54` does `from phaze.services.stage_status import done_clause, failed_clause`. The derived-status layer PR-A's readers depend on is *upstream* of shadow_compare; deleting the downstream consumer is inherently safe.

### Why no relocation is needed

1. **The migration already froze the invariants.** 039 transcribed the 13 hard invariants as inline SQL (`039:96-113`) with zero `phaze.*` imports (`039:52`). The frozen safety lives in the migration file, not in the app module. `[VERIFIED: read alembic/versions/039_drop_files_state_column.py]`
2. **`INVARIANTS` / `run_shadow_compare` have no app-layer consumer.** `git grep "INVARIANTS\|run_shadow_compare"` outside `shadow_compare.py` matches ONLY `cli/shadow_compare.py` and four test files (below). No dashboard/reader/router path calls them. `[VERIFIED: grep]`
3. **The one shared predicate is already duplicated in the derivation layer.** `shadow_compare._dedup_exists` (`shadow_compare.py:85`) has an independent, byte-identical twin `dedup_resolved_clause()` in `stage_status.py:94-115` — the docstring at `stage_status.py:101` explicitly says so. Live readers use the `stage_status.py` copy; they never reach into shadow_compare. `[VERIFIED: read both]`

### Test files that import the subsystem (retire/migrate — see Q2)

`git grep "from phaze.*shadow_compare\|import phaze.cli.shadow_compare"`:
- `tests/integration/test_shadow_compare.py:47` (imports `INVARIANTS, Invariant, InvariantResult, Report, run_shadow_compare`)
- `tests/integration/test_shadow_compare_skipped.py:45`
- `tests/shared/test_shadow_compare_cli.py:18`
- `tests/integration/test_dedup_resolve_undo_shadow.py:40` (`run_shadow_compare`)

**CLI registration:** `cli/shadow_compare.py` is a standalone `python -m phaze.cli.shadow_compare` module (`if __name__ == "__main__"` at `:138`). It is NOT registered in `cli/__init__.py` and has no `pyproject.toml` script entry (`grep shadow pyproject.toml` → none). So removal needs no de-registration — just delete the file. `[VERIFIED: grep]`

---

## Q2 — the ~90-file test migration mechanics

**Current count:** `git grep -l FileState tests | wc -l` = **91 files**; 408 `state=` seed sites across 96 files; **132** are `make_file(state=...)` calls. `[VERIFIED: grep]`

### The central lever: `tests/conftest.py:416 make_file`

```python
# tests/conftest.py:416
def make_file(session):
    async def _make(*, state: str = FileState.DISCOVERED, original_filename=..., ...):
        record = FileRecord(..., state=state)   # <-- :436, dies when the column is dropped
```

Once `files.state` is removed from `FileRecord`, `FileRecord(state=...)` raises `TypeError: 'state' is an invalid keyword argument`. So `make_file` MUST drop the `state=` param and the `state=state` line, and every `make_file(state=...)` / direct `FileRecord(state=...)` call must drop the kwarg.

**Highest-leverage single edit:** `make_file` has **sibling factories in the same file** that hardcode `state=FileState.X` internally — `conftest.py:461` (PROPOSAL_GENERATED), `:497`/`:552` (MOVED), `:532`/`:679` (EXECUTED), `:614` (ANALYZED), `:712`/`:722` (AWAITING_CLOUD). Converting these 8 in-file seeds to derived-marker seeds (AnalysisResult / RenameProposal / cloud_job) fixes the fixtures the whole suite composes on, in ONE file.

### Can the lever collapse the 90 files to import-swaps-only? PARTIALLY — no.

- **Incidental seeds (the majority):** tests that pass `state=` only to satisfy the old NOT-NULL column, and never assert on derived status. → **pure kwarg deletion + drop the `FileState` import.** Mechanical.
- **Derived-status seeds (the minority):** tests that seeded `state=FileState.ANALYZED` and expect the file to appear in an analyze-derived reader. → must seed the marker (`AnalysisResult.analysis_completed_at` / `failed_at`, `cloud_job.status`, `DedupResolution`, `FileMetadata`) and assert the derived result — **exactly the pattern PR-A/PR-B already used** (90-01-SUMMARY §"derived authority"; 90-02-SUMMARY `patterns` block: "Migrate a broken `X.state == FileState.<value>` test assertion to the derived authority: cloud_job status for AWAITING_CLOUD/PUSHING, analysis_completed_at/failed_at for ANALYZED/ANALYSIS_FAILED, the metadata/DedupResolution marker for METADATA_EXTRACTED/DUPLICATE_RESOLVED").

**Optional enhancement (recommended if the derived-status set is large):** give `make_file` convenience flags (`analyzed=True`, `analysis_failed=True`, `awaiting_cloud=True`) that seed the marker in one place, so call sites do `make_file(analyzed=True)` instead of hand-rolling an AnalysisResult. Centralizes the derived-seed logic; keeps the diff small.

### Retire vs migrate — classification (with a draft-plan critique)

| File | Draft plan | RESEARCH verdict | Why |
|------|-----------|-----------------|-----|
| `tests/integration/test_shadow_compare.py` | retire | **RETIRE** | Pure subsystem test; imports `INVARIANTS`. Dies with the module. |
| `tests/integration/test_shadow_compare_skipped.py` | retire | **RETIRE** | Same. |
| `tests/shared/test_shadow_compare_cli.py` | retire | **RETIRE** | Tests the deleted CLI. |
| `tests/integration/test_pending_set_divergence.py` | retire | **RETIRE** | Premise is "seed inconsistent `state` vs markers, prove readers use markers." Once `state` is gone the inconsistency is unseedable and marker-authority is *structurally* guaranteed. ⚠ coverage note below. |
| `tests/integration/test_dedup_divergence.py` | retire | **RETIRE** | Same premise (`test_dedup_divergence.py:1-25` docstring: File A `state='analyzed'`+marker vs File B `state='duplicate_resolved'`+no-marker). Unseedable post-drop. ⚠ coverage note below. |
| `tests/integration/test_drain_double_dispatch.py` | retire | **MIGRATE — do NOT delete** | **Draft-plan error.** This is a ROADMAP-designated HARD GATE (`test_drain_double_dispatch.py:1-10`). Its core assertion — a file dispatched **exactly once** across two `stage_cloud_window` ticks, blocked on tick 2 by `~inflight_clause` over the committed `scheduling_ledger` row — is **state-independent**. The only state coupling is the removed `LOCAL_ANALYZING` flip (a PR-B writer). Strip the state seeds/assertions; KEEP the ledger dispatch-count coverage. Delete outright ONLY if inspection proves every assertion is state-bound (it is not, per the module docstring). |
| `tests/integration/test_dedup_resolve_undo_shadow.py` | keep non-shadow arm | **MIGRATE (or retire if PR-A twin exists)** | Most tests here assert BOTH `run_shadow_compare(...).hard_fail_total == 0` (lines 126,132,137,168,194,204,224,239…) AND `dup.state == FileState.DISCOVERED` (lines 203,223). Both classes become invalid (subsystem gone; column gone). KEEP the marker-based resolve/undo/noop/malformed-uuid coverage. **First check:** PR-A (90-01 Task 4) already created id-only `/resolve`→`/undo` round-trip regressions — if those cover the same behavior, this file can retire without coverage loss. Planner should diff before choosing. |

**Cross-test import safety:** the "copied from test_dedup_divergence / test_shadow_compare" strings in `test_enrich_pending_independence.py:26,113`, `test_pending_set_divergence.py:75`, `test_dedup_resolve_undo_shadow.py:68` are **docstring prose** — each file has its OWN copied `db_session` fixture. No Python `import` couples them (`git grep "import test_dedup_divergence"` → none). Deleting the divergence tests is import-safe. `[VERIFIED: grep]`

`test_enrich_pending_independence.py` has 12 `FileState` refs and IS in the 90-set — it is a KEEP-and-migrate (it seeds output rows to advance stages per its docstring), not a shadow test. `[VERIFIED: grep]`

### ⚠ Coverage risk (real, must be managed)

Two gates exist: **per-module 90%** (`scripts/coverage_floor.py:33 FLOOR = 90.0`) and **combined 95%** (`justfile` `coverage-combine` → `coverage report --fail-under=95`). Deleting shadow_compare removes source AND its tests together (net-neutral). But retiring `test_dedup_divergence` / `test_pending_set_divergence` WITHOUT removing the code they exercise (the dedup readers, the pending-set builders) can drop `services/dedup.py` / `services/pipeline.py` below the 90% per-module floor. Ensure the migrated/surviving PR-A tests preserve those modules' coverage; run `just coverage-combine` before declaring done. `[VERIFIED: read coverage_floor.py, justfile]`

### Recommended migration order (lowest-risk)

1. **Delete the shadow subsystem** (`services/shadow_compare.py`, `cli/shadow_compare.py`) + its 3 dedicated tests; migrate `test_drain_double_dispatch` and `test_dedup_resolve_undo_shadow` per above. (Removes the app-layer `FileState` importer.)
2. **Single coordinated sweep** — remove the model column/enum/index (Q3) AND fix `conftest.py` `make_file` + the 8 sibling factories in the same change. (These MUST land together; a half-state where the column is gone but `make_file` still passes `state=` is red for everyone.)
3. **Bucket-by-bucket fallout** (export the `:5433` DB URLs): mechanical kwarg-drop + `FileState` import removal for incidental seeds; marker-seed for derived-status tests. Run each bucket to surface the derived-status minority.
4. **Guard + autogen** last: add `test_no_filestate_guard.py`; confirm `test_039_autogenerate_diff_is_empty_for_dropped_objects` flips GREEN; `just coverage-combine`.

---

## Q3 — enum / column / index removal surface

### Exact deletions

**`src/phaze/models/file.py`:**
- `class FileState(enum.StrEnum)` — **lines 20-71** (the whole class + its docstring).
- `state: Mapped[str] = mapped_column(String(30), nullable=False, default=FileState.DISCOVERED)` — **line 86**.
- `Index("ix_files_state", "state")` — **line 97** (first entry of the `__table_args__` tuple at :96-100; pairs 1:1 with 039's `op.drop_index('ix_files_state')`).
- `import enum` — **line 5** — becomes unused; remove (ruff `F401` will flag it).

**`src/phaze/models/__init__.py`:**
- **line 9** `from phaze.models.file import FileRecord, FileState` → `from phaze.models.file import FileRecord`.
- **line 36** `"FileState",` in `__all__` → remove.

**`src/phaze/config.py`:**
- **line 619** — comment-only (`# ...held in FileState.AWAITING_CLOUD...`). No code dep; tidy optional (the guard strips comments, so it does not force this).

### Residual LIVE `FileState` refs in `src/phaze/**` that PR-A/PR-B did NOT remove

`git grep -c FileState src/phaze` spans ~20 files. Classified line-by-line (`git grep -n`):

**LIVE (executable) — MUST delete in 90-04:**
- `services/pipeline.py:21` — `from phaze.models.file import FileRecord, FileState` (live import).
- `services/pipeline.py:81-90` — `PIPELINE_STAGES = [FileState.DISCOVERED, …, FileState.EXECUTED]` (live module-level list). **No runtime consumer** — `git grep PIPELINE_STAGES` shows the only consumer is `tests/shared/services/test_pipeline.py:155-157` (`assert FileState.ANALYSIS_FAILED not in PIPELINE_STAGES`); the other src refs (`:100`, `:1299`, `:1324`) are comments. → **DELETE the list + the import; migrate/remove that one test.** `[VERIFIED: grep]`

**DEAD prose (comments/docstrings) — do NOT break mypy/ruff; the D-08 guard MUST strip these:**
`config.py:619`, `models/analysis.py:36,41`, `models/cloud_job.py:15,31`, `models/dedup_resolution.py:8`, `routers/agent_analysis.py:294`, `routers/agent_fingerprint.py:102`, `routers/agent_push.py:23,197`, `routers/pipeline.py:600,1165,1303`, `schemas/agent_push.py:8`, `services/agent_client.py:334`, `services/backends.py:10,21,322,383,486`, `services/stage_status.py:125`, `tasks/agent_worker.py:85`, `tasks/reenqueue.py:151`, `tasks/release_awaiting_cloud.py:4,13,187,241`, `templates/pipeline/partials/{analyzing_cloud,awaiting_cloud,staged_pushing}_card.html`.

**Net:** after Task 2, the only executable `FileState` in `src/phaze` are the two `pipeline.py` sites + the model/`__init__` definitions. Everything else is dead prose. mypy + ruff + import go green once the four executable sites are gone.

### ⚠ Draft-plan correction (Task 2 acceptance is wrong)

The draft plan's acceptance `grep -rn "FileState" src/phaze | grep -v '#'` **== 0** will FAIL. Most residual refs live in **triple-quoted docstrings** (e.g. `backends.py:10,21,322,383,486`), which are NOT `#` comments — `grep -v '#'` does not filter them, so they remain and the check never reaches 0. **Fix:** the real acceptance is "no *executable* `FileState` reference" — i.e. `uv run mypy .` + `uv run ruff check .` clean and `python -c "import phaze"` succeeds. The docstring prose is harmless and is what the D-08 guard's comment/string stripping is FOR.

---

## Q4 — the D-08 mutation-tested guard

### Analog structure (`tests/shared/test_no_raw_state_render.py`)

- Module-level scanned-root + compiled regexes; `_iter_*_files()` returning a `list[Path]`.
- `_strip_comments_keep_lines()` blanks `{# … #}` while preserving line count.
- **Vacuous-glob assert** — `assert _iter_template_files(), "guard scanned no templates"` (a silent empty glob must not pass).
- **Planted-match self-test** (`test_guard_flags_a_planted_render`) — asserts the regexes MATCH each forbidden form AND do NOT match legitimate lookalikes.
- Mutation observation recorded in the SUMMARY.

### Guard spec for `tests/shared/test_no_filestate_guard.py`

**Scan** `src/phaze/**/*.py`.

**Strip comments AND string/docstring literals — this is load-bearing.** ~20 src files carry `FileState` in docstrings (Q3). A `#`-only strip (the analog's approach) is INSUFFICIENT because docstrings are triple-quoted strings, not `#` comments. **Use `tokenize`** to drop `COMMENT` and `STRING` tokens, then scan the reconstructed executable-token stream. This also naturally solves the multi-line problem (tokenize is line-agnostic).

**Forbidden forms to catch (non-toothless):**
1. **bare token `FileState`** in executable code — strongest signal: the class is deleted, so ANY executable reference is a reintroduction.
2. **`FileRecord.state`** and **`files.state`** (attribute read/write on the file record / table).
3. **`.values(state=…)` including the multi-line `.values(\n    state=…)` form** — scan whole-file text with `re.DOTALL` (or the token stream), NOT line-by-line. Memory `feedback_mutation_test_guard_tests` warns line-grep is blind to multi-line `.values`.

**`.values(**splat)` limitation — be honest:** a dynamically-built dict `{"state": …}` splatted into `.values(**payload)` cannot be statically resolved to a state key. Recommendation: emit a **soft WARNING match** on `.values(**` for manual review, and document that the real backstop is structural — the `files.state` column no longer exists, so any `.values(**{"state":…})` against the `files` table fails at SQL compile/execute and is caught by any test exercising that path. Do not pretend the static guard catches it.

**Specificity — avoid FastAPI false positives.** Do NOT use a bare `\w+\.state` regex (the analog's template form): a FastAPI codebase has `request.app.state`, `app.state.redis`, `websocket.state`. Scope form 2 to `FileRecord.state` and `\bfiles\.state\b` (plus the `FileState` token) so the guard stays specific. The planted self-test MUST include negative cases: `stage_status`, `cloud_job.status`, `app.state.redis`, `request.app.state` — assert NONE match.

**Vacuous-glob assert:** `assert py_files, "guard scanned no src/phaze files"`.

**Planted-match self-test:** assert the regex set matches each forbidden form including a multi-line `.values(\n    state="x")` literal and a `.values(**payload)` string; assert the lookalikes above do NOT match.

**MANUAL mutation (record in SUMMARY):** reintroduce a real `FileState` import or `.values(state=…)` into a src file → run guard → confirm RED → restore → GREEN. A green guard proves nothing (memory `feedback_mutation_test_guard_tests`: Phase 83 shipped two toothless guards).

### ⚠ Draft-plan correction (buckets.json is a no-op)

`just test-bucket NAME` runs `pytest tests/{{NAME}}` (`justfile:125`); a "bucket" is a top-level subdir of `tests/`, and `tests/buckets.json` is just the list of those dir NAMES (`["discovery","metadata",…,"shared"]`). Placing the guard at `tests/shared/test_no_filestate_guard.py` puts it in the existing **"shared"** bucket automatically. **No `buckets.json` edit is required** — the draft plan's "register in tests/buckets.json (shared bucket)" step is a no-op (the analog `test_no_raw_state_render.py` and `test_shadow_compare_cli.py` already live in `tests/shared/` with no per-file registration). `[VERIFIED: read justfile, buckets.json, .github/workflows/tests.yml]`

---

## Runtime State Inventory

This is a code/test-only deletion; the *data* migration (drop column, archive) already shipped in 039 (90-03). For 90-04 specifically:

| Category | Items Found | Action |
|----------|-------------|--------|
| Stored data | None — the `files.state` column + archive were handled by 039 in 90-03. | none |
| Live service config | None. `shadow_compare` CLI is a manual `python -m` operator tool, not a registered/scheduled service (no cron, no pyproject script, no cli/__init__ registration). | none |
| OS-registered state | None. | none |
| Secrets/env vars | None. The `--database-url` arg of the deleted CLI carried a DSN but no stored secret key changes. | none |
| Build artifacts | None new. (`FileState` removal may leave stale `__pycache__` .pyc for deleted test modules — harmless; `find tests -name '*.pyc'` shows the 3 divergence .pyc, auto-regenerated.) | none |

## Environment Availability

| Dependency | Required by | Available | Notes |
|------------|-------------|-----------|-------|
| Test DB `:5433` (`+asyncpg`) | every DB test in the sweep | provision via `just test-db` | Export `TEST_DATABASE_URL` + `MIGRATIONS_TEST_DATABASE_URL` at `:5433` before running — `just test-bucket` does NOT export them (memory `reference_migrations_test_db_port`). Bare `postgresql://` fails `ModuleNotFoundError: psycopg2`. |

No new packages. No Package Legitimacy Audit needed (zero installs).

---

## Common Pitfalls

1. **Deleting `test_drain_double_dispatch.py` outright** — loses the ROADMAP double-dispatch hard gate (the 2026-06-18 ~44.5K over-enqueue class). Its core is `~inflight_clause`/scheduling_ledger, state-independent. MIGRATE.
2. **`grep -v '#'` acceptance for FileState removal** — misses triple-quoted docstrings; use mypy/ruff/import-clean instead.
3. **A `\w+\.state` guard regex** — false-positives on FastAPI `app.state`/`request.app.state`. Scope to `FileRecord.state`/`files.state`/`FileState`.
4. **`#`-only comment strip in the guard** — leaves ~20 docstring `FileState` mentions → guard self-fails. Strip STRING tokens too (use `tokenize`).
5. **Per-module coverage floor (90%) drop** after retiring divergence tests without removing the code they cover. Run `just coverage-combine` (also enforces combined 95%).
6. **Removing `make_file`'s `state=` while callers still pass it** — TypeError storm. Column drop + `make_file` param drop + call-site sweep must converge to green within the plan; expect transient red mid-sweep (fine within one PR).
7. **Line-by-line scan missing multi-line `.values(\n  state=…)`** — scan whole-file/token-stream.

## State of the Art

| Old (pre-90) | Current (post PR-A/B, this plan finalizes) | Impact |
|--------------|-------------------------------------------|--------|
| `FileRecord.state` scalar cursor read across dashboard/dedup/pending | Derived from output tables via `stage_status.py` clause builders + `cloud_job` sidecar + markers | 90-04 deletes the now-orphaned enum/column/index |
| `shadow_compare` standing gate (state ⇔ derived) | Void once the column is gone; hard invariants frozen inline in 039 | Full subsystem removal |

---

## Planner Guidance

**Task ordering (4 tasks, matches the draft's shape with corrections):**
1. **Retire shadow_compare** — delete `services/shadow_compare.py` + `cli/shadow_compare.py`; delete `test_shadow_compare.py`, `test_shadow_compare_skipped.py`, `test_shadow_compare_cli.py`; **MIGRATE** (not delete) `test_drain_double_dispatch.py` (strip state seeds, keep ledger exactly-once coverage) and `test_dedup_resolve_undo_shadow.py` (strip `run_shadow_compare`/`dup.state==`, keep marker resolve-undo; or retire if PR-A twin covers it); **RETIRE** `test_dedup_divergence.py` + `test_pending_set_divergence.py` (unseedable post-drop). No CLI de-registration needed.
2. **Delete model surface + fix the lever together** — `file.py` (class 20-71, column :86, index :97, `import enum` :5), `models/__init__.py` (:9, :36), `pipeline.py` (`FileState` import :21 + `PIPELINE_STAGES` :81-90), optional `config.py:619` tidy; AND `conftest.py` `make_file` (drop `state=` param + `:436` line) + the 8 sibling factories (:461,497,532,552,614,679,712,722) converted to marker seeds. Acceptance = mypy/ruff/import clean (NOT `grep -v '#'`).
3. **Sweep ~90 test files bucket-by-bucket** (export `:5433` URLs) — mechanical kwarg-drop + import removal for incidental seeds; marker-seed (AnalysisResult/cloud_job/DedupResolution/FileMetadata) for derived-status tests per 90-01/90-02 pattern. Confirm `test_039_autogenerate_diff_is_empty_for_dropped_objects` flips GREEN. Run `just coverage-combine` (90% per-module + 95% combined).
4. **Guard** — `tests/shared/test_no_filestate_guard.py`: tokenize-strip comments+strings, forbid `FileState`/`FileRecord.state`/`files.state`/multi-line `.values(state=`, soft-warn `.values(**`, specificity negatives for `app.state`/`cloud_job.status`/`stage_status`, vacuous-glob assert, planted self-test, documented manual RED→GREEN. **No buckets.json edit.**

**The shadow_compare removal verdict:** FULL REMOVAL, no relocation. All app-layer couplings are prose; the dependency runs shadow_compare→stage_status (not the reverse); the hard invariants are frozen inline in 039.

**The test-migration lever:** `conftest.py:416 make_file` + its 8 sibling factories — fix these in one file and the incidental majority collapses to kwarg-drops; only the derived-status minority needs marker seeds.

**The guard spec:** tokenize-based comment+string strip (not `#`-only), whole-file/DOTALL scan for multi-line `.values`, scoped attribute regexes to dodge FastAPI `app.state`, honest `.values(**splat)` limitation note.

---

## Sources

### Primary (HIGH — repo read/grep at phase HEAD, 2026-07-12)
- `src/phaze/services/shadow_compare.py`, `src/phaze/cli/shadow_compare.py`, `src/phaze/services/stage_status.py` (dependency direction)
- `src/phaze/models/file.py`, `src/phaze/models/__init__.py`, `src/phaze/services/pipeline.py:21,81-90`, `src/phaze/config.py:619`
- `alembic/versions/039_drop_files_state_column.py:52,96-113` (frozen inline invariants)
- `tests/conftest.py:416-443,461-722` (`make_file` + siblings)
- `tests/shared/test_no_raw_state_render.py` (guard analog)
- `tests/integration/test_dedup_divergence.py`, `test_drain_double_dispatch.py`, `test_dedup_resolve_undo_shadow.py` (retire/migrate classification)
- `justfile:123-140`, `tests/buckets.json`, `.github/workflows/tests.yml:19-104`, `scripts/coverage_floor.py:33` (bucket + coverage mechanics)
- 90-CONTEXT.md, 90-01-SUMMARY.md, 90-02-SUMMARY.md, 90-03-SUMMARY.md, 90-04-PLAN.md (draft), `.planning/REQUIREMENTS.md` MIG-04

### Project memory applied
- `feedback_mutation_test_guard_tests` (mutate every syntactic form; multi-line `.values`), `reference_migrations_test_db_port` (`:5433` DB URL export), `project_phase90_surviving_state_readers`.

## Metadata
- **Confidence:** shadow_compare removal HIGH (grep-proven prose-only couplings); removal surface HIGH (line-exact); test migration HIGH (pattern + lever proven by PR-A/B summaries); guard HIGH (analog + memory).
- **Valid until:** phase HEAD only — line numbers drift on any edit. Re-grep before executing.
