# Phase 63: Parallel CI & Code-Change Gating - Pattern Map

**Mapped:** 2026-07-02
**Files analyzed:** 6 modify targets + 1 new guard test + 9 new bucket dirs (reorg)
**Analogs found:** 7 / 7 (every target has a same-repo analog; this is a restructure, not greenfield)

> This is a CI-workflow + test-layout phase. **No product code changes.** Every "file to create/modify"
> already has a close in-repo analog to copy from — the work is re-shaping existing patterns, so match
> quality is uniformly high. All frozen action SHAs, recipe shapes, and the conftest collection hook are
> extracted verbatim below so the planner can cite exact lines.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `.github/workflows/tests.yml` (matrix + combine) | config (CI reusable workflow) | batch / fan-out | itself (current single `test` job) + `docker-publish.yml` artifact steps | exact (self) + role-match (artifacts) |
| `.github/workflows/ci.yml` (broaden classifier) | config (CI orchestrator) | event-driven (gate) | itself — `detect-changes` + `aggregate-results` blocks | exact (self) |
| `justfile` (`test-bucket`, `coverage-combine`) | config (command runner) | request-response (invoke) | existing `[group('test')]` recipes (`test-ci`, `test-file`, `integration-test`) | exact |
| `pyproject.toml` (xdist dep + coverage cfg) | config | — | existing `[tool.coverage.run]` + `[dependency-groups]` + `[tool.uv]` | exact |
| `tests/<bucket>/` × 9 (reorg of 212 files) | test (layout) | batch | existing `tests/test_*/` package dirs (`test_services`, `test_routers`, `integration`) | exact |
| `tests/<bucket>/test_partition_guard.py` (NEW, D-06) | test (structural guard) | transform (walk collected items) | `tests/conftest.py::pytest_collection_modifyitems` (path-parts walk) + `tests/test_dead_template_guard.py` (walk-tree-assert-invariant) | exact (hook) + role-match (guard skeleton) |
| `tests/<bucket>/test_change_gate.py` (NEW, D-09) | test (regression) | transform (classify file lists) | `tests/test_no_default_queue_producers.py` meta-tests (feed sample → assert) | role-match |

## Pattern Assignments

### `.github/workflows/tests.yml` (config, batch/fan-out)

**Analog:** itself (current single `test` job) — lift the services block + step order VERBATIM into each
matrix leg; move the Codecov step into a new `combine` job.

**Frozen action SHAs to reuse (do NOT re-pin; copy exactly)** — from `tests.yml:54-83` and
`docker-publish.yml:401,479`:
```yaml
- uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0        # v7.0.0
- uses: astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39      # v8.2.0
- uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405    # v6.2.0
- uses: extractions/setup-just@53165ef7e734c5c07cb06b3c8e7b647c5aa16db3  # v4.0.0
- uses: codecov/codecov-action@fb8b3582c8e4def4969c97caa2f19720cb33a72f  # v7.0.0
- uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a    # v7.0.1
- uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c  # v8.0.1
```
> upload/download-artifact SHAs come from `docker-publish.yml` — the repo already vendors v4+ artifact
> actions at these exact pins. Reuse them; do not introduce new pins.

**Services + env block to lift VERBATIM into each matrix leg** (`tests.yml:19-52`):
```yaml
services:
  postgres:
    image: postgres:18-alpine
    env: { POSTGRES_USER: phaze, POSTGRES_PASSWORD: phaze, POSTGRES_DB: phaze_test }
    ports: ["5432:5432"]
    options: >-
      --health-cmd "pg_isready -U phaze" --health-interval 5s --health-timeout 5s --health-retries 5
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    options: >-
      --health-cmd "redis-cli ping" --health-interval 5s --health-timeout 5s --health-retries 5
env:
  DATABASE_URL: postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test
  TEST_DATABASE_URL: postgresql+asyncpg://phaze:phaze@localhost:5432/phaze_test
  PHAZE_REDIS_URL: redis://localhost:6379/0
```
Per D-07 (revised by research Q-A): provision this in **every** bucket leg — 55% of files consume DB
fixtures, so "service-free unit buckets" is impractical. Keep the migrations-DB creation step verbatim
(`tests.yml:70-74`):
```yaml
- name: 🐘 Create migrations test database
  env: { PGPASSWORD: phaze }
  run: psql -h localhost -U phaze -d postgres -c 'CREATE DATABASE phaze_migrations_test OWNER phaze;'
```

**Matrix shape** (new; `fail-fast: false` so one bucket's failure still surfaces the others):
```yaml
strategy:
  fail-fast: false
  matrix:
    bucket: [discovery, metadata, fingerprint, analyze, identify, review, agents, integration, shared]
```

**Per-leg run + artifact upload** (delegate to `just` per D-10; the run step replaces the current
`- run: just test-ci` at `tests.yml:80`):
```yaml
- run: just install
- run: just test-bucket ${{ matrix.bucket }}   # XDIST passed only for DB-free buckets
  env: { DATABASE_URL: ..., TEST_DATABASE_URL: ..., PHAZE_REDIS_URL: ... }  # same as leg env
- uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a  # v7.0.1
  with:
    name: coverage-${{ matrix.bucket }}
    path: .coverage.${{ matrix.bucket }}
    include-hidden-files: true    # .coverage.* is a dotfile — v4 default drops it
    if-no-files-found: error      # a bucket producing no data is a bug; fail loud
```

**Combine job** (new; the Codecov step MOVES here from `tests.yml:82-89` — single upload preserved,
CODECOV_TOKEN only here per the info-disclosure mitigation):
```yaml
combine:
  needs: [test]
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0      # v7.0.0
    - uses: astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39    # v8.2.0
    - uses: extractions/setup-just@53165ef7e734c5c07cb06b3c8e7b647c5aa16db3 # v4.0.0
    - run: just install
    - uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c  # v8.0.1
      with: { pattern: coverage-*, merge-multiple: true }
    - run: just coverage-combine
    - uses: codecov/codecov-action@fb8b3582c8e4def4969c97caa2f19720cb33a72f  # v7.0.0
      with: { flags: unittests, disable_search: true, files: ./coverage.xml }
      env: { CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }} }
```
Keep the emoji-prefixed step names convention (`🔀 Checkout`, `📦 Install`, `🧪 Run`, `📊 Upload`) from
`tests.yml:54,57,76,82`.

**Required-check topology (do NOT touch):** the matrix + combine both live INSIDE `tests.yml`, so
`ci.yml`'s `test:` node stays a single reusable-workflow call and `aggregate-results` (the branch-
protection required check) is unchanged. See the `ci.yml` assignment below.

---

### `.github/workflows/ci.yml` (config, event-driven gate)

**Analog:** itself — `detect-changes` (lines 33-80) and `aggregate-results` (lines 111-158) are ~80%
of CI-04. Broaden ONE line; add regression tests. Do NOT restructure the job graph.

**The exact line to broaden** — `ci.yml:72`:
```bash
# CURRENT (markdown-only skip):
NON_MD_FILES=$(echo "${CHANGED_FILES}" | grep -v '\.md$' || true)
if [[ -z "${NON_MD_FILES}" ]]; then ...
```
Broaden the skippable set to `.planning/**`, `LICENSE`, docs (D-08). Keep the classifier **conservative**
(anything not clearly a doc path stays `code-changed=true`) per the security mitigation:
```bash
CODE_FILES=$(echo "${CHANGED_FILES}" | grep -vE '(\.md$|^\.planning/|^LICENSE$|^docs/|\.txt$)' || true)
if [[ -z "${CODE_FILES}" ]]; then
  echo "code-changed=false" >> "${GITHUB_OUTPUT}"
else
  echo "code-changed=true"  >> "${GITHUB_OUTPUT}"
fi
```
Per D-09/research: extract the classifier to a small **tested script** (shell or Python) so
`test_change_gate.py` can unit-test it. The SHA edge-case block (`ci.yml:52-67`: schedule/tag,
zero-SHA new branch, force-push gone before-SHA) stays UNTOUCHED — it already handles PR/new-branch/
force-push correctly.

**Skip-with-success contract to preserve VERBATIM** — `aggregate-results` (`ci.yml:119,144-148`):
```yaml
aggregate-results:
  needs: [detect-changes, quality, test, security, docker]
  if: always()                         # <- runs even when heavy jobs skip = never skip-ABSENT
```
```bash
if [[ "${CODE_CHANGED}" == "false" ]]; then
  echo "📄 Docs-only change — skipped jobs are expected"
  echo "✅ All required pipeline workflows passed!"
  exit 0                               # <- skip-with-SUCCESS: the required check stays green + mergeable
fi
```
This is THE required check. Leaving `if: always()` + the `code-changed==false → exit 0` branch intact is
what keeps a `.planning/**`-only PR mergeable under branch protection. Anti-pattern to avoid (research):
never convert this to trigger-level `paths-ignore` (that produces skip-*absent* → unmergeable).

---

### `justfile` (config, command runner)

**Analog:** existing `[group('test')]` recipes — `test-ci` (lines 85-88), `test-file` (90-93),
`integration-test` (168-178). Copy the `[doc(...)]` + `[group('test')]` attribute stanza + `uv run pytest`
body shape exactly.

**Existing recipe to mirror** (`justfile:85-93`):
```makefile
[doc('Run tests with coverage XML output (for CI)')]
[group('test')]
test-ci:
    uv run pytest --cov=phaze --cov-report=xml --cov-report=term-missing

[doc('Run a specific test file')]
[group('test')]
test-file FILE:
    uv run pytest {{FILE}} -x -v
```

**New recipes** (D-10; parameter default `XDIST=""` mirrors `test-file`'s `FILE` / `integration-test`'s
env pattern — serial DB-safe default, `-n auto` opt-in only for verified DB-free buckets per Q-B):
```makefile
[doc('Run a single test bucket, writing coverage data to .coverage.<bucket> (CI shard)')]
[group('test')]
test-bucket NAME XDIST="":
    COVERAGE_FILE=.coverage.{{NAME}} uv run pytest tests/{{NAME}} {{XDIST}} --cov=phaze --cov-report= -q

[doc('Combine per-bucket .coverage.* shards into coverage.xml and enforce the gate')]
[group('test')]
coverage-combine:
    uv run coverage combine
    uv run coverage xml
    uv run coverage report --fail-under=85
```
> The `--fail-under=85` mirrors `[tool.coverage.report] fail_under = 85` (pyproject.toml:68). Phase 63
> keeps 85 — Phase 64 raises it. Do NOT raise here.

**Keep `scripts/update-project.sh` current** (feedback rule): that script treats the justfile as the
single source of truth (`update-project.sh:20`) and already runs `just lint/typecheck/test` in its verify
phase (lines 1046-1060). Adding recipes needs no script edit UNLESS a new recipe should join the CI
verify sweep — flag to the planner but no structural change required.

---

### `pyproject.toml` (config)

**Analog:** existing `[tool.coverage.run]` (72-75), `[dependency-groups] dev` (207-226), `[tool.uv]`
exclude-newer (188-197).

**Coverage config — ADD `relative_files`, KEEP concurrency** (`pyproject.toml:72-75`):
```toml
[tool.coverage.run]
concurrency = ["greenlet", "thread"]   # KEEP AS-IS. Do NOT add "multiprocessing" (research A2):
                                       # xdist workers are execnet processes merged by pytest-cov,
                                       # not multiprocessing children of code-under-test.
omit = ["tests/*"]
source = ["phaze"]
relative_files = true                  # ADD: lets `coverage combine` union shard data by relative path
```

**Dev dependency — ADD pytest-xdist** (alphabetical, between `pre-commit` and `pytest`;
`pyproject.toml:220-223`):
```toml
    "pre-commit>=4.6.0",
    "pytest>=9.1.1",
    "pytest-asyncio>=1.4.0",
    "pytest-cov>=7.1.0",
    "pytest-xdist>=3.8.0",   # ADD — verify publish date clears the `exclude-newer = "7 days"` cooldown
                             # (pyproject.toml:197) at lock time; pin an older floor (3.7.0/3.6.1) if not.
```
> Ordering note: `[dependency-groups] dev` is alphabetically sorted (CLAUDE.md convention). `pytest-xdist`
> sorts AFTER `pytest-cov`. `respx`/`ruff` follow.

**pytest markers** (`pyproject.toml:125-130`) — unchanged; the `integration` marker + auto-marker stay.

---

### `tests/<bucket>/` × 9 (test layout reorg)

**Analog:** existing `tests/test_*/` package dirs. Current layout is code-LAYER dirs
(`test_services` 38, `test_routers` 36, `test_tasks` 28, `test_migrations` 14, …, plus 45 files at
`tests/` root). Reorg is to 9 DOMAIN buckets. Every bucket dir needs `__init__.py` (import-mode=prepend
requires packages — same as every existing `tests/test_*/` dir today).

**Files that STAY at `tests/` root (D-05 — do NOT move):**
```
tests/__init__.py            tests/conftest.py            tests/_queue_fakes.py
tests/_route_introspection.py    tests/kube_fakes.py
```
Hierarchical conftest keeps the root fixtures applying to every bucket subdir; absolute imports
(`from tests._queue_fakes import …`) keep resolving. **Only `test_*.py` files relocate.**

**Reorg hazards to respect (research Pitfalls 1-2, LOAD-BEARING):**
- **Same-dir basename collisions** — `test_fingerprint.py`, `test_pipeline.py`, `test_proposal.py`,
  `test_execution.py` (+~15) exist in 2+ current dirs. Two same-named files in one `tests/<bucket>/`
  break collection. Detect BEFORE wiring the matrix:
  `find tests -name 'test_*.py' | xargs -n1 basename | sort | uniq -d` must be empty within any single
  bucket dir. Fix by one level of sub-nesting (each a package) or rename-on-move.
- **`from tests.test_migrations.conftest import …`** (13 sites) breaks if `test_migrations/` moves.
  Rewrite the dotted path OR keep migrations at a stable location (under `integration/` keeps both
  `"integration"` and `"test_migrations"` in `path_parts` → auto-marker still fires; see next file).

---

### `tests/<bucket>/test_partition_guard.py` (NEW — test, structural guard) — D-06

**Primary analog:** `tests/conftest.py::pytest_collection_modifyitems` (lines 116-135) — the EXACT
precedent for reading `item.path.parts` off collected items. The guard is a path check over the same
`item.path` surface.

**The collection-hook pattern to mirror** (`tests/conftest.py:132-135`):
```python
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        path_parts = item.path.parts
        if DB_FIXTURES & set(getattr(item, "fixturenames", ())) or "test_migrations" in path_parts or "integration" in path_parts:
            item.add_marker(pytest.mark.integration)
```
The guard reuses `item.path.parts` the same way, but ASSERTS the top segment under `tests/` is a known
bucket rather than adding a marker. Two implementation options:
- (a) A `pytest_collection_modifyitems` hook in the guard's own conftest that raises if any item escapes a
  bucket, OR
- (b) A plain test that shells `pytest --collect-only -q` / imports the session — but option (a) reuses
  the established `item.path.parts` surface directly and is the closest analog.

**Secondary analog — walk-tree-assert-invariant skeleton:** `tests/test_dead_template_guard.py`
(lines 43-95). Copy its module shape: `_REPO_ROOT = Path(__file__).resolve().parents[1]`, a single
`set`-difference invariant, one assertion with a diagnostic message listing offenders:
```python
_REPO_ROOT = Path(__file__).resolve().parents[1]
...
orphans = all_templates - reachable - _ALLOWLIST
assert not orphans, f"Orphaned templates (referenced by nobody): {sorted(orphans)}"
```
For the partition guard the invariant is: `{top-segment of each collected test path} ⊆ KNOWN_BUCKETS`.
Failure message must name the offending file + its (unknown) segment so a new unbucketed test fails loud.

**Single-source-of-truth note (research Pattern 2):** derive `KNOWN_BUCKETS` from the immediate subdirs
of `tests/` (or a shared `tests/buckets.json` consumed by matrix + guard + `just test-bucket`) so the
matrix, the guard, and the recipe can't drift.

---

### `tests/<bucket>/test_change_gate.py` (NEW — test, regression) — D-09

**Analog:** the meta-tests in `tests/test_no_default_queue_producers.py` (lines 127-163) — the
"feed a crafted sample through the logic, assert the output" pattern, proving the guard isn't vacuously
green.

**Meta-test skeleton to mirror** (`test_no_default_queue_producers.py:127-138`):
```python
def test_static_guard_would_catch_a_reintroduced_producer() -> None:
    """Meta-test: the AST visitor flags both offence classes on a crafted sample."""
    sample = "def boom(request):\n    q = request.app.state.queue\n    return Queue.from_url(url)\n"
    visitor = _ProducerVisitor()
    visitor.visit(ast.parse(sample))
    assert [lineno for lineno, _ in visitor.default_refs] == [2]
    assert [lineno for lineno, _ in visitor.unnamed_queues] == [3]
```
Apply the same shape to the extracted change-gate classifier: feed representative changed-file lists
(all-`.planning/`, mixed doc+`.py`, `LICENSE`-only, a bare `.py`) and assert the `code-changed` output.
The conservative-classifier security property (any non-doc path ⇒ `code-changed=true`) MUST have a
positive test.

---

## Shared Patterns

### Frozen action SHAs (V14 CI supply-chain, CLAUDE.md convention)
**Source:** `tests.yml:54-83`, `docker-publish.yml:401,479`
**Apply to:** every `uses:` in `tests.yml`
Reuse the exact pins listed in the `tests.yml` assignment above. Every action is `@<40-char-sha>  # vX.Y.Z`.
Never use a floating tag. The `check-jsonschema` + `actionlint` pre-commit hooks validate these.

### `just` delegation (D-10, feedback: workflows-use-just)
**Source:** `tests.yml:76-80` (`run: just install` / `run: just test-ci`)
**Apply to:** every non-trivial CI step
CI steps invoke `just <recipe>`, never inline multi-line shell. New CI logic (`test-bucket`,
`coverage-combine`) lands as recipes first, then the workflow calls them.

### Emoji-prefixed step names
**Source:** `tests.yml:54,57,60,65,70,76,79,82` and `ci.yml:38,43`
**Apply to:** every named workflow step (`🔀 Checkout`, `📦 Install uv`, `🐍 Set up Python`,
`🔧 Setup Just`, `🐘 Create … database`, `🧪 Run tests`, `📊 Upload coverage`).

### Structural guard test skeleton
**Source:** `tests/test_dead_template_guard.py`, `tests/test_no_default_queue_producers.py`
**Apply to:** both new guard tests (`test_partition_guard.py`, `test_change_gate.py`)
Shape: `_REPO_ROOT = Path(__file__).resolve().parents[1]`; compute a set/invariant; ONE assertion whose
message enumerates offenders; add a meta-test proving the guard is not vacuously green.

### Codecov single-upload convention
**Source:** `tests.yml:82-89`
**Apply to:** the combine job ONLY
`codecov/codecov-action@fb8b…  # v7.0.0` with `flags: unittests`, `disable_search: true`,
`files: ./coverage.xml`, `CODECOV_TOKEN` in env. Exactly ONE upload across the whole run (moved from the
per-run `test` job to the `combine` job). No token in any per-bucket matrix leg (info-disclosure
mitigation).

## No Analog Found

None. Every target has a same-repo analog. The only genuinely-custom (non-copy) work is judgment, not
pattern:
- **file→bucket assignment** (research Q-C): semantic/manual mapping of 212 files to 9 buckets — no
  code analog; produce an explicit mapping table as a plan artifact, enforced by the partition guard.
- **basename-collision resolution** during the reorg (research Pitfall 1): mechanical, verified by the
  `basename | sort | uniq -d` check.

## Metadata

**Analog search scope:** `.github/workflows/`, `justfile`, `pyproject.toml`, `tests/` (conftest +
existing structural guards + dir tree), `scripts/update-project.sh`.
**Files scanned:** ci.yml, tests.yml, docker-publish.yml (SHAs), justfile, pyproject.toml,
tests/conftest.py, tests/integration/conftest.py, tests/test_dead_template_guard.py,
tests/test_no_default_queue_producers.py, scripts/update-project.sh, full tests/ dir listing.
**Pattern extraction date:** 2026-07-02
</content>
</invoke>
