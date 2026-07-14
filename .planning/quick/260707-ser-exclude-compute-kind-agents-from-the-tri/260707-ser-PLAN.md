---
phase: quick-260707-ser
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/routers/pipeline.py
  - src/phaze/routers/shell.py
  - tests/shared/routers/test_pipeline.py
autonomous: true
requirements: [SER-01]
must_haves:
  truths:
    - "The Trigger Scan agent-picker dropdown lists ONLY kind='fileserver' agents"
    - "A kind='compute' agent (e.g. k8s-vox) never appears as a scan target option"
    - "build_dashboard_context returns only fileserver agents in context['agents']"
    - "The shell Analyze empty-state and Discover workspace return only fileserver agents"
  artifacts:
    - path: "src/phaze/routers/pipeline.py"
      provides: "build_dashboard_context agents query filtered to kind='fileserver'"
      contains: "Agent.kind == \"fileserver\""
    - path: "src/phaze/routers/shell.py"
      provides: "Analyze empty-state + Discover agent queries filtered to kind='fileserver'"
      contains: "Agent.kind == \"fileserver\""
  key_links:
    - from: "src/phaze/routers/pipeline.py"
      to: "trigger_scan_card.html"
      via: "context['agents'] -> {% for agent in agents %}"
      pattern: "Agent.kind == .fileserver."
status: complete
---

<objective>
Exclude `kind="compute"` agents (Kueue/burst backends like `k8s-vox`, `k8s-xenolab`)
from the operator "Trigger Scan" agent-picker dropdown. Compute agents are media-less and
cannot be scan targets, so listing them is a bug. All three queries that build the
`agents` list consumed by `trigger_scan_card.html` currently filter only on
`revoked_at.is_(None)` with no kind filter.

Purpose: The scan-picker must offer only file-server agents that actually host media.
Output: Three query changes (add `Agent.kind == "fileserver"`) plus a regression test.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

<interfaces>
Agent model (src/phaze/models/agent.py):
- `kind: Mapped[str]` — CheckConstraint kind_enum: values `'fileserver'` | `'compute'`, server_default `'fileserver'`.
- `revoked_at: Mapped[datetime | None]`.

Current query (identical in all THREE locations):
`select(Agent).where(Agent.revoked_at.is_(None)).order_by(Agent.name)`

Target query (all THREE):
`select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)`

Template (src/phaze/templates/pipeline/partials/trigger_scan_card.html:33-34):
`{% for agent in agents %}<option value="{{ agent.id }}">{{ agent.name }} ({{ agent.id }})</option>`

Test helper (tests/_queue_fakes.py:366):
`async def seed_active_agent(session, agent_id="nox", *, kind="fileserver") -> Agent`
— pass `kind="compute"` to seed a compute agent.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add kind='fileserver' filter to all three scan-picker agent queries</name>
  <files>src/phaze/routers/pipeline.py, src/phaze/routers/shell.py</files>
  <action>
  Add `Agent.kind == "fileserver"` to the WHERE clause of the three `agents_stmt` queries
  that feed the Trigger Scan dropdown. Keep the existing `Agent.revoked_at.is_(None)` filter
  and the `.order_by(Agent.name)` in each. Match local style (comma-separated args inside a
  single `.where(...)` is fine).

  1. src/phaze/routers/pipeline.py ~line 482 (in `build_dashboard_context`):
     change to `select(Agent).where(Agent.revoked_at.is_(None), Agent.kind == "fileserver").order_by(Agent.name)`.
     Update the adjacent comment to note compute agents are excluded (media-less, not scan targets).
  2. src/phaze/routers/shell.py ~line 184 (Analyze empty-state branch): same change.
  3. src/phaze/routers/shell.py ~line 193 (Discover workspace branch): same change.

  Do NOT touch src/phaze/routers/admin_agents.py `_load_agents` (its liveness table
  intentionally shows all kinds). Do NOT modify the template, the Agent model, or the
  compute-agent registration path.
  </action>
  <verify>
    <automated>grep -rn 'Agent.kind == "fileserver"' src/phaze/routers/pipeline.py src/phaze/routers/shell.py | grep -vc '^#' | grep -qx 3 && echo OK</automated>
  </verify>
  <done>All three queries include `Agent.kind == "fileserver"`; admin_agents.py unchanged; ruff/mypy clean.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Regression test — compute agent absent, fileserver agent present in dropdown context</name>
  <files>tests/shared/routers/test_pipeline.py</files>
  <behavior>
    - Seed one `kind="fileserver"` agent and one `kind="compute"` agent (use `seed_active_agent(session, agent_id="k8s-vox", kind="compute")`).
    - `build_dashboard_context(app_state, session)` returns `ctx["agents"]` containing the fileserver agent id but NOT the compute agent id.
    - Assert on agent ids in the returned list (not just count) so the compute-exclusion is provable.
  </behavior>
  <action>
  Add an async test in tests/shared/routers/test_pipeline.py near the existing
  `test_dashboard_context_binds_lanes`. Reuse the `client`/`session` fixtures and the
  `seed_active_agent` helper from tests/_queue_fakes.py (already imported in this module —
  reuse the existing import). Seed a fileserver agent and a compute agent, call
  `build_dashboard_context`, and assert the compute agent id is absent while the fileserver
  agent id is present in `ctx["agents"]`.

  If cheap, also add a shell-route assertion: GET the Discover workspace (or the Analyze
  empty-state) and assert the rendered `<option>` for the compute agent id is absent while the
  fileserver option is present (option form `<option value="{id}">{name} ({id})</option>`).
  One solid dashboard-context test is the required minimum; add the shell coverage only if it
  fits the existing harness without new fixtures.
  </action>
  <verify>
    <automated>just test-db && PHAZE_TEST_DB=1 uv run pytest tests/shared/routers/test_pipeline.py -k "compute or fileserver or dropdown" -x -q</automated>
  </verify>
  <done>New test seeds both kinds and proves compute agent is excluded from the scan-picker context; passes against the ephemeral test DB (Postgres 5433).</done>
</task>

</tasks>

<verification>
- `uv run ruff check src/phaze/routers/pipeline.py src/phaze/routers/shell.py tests/shared/routers/test_pipeline.py` clean.
- `uv run mypy src/phaze/routers/pipeline.py src/phaze/routers/shell.py` clean.
- New regression test passes; existing pipeline/shell tests unaffected.
- admin_agents.py `_load_agents` untouched (still shows all kinds).
</verification>

<success_criteria>
- All three scan-picker queries filter `Agent.kind == "fileserver"`.
- A `kind="compute"` agent is provably absent from the Trigger Scan dropdown context/options.
- A `kind="fileserver"` agent is still present.
- No changes to the template, Agent model, admin liveness table, or compute registration.
</success_criteria>

<output>
Create `.planning/quick/260707-ser-exclude-compute-kind-agents-from-the-tri/260707-ser-SUMMARY.md` when done
</output>
