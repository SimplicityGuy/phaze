"""Dead-template AST guard (Phase 57 SHELL-05 — seeded GREEN, kept green through cutover).

A Jinja2 template is an *orphan* if no router renders it (directly or transitively
via `{% extends %}` / `{% include %}` / `{% import %}`). Orphans accumulate silently
as the UI evolves; the v7.0 shell cutover (Phase 62 / CUT-02) deletes the legacy
per-page templates, and this guard is the proof that nothing was left dangling.

How it works:

* **Entry set** — the templates a router actually renders, extracted as quoted
  ``"...html"`` string literals from every ``src/phaze/routers/*.py``. We capture
  every ``.html`` literal, not only the ``name="..."`` form: routers also render
  through the ``_render_partial(request, "<tpl>.html", ...)`` helper (positional
  arg) and via a ternary-assigned ``template`` variable (``admin_agents.py``), so a
  ``name=``-only regex under-captures and false-flags reachable partials as orphans
  (RESEARCH caveat A4 — "renders use only ``name=`` literals" — was inaccurate). A
  stray non-template ``.html`` literal is harmless: it simply has no on-disk target
  to follow and is not itself under ``templates/``.
* **Reachable set** — the transitive closure of
  ``jinja2.meta.find_referenced_templates`` (jinja2 3.1.6) over each template's
  ``extends`` / ``include`` / ``import`` targets, starting from the entry set.
* **Orphan** — any ``templates/**/*.html`` reachable from nobody.

`find_referenced_templates` yields ``None`` for a dynamic ``{% include some_var %}``
target. That is expected and safe: the dynamic-partial form the v7.0 shell introduces
(Plans 02/03) keeps those partials reachable via the existing legacy pages this phase,
so the guard stays green. We drop the ``None`` sentinel rather than weakening the
closure.

If a future dynamic ``name=`` is ever introduced (making a real template
un-discoverable statically), add it to ``_ALLOWLIST`` with an inline justification —
do NOT relax the closure logic to force green.
"""

from __future__ import annotations

from pathlib import Path
import re

from jinja2 import Environment, meta


_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _REPO_ROOT / "src" / "phaze" / "templates"
_ROUTERS = _REPO_ROOT / "src" / "phaze" / "routers"

# Any quoted "....html" string literal in router source — covers name="x.html",
# _render_partial(request, "x.html", ...) positional args, and ternary-assigned
# template-name variables.
_HTML_LITERAL = re.compile(r"""["']([^"']+\.html)["']""")

# Templates genuinely unreachable today but intentionally retained until the v7.0
# cutover deletes legacy templates (Phase 62 / CUT-02). Every entry MUST carry an
# inline comment justifying it; do NOT add a reachable template here — fix the
# entry-set / closure extraction instead.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Pre-existing dead partial: no router, include, or JS references it. The
        # duplicates/proposals/cue toast partials are the live ones; this tracklists
        # copy is unused. Slated for removal by CUT-02 (Phase 62) — the guard proves
        # it dangles. Tracked here so the guard stays green without masking new dead
        # templates.
        "tracklists/partials/toast.html",
        # Superseded by Phase 61 (61-03): the ⌘K palette repurposed the `/search/`
        # HX branch to render `search/partials/palette_results.html`, so the old
        # flat-table search page and its partials are no longer reached by any
        # router. Left on disk (not deleted) because dead old-UI template removal
        # is CUT-02's explicit scope (Phase 62); allowlisted so the guard proves
        # they dangle without blocking Phase 61.
        "search/page.html",
        "search/partials/results_content.html",
        "search/partials/results_row.html",
        "search/partials/results_table.html",
        "search/partials/search_form.html",
        "search/partials/summary_counts.html",
    }
)


def _entry_templates() -> set[str]:
    """Templates rendered directly by a router (any quoted "...html" literal)."""
    names: set[str] = set()
    for py in sorted(_ROUTERS.glob("*.py")):
        names |= set(_HTML_LITERAL.findall(py.read_text()))
    return names


def _referenced_from(env: Environment, rel_path: str) -> set[str]:
    """extends/include/import targets of one template (drops dynamic None targets)."""
    src = (_TEMPLATES / rel_path).read_text()
    return {t for t in meta.find_referenced_templates(env.parse(src)) if t is not None}


def test_no_orphan_templates() -> None:
    """Every templates/**/*.html is reachable from some router; no dead templates."""
    env = Environment(autoescape=True)
    all_templates = {p.relative_to(_TEMPLATES).as_posix() for p in _TEMPLATES.rglob("*.html")}

    reachable: set[str] = set()
    frontier = _entry_templates()
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        # Only follow references for templates that actually exist on disk.
        if (_TEMPLATES / current).is_file():
            frontier |= _referenced_from(env, current) - reachable

    orphans = all_templates - reachable - _ALLOWLIST
    assert not orphans, f"Orphaned templates (referenced by nobody): {sorted(orphans)}"
