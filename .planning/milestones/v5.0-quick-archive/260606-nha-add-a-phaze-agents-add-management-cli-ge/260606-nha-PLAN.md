---
phase: quick-260606-nha
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/cli/__init__.py
  - pyproject.toml
  - tests/test_cli/__init__.py
  - tests/test_cli/test_agents_add.py
  - docs/deployment.md
  - docs/configuration.md
autonomous: true
requirements: [QUICK-CLI-01]

must_haves:
  truths:
    - "Operator runs `phaze agents add --id x-y --name X --scan-roots /data/music` and an agents row is inserted whose token_hash == hash_token(printed_token)"
    - "The cleartext token is printed exactly once with a not-recoverable notice and the derived queue name `phaze-agent-x-y` is printed"
    - "Invalid ids (Foo_Bar, -x, x-, empty) are rejected with a non-zero exit BEFORE any DB write"
    - "Duplicate id produces a friendly message and a non-zero exit"
    - "docs/deployment.md Step 3 shows `phaze agents add` as the primary registration path; the queue convention is documented in deployment.md and configuration.md"
  artifacts:
    - path: "src/phaze/cli/__init__.py"
      provides: "argparse CLI exposing main(); add_agent() core coroutine; agent_id validation; queue-name derivation"
      exports: ["main", "add_agent"]
    - path: "tests/test_cli/test_agents_add.py"
      provides: "Coverage for happy path, id-charset rejection, duplicate-id error"
    - path: "pyproject.toml"
      provides: "[project.scripts] phaze entry point"
      contains: "[project.scripts]"
  key_links:
    - from: "src/phaze/cli/__init__.py"
      to: "phaze.routers.agent_auth.hash_token"
      via: "import + call to hash the minted token"
      pattern: "hash_token"
    - from: "src/phaze/cli/__init__.py"
      to: "phaze.database.async_session"
      via: "async context manager for the insert"
      pattern: "async_session"
    - from: "src/phaze/cli/__init__.py"
      to: "agents.id_charset CheckConstraint"
      via: "regex ^[a-z0-9]+(-[a-z0-9]+)*$ validated pre-DB"
      pattern: "a-z0-9"
---

<objective>
Add a `phaze agents add` management CLI (stdlib argparse, NO new dependency) that
mints an agent token, inserts an `agents` row, and prints the token once plus the
derived `phaze-agent-<id>` queue name. Replace the hand-written SQL INSERT in the
deployment docs with the CLI as the primary path and document the PHAZE_AGENT_QUEUE
naming convention in both docs.

Purpose: Agent provisioning currently requires the operator to compute sha256(token)
by hand and risks violating the `agents.id` charset constraint. There is also no
documentation of how PHAZE_AGENT_QUEUE relates to an agent (no queue column exists --
the name is derived from agent_id by convention and asserted at worker startup).

Output: `src/phaze/cli/__init__.py`, a `[project.scripts]` entry, tests keeping the
new module >=85% covered, and updated deployment/configuration docs.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@CLAUDE.md

<interfaces>
<!-- Reuse these directly -- do not reimplement. -->

From src/phaze/routers/agent_auth.py:
```python
def hash_token(token: str) -> str:  # sha256 hex of the FULL wire token (prefix incl.)
```

From src/phaze/database.py:
```python
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
# Use as: `async with async_session() as session: ...`  (NOT the get_session generator)
```

From src/phaze/models/agent.py:
```python
class Agent(TimestampMixin, Base):  # __tablename__ = "agents"
    id: Mapped[str]            # String(64), PK, CheckConstraint id_charset: ^[a-z0-9]+(-[a-z0-9]+)*$
    name: Mapped[str]          # String(128), NOT NULL
    token_hash: Mapped[str | None]   # String(128)
    scan_roots: Mapped[list[str]]    # JSONB, NOT NULL, server_default '[]'
# Token wire format: "phaze_agent_" + secrets.token_urlsafe(32)
```

From src/phaze/tasks/agent_worker.py (~line 104) -- the queue-docs answer:
```python
expected_queue = f"phaze-agent-{identity.agent_id}"   # derived by convention, asserted at startup
```

From tests/conftest.py -- reuse these fixtures (do NOT create new DB harness):
```python
@pytest_asyncio.fixture
async def async_engine(...) -> AsyncEngine   # creates schema, seeds legacy agent, drops on teardown
@pytest_asyncio.fixture
async def session(async_engine) -> AsyncSession   # per-test session
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Create `phaze agents add` CLI and register the entry point</name>
  <files>src/phaze/cli/__init__.py, pyproject.toml</files>
  <behavior>
    - validate_agent_id("x-y") returns None (ok); validate_agent_id raises ValueError for "Foo_Bar", "-x", "x-", "" (uppercase, leading/trailing hyphen, empty).
    - validate_scan_roots(["/data/music"]) ok; raises ValueError for a relative path like "data/music".
    - derive_queue_name("x-y") == "phaze-agent-x-y".
    - add_agent(session, "x-y", "X", ["/data/music"]) inserts one Agent row, commits, and returns a cleartext token starting with "phaze_agent_"; the stored token_hash equals hash_token(returned_token); scan_roots stored as ["/data/music"].
    - add_agent against an already-present id raises sqlalchemy.exc.IntegrityError.
  </behavior>
  <action>
    Create `src/phaze/cli/__init__.py` as a stdlib-only argparse CLI. NO new third-party dependency (no click/typer). Module-level constant `AGENT_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")` -- the SAME regex as the `agents.id_charset` CheckConstraint; do NOT weaken or restate it differently. Provide these typed functions:
    - `validate_agent_id(agent_id: str) -> None` -- raise ValueError with a clear message if it does not fully match AGENT_ID_RE.
    - `validate_scan_roots(scan_roots: list[str]) -> None` -- raise ValueError if any entry is not an absolute path (use `pathlib.Path(p).is_absolute()`, per project PTH rules; never use os.path).
    - `derive_queue_name(agent_id: str) -> str` -- return f"phaze-agent-{agent_id}" (mirror agent_worker.py's convention exactly).
    - `async def add_agent(session: AsyncSession, agent_id: str, name: str, scan_roots: list[str]) -> str` -- generate token = "phaze_agent_" + secrets.token_urlsafe(32) (use `secrets`, NOT `random` -- S-rules enforce this); compute token_hash via the EXISTING `from phaze.routers.agent_auth import hash_token`; construct Agent(id=agent_id, name=name, token_hash=token_hash, scan_roots=scan_roots); session.add(...); await session.commit(); return the cleartext token. Do NOT catch IntegrityError here -- let it propagate so callers (and tests) can handle it.
    - `def main(argv: list[str] | None = None) -> int` -- build argparse with subparsers so future `agents` subcommands (list/revoke) can be added; only implement `agents add` now (`--id` required; `--name` optional defaulting to the titleized id; `--scan-roots` required, comma-separated, split into a list[str]). Order of operations: parse args, then validate_agent_id + validate_scan_roots and on ValueError print the message to stderr and return 1 (a non-zero exit BEFORE any DB access); otherwise run `asyncio.run(_run_add(...))` where `_run_add` opens `async with async_session() as session:` (import `from phaze.database import async_session`) and calls add_agent. On success print, exactly once to stdout: the cleartext token, a "save this now -- it is not recoverable" notice, and the derived queue name `phaze-agent-<id>` with a hint to set PHAZE_AGENT_QUEUE to it; return 0. On sqlalchemy.exc.IntegrityError print a friendly "agent id already exists" message to stderr and return 1. The token is the ONLY secret output and MUST NEVER be passed to a logger -- print() only (T201 is already ignored for `src/phaze/cli/**` in ruff per-file-ignores; no ruff config change needed). End the module with a `if __name__ == "__main__": raise SystemExit(main())` guard. Type hints on every function; double quotes.
    In pyproject.toml add a new `[project.scripts]` table placed AFTER the `[project]` table (after line 35, the closing of `dependencies`) and BEFORE `[tool.hatch.build.targets.wheel]` -- per CLAUDE.md section order [build-system] -> [project] -> [project.scripts] -> [tool.*]. Content: `phaze = "phaze.cli:main"`.
  </action>
  <verify>
    <automated>uv run ruff check src/phaze/cli/__init__.py && uv run ruff format --check src/phaze/cli/__init__.py && uv run mypy . && uv run python -c "import tomllib,pathlib; d=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); assert d['project']['scripts']['phaze']=='phaze.cli:main'"</automated>
  </verify>
  <done>CLI module type-checks under strict mypy, passes ruff (incl. S-rules with secrets), and `phaze = "phaze.cli:main"` is registered in [project.scripts] in the correct section position.</done>
</task>

<task type="auto">
  <name>Task 2: Tests for the CLI (>=85% coverage on the new module)</name>
  <files>tests/test_cli/__init__.py, tests/test_cli/test_agents_add.py</files>
  <action>
    Create `tests/test_cli/__init__.py` (empty) and `tests/test_cli/test_agents_add.py`. Reuse the existing `session` / `async_engine` fixtures from tests/conftest.py -- do NOT spin up a new DB harness. Cover:
    - Happy path: `token = await add_agent(session, "x-y", "X", ["/data/music"])`; assert token.startswith("phaze_agent_"); SELECT the row back and assert `row.token_hash == hash_token(token)` (re-hash the returned token via `from phaze.routers.agent_auth import hash_token`), `row.name == "X"`, `row.scan_roots == ["/data/music"]`; assert `derive_queue_name("x-y") == "phaze-agent-x-y"`.
    - id charset validation: parametrize over ["Foo_Bar", "-x", "x-", "", "a b"] and assert `validate_agent_id(bad)` raises ValueError; assert validate_agent_id("x-y") / "a1" / "fileserver-east" return None. (Pure-function test proves rejection happens with no DB write.)
    - scan-roots validation: `validate_scan_roots(["data/music"])` raises ValueError; `validate_scan_roots(["/data/music","/data/concerts"])` is fine.
    - Duplicate id: insert an Agent("dup", ...) via the session (or call add_agent once), then assert a second `await add_agent(session, "dup", ...)` raises `sqlalchemy.exc.IntegrityError` (rollback the session after to keep the fixture clean).
    - main() exit codes (drives the print/exit branches for coverage): for an invalid id, `assert main(["agents","add","--id","Bad_Id","--scan-roots","/data/music"]) == 1` and assert captured stderr is non-empty (use capsys). For the success branch, monkeypatch `phaze.cli.async_session` with `async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)` bound to the test `async_engine` fixture, call `main(["agents","add","--id","cli-ok","--name","CLI OK","--scan-roots","/data/music"])`, assert it returns 0, and assert capsys stdout contains both "phaze_agent_" and "phaze-agent-cli-ok".
    Tests live under tests/ (excluded from mypy/T201 per existing config) so no source-side concessions are needed.
  </action>
  <verify>
    <automated>uv run pytest tests/test_cli/ -x && uv run pytest --cov=phaze.cli --cov-report=term-missing tests/test_cli/ | grep -E "phaze/cli"</automated>
  </verify>
  <done>All CLI tests pass; coverage report shows src/phaze/cli at >=85%; happy path, id/scan-root validation, duplicate-id, and main() exit-code branches are all exercised.</done>
</task>

<task type="auto">
  <name>Task 3: Update deployment + configuration docs (CLI path + queue convention)</name>
  <files>docs/deployment.md, docs/configuration.md</files>
  <action>
    Keep the line-1 `<!-- generated-by: gsd-doc-writer -->` marker intact on BOTH files (do not move or duplicate it).
    docs/deployment.md Step 3 (around lines 129-153): make `phaze agents add` the PRIMARY registration path. Replace the lead-in so the operator runs:
    `phaze agents add --id fileserver-east --name "File Server East" --scan-roots /data/music,/data/concerts`
    State that it prints the cleartext token EXACTLY ONCE ("save it now -- not recoverable") and prints the derived queue name `phaze-agent-fileserver-east` to put in PHAZE_AGENT_QUEUE. Demote the existing raw SQL INSERT to a collapsible/"what it does under the hood" fallback note (keep the `python -c "import secrets; ..."` snippet only as part of that fallback). Do not change the token format or hashing description.
    docs/deployment.md Step 4 (env table around line 171): annotate `PHAZE_AGENT_QUEUE=phaze-agent-fileserver-east` to state the convention -- it MUST equal `phaze-agent-<agent_id>` (the value `phaze agents add` printed in Step 3).
    docs/configuration.md: add `PHAZE_AGENT_QUEUE` documentation (it is currently undocumented). Best placement: the "Agent role settings (`PHAZE_ROLE=agent`)" required-fields table (around line 110-116) -- add a row stating it is required for the agent role and MUST equal `phaze-agent-<PHAZE_AGENT_ID>`. Explicitly note there is NO queue column on the `agents` table: both the control plane and the agent worker derive the queue name from agent_id, and `phaze.tasks.agent_worker` asserts `PHAZE_AGENT_QUEUE == f"phaze-agent-{agent_id}"` at startup and exits non-zero on mismatch (cite agent_worker.py). Reference `phaze agents add` as the source of the correct value.
  </action>
  <verify>
    <automated>grep -q "phaze agents add" docs/deployment.md && grep -qE "phaze-agent-<agent_id>|phaze-agent-\{agent_id\}|phaze-agent-<PHAZE_AGENT_ID>" docs/deployment.md && grep -q "PHAZE_AGENT_QUEUE" docs/configuration.md && head -1 docs/deployment.md | grep -q "gsd-doc-writer" && head -1 docs/configuration.md | grep -q "gsd-doc-writer"</automated>
  </verify>
  <done>deployment.md presents `phaze agents add` as the primary registration path with the SQL INSERT demoted to a fallback note; the queue convention (PHAZE_AGENT_QUEUE == phaze-agent-<agent_id>, no queue column, asserted by agent_worker.py) is documented in both deployment.md and configuration.md; line-1 gsd-doc-writer markers preserved on both files.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| operator CLI -> agents table | Operator-supplied --id/--scan-roots cross into a DB write and a minted bearer credential |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-quick-01 | Information disclosure | minted token in src/phaze/cli/__init__.py | mitigate | token printed to stdout exactly once with not-recoverable notice; NEVER passed to a logger; only sha256 hash persisted via existing hash_token |
| T-quick-02 | Spoofing | weak token randomness | mitigate | token = "phaze_agent_" + secrets.token_urlsafe(32) using `secrets` CSPRNG (S-rules forbid `random`); reuse existing wire format unchanged |
| T-quick-03 | Tampering | agents.id charset | mitigate | validate_agent_id enforces ^[a-z0-9]+(-[a-z0-9]+)*$ pre-DB, matching the id_charset CheckConstraint; invalid ids exit non-zero before any write |
</threat_model>

<verification>
- `uv run ruff check .` and `uv run ruff format --check .` pass
- `uv run mypy .` passes (new src module is strict-clean; tests excluded)
- `uv run pytest` passes; `uv run pytest --cov --cov-report=term-missing` keeps src/phaze/cli >=85% and overall >=85%
- `phaze agents add --id x-y --name "X" --scan-roots /data/music` inserts a row whose token_hash == hash_token(printed token), prints the token once + `phaze-agent-x-y`; invalid ids rejected pre-DB
- pre-commit (frozen-SHA hooks incl. bandit -x tests -s B608, ruff, local mypy) passes; never --no-verify
</verification>

<success_criteria>
- `phaze` console script registered via [project.scripts] in correct pyproject section order
- CLI mints token via secrets, hashes via existing hash_token, inserts Agent via async_session, prints token once + derived queue name
- Invalid id / non-absolute scan-root / duplicate id all exit non-zero with clear messages, no partial writes
- Tests prove happy path, validation rejection, duplicate handling, and main() exit codes; coverage >=85%
- No new third-party dependency added; id CheckConstraint, token format, and hashing unchanged
- Docs show the CLI as the registration path and document the PHAZE_AGENT_QUEUE = phaze-agent-<agent_id> convention in both files
</success_criteria>

<output>
Create `.planning/quick/260606-nha-add-a-phaze-agents-add-management-cli-ge/260606-nha-SUMMARY.md` when done
</output>
