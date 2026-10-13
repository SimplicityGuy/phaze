"""Static guards for the recording page's remote look-up panel (phaze-q6j9w).

The behavioural half lives in tests/integration/test_remote_lookup_ui.py, against the real service. These are the
cheap, always-on half: properties of the SOURCE that no render of one scenario can prove, each pinned so a later edit
that breaks it fails here and not in front of the operator.

* Rendering must never issue a lookup: the read-only GET handler cannot reach a service call that talks to a provider,
  and nothing in the panel loads or polls an action.
* Every mutation is an explicit operator form that carries the compare-and-set values or the reason it requires.
* An attribution link opens in a new tab with ``rel="noopener noreferrer"``.
* No provider is named in a template or a route: operator-visible names come from the descriptor's ``display_name``.
"""

import ast
from pathlib import Path
import re

from bs4 import BeautifulSoup

from phaze.services.remote_providers import REMOTE_PROVIDERS


_SRC = Path(__file__).resolve().parents[3] / "src" / "phaze"
_PANEL_TEMPLATES = sorted((_SRC / "templates" / "record" / "remote").glob("*.html"))
_ROUTER = _SRC / "routers" / "remote_lookup_ui.py"
_JINJA = re.compile(r"\{#.*?#\}", re.S)

# Everything on RemoteLookup / the decision service that sends a request, writes a decision or lifts a stop.
_ACTIONS = frozenset({"lookup", "load", "clear_stop", "decide_remote_candidate", "commit", "add", "execute"})


def _source(path: Path) -> str:
    return _JINJA.sub("", path.read_text())


def _forms() -> list[tuple[str, BeautifulSoup]]:
    found = []
    for path in _PANEL_TEMPLATES:
        for form in BeautifulSoup(_source(path), "html.parser").find_all("form"):
            found.append((path.name, form))
    return found


def test_the_panel_templates_exist_and_are_scanned() -> None:
    assert {path.name for path in _PANEL_TEMPLATES} >= {"_panel.html", "_group.html", "_notice.html", "_assessment.html", "_parts.html"}
    assert len(_forms()) >= 5, "the lookup, load, reject, decision, reopen and clear-stop forms"


def test_the_read_only_handler_cannot_reach_an_action() -> None:
    tree = ast.parse(_ROUTER.read_text())
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)}
    for name in ("remote_lookup_panel", "_render", "_statuses", "_service"):
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name)
        }
        assert not (called & _ACTIONS), f"{name} must only read, but calls {sorted(called & _ACTIONS)}"
    decorators = {
        node.name: [ast.unparse(decorator) for decorator in node.decorator_list] for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
    }
    assert any(".get(" in text for text in decorators["remote_lookup_panel"])
    for name, texts in decorators.items():
        if name != "remote_lookup_panel" and name.startswith("remote_"):
            assert all(".post(" in text for text in texts if "router." in text), name
            assert any("require_same_origin_form" in text for text in texts), f"{name} is a mutation without the same-origin guard"


def test_nothing_in_the_panel_loads_polls_or_gets_an_action() -> None:
    for path in _PANEL_TEMPLATES:
        text = _source(path)
        assert "hx-get" not in text and "hx-trigger" not in text, f"{path.name}: the panel's own markup issues nothing by itself"
    slot = _source(_SRC / "templates" / "record" / "_record_content.html")
    match = re.search(r"<section[^>]*data-remote-lookup-slot[^>]*>", slot)
    assert match is not None
    assert 'hx-get="/files/{{ file.id }}/remote-lookup"' in match.group(0) and "hx-post" not in match.group(0)


def test_every_action_form_is_a_post_to_the_panel_routes() -> None:
    for name, form in _forms():
        action = form.get("hx-post")
        assert isinstance(action, str) and action.startswith("/files/{{ file_id }}/remote-lookup/"), (name, form.get("hx-post"))
        assert form.get("method") is None and form.get("action") is None, "an htmx-only form, guarded by data-hx-guard"
        submits = [button for button in form.find_all("button") if button.get("type") == "submit"]
        assert submits and all(button.has_attr("disabled") and button.has_attr("data-hx-guard") for button in submits), name


def test_decision_forms_carry_the_compare_and_set_values_and_a_server_minted_decision() -> None:
    parts = _source(next(path for path in _PANEL_TEMPLATES if path.name == "_parts.html"))
    for field in ("observation_id", "decision_id", "expected_selection_token", "expected_revision", "expected_media_sha256"):
        assert f'name="{field}"' in parts, field
    assert "actor" not in parts, "the actor is server-owned and never a form field"
    for name, form in _forms():
        if str(form.get("hx-post")).endswith("/decision"):
            assert name == "_group.html"
    group = _source(next(path for path in _PANEL_TEMPLATES if path.name == "_group.html"))
    decisions = group.count('remote-lookup/decision"')
    assert decisions == 3 and group.count("parts.decision_fields(") == decisions, "reopen, reject and select/reject each carry the fields"


def test_reopen_and_clear_stop_require_a_labelled_reason() -> None:
    required = {"data-reopen-form": "reopen", "data-clear-stop-form": "clear-stop"}
    seen = set()
    for _name, form in _forms():
        for marker, label in required.items():
            if form.has_attr(marker):
                seen.add(label)
                area = form.find("textarea", attrs={"name": "reason"})
                assert area is not None and area.has_attr("required") and area.get("maxlength") == "512", label
                assert form.find("label", attrs={"for": area["id"]}) is not None, f"{label}: the reason field needs a label"
    assert seen == set(required.values())


def test_every_textarea_has_a_label_and_every_new_tab_link_is_noopener_noreferrer() -> None:
    for path in _PANEL_TEMPLATES:
        soup = BeautifulSoup(_source(path), "html.parser")
        for area in soup.find_all("textarea"):
            assert soup.find("label", attrs={"for": area.get("id")}) is not None, f"{path.name}: unlabelled textarea"
        for link in soup.find_all("a"):
            if link.get("target") == "_blank":
                assert set(link.get("rel", [])) >= {"noopener", "noreferrer"}, f"{path.name}: {link}"
                assert link.find(class_="sr-only") is not None, "a new-tab link says so to assistive technology"


def test_the_panel_names_no_provider() -> None:
    sources = [_source(path) for path in _PANEL_TEMPLATES] + [_ROUTER.read_text(), (_SRC / "services" / "remote_lookup_view.py").read_text()]
    registry = {binding.profile.provider_id for binding in REMOTE_PROVIDERS}
    for text in sources:
        assert not re.search(r"provider(_id)?\s*(==|!=|in)\s*[\[(\"']", text), "a per-provider branch"
        assert not re.search(r"""provider\.[a-z_]+\s*==\s*['"]""", text)
        for provider_id in registry:
            assert provider_id not in text, f"{provider_id} is hard-coded; use the descriptor's display_name"
