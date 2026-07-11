"""UI-01 anti-feature guard: no template renders the raw-enum ``FileRecord.state`` string (87-05).

The REQUIREMENTS.md anti-feature table forbids "rendering raw internal status strings". Phase 87 cuts
per-stage status over to the DERIVED five-bucket pill matrix (``_stage_pill`` / ``_stage_matrix``), and
87-05 retired the last raw ``{'text': f.state}`` render (metadata_workspace.html). This test is the
permanent teeth: it scans the operator surface templates for a raw-enum ``.state`` RENDER site and fails
if ANY remains -- so the retired cell can never silently return.

**Specificity (why this guard is not a blunt line-grep):** it flags only two RENDER forms --

* a Jinja output of a ``.state`` attribute: ``{{ f.state }}`` (optionally piped, ``{{ f.state | x }}``);
* the reusable ``_file_table.html`` cell-dict text render: ``{'text': f.state}`` / ``{"text": f.state}``.

It deliberately does NOT flag non-render references, which are legitimate:

* comparisons / membership tests -- ``{% if f.state == 'awaiting_cloud' %}``,
  ``{% set extracted = f.state in (...) %}`` (analyze_workspace derives friendly words from these);
* prose inside Jinja comments (``{# ... #}``) mentioning ``FileRecord.state`` / the retired
  ``{'text': f.state}`` cell -- comment regions are stripped before scanning so a partial can
  DOCUMENT the retirement (as metadata_workspace/analyze_workspace do) without tripping the guard.

**Mutation observation (recorded in 87-05-SUMMARY.md):** re-adding ``{'text': f.state}`` to
metadata_workspace.html turns this test RED; removing it restores GREEN.
"""

from __future__ import annotations

from pathlib import Path
import re


# The operator surfaces that render per-file/per-stage status: the pipeline partials (files table,
# stage workspaces), the per-file record views, and the admin partials (Phase 88's agent-activity
# drill-in renders a per-agent stage matrix here -- T-88-10). If a new status surface is added
# elsewhere, extend this list -- the guard is only as wide as the directories it scans.
_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "src" / "phaze" / "templates"
_SCANNED_DIRS = (
    _TEMPLATE_ROOT / "pipeline" / "partials",
    _TEMPLATE_ROOT / "record",
    _TEMPLATE_ROOT / "admin" / "partials",
)

# A Jinja output of a bare ``<var>.state`` attribute (optionally through a filter): ``{{ f.state }}``.
_RENDER_OUTPUT = re.compile(r"\{\{\s*\w+\.state\s*(?:\|[^}]*)?\}\}")
# The _file_table.html cell-dict text render: ``{'text': f.state}`` / ``{"text": f.state}``.
_RENDER_CELL = re.compile(r"""['"]text['"]\s*:\s*\w+\.state\b""")


def _iter_template_files() -> list[Path]:
    files: list[Path] = []
    for d in _SCANNED_DIRS:
        if d.is_dir():
            files.extend(sorted(d.glob("*.html")))
    return files


# A Jinja comment region ``{# ... #}`` (possibly multi-line). Stripped before scanning so a prose
# mention of the retired render form inside a comment is not a false positive.
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _strip_comments_keep_lines(text: str) -> str:
    """Blank out ``{# ... #}`` regions while preserving line count (so line numbers stay accurate)."""
    return _JINJA_COMMENT.sub(lambda m: "\n" * m.group().count("\n"), text)


def _raw_state_render_sites() -> list[str]:
    """Return ``"<path>:<lineno>: <line>"`` for every raw-enum ``.state`` RENDER site found."""
    violations: list[str] = []
    for path in _iter_template_files():
        scannable = _strip_comments_keep_lines(path.read_text())
        for lineno, line in enumerate(scannable.splitlines(), start=1):
            if _RENDER_OUTPUT.search(line) or _RENDER_CELL.search(line):
                violations.append(f"{path}:{lineno}: {line.strip()}")
    return violations


def test_no_raw_enum_state_render_in_operator_templates() -> None:
    """No pipeline/record/admin-partial template renders the raw-enum ``.state`` string (UI-01 anti-feature)."""
    # Sanity: the guard is actually scanning real files (a silent empty glob must not pass vacuously).
    assert _iter_template_files(), f"guard scanned no templates under {_SCANNED_DIRS}"

    violations = _raw_state_render_sites()
    assert not violations, "raw-enum `.state` render sites must be retired (UI-01):\n" + "\n".join(violations)


def test_guard_flags_a_planted_render() -> None:
    """The guard's regexes actually MATCH the two render forms (self-test so the guard can go RED).

    This proves the guard is not a vacuous no-op: both the ``{{ f.state }}`` and the ``{'text': f.state}``
    forms are detected, so re-introducing either in a scanned template would fail
    ``test_no_raw_enum_state_render_in_operator_templates``.
    """
    assert _RENDER_OUTPUT.search("<td>{{ f.state }}</td>")
    assert _RENDER_OUTPUT.search("{{ file.state | upper }}")
    assert _RENDER_CELL.search("{'text': f.state, 'color': c},")
    assert _RENDER_CELL.search('{"text": row.state}')
    # Non-render references must NOT match (specificity -- comparisons + comments are legitimate).
    assert not _RENDER_OUTPUT.search("{% if f.state == 'awaiting_cloud' %}")
    assert not _RENDER_CELL.search("{% set extracted = f.state in ('metadata_extracted',) %}")
    assert not _RENDER_OUTPUT.search("a missing app.state.redis surfaces as False")
