# Phase 48: Compute-agent type - Pattern Map

**Mapped:** 2026-06-25
**Files analyzed:** 10 (4 source create/modify, 1 migration create, 1 template create, 1 template modify, ~4 test create/extend)
**Analogs found:** 10 / 10 (every touch-point has an exact in-repo analog — this is a labeling phase on an already-shipped subsystem)

> **Phase nature (from RESEARCH):** Phase 48 adds a `kind` discriminator on top of the *existing* file-server Agent machinery. The job-pull/result-PUT/heartbeat/liveness/queue mechanics need ZERO new code — the compute agent runs the same `agent_worker`, drains the same `phaze-agent-<id>` queue, PUTs to the same endpoint. Every change is additive labeling. **Do NOT fork the worker, queue router, or analysis endpoint.**

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `src/phaze/models/agent.py` (MODIFY) | model | CRUD | self — existing `id_charset` CheckConstraint pattern on same model | exact (in-file) |
| `alembic/versions/024_add_agents_kind.py` (NEW) | migration | transform/DDL | `alembic/versions/023_add_scheduling_ledger_job_policy.py` | exact (role + additive) |
| `src/phaze/cli/__init__.py` (MODIFY) | CLI / utility | CRUD (insert) | self — existing `agents add` / `add_agent` / `validate_scan_roots` | exact (in-file) |
| `src/phaze/config.py` :: `AgentSettings` (MODIFY) | config | request-response (validation) | self — existing `AgentSettings` field + `_enforce_required_agent_fields` validator | exact (in-file) |
| `src/phaze/templates/admin/partials/_kind_badge.html` (NEW) | component (template) | request-response | `src/phaze/templates/admin/partials/_status_pill.html` | exact (geometry + if/elif) |
| `src/phaze/templates/admin/partials/agents_table.html` (MODIFY) | component (template) | request-response | self — existing `{% include _status_pill %}` + `<th>` pattern | exact (in-file) |
| `src/phaze/routers/admin_agents.py` | route | request-response | **NO CHANGE** — `_load_agents` already `SELECT Agent`; `kind` rides free | n/a |
| `tests/test_models/test_agent.py` (EXTEND) | test | — | self — existing column/constraint assertions | exact |
| `tests/test_migrations/test_024.py` (NEW) | test | — | `tests/test_migrations/test_023.py` | exact |
| `tests/test_cli/test_agents_add.py` (EXTEND) | test | — | self — existing `add_agent` + `main()` tests | exact |
| `tests/test_config/test_agent_settings_kind.py` (NEW) | test | — | `tests/test_config/test_agent_settings_windows.py` | exact |
| `tests/test_routers/test_admin_agents.py` (EXTEND) | test | — | self — existing status-pill render assertions | exact |
| `tests/test_task_split.py` (REAFFIRM) | test | — | self — existing subprocess import-boundary test | exact (already covers) |

---

## Pattern Assignments

### `src/phaze/models/agent.py` (model, CRUD) — ADD `kind` column

**Analog:** self — the existing `scan_roots` server-default column + the `id_charset` CheckConstraint in `__table_args__`.

**Existing column-with-server-default pattern** (`src/phaze/models/agent.py:25-38`):
```python
scan_roots: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
last_status: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

__table_args__ = (
    CheckConstraint(
        "id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
        name="id_charset",
    ),
)
```

**To add (mirror the above + RESEARCH Pattern 1):**
```python
kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'fileserver'"))
# ...and append to __table_args__ tuple:
CheckConstraint("kind IN ('fileserver', 'compute')", name="kind_enum"),
```
- `String` and `text` are already imported (`agent.py:7`). No new imports.
- **Constraint naming:** SQLAlchemy auto-prefixes `name="kind_enum"` → DB constraint `ck_agents_kind_enum` (the existing `name="id_charset"` surfaces as `ck_agents_id_charset` — verified in `tests/test_models/test_agent.py:42`). The model test must assert `ck_agents_kind_enum`.
- Default `'fileserver'` so every existing/legacy row (incl. `LEGACY_AGENT_ID`) reads correctly without a data backfill step.

---

### `alembic/versions/024_add_agents_kind.py` (migration, additive DDL) — NEW

**Analog:** `alembic/versions/023_add_scheduling_ledger_job_policy.py` (the current head).

**Full analog file** (`alembic/versions/023_...py:26-49`):
```python
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "023"
down_revision: str | Sequence[str] | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("scheduling_ledger", sa.Column("timeout", sa.Integer(), nullable=True))
    op.add_column("scheduling_ledger", sa.Column("retries", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduling_ledger", "retries")
    op.drop_column("scheduling_ledger", "timeout")
```

**For 024 (copy structure, change revision chain + add CHECK):**
- `revision = "024"`, `down_revision = "023"` (current head is `023` — RESEARCH Pitfall 3: a wrong `down_revision` forks the tree; confirm `uv run alembic heads` shows a single head before/after).
- `upgrade()`: `op.add_column("agents", sa.Column("kind", sa.String(16), nullable=False, server_default="fileserver"))` (the `server_default` backfills existing rows automatically — RESEARCH Runtime State Inventory), then `op.create_check_constraint("kind_enum", "agents", "kind IN ('fileserver', 'compute')")`.
- `downgrade()`: drop the constraint then `op.drop_column("agents", "kind")` (mirror order — drop dependents first).
- Bare-number revision string convention is LOCKED (test asserts it — see test pattern below).

---

### `src/phaze/cli/__init__.py` (CLI, insert) — ADD `--kind`, relax `--scan-roots` for compute

**Analog:** self. Three edit sites.

**1. Argparse flag** (`cli/__init__.py:106-114`) — currently `--scan-roots` is unconditionally required:
```python
add = agents_sub.add_parser("add", help="Register an agent and mint a bearer token.")
add.add_argument("--id", dest="agent_id", required=True, help="Agent id (kebab-case: ^[a-z0-9]+(-[a-z0-9]+)*$).")
add.add_argument("--name", dest="name", default=None, help="Human-readable name (defaults to the titleized id).")
add.add_argument(
    "--scan-roots",
    dest="scan_roots",
    required=True,
    help="Comma-separated absolute paths the agent may read/write (e.g. /data/music,/data/concerts).",
)
```
**Change:** add `add.add_argument("--kind", choices=("fileserver", "compute"), default="fileserver", ...)` (mirror the `choices=` enum-at-CLI defense layer); make `--scan-roots` `required=False, default=""` and enforce the non-empty/absolute rule only when `kind == "fileserver"`.

**2. `validate_scan_roots`** (`cli/__init__.py:62-67`) — rejects empty entries. For compute, `scan_roots == []` must pass. Gate the call in `main()` on kind (compute → skip / accept empty), keeping `validate_scan_roots` itself unchanged for fileserver.

**3. `add_agent` signature + insert** (`cli/__init__.py:75-89`):
```python
async def add_agent(session: AsyncSession, agent_id: str, name: str, scan_roots: list[str]) -> str:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    agent = Agent(id=agent_id, name=name, token_hash=hash_token(token), scan_roots=scan_roots)
    session.add(agent)
    await session.commit()
    return token
```
**Change:** add `kind: str = "fileserver"` param, pass `kind=kind` to the `Agent(...)` constructor, thread it through `_run_add` (`:92-95`) and `main()` arg parsing (`:127-140`). **Preserve the token D-13 invariant:** the minted token stays `print()`-only and is NEVER logged (`main()` docstring + `:149-156`).

---

### `src/phaze/config.py` :: `AgentSettings` (config, validation) — ADD `kind`, relax scan-roots gate

**Analog:** self. The existing alias-bound field pattern + the `_enforce_required_agent_fields` validator.

**Existing field pattern** (`config.py:390-394`, the `agent_env` Literal field is the closest shape — an enum-ish config knob with an alias):
```python
agent_env: Literal["dev", "production"] = Field(
    default="dev",
    validation_alias=AliasChoices("PHAZE_AGENT_ENV", "agent_env"),
    description="Deployment mode. Production refuses passwordless Redis URLs (Phase 29 D-06).",
)
```
**To add (mirror it):**
```python
kind: Literal["fileserver", "compute"] = Field(
    default="fileserver",
    validation_alias=AliasChoices("PHAZE_AGENT_KIND", "kind"),
    description="Agent kind. 'compute' (cloud) agents own no scan roots; relaxes the empty-scan-roots gate (Phase 48).",
)
```
(Using `Literal[...]` matches `agent_env` and gives a config-layer enum check — the third defense layer alongside the CLI `choices=` and DB CHECK.)

**The validator gate to relax** (`config.py:506-514`):
```python
@model_validator(mode="after")
def _enforce_required_agent_fields(self) -> "AgentSettings":
    if not self.agent_api_url:
        raise ValueError("PHAZE_AGENT_API_URL is required when PHAZE_ROLE=agent")
    if not self.agent_token.get_secret_value():
        raise ValueError("PHAZE_AGENT_TOKEN is required when PHAZE_ROLE=agent")
    if not self.scan_roots:
        raise ValueError("AgentSettings.scan_roots is required when PHAZE_ROLE=agent (set PHAZE_AGENT_SCAN_ROOTS=/path1,/path2)")
    return self
```
**Change:** gate only the `scan_roots` check on kind — `if self.kind != "compute" and not self.scan_roots: raise ValueError(...)`. `agent_api_url` + `agent_token` stay required for all kinds (compute still PUTs over HTTP with a bearer). RESEARCH Pitfall 1: without this, a compute worker crash-loops at startup.

---

### `src/phaze/templates/admin/partials/_kind_badge.html` (component) — NEW

**Analog:** `src/phaze/templates/admin/partials/_status_pill.html` (full file, 19 lines).

**Analog structure (geometry + if/elif + aria-label is LOCKED, copy verbatim except hue/label):**
```jinja
{% if agent._status == 'alive' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400" aria-label="Status: alive">ALIVE</span>
{% elif agent._status == 'revoked' %}
<span class="text-xs font-semibold px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300" aria-label="Status: revoked">REVOKED</span>
{% endif %}
```

**To build (per 48-UI-SPEC §Component Contract — palette + labels LOCKED):**
- Geometry literal: `text-xs font-semibold px-2 py-0.5 rounded-full` (copy exactly — do NOT change `py-0.5` to `py-1`; UI-SPEC §Spacing locks this).
- `agent.kind == 'compute'` → label `COMPUTE`, classes `bg-indigo-100 dark:bg-indigo-950 text-indigo-700 dark:text-indigo-400`, `aria-label="Kind: compute"`.
- `agent.kind == 'fileserver'` (and the `{% else %}` fallback) → label `FILE SERVER`, classes `bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300`, `aria-label="Kind: file server"`.
- Use `if / elif compute / else` so a value outside the enum falls back to the neutral fileserver branch (cell is never blank — mirrors the `_status_pill.html` defensive if/elif).
- Loop var is `agent` (set by `{% for agent in agents %}` in `agents_table.html:45`).

---

### `src/phaze/templates/admin/partials/agents_table.html` (component) — ADD Kind column

**Analog:** self — the existing `<th>` header row and the `{% include "admin/partials/_status_pill.html" %}` cell.

**Existing header row** (`agents_table.html:35-42`):
```jinja
<tr>
    <th scope="col" class="px-4 py-3">Agent</th>
    <th scope="col" class="px-4 py-3">Status</th>
    <th scope="col" class="px-4 py-3">Queue</th>
    <th scope="col" class="px-4 py-3">Last seen</th>
    <th scope="col" class="px-4 py-3">Scan roots</th>
    <th scope="col" class="px-4 py-3">Actions</th>
</tr>
```
**Existing Agent cell + status-pill include** (`agents_table.html:47-51`):
```jinja
<td class="px-4 py-3">
    <span class="text-sm font-semibold text-gray-900 dark:text-gray-100 block" title="{{ agent.id }}">{{ agent.name }}</span>
    <span class="font-mono text-xs text-gray-500 dark:text-gray-400 block">{{ agent.id }}</span>
</td>
<td class="px-4 py-3">{% include "admin/partials/_status_pill.html" %}</td>
```
**Change (per UI-SPEC §Placement — Kind goes AFTER Agent, BEFORE Status):**
- Header: insert `<th scope="col" class="px-4 py-3">Kind</th>` between the Agent `<th>` (`:36`) and the Status `<th>` (`:37`).
- Body: insert `<td class="px-4 py-3">{% include "admin/partials/_kind_badge.html" %}</td>` after the Agent `<td>` (`:50`), before the Status `<td>` (`:51`).
- **One edit site covers both render paths** — `admin_agents.page` and `table_partial` both render this same `agents_table.html` partial (RESEARCH Pitfall 5: avoids first-load-vs-poll flicker). No router change.

---

### `src/phaze/routers/admin_agents.py` — NO CHANGE

`_load_agents` does a bare `select(Agent)` (`admin_agents.py:67`) with no column projection, so `kind` loads automatically once the model column exists. UI-SPEC §Implementation Pointers confirms: "No change."

---

## Test Pattern Assignments

### `tests/test_models/test_agent.py` (EXTEND)
**Analog:** self. Add to the column-set assertion (`:16-28`, add `"kind"` to `required`) and add a constraint assertion mirroring `test_id_charset_constraint_declared` (`:40-42`):
```python
def test_kind_charset_constraint_declared(self) -> None:
    constraint_names = {c.name for c in Agent.__table__.constraints}
    assert "ck_agents_kind_enum" in constraint_names
```
Also assert `Agent.__table__.c.kind.nullable is False` and `String(16)` (mirror `test_name_required` / `test_token_hash_max_length` at `:44-49`). Note the auto-prefix: declared `name="kind_enum"` → `ck_agents_kind_enum`.

### `tests/test_migrations/test_024.py` (NEW)
**Analog:** `tests/test_migrations/test_023.py` (copy whole file structure).
- `_load_migration_024()` via `importlib.util.spec_from_file_location` (`test_023.py:35-42`) because the filename starts with a digit.
- `test_revision_identifiers_are_bare_numbers`: assert `revision == "024"`, `down_revision == "023"`, `branch_labels is None` (`test_023.py:51-56`).
- `@pytest.mark.asyncio` round-trip (`test_023.py:66-108`): `downgrade_to(base)` → `upgrade_to("023")` → assert `kind` column absent → `upgrade_to("024")` → assert column present, `is_nullable='NO'`, `data_type='character varying'`, default `'fileserver'` → assert an existing row backfilled to `'fileserver'` → assert CHECK rejects a bad `kind` (insert `kind='bogus'` raises) → `downgrade_to("023")` drops it. Reuse the `conftest` helpers `MIGRATIONS_TEST_DATABASE_URL`, `_build_alembic_config`, `downgrade_to`, `upgrade_to` (`test_023.py:24-29`). Same `_COLUMNS_SQL` information_schema query shape (`test_023.py:45-48`), targeting `table_name='agents'`.

### `tests/test_cli/test_agents_add.py` (EXTEND)
**Analog:** self.
- DB-backed: mirror `test_add_agent_happy_path` (`:56-64`) — call `add_agent(session, "oci-a1", "OCI A1", [], kind="compute")`, assert `row.kind == "compute"` and `row.scan_roots == []`.
- Assert `add_agent(..., kind="fileserver")` (default) still requires scan roots at the `main()` layer.
- `main()` path: mirror `test_main_success_inserts_and_prints` (`:93-110`) with `--kind compute` and NO `--scan-roots` → `rc == 0`, token printed. Add a negative: `--kind fileserver` without `--scan-roots` → `rc == 1` (reuse the NullPool/`monkeypatch.setattr(cli, "async_session", ...)` harness at `:100-102`).

### `tests/test_config/test_agent_settings_kind.py` (NEW)
**Analog:** `tests/test_config/test_agent_settings_windows.py`.
- Copy the `_make_settings(**overrides)` helper (`:28-37`) that supplies valid `agent_api_url`/`agent_token`/`scan_roots`.
- `test_kind_defaults_fileserver`: `_make_settings().kind == "fileserver"`.
- `test_compute_accepts_empty_scan_roots`: `AgentSettings(agent_api_url=..., agent_token=..., kind="compute", scan_roots=[])` does NOT raise.
- `test_fileserver_still_requires_scan_roots`: same but `kind="fileserver", scan_roots=[]` → `pytest.raises(ValueError, match="scan_roots is required")`.
- `test_kind_env_alias`: monkeypatch `PHAZE_AGENT_KIND=compute` binds (mirror the windows test's alias check). No DB/Redis needed.

### `tests/test_routers/test_admin_agents.py` (EXTEND)
**Analog:** self. Mirror `test_status_pills_render_all_5_states` (`:150-171`) — seed agents with `kind="compute"` and `kind="fileserver"` (the `smoke` fixture's `session.add_all` at `:50-74` shows the `Agent(id=..., name=..., scan_roots=...)` seed shape; add `kind=`), then GET both `/admin/agents` (full page) and `/admin/agents/_table` (partial) and assert `"COMPUTE"` + `bg-indigo-100 dark:bg-indigo-950` + `aria-label="Kind: compute"` and `"FILE SERVER"` + `bg-slate-100` + `aria-label="Kind: file server"` appear in both render paths.

### `tests/test_task_split.py` (REAFFIRM / extend comment)
**Analog:** self — `test_agent_worker_does_not_import_phaze_database` (`:33-91`) already runs the exact module the compute agent runs (`phaze.tasks.agent_worker`) in a subprocess and asserts `forbidden = ("phaze.database", "phaze.tasks.session", "sqlalchemy.ext.asyncio")` stay out of `sys.modules` while `saq.queue.postgres` is present. **This already covers CLOUDAGENT-02's ORM import boundary for the compute agent** — only extend the docstring to name the compute-agent invariant (RESEARCH Pitfall 4: do NOT duplicate this test or attempt to ban `essentia`/file reads — the "no media" half is runtime-enforced via empty scan roots + no mount, a Phase 51 compose concern).

---

## Shared Patterns

### Defense-in-depth `kind` enum at three layers
**Apply to:** model, migration, CLI, config.
The `kind` value is constrained identically at every layer so a bad value is rejected everywhere (RESEARCH Security V5 / STRIDE Tampering):
- **DB:** `CheckConstraint("kind IN ('fileserver', 'compute')", name="kind_enum")` (`models/agent.py` `__table_args__`, mirroring `id_charset` at `:34-37`).
- **CLI:** `add.add_argument("--kind", choices=("fileserver", "compute"), default="fileserver")` (argparse rejects pre-DB, like `validate_agent_id` rejecting before any session opens).
- **Config:** `kind: Literal["fileserver", "compute"]` (`config.py` `AgentSettings`, mirroring the `agent_env: Literal["dev", "production"]` field at `:390`).

### Server-default backfill (no separate data migration)
**Source:** `models/agent.py:28` (`scan_roots ... server_default=text("'[]'::jsonb")`) + `alembic/versions/023` additive `op.add_column`.
**Apply to:** model column + migration 024. `NOT NULL DEFAULT 'fileserver'` backfills every existing row (incl. `LEGACY_AGENT_ID`) at `ALTER TABLE` time — the only stale runtime state in the phase, handled automatically (RESEARCH Runtime State Inventory).

### Transient ORM attribute (already used for status; kind needs none)
**Source:** `routers/admin_agents.py:70-76` — `a._status = classify(a, now)`.
**Note:** unlike `_status`, `kind` is a real Mapped column, so it needs NO transient injection — it loads with the bare `select(Agent)`. The badge template reads `agent.kind` directly. (Documented here so the planner does not add an unnecessary `_kind` injection.)

### LOCKED pill geometry (visual consistency)
**Source:** `_status_pill.html` + `scan_status_pill.html` — `text-xs font-semibold px-2 py-0.5 rounded-full`.
**Apply to:** `_kind_badge.html`. Copy verbatim; `py-0.5` (2px) is intentional and LOCKED (UI-SPEC §Spacing) — matching it keeps the kind badge the same height as the adjacent status pill.

### Token secrecy invariant (D-13)
**Source:** `cli/__init__.py:16-17,122-123,149-156` — minted token is `print()`-only, never logged.
**Apply to:** the `--kind` CLI changes. Adding `kind` must not alter the token-handling path; `configure_logging()` runs first and the token stays out of any logger.

---

## No Analog Found

None. Every file in this phase has an exact in-repo analog (often the same file). This is the expected shape for an additive labeling phase on an already-shipped subsystem (v4.0 Distributed Agents, phases 24-29 + 46).

---

## Metadata

**Analog search scope:** `src/phaze/models/`, `src/phaze/cli/`, `src/phaze/config.py`, `src/phaze/routers/`, `src/phaze/templates/admin/partials/`, `alembic/versions/`, `tests/test_models/`, `tests/test_cli/`, `tests/test_migrations/`, `tests/test_config/`, `tests/test_routers/`, `tests/test_task_split.py`
**Files read (full):** `models/agent.py`, `cli/__init__.py`, `_status_pill.html`, `agents_table.html`, `alembic/versions/023_...py`, `tests/test_task_split.py`, `routers/admin_agents.py`, `tests/test_migrations/test_023.py`, `tests/test_cli/test_agents_add.py`, `tests/test_models/test_agent.py`, `tests/test_routers/test_admin_agents.py`
**Files read (partial):** `config.py:355-528` (AgentSettings), `tests/test_config/test_agent_settings_windows.py:1-55`
**Pattern extraction date:** 2026-06-25
