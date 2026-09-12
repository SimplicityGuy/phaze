"""Present-tense integrity guards for maintained architecture and UI documentation.

These filesystem-only tests keep maintained docs aligned with the live package inventory,
migration head, import boundaries, ``process_file`` policy, and source-cited Mermaid diagrams.
They follow the prose-guard pattern in ``tests/shared/core/test_dead_template_guard.py`` and
``tests/shared/core/test_base_html_sri.py`` and remain in the fast, fixture-free lane.
"""

from __future__ import annotations

import ast
from functools import cache
from itertools import chain
from pathlib import Path
import re

from scripts.check_documentation_integrity import migration_heads


_REPO_ROOT = Path(__file__).resolve().parents[3]
_README = _REPO_ROOT / "README.md"
_ARCHITECTURE = _REPO_ROOT / "docs" / "architecture.md"
_DATABASE = _REPO_ROOT / "docs" / "database.md"
_API = _REPO_ROOT / "docs" / "api.md"
_PROJECT_STRUCTURE = _REPO_ROOT / "docs" / "project-structure.md"
_ESSENTIA_ANALYSIS = _REPO_ROOT / "docs" / "essentia-analysis.md"
_TRACKLIST_SCRAPING = _REPO_ROOT / "docs" / "tracklist-scraping.md"
_UI_DESIGN_REFERENCE = _REPO_ROOT / "docs" / "ui-design-reference.md"
_QUICK_START = _REPO_ROOT / "docs" / "quick-start.md"

_MAINTAINED_ARCHITECTURE_DOCS: tuple[Path, ...] = (
    _ARCHITECTURE,
    _DATABASE,
    _API,
    _PROJECT_STRUCTURE,
    _ESSENTIA_ANALYSIS,
    _TRACKLIST_SCRAPING,
    _UI_DESIGN_REFERENCE,
)
_PACKAGE_AREAS: tuple[str, ...] = ("agent_watcher", "cli", "enums", "models", "routers", "schemas", "services", "tasks", "telemetry", "utils", "web")
_BOX_DRAWING = frozenset("┌┐└┘├┤┬┴┼│─")
_MERMAID_BLOCK = re.compile(r"```mermaid\n(?P<body>.*?)\n```", re.DOTALL)
_MERMAID_EDGE = re.compile(r"(?m)^(?!\s*%%)(?P<edge>\s*[^\n]*--[^\n]*)$")
_EVIDENCED_MERMAID_EDGE = re.compile(r"(?m)^\s*%% evidence:\s*(?P<paths>[^\n]+)\n(?P<edge>\s*(?!%%)[^\n]*--[^\n]*)$")

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


@cache
def _read_text(path: Path) -> str:
    return path.read_text()


def _box_drawing_symbols(path: Path) -> list[str]:
    return sorted(set(_read_text(path)) & _BOX_DRAWING)


def _evidence_claims_for_block(doc: Path, body: str) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    matches = tuple(_EVIDENCED_MERMAID_EDGE.finditer(body))
    assert len(matches) == len(_MERMAID_EDGE.findall(body)), f"{doc.name} Mermaid edge lacks an immediately preceding evidence citation"
    return tuple((doc, tuple(path.strip() for path in match.group("paths").split(";"))) for match in matches)


def _mermaid_evidence_claims(doc: Path) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    bodies = tuple(match.group("body") for match in _MERMAID_BLOCK.finditer(_read_text(doc)))
    evidenced_bodies = (body for body in bodies if "%% evidence:" in body)
    return tuple(chain.from_iterable(_evidence_claims_for_block(doc, body) for body in evidenced_bodies))


def _missing_evidence_paths(claims: tuple[tuple[Path, tuple[str, ...]], ...]) -> tuple[str, ...]:
    paths = chain.from_iterable(paths for _, paths in claims)
    return tuple(path for path in paths if not (_REPO_ROOT / path).is_file())


def _is_model_import(node: ast.AST) -> bool:
    return isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("phaze.models")


def _imports_models(path: Path) -> bool:
    return any(map(_is_model_import, ast.walk(ast.parse(_read_text(path)))))


def _direct_model_importers() -> set[str]:
    router_root = _REPO_ROOT / "src" / "phaze" / "routers"
    return {router.relative_to(_REPO_ROOT).as_posix() for router in router_root.rglob("*.py") if _imports_models(router)}


def test_docs_have_no_stale_deleted_dashboard_claims() -> None:
    """No owned doc still claims the deleted dashboard page / dag_canvas.html renders live."""
    offenders: dict[str, list[str]] = {}
    for doc in _ALL_DOCS:
        lowered = _read_text(doc).lower()
        hits = [marker for marker in _STALE_DELETED_SURFACE_MARKERS if marker in lowered]
        if hits:
            offenders[doc.name] = hits
    assert not offenders, (
        "docs still describe the CUT-02-deleted pipeline dashboard / dag_canvas.html as a live "
        f"surface (describe the DAG rail + /s/<stage> workspaces instead): {offenders}"
    )


def test_readme_describes_dag_centric_shell() -> None:
    """README describes the DAG-centric console (command palette + the DAG spine)."""
    text = _read_text(_README)
    assert "command palette" in text.lower(), "README must describe the Cmd-K command palette"
    assert "DAG" in text, "README must describe the DAG rail / DAG-centric shell"


def test_architecture_has_ui_ia_section() -> None:
    """docs/architecture.md carries a UI/IA section (/s/ stage routing + record slide-in)."""
    text = _read_text(_ARCHITECTURE)
    assert "/s/" in text, "architecture.md must document the /s/<stage> HTMX stage routing"
    assert "record slide-in" in text, "architecture.md must document the per-file record slide-in"


def test_project_structure_maps_shell_templates() -> None:
    """docs/project-structure.md maps the shell template tree + /s/ router relationship."""
    text = _read_text(_PROJECT_STRUCTURE)
    assert "templates/shell" in text, "project-structure.md must map the templates/shell tree"
    assert "/s/" in text, "project-structure.md must map the /s/<stage> router-to-workspace relationship"


def test_maintained_architecture_docs_have_no_box_drawing_diagrams() -> None:
    """Maintained architecture is renderable Mermaid/tables, never a stale hand-drawn tree."""
    symbols = {doc.name: _box_drawing_symbols(doc) for doc in _MAINTAINED_ARCHITECTURE_DOCS}
    offenders = {name: found for name, found in symbols.items() if found}
    assert not offenders, f"maintained docs contain box-drawing architecture: {offenders}"


def test_project_structure_generated_inventory_matches_live_packages() -> None:
    """The concise package table is generated from the live recursive Python-file counts."""
    text = _read_text(_PROJECT_STRUCTURE)
    package_root = _REPO_ROOT / "src" / "phaze"
    for area in _PACKAGE_AREAS:
        count = sum(1 for _ in (package_root / area).rglob("*.py"))
        assert f"| `{area}/` | {count} |" in text, f"project inventory is stale for src/phaze/{area}: expected {count} Python files"


def test_documented_mermaid_claims_have_resolvable_live_evidence() -> None:
    """Every maintained Mermaid dependency line added here cites existing source paths."""
    docs = (_ARCHITECTURE, _DATABASE, _PROJECT_STRUCTURE)
    claims = tuple(chain.from_iterable(_mermaid_evidence_claims(doc) for doc in docs))
    missing = _missing_evidence_paths(claims)
    assert not missing, f"maintained Mermaid diagrams cite missing source evidence: {missing}"
    assert len(claims) >= 30, "too few source-backed Mermaid dependency claims were checked"


def test_database_reference_tracks_migration_head_and_set_profile_relationship() -> None:
    """The database reference follows the live Alembic head and the 063 ORM relationship."""
    heads = migration_heads(_REPO_ROOT)
    text = _read_text(_DATABASE)
    assert heads == {"063"}
    assert "head, **`063`**" in text
    assert "| `set_profile`" in text

    from phaze.models.file import FileRecord
    from phaze.models.set_profile import SetProfile

    foreign_key = next(iter(SetProfile.__table__.foreign_keys))
    assert foreign_key.target_fullname == "files.id"
    assert foreign_key.ondelete == "CASCADE"
    relationship = FileRecord.set_profile.property
    assert relationship.uselist is False
    assert relationship.passive_deletes is True
    assert "delete-orphan" in relationship.cascade


def test_process_file_docs_match_live_timeout_retry_and_heartbeat_policy() -> None:
    """Architecture and analysis docs cannot restore the retired elapsed-time job timeout."""
    from phaze.config import BaseSettings
    from phaze.tasks._shared.queue_defaults import _FUNCTION_HEARTBEAT_POLICY, _FUNCTION_JOB_POLICY

    assert _FUNCTION_JOB_POLICY["process_file"] == (0, 2)
    assert _FUNCTION_HEARTBEAT_POLICY["process_file"] == "analysis_job_heartbeat_sec"
    settings = BaseSettings(analysis_stall_timeout_sec=900)
    assert settings.analysis_job_heartbeat_sec == 1800

    for doc in (_ARCHITECTURE, _API, _ESSENTIA_ANALYSIS):
        text = _read_text(doc)
        assert "timeout=0" in text, f"{doc.name} does not name the timeout-free process_file contract"
        assert "retries=2" in text, f"{doc.name} does not name the bounded process_file retry policy"
        assert "analysis_job_heartbeat_sec" in text, f"{doc.name} does not name the derived process_file heartbeat"


def test_projection_and_router_import_references_match_live_modules() -> None:
    """Projection helpers stay represented and docs acknowledge direct router model imports."""
    projection_modules = (
        "set_projection.py",
        "set_projection_writer.py",
        "set_projection_backfill.py",
        "analysis_timeline.py",
        "harmonic_journey.py",
        "track_segments.py",
        "record_facts.py",
        "set_glyph_colors.py",
    )
    maintained_text = "\n".join(map(_read_text, _MAINTAINED_ARCHITECTURE_DOCS))
    for module in projection_modules:
        assert (_REPO_ROOT / "src" / "phaze" / "services" / module).is_file()
        assert module in maintained_text, f"maintained references omit projection module {module}"

    direct_model_importers = _direct_model_importers()
    assert "src/phaze/routers/record.py" in direct_model_importers
    assert "Routers call services where a reusable seam exists, but many routers also import" in _read_text(_PROJECT_STRUCTURE)


def test_quick_start_has_no_stale_legacy_nav() -> None:
    """docs/quick-start.md no longer instructs visiting the removed legacy full-page tabs."""
    text = _read_text(_QUICK_START)
    offenders = [url for url in _STALE_NAV_URLS if url in text]
    assert not offenders, f"quick-start.md still points at removed legacy pages: {offenders}"
