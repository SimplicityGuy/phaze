# ruff: noqa
"""Hand-audited vulture false-positive whitelist for the `src/phaze` dead-code sweep (CLEAN-02, D-12/D-13).

This file is consumed by `just vulture` (see the justfile `vulture` recipe):

    uv run vulture src/phaze vulture_whitelist.py \\
        --min-confidence 80 \\
        --ignore-decorators "@router.*,@app.*,@field_validator,@model_validator,@validator,@pytest.fixture"

Every reference below marks a symbol as "used" so vulture does not report it.

## Scope: `--min-confidence 80` is what decides what belongs here

This is the rule that governs the file's size, and getting it wrong is how the previous revision
rotted. vulture assigns a fixed confidence per finding KIND, not per symbol:

  * unused variable (a plain local assignment / unused parameter) -- 100%
  * unused import ---------------------------------------------- 90%
  * unused function / method / class / property / attribute ----- 60%

The recipe pins `--min-confidence 80`, so the ENTIRE 60% tier is already filtered out before the
whitelist is consulted. Measured on `src/phaze` at ebae0f45 (2026-08-21): 134 findings at
confidence 0, of which 119 are 60% and never reach the sweep. Only the 12 unused-import and
3 unused-variable findings below are live.

That is a real difference from how the file was originally seeded. It was built with
`vulture --make-whitelist` at the DEFAULT confidence, which emits the 60% tier, and then the
recipe added `--min-confidence 80` on top. The result was ~170 entries suppressing findings that
`--min-confidence 80` had already suppressed -- inert, unverifiable, and free to rot: an audit
found 51 of them naming symbols or whole modules that no longer existed (`routers/pipeline.py`
and `routers/tracklists.py` have since been split into packages), and 114 more whose `file:line`
comment had drifted off the symbol it claimed to point at. None of that was detectable from the
sweep, because none of those entries ever suppressed anything.

**Do not re-seed this file with `--make-whitelist` at the default confidence.** Generate at the
confidence the recipe actually uses:

    uv run vulture src/phaze --min-confidence 80 --make-whitelist

## The two live false-positive categories

  * **`CursorResult` (unused import, 90%)** -- imported solely to satisfy a STRING-form
    `cast("CursorResult[Any]", ...)`. `session.execute` is typed `Result[Any]`, but a DML statement
    (INSERT ... ON CONFLICT DO UPDATE, UPDATE, DELETE) always yields a `CursorResult` at runtime,
    which is what exposes `.rowcount`. vulture does not parse string annotations, so it sees the
    import and no bare reference. One entry suppresses the name across all 12 modules; the
    per-module comments below are an inventory, not 12 independent suppressions.

  * **Protocol method parameters (unused variable, 100%)** -- `RenderPage` in
    `services/tracklist_render.py` is a structural `Protocol` whose method bodies are `...`. The
    parameter NAMES are the contract (callers pass `wait_until=` / `selector=`), so they cannot be
    renamed or dropped, but no body consumes them. Only the names not already used elsewhere in
    `src/phaze` are flagged -- `timeout`, `state` and `url` happen to be live names elsewhere, which
    is why they are absent here rather than exempt.

## Rules for changing this file

  * If a NEW genuinely-dead symbol appears, **DELETE the symbol** (removal-only) rather than
    whitelisting it. Only add an entry here when a symbol is a proven framework/dynamic
    false-positive, with a justification.
  * Do NOT add the LIVE view-adjacent helpers `build_dashboard_context` / `get_stage_progress` /
    `get_queue_activity` -- vulture does not flag them (they have live callers) and they feed the
    Analyze workspace + `/pipeline/stats`.
  * If `--min-confidence` is ever lowered below 80, the 60% tier reappears -- FastAPI route
    handlers, Pydantic validators, SQLAlchemy ORM column attributes, pydantic-settings fields,
    watchdog `on_created`/`on_modified` callbacks, and transient runtime attributes. That is a
    deliberate re-audit, not a mechanical re-seed: those categories are why the sweep is
    non-blocking and hand-verified in the first place.
"""

# --- unused import (90%): string-form `cast("CursorResult[Any]", ...)` only ---
# Inventory verified against the sweep at ebae0f45 (2026-08-21); 12 modules, unchanged in count.
# src/phaze/routers/agent_analysis.py:48          src/phaze/services/scan_deletion.py:32
# src/phaze/routers/agent_push.py:66              src/phaze/services/stage_control.py:41
# src/phaze/services/agent_s3_reports.py:51       src/phaze/services/tracklist_lookup_cache.py:20
# src/phaze/services/backends/admission.py:25     src/phaze/tasks/ledger_reaper.py:92
# src/phaze/services/companion.py:7               src/phaze/tasks/submit_cloud_job.py:37
# src/phaze/services/filename_convention_learner.py:86   src/phaze/tasks/tracklist.py:66
CursorResult

# --- unused variable (100%): `RenderPage` Protocol parameters, bodies are `...` ---
wait_until  # src/phaze/services/tracklist_render.py:161 (goto), :163 (reload)
selector  # src/phaze/services/tracklist_render.py:165 (wait_for_selector)
