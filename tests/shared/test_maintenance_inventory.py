from scripts import maintenance_inventory as inventory


def test_every_tracked_file_is_a_surface_or_justified_exclusion() -> None:
    result = inventory.build_inventory()
    assert result["unclassified"] == []

    tracked = set(inventory.tracked_paths(str(result["revision"])))
    groups = (*dict(result["code_surfaces"]).values(), *dict(result["documentation"]).values(), *dict(result["excluded"]).values())
    assert tracked == {path for paths in groups for path in paths}


def test_documentation_classes_are_exclusive_and_complete() -> None:
    result = inventory.build_inventory()
    documentation = dict(result["documentation"])
    assert set(documentation) == set(inventory.DOCUMENTATION_CLASS_NAMES)
    paths = [path for members in documentation.values() for path in members]
    assert len(paths) == len(set(paths))


def test_required_code_comment_surfaces_are_present() -> None:
    code = dict(inventory.build_inventory()["code_surfaces"])
    assert set(code) == set(inventory.CODE_SURFACE_NAMES)
    assert "justfile" in code["justfile"]
    assert "alembic/script.py.mako" in code["runtime-template"]
    assert ".github/actions/docker-build-cache/action.yml" in code["github-automation"]


def test_generated_document_requires_an_explicit_marker() -> None:
    result = inventory.build_inventory()
    generated = set(dict(result["documentation"])["generated"])
    assert generated == set(inventory.generated_documents(str(result["revision"])))


def test_classification_precedence_for_special_documentation() -> None:
    generated = frozenset({"docs/README.md", ".planning/old.md"})
    assert inventory.documentation_class("src/phaze/prompts/naming.md", generated=generated) == "runtime-loaded"
    assert inventory.documentation_class("docs/design/0018-set-projection-and-file-viewer.md", generated=generated) == "accepted-decision"
    assert inventory.documentation_class("CLAUDE.md", generated=generated) == "tool-local"
    assert inventory.documentation_class("docs/README.md", generated=generated) == "generated"
    assert inventory.documentation_class(".planning/old.md", generated=generated) == "generated"
    assert inventory.documentation_class("docs/spikes/result.md", generated=generated) == "historical-evidence"
    assert inventory.documentation_class("README.md", generated=generated) == "maintained"
