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
import re

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


# ---------------------------------------------------------------------------
# Guard 2 (phaze-vvmh): every swap target must resolve to an id the SHELL can actually render.
#
# This is the guard the bead asks for, and the one that would have caught the defect it fixes.
# "Execute Approved" lived in proposals/partials/stats_bar.html, which was included ONLY from two
# OOB wrappers addressed to `#stats-bar` -- an id whose sole host, the legacy proposals list page,
# the Phase-62 cutover deleted. Both wrappers kept rendering, htmx kept discarding them
# (htmx:oobErrorNoTarget), and the product's terminal action had no trigger in any served document.
# The same cutover left `#stats-header` orphaned in the dedupe responses (phaze-nt8f) and
# `#duplicates-list` referenced by a partial nobody mounted.
#
# The reachable set is deliberately the SHELL closure -- shell.html plus every STAGE_PARTIALS and
# UTILITY_PANES value plus their include/import/extends transitive closure -- not "every template on disk". That
# distinction IS the guard: `#stats-bar` was declared, by the very OOB wrapper that targeted it and
# by the partial inside it, so any check that accepted providers from anywhere on disk would have
# called the dead chain healthy. Only "an id the mounted document contains" is a meaningful target.
# ---------------------------------------------------------------------------

_SHELL_ROOT = "shell/shell.html"

# Ids that legitimately exist in a served document but are NOT declared by a shell-closure template.
# Each entry MUST name the element that really provides it and why the closure cannot see it. Keep
# this SMALL: an entry here is a claim that a human verified the target exists, and the whole point
# of the guard is that such claims decay silently.
_EXTERNAL_TARGETS: frozenset[str] = frozenset(
    {
        # Declared by <body> in base.html via the shell's own extends chain is NOT the case here:
        # this is the htmx "swap into the document body" convention used by the full-page redirect
        # paths (response_shape rule 2 -- a history restore ignores hx-target and swaps <body>).
        "body",
    }
)


def _oob_target(el: Tag) -> str | None:
    """The id an ``hx-swap-oob`` element addresses.

    Two spellings, both used in this repo: ``hx-swap-oob="true"``/``"delete"``/``"outerHTML"`` on an
    element that carries the target id itself, and ``hx-swap-oob="<swap>:#<id>"`` naming it inline
    (``beforeend:#toast-container``).
    """
    raw = el.get("hx-swap-oob")
    if not isinstance(raw, str):
        return None
    _, _, selector = raw.partition(":")
    if selector.startswith("#"):
        return selector[1:]
    own_id = el.get("id")
    return own_id if isinstance(own_id, str) else None


def _normalise(candidate: str) -> str:
    """Collapse Jinja interpolation to a single ``*`` so a templated id can be matched by shape.

    ``dupe-group-{{ group.sha256_hash }}`` and ``dupe-group-{{ hash }}`` are the same id SHAPE and
    must compare equal -- the target and its provider are usually spelled with different variable
    names in different files. Everything outside the braces is compared literally, so
    ``rename-row-*`` never matches ``move-row-*``.
    """
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(candidate):
        if candidate.startswith("{{", i) or candidate.startswith("{%", i):
            depth += 1
            i += 2
            if not out or out[-1] != "*":
                out.append("*")
            continue
        if candidate.startswith("}}", i) or candidate.startswith("%}", i):
            depth = max(0, depth - 1)
            i += 2
            continue
        if depth == 0:
            out.append(candidate[i])
        i += 1
    return "".join(out).strip()


def _shell_closure() -> set[str]:
    """Templates that can appear in a document the shell serves: shell plus stage and utility panes."""
    from jinja2 import Environment, meta

    from phaze.routers.shell import STAGE_PARTIALS, UTILITY_PANES

    env = Environment(autoescape=True)
    reachable: set[str] = set()
    frontier = {_SHELL_ROOT, *STAGE_PARTIALS.values(), *UTILITY_PANES.values()}
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        path = _TEMPLATES / current
        if path.is_file():
            refs = {t for t in meta.find_referenced_templates(env.parse(path.read_text())) if t is not None}
            frontier |= refs - reachable
    return reachable


# Literal values assigned inside a Jinja tag: {% set row_id_prefix = "rename-row" %},
# {% with row_id_prefix="move-row", ... %}. These are how the shell parameterises the handful of
# templates that build an id out of a variable (_diff_row.html's `{{ row_id_prefix }}-{{ pid }}`,
# _file_table.html's `{{ table_id }}`), so they are what those ids ACTUALLY render as.
_JINJA_LITERAL = re.compile(r"""(\w+)\s*=\s*["']([A-Za-z][\w-]*)["']""")


def _shell_id_stems(templates: set[str]) -> set[str]:
    """Literal id stems the shell passes into its parameterised partials.

    Without these, a provider spelled entirely in Jinja normalises to a bare ``*`` and would match
    every target on earth -- which silently disarms the whole guard (it did, on the first draft:
    ``#stats-bar`` "resolved" against ``_diff_row.html``'s ``*-*``). Substituting the real literals
    keeps ``rename-row-<id>`` resolvable while leaving an invented id unresolvable.
    """
    stems: set[str] = set()
    for name in templates:
        path = _TEMPLATES / name
        if path.is_file():
            stems |= {value for _, value in _JINJA_LITERAL.findall(path.read_text())}
    # Spelled in Python, not markup: the propose workspace's container id is injected by
    # routers/shell.py so the router's HX-Target comparison and the element cannot drift.
    from phaze.routers.shell import PROPOSE_LIST_CONTAINER_ID

    stems.add(PROPOSE_LIST_CONTAINER_ID)
    return stems


def _provider_ids(templates: set[str]) -> set[str]:
    """Ids DECLARED by those templates -- excluding ids that only appear on an OOB wrapper.

    The exclusion is the crux. ``<div id="stats-bar" hx-swap-oob="true">`` does not CREATE
    ``#stats-bar``; it asks the browser to find one. Counting it as a declaration is exactly the
    reasoning error that let a fragment satisfy its own target and kept a dead chain looking alive.

    A provider whose id is built from Jinja is expanded against the literal stems the shell passes
    (:func:`_shell_id_stems`) and then DROPPED if nothing literal survives -- an id shape with no
    literal characters is not evidence that any particular id exists.
    """
    stems = _shell_id_stems(templates)
    ids: set[str] = set()
    for name in templates:
        path = _TEMPLATES / name
        if not path.is_file():
            continue
        for el in BeautifulSoup(path.read_text(), "html.parser").find_all(attrs={"id": True}):
            # Jinja's conditional marker survives this raw parse as a ``{%`` attribute: those
            # elements are normal providers in shell renders and OOB wrappers only in responses.
            if not isinstance(el, Tag) or (el.has_attr("hx-swap-oob") and not el.has_attr("{%")):
                continue
            raw = el.get("id")
            if not isinstance(raw, str):
                continue
            shape = _normalise(raw)
            if shape.replace("*", "").strip("-_"):
                ids.add(shape)
                continue
            # Pure-wildcard shape: expand it through every literal stem the shell hands its
            # parameterised partials, rather than letting it match everything.
            # Only the FIRST wildcard is the stem: `*-*` (_diff_row's `{{ row_id_prefix }}-{{ pid }}`)
            # becomes `rename-row-*`, keeping the per-row suffix open while pinning the family.
            ids.update(shape.replace("*", stem, 1) for stem in stems)
    return ids


def _resolves(target: str, providers: set[str]) -> bool:
    """True when ``target`` matches a provider id, comparing shapes (``*`` = a Jinja expression)."""
    if target in providers or target in _EXTERNAL_TARGETS:
        return True
    return any(_shapes_match(target, provider) for provider in providers)


def _shapes_match(a: str, b: str) -> bool:
    """Compare two normalised ids where ``*`` stands for "some Jinja expression"."""
    import fnmatch

    return fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)


def test_every_oob_fragment_targets_an_id_the_shell_renders() -> None:
    """No ``hx-swap-oob`` fragment addresses an id that no shell-reachable template declares.

    An OOB fragment with no landing element is silently discarded by htmx, so the server pays to
    build it and the operator sees nothing -- the failure mode is invisible from both ends. Every
    fragment checked here is emitted by a route response, i.e. by markup that never appears in the
    document itself, which is why no render test covers it.
    """
    providers = _provider_ids(_shell_closure())
    violations: list[str] = []
    for path in _templates():
        for el in BeautifulSoup(path.read_text(), "html.parser").find_all(attrs={"hx-swap-oob": True}):
            if not isinstance(el, Tag):
                continue
            target = _oob_target(el)
            if target is None:
                continue
            normalised = _normalise(target)
            if not _resolves(normalised, providers):
                violations.append(f'{path.relative_to(_TEMPLATES).as_posix()}: hx-swap-oob -> "#{target}"')
    assert not violations, (
        "OOB fragment(s) addressed at ids no shell-reachable template declares -- htmx discards these "
        "with htmx:oobErrorNoTarget:\n  " + "\n  ".join(sorted(violations))
    )


def test_every_shell_control_targets_an_id_the_shell_renders() -> None:
    """Every ``hx-target="#x"`` inside the shell's own document resolves to an id that document has.

    Scoped to the shell closure because those templates ARE the served document: a control there
    whose target is missing fails at click time with ``htmx:targetError`` and issues no request at
    all -- the phaze-be1j shape, where the toast's Undo button silently did nothing while dismissing
    itself.
    """
    closure = _shell_closure()
    providers = _provider_ids(closure)
    violations: list[str] = []
    for name in sorted(closure):
        path = _TEMPLATES / name
        if not path.is_file():
            continue
        for el in BeautifulSoup(path.read_text(), "html.parser").find_all(attrs={"hx-target": True}):
            if not isinstance(el, Tag):
                continue
            target = _target_id(el)
            if target is None:
                continue
            if not _resolves(_normalise(target), providers):
                violations.append(f'{name}: <{el.name}> hx-target="#{target}"')
    assert not violations, "shell control(s) aimed at ids the shell never renders:\n  " + "\n  ".join(sorted(violations))


def test_execute_approved_has_a_live_host_in_the_shell() -> None:
    """POST /execution/start is triggerable from a document the shell serves (phaze-vvmh's core).

    The two guards above are structural; this one names the specific product guarantee they were
    added to protect. "Nothing moves without review, then execute" had no execute step: the only
    caller of /execution/start rode inside an OOB fragment aimed at a deleted id, so approvals
    accumulated with no way to dispatch them. Asserted on the shell closure -- an execute control
    that exists only in a response fragment is exactly the bug.
    """
    closure = _shell_closure()
    hosts = [name for name in sorted(closure) if "/execution/start" in (_TEMPLATES / name).read_text()]
    assert hosts, "no shell-reachable template posts to /execution/start -- Execute Approved is unreachable again"

    providers = _provider_ids(closure)
    for name in hosts:
        for el in BeautifulSoup((_TEMPLATES / name).read_text(), "html.parser").find_all(attrs={"hx-post": "/execution/start"}):
            assert isinstance(el, Tag)
            target = _target_id(el)
            assert target is not None, f"{name}: the execute trigger names no id target"
            assert _resolves(_normalise(target), providers), f"{name}: the execute trigger's response sink #{target} is not rendered by the shell"
