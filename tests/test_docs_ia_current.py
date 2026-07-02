"""CUT-03 docs-currency guard (Phase 62 -- Polish & cutover).

The v7.0 milestone replaced the MVP flat-tab admin UI with a DAG-centric three-column
"Hybrid Console" shell: the pipeline DAG rail is the navigation spine, clicking a stage
swaps the center workspace over HTMX via ``GET /s/<stage>``, global search collapsed into
a ``command palette`` (Cmd-K), agent/compute health moved to a header status strip, and
the per-file "full record" opens as a slide-in over the shell. CUT-03 refreshes the user-
facing docs so they describe that architecture instead of the retired tab sprawl.

This guard keeps those docs honest without a browser or a rendering step -- it is a pure
filesystem structural check mirroring the repo's established guard-test idiom
(``tests/test_dead_template_guard.py`` for repo-root path constants,
``tests/test_base_html_sri.py`` for ``read_text`` substring assertions). It touches no
``client`` / DB / session fixture, so ``conftest.py`` does NOT auto-mark it ``integration``
-- it runs in the fast lane (``uv run pytest -m "not integration"``).

One assertion function per docs-currency behavior:

* ``test_readme_describes_dag_centric_shell`` -- README carries the new-IA vocabulary
  (``command palette`` + ``DAG``).
* ``test_architecture_has_ui_ia_section`` -- ``docs/architecture.md`` gained a UI/IA
  section (``/s/`` stage routing + the ``record slide-in``).
* ``test_project_structure_maps_shell_templates`` -- ``docs/project-structure.md`` maps
  the shell template tree (``templates/shell`` + the ``/s/`` router relationship).
* ``test_quick_start_has_no_stale_legacy_nav`` -- ``docs/quick-start.md`` no longer tells
  the operator to visit the removed legacy full-page tabs.

The stale-nav check is deliberately targeted at the specific host-qualified legacy-page
visit URLs so it stays robust: it must NOT trip on the still-live ``POST /pipeline/*`` API
endpoints the walkthrough legitimately references (those carry no ``localhost:8000`` prefix
and no trailing bare ``/``).
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_README = _REPO_ROOT / "README.md"
_ARCHITECTURE = _REPO_ROOT / "docs" / "architecture.md"
_PROJECT_STRUCTURE = _REPO_ROOT / "docs" / "project-structure.md"
_QUICK_START = _REPO_ROOT / "docs" / "quick-start.md"

# Full-page, browser-visit URLs to the legacy tab pages the v7.0 shell superseded. Their
# router wrappers are deleted in CUT-02 and the routes now 302-redirect into the shell, so
# a "visit this page" step in the quick-start is stale navigation. These host-qualified
# forms are chosen precisely so the guard does NOT flag the still-live POST /pipeline/*
# API endpoints (e.g. ``POST /pipeline/extract-metadata``) the walkthrough keeps.
_STALE_NAV_URLS: tuple[str, ...] = (
    "localhost:8000/pipeline/",
    "localhost:8000/proposals/",
    "localhost:8000/duplicates/",
    "localhost:8000/tracklists/",
)

# Every doc the CUT-03 refresh owns. The negative anti-drift check below runs across all of
# them so a stale claim reintroduced in ANY of these files trips the guard.
_ALL_DOCS: tuple[Path, ...] = (_README, _ARCHITECTURE, _PROJECT_STRUCTURE, _QUICK_START)

# Substrings that describe UI surfaces CUT-02 (Phase 62) DELETED: the standalone pipeline
# dashboard page and its single ``dag_canvas.html`` SVG DAG canvas partial. CUT-03 (62-03,
# wave 1) added the correct new-IA sections but ran BEFORE CUT-02 (wave 2) removed those
# surfaces, so pre-existing prose kept describing a template + page that no longer render.
# These markers are matched case-insensitively and MUST NOT appear in any owned doc — the
# DAG is now the left rail + per-stage ``/s/<stage>`` workspaces, and ``/pipeline/`` is a pure
# 302 redirect into the shell. (The underlying stage-progress services -- get_stage_progress,
# get_queue_activity, build_dashboard_context -- still exist and feed the Analyze workspace +
# the /pipeline/stats poll; only the dashboard *page* + dag_canvas.html were removed, so this
# guard targets the deleted-surface vocabulary, NOT those living service names.)
_STALE_DELETED_SURFACE_MARKERS: tuple[str, ...] = (
    "dag_canvas.html",
    "svg dag canvas",
)


def test_docs_have_no_stale_deleted_dashboard_claims() -> None:
    """No owned doc still claims the deleted dashboard page / dag_canvas.html renders live."""
    offenders: dict[str, list[str]] = {}
    for doc in _ALL_DOCS:
        lowered = doc.read_text().lower()
        hits = [marker for marker in _STALE_DELETED_SURFACE_MARKERS if marker in lowered]
        if hits:
            offenders[doc.name] = hits
    assert not offenders, (
        "docs still describe the CUT-02-deleted pipeline dashboard / dag_canvas.html as a live "
        f"surface (describe the DAG rail + /s/<stage> workspaces instead): {offenders}"
    )


def test_readme_describes_dag_centric_shell() -> None:
    """README describes the DAG-centric console (command palette + the DAG spine)."""
    text = _README.read_text()
    assert "command palette" in text.lower(), "README must describe the Cmd-K command palette"
    assert "DAG" in text, "README must describe the DAG rail / DAG-centric shell"


def test_architecture_has_ui_ia_section() -> None:
    """docs/architecture.md carries a UI/IA section (/s/ stage routing + record slide-in)."""
    text = _ARCHITECTURE.read_text()
    assert "/s/" in text, "architecture.md must document the /s/<stage> HTMX stage routing"
    assert "record slide-in" in text, "architecture.md must document the per-file record slide-in"


def test_project_structure_maps_shell_templates() -> None:
    """docs/project-structure.md maps the shell template tree + /s/ router relationship."""
    text = _PROJECT_STRUCTURE.read_text()
    assert "templates/shell" in text, "project-structure.md must map the templates/shell tree"
    assert "/s/" in text, "project-structure.md must map the /s/<stage> router-to-workspace relationship"


def test_quick_start_has_no_stale_legacy_nav() -> None:
    """docs/quick-start.md no longer instructs visiting the removed legacy full-page tabs."""
    text = _QUICK_START.read_text()
    offenders = [url for url in _STALE_NAV_URLS if url in text]
    assert not offenders, f"quick-start.md still points at removed legacy pages: {offenders}"
