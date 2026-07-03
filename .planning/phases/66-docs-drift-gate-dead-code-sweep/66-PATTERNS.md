# Phase 66: Docs-Drift Gate & Dead-Code Sweep - Pattern Map

**Mapped:** 2026-07-03
**Files analyzed:** 7 (2 create, 5 modify)
**Analogs found:** 7 / 7

> Every deliverable in this phase EXTENDS an existing, proven repo mechanism rather than
> introducing new infrastructure. The analogs below are exact-idiom matches already in-repo.
> Project conventions apply to all new/edited Python: Python 3.14, `uv run` only, ruff 150-col,
> double quotes, `from __future__ import annotations`, mypy strict (but `tests/` is excluded).

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `tests/shared/core/test_requirements_traceability.py` (CREATE) | test (fs guard) | transform (parse-then-assert) | `tests/shared/core/test_docs_ia_current.py` | exact |
| `tests/shared/core/test_dead_template_guard.py` (MODIFY) | test (fs/AST guard) | transform | itself — `_entry_templates()` site | exact (in-file) |
| `src/phaze/routers/admin_agents.py` (MODIFY) | router | request-response | itself `::page` + `pipeline_scans.py` for `get_settings()` | exact |
| `src/phaze/templates/admin/agents.html` (MODIFY) | template | request-response | itself (`{% block content %}`) | exact (in-file) |
| `pyproject.toml` `[dependency-groups]` dev (MODIFY) | config | — | existing alpha-sorted dev list | exact |
| `vulture_whitelist.py` (CREATE, maybe) | config (tool ignore) | — | no precedent in repo (vulture-generated) | none |
| `justfile` + `.github/workflows/code-quality.yml` (MODIFY) | config (CI) | — | `justfile` test-group recipes + `code-quality.yml` steps | exact |

---

## Pattern Assignments

### `tests/shared/core/test_requirements_traceability.py` (CREATE — test, transform) — DOCS-01

**Analog:** `tests/shared/core/test_docs_ia_current.py` (closest — same directory, same "parse
`.planning`/docs markdown then assert with a precise offender message" shape, zero `phaze.*`
imports → hermetic, immune to the `get_settings` lru_cache / `saq_jobs` stub cross-test poison).
Secondary analog for the repo-root path constant + one-assertion-per-behavior: `test_dead_template_guard.py`.

**Repo-root pathing + module-level constants** (`test_docs_ia_current.py` lines 34-59) — copy exactly:
```python
from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]   # tests/shared/core/X.py -> repo root
_PLANNING = _REPO_ROOT / ".planning"
# named-doc constants as module globals, e.g. _README = _REPO_ROOT / "README.md"
```
Note: `parents[3]` is correct for `tests/shared/core/` (three levels up = repo root). The sibling
guard `tests/shared/test_ci_workflow_wiring.py` uses `parents[2]` because it is one level shallower —
do NOT copy that constant; match `test_docs_ia_current.py`'s `parents[3]`.

**One assertion function per behavior + precise offender message** (`test_docs_ia_current.py` lines 77-116):
```python
def test_docs_have_no_stale_deleted_dashboard_claims() -> None:
    """<behavior in one line>."""
    offenders: dict[str, list[str]] = {}
    for doc in _ALL_DOCS:
        ...
    assert not offenders, (
        "human-readable explanation of the drift + how to fix it: "
        f"{offenders}"
    )
```
Mirror this into the 5 drift-class assertions RESEARCH names (lines 190-196 of RESEARCH):
`test_active_passed_phases_have_all_requirements_marked`,
`test_active_marked_requirements_have_passed_phases`,
`test_active_checkbox_and_table_status_agree`,
`test_archived_milestones_internally_consistent`,
`test_inflight_phase_with_unmarked_requirements_passes` (D-05 regression — must be GREEN while Phase 66 is `[ ]`).

**Small pure-regex helpers (no markdown lib)** — the repo idiom is `re` over `read_text()`, kept
dependency-free. Parser regexes are specified verbatim in RESEARCH Deep-Dive 1 (lines 132-169):
```python
# requirement checkbox: ^- \[([ x])\] \*\*([A-Z]+-\d+)\*\*
# traceability row:      ^\|\s*([A-Z]+-\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|
# roadmap phase-pass:    ^- \[([ x])\] \*\*Phase (\d+(?:\.\d+)?)[:\s]
# verification status:   ^status:\s*passed   (fallback \*\*Status:\*\*\s*passed)
```
Keep helpers named `_parse_requirements`, `_parse_roadmap_phases`, `_verification_passed` so
assertions read declaratively (RESEARCH line 197).

**Active-vs-archived degradation** (D-04): active milestone (`.planning/REQUIREMENTS.md` +
`.planning/ROADMAP.md` + `.planning/phases/`) gets full ROADMAP-`[x]`+VERIFICATION-`passed` gating;
archived (`.planning/milestones/vN.M-*`) gets internal-consistency-only (checkbox↔table↔Complete/Deferred),
NEVER VERIFICATION-file gating. Status-vocab normalize `{complete,done}→COMPLETE`, `{deferred}→DEFERRED`.

---

### `tests/shared/core/test_dead_template_guard.py` (MODIFY — test, transform) — CLEAN-02 / D-14

**Analog:** itself. The change extends the existing `_entry_templates()` / `_HTML_LITERAL` site
(lines 47-69). All existing infrastructure is reused — no new constants needed
(`_TEMPLATES = _REPO_ROOT / "src" / "phaze" / "templates"` already exists at line 44).

**Existing entry-literal capture** (lines 47-69) — the D-14 assertion consumes `_entry_templates()` as-is:
```python
_HTML_LITERAL = re.compile(r"""["']([^"']+\.html)["']""")

def _entry_templates() -> set[str]:
    """Templates rendered directly by a router (any quoted "...html" literal)."""
    names: set[str] = set()
    for py in sorted(_ROUTERS.glob("*.py")):
        names |= set(_HTML_LITERAL.findall(py.read_text()))
    return names
```

**Existing assertion style to match** (lines 78-95) — add a NEW sibling assertion, do NOT relax
`test_no_orphan_templates`. RESEARCH gives the exact new function (lines 278-285):
```python
def test_entry_literals_resolve_to_templates() -> None:
    """Every router "...html" literal points at a real template (no dead entry-root literal)."""
    missing = sorted(lit for lit in _entry_templates() if not (_TEMPLATES / lit).is_file())
    assert not missing, (
        "router source references template literals that don't exist on disk "
        f"(dead entry-root literal — delete the literal or restore the template): {missing}"
    )
```
**De-risked:** all 67 current router `.html` literals already resolve → green today, no allowlist.
Guardrail (per the module docstring lines 30-32 + 55-61): if a legit non-template `.html` href literal
is ever added, introduce a tiny explicit `_NON_TEMPLATE_HTML: frozenset[str]` with inline justification
mirroring the `_ALLOWLIST` idiom (line 61) — do NOT weaken the assertion.

---

### `src/phaze/routers/admin_agents.py` (MODIFY — router, request-response) — CLEAN-01 / D-09

**Analog:** itself (`::page` handler, lines 80-111) for the context dict; `routers/pipeline_scans.py`
(line 39) for the `get_settings()` import idiom (prefer function call over module-level import so the
test `get_settings` lru_cache-clear fixture is respected).

**Import to add** (mirror `pipeline_scans.py` line 39):
```python
from phaze.config import get_settings
```

**Existing `::page` context dict** (lines 99-111) — add ONE key. The `/_table` partial handler
(lines 114-138) does NOT need it (the link lives in the full page shell, not the polled partial):
```python
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "request": request,
            "agents": agents,
            "now": now,
            "current_page": "admin_agents",
            "refreshed_at_iso": now.isoformat(),
            "compute_lane_state": compute_lane_state,
            "compute_lane_count": compute_lane_count,
            "enable_saq_ui": get_settings().enable_saq_ui,   # <-- CLEAN-01 add (presentation-only)
        },
    )
```
`settings.enable_saq_ui` lives on `BaseSettings` (`config.py:292`, default `True`, alias
`PHAZE_ENABLE_SAQ_UI`) — available on both Control and Agent settings; it gates the `/saq` mount in
`main.py:160`. This is a template-context addition ONLY, not a backend behavior change.

**Test analog:** a TestClient render test asserting the link appears when `enable_saq_ui=True` and is
absent when `False` (monkeypatch pattern from `tests/.../test_main_lifespan.py`). Belongs in the
`agents` bucket — it uses a TestClient/DB fixture so `conftest.py` auto-marks it `integration`.

---

### `src/phaze/templates/admin/agents.html` (MODIFY — template, request-response) — CLEAN-01 / D-10/D-11

**Analog:** itself. `{% block content %}` (lines 6-19) wraps the page in `<div class="space-y-6">…</div>`.
Existing muted-text utility styling is on line 9 (`text-sm text-gray-500 dark:text-gray-400`) — reuse the
same muted Tailwind palette for the discreet footer link.

**Edit:** after the main `<div class="space-y-6">…</div>` closes (line 19), before the `<script>`
(line 27), add the flag-gated footer link (RESEARCH lines 298-303):
```jinja
{% if enable_saq_ui %}
<p class="mt-6 text-xs text-gray-400 dark:text-gray-500">
  <a href="/saq" target="_blank" rel="noopener" class="hover:underline">SAQ monitor ↗</a>
</p>
{% endif %}
```
Low visual weight (D-10), new tab + `rel="noopener"` reverse-tabnabbing guard (D-11), gated so it never
renders a dead 404 when `/saq` is unmounted (D-09). Conditional-block form `{% if %}…{% endif %}` matches
the existing Jinja idiom (e.g. the `{% block %}` structure already in this file).

---

### `pyproject.toml` `[dependency-groups]` dev (MODIFY — config) — CLEAN-02 / D-13

**Analog:** the existing alpha-sorted dev list (lines 208-228). Insert `vulture` in alpha position —
it sorts LAST, after `"ruff>=0.15.18",` (line 227):
```toml
[dependency-groups]
dev = [
    "bandit>=1.9.4",
    ...
    "respx>=0.23.1",
    "ruff>=0.15.18",
    "vulture>=2.16",        # <-- CLEAN-02 add (dev-only, cooldown-safe: 2.16 released 2026-03-25)
]
```
Then `uv lock && uv sync` (via `just install`) to regenerate `uv.lock` + the dev venv. `>=2.16` clears
the `exclude-newer = "7 days"` cooldown. Per the supply-chain protocol, confirm the resolved source is
`github.com/jendrikseipp/vulture` before `uv sync` (planner may add a `checkpoint:human-verify` task).

---

### `vulture_whitelist.py` (CREATE, maybe — config, tool ignore) — CLEAN-02 / D-13

**Analog:** NONE in-repo (no existing `.vulture*` / whitelist / ignore file at root). This is a
vulture-generated artifact, not a copy-from-analog file. Generate + hand-audit:
```bash
uv run vulture src/phaze --make-whitelist > vulture_whitelist.py   # then remove genuinely-dead entries
uv run vulture src/phaze vulture_whitelist.py \
  --min-confidence 80 \
  --ignore-decorators "@router.*,@app.*,@field_validator,@model_validator,@validator,@pytest.fixture"
```
Known this-repo false-positive sources to whitelist (RESEARCH lines 255-263): FastAPI route handlers,
Pydantic validators, transient ORM attrs (`_status` in `admin_agents._load_agents`, Phase 27
`_agent_name`/`_elapsed_seconds`), SAQ hooks/lifespan callbacks, CLI entry points.
**Do NOT delete** `build_dashboard_context` / `get_stage_progress` / `get_queue_activity` — vulture may
flag them but they still feed the Analyze workspace + `/pipeline/stats` (RESEARCH lines 267, 322).
Guardrail (D-12): delete only after `grep -rn` for dynamic refs AND a green `just integration-test`.

---

### `justfile` + `.github/workflows/code-quality.yml` (MODIFY — config, CI) — DOCS-01 / D-07

**Analog (justfile):** the test-group recipes (lines 90-103) — `[doc('...')]` + `[group('test')]`
attributes then a `uv run pytest ...` body. Add a `docs-drift` recipe in the same group:
```make
[doc('Run the REQUIREMENTS/ROADMAP docs-drift traceability guard (DOCS-01)')]
[group('test')]
docs-drift:
    uv run pytest tests/shared/core/test_requirements_traceability.py -q
```

**Analog (workflow):** `code-quality.yml` step list (lines 48-52) — emoji-prefixed step names,
`run: just <recipe>` (workflows delegate to just, never inline shell). Add ONE step after the
pre-commit step:
```yaml
      - name: 🧭 Docs-drift traceability gate
        run: just docs-drift
```
**Why `code-quality.yml`:** its `code-quality` job has NO `if:` gate → it runs on EVERY PR including
doc-only, so the drift gate runs at the exact moment drift is introduced (D-07). It does NOT re-enable
the CI-04-skipped heavy `test`/`security`/`docker` jobs, so skip-with-success is preserved. `just install`
already runs in this job (line 49) so pytest is available. Do NOT add `.planning/**` to trigger paths
(RESEARCH lines 231 — rejected; breaks CI-04 skip-with-success).

**Optional wiring guard:** `tests/shared/test_ci_workflow_wiring.py` (parses `justfile` as text +
`.github/workflows/tests.yml` as YAML, `parents[2]` root) is the analog if the planner wants a structural
assertion that the `just docs-drift` step stays present in `code-quality.yml` — discretionary, not required.

---

## Shared Patterns

### Hermetic filesystem-guard idiom (DOCS-01 + D-14)
**Source:** `tests/shared/core/test_dead_template_guard.py`, `tests/shared/core/test_docs_ia_current.py`
**Apply to:** the new traceability guard.
```python
from __future__ import annotations
from pathlib import Path
import re

_REPO_ROOT = Path(__file__).resolve().parents[3]   # tests/shared/core/ -> root
# pure read_text() parse; small `re` helpers; one assertion function per behavior;
# every assert has a precise offender message; NO phaze.* import -> hermetic.
```
This zero-import property is what makes the guard immune to the `get_settings` lru_cache leak /
`saq_jobs` stub poison and lets it pass in isolation via `just test-bucket shared` (bucket placement:
`tests/shared/core/`, the `shared` bucket in `tests/buckets.json`).

### `get_settings()` at call-site, not module-level (CLEAN-01)
**Source:** `src/phaze/routers/pipeline_scans.py:39,146`
**Apply to:** `admin_agents.py` context injection.
`from phaze.config import get_settings` then `get_settings().enable_saq_ui` inside the handler —
respects the test lru_cache-clear fixture; avoids a stale module-level snapshot.

### Workflows delegate to `just` (DOCS-01 CI)
**Source:** `.github/workflows/code-quality.yml:48-52` (`run: just install` / `run: just pre-commit`)
**Apply to:** the new docs-drift CI step — `run: just docs-drift`, never inline shell. Emoji-prefixed
step name (project convention).

---

## No Analog Found

| File | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `vulture_whitelist.py` | config (tool ignore) | — | No existing tool-ignore/whitelist file at repo root; it is vulture-`--make-whitelist`-generated, not copied from an in-repo analog. Follow vulture's documented mechanism (RESEARCH lines 247-252). |

## Metadata

**Analog search scope:** `tests/shared/core/`, `tests/shared/`, `src/phaze/routers/`,
`src/phaze/templates/admin/`, `justfile`, `.github/workflows/`, `pyproject.toml`
**Files scanned:** 8 analog files read in full or targeted-range
**Pattern extraction date:** 2026-07-03
