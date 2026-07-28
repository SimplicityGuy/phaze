"""Template-contract guards for htmx swap targets (phaze-thd6).

Two whole classes of v7-shell defect are invisible to every existing test because they are
*document* properties, not response-body properties: our route tests assert on what an endpoint
RETURNS, never on where that return value can land. This module asserts the missing half.

Guard 1 -- ``test_no_control_is_nested_inside_its_own_swap_target`` (phaze-thd6): a control whose
``hx-target`` resolves to one of its OWN ancestors deletes itself on the success swap. Discover's
RECOVER button was exactly this: the button, its ``hx-indicator`` spinner and its error hint all
lived inside ``#discover-recover-response``, and ``POST /pipeline/recover`` returns 200
unconditionally, so the *normal* path destroyed the control and the failure path was the only one
that preserved it.

Scope + honesty about it: the parse is per-template and static (raw Jinja source through
``html.parser`` -- attribute values keep their ``{{ }}`` placeholders, which is fine because we only
compare ids). It therefore sees nesting WITHIN one template file, not nesting created across an
``{% include %}`` boundary. That is the shape both known instances take, and a per-file check needs
no context fixtures, so it runs on every template in the tree rather than on the handful a
render-based test could reach.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag


_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEMPLATES = _REPO_ROOT / "src" / "phaze" / "templates"

# Swap styles that discard the target's existing children WITHOUT the response being understood
# as a re-render of them. ``innerHTML`` is htmx's default when ``hx-swap`` is absent, so an
# unspecified swap counts.
#
# ``outerHTML`` is deliberately NOT here, and that exclusion is the whole reason this guard is
# usable. "Replace my own row/card with the server's new version of it" is the app's dominant and
# CORRECT idiom -- ``_diff_row.html``'s APPROVE targets its own ``#rename-row-<id>`` wrapper,
# ``_dupe_group.html``'s keeper radio targets its own ``#dupe-group-<hash>`` card -- and in that
# shape the response re-emits the control, so nothing is lost. ``innerHTML`` into an ancestor is
# the broken shape: the response is a status/ack fragment that was never meant to contain the
# trigger, so the trigger is destroyed with no replacement.
# ``beforeend``/``afterbegin``/``beforebegin``/``afterend`` only INSERT and are likewise fine.
_DESTRUCTIVE_SWAPS = frozenset({"innerhtml", "textcontent", "delete"})


def _templates() -> list[Path]:
    return sorted(_TEMPLATES.rglob("*.html"))


def _target_id(el: Tag) -> str | None:
    """The bare id an element's ``hx-target="#x"`` points at, or None for any other target form.

    ``this``/``closest ...``/``find ...``/``next ...`` extended selectors are out of scope: they
    resolve relative to the element at runtime and cannot be judged statically.
    """
    raw = el.get("hx-target")
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw.startswith("#"):
        return None
    return raw[1:]


def _swap_is_destructive(el: Tag) -> bool:
    raw = el.get("hx-swap")
    if not isinstance(raw, str):
        # htmx default.
        return True
    # hx-swap carries modifiers: `outerHTML swap:1s`, `innerHTML show:top`.
    return raw.strip().split()[0].lower() in _DESTRUCTIVE_SWAPS


def test_no_control_is_nested_inside_its_own_swap_target() -> None:
    """No element's destructive ``hx-target`` resolves to one of its own ancestors.

    A control that swaps into an ancestor is removed from the DOM by its own success response.
    The correct shape -- used by metadata / fingerprint / propose / rename / tagwrite / dedupe --
    keeps the trigger in the header's ``actions`` slot and puts a SIBLING
    ``#<stage>-trigger-response`` sink in the body.
    """
    violations: list[str] = []
    for path in _templates():
        soup = BeautifulSoup(path.read_text(), "html.parser")
        for el in soup.find_all(attrs={"hx-target": True}):
            if not isinstance(el, Tag):
                continue
            target = _target_id(el)
            if target is None or not _swap_is_destructive(el):
                continue
            for ancestor in el.parents:
                if isinstance(ancestor, Tag) and ancestor.get("id") == target:
                    violations.append(f'{path.relative_to(_TEMPLATES).as_posix()}: <{el.name}> hx-target="#{target}" is its own ancestor')
                    break
    assert not violations, "control(s) nested inside their own swap target -- the success response deletes the control:\n  " + "\n  ".join(violations)
