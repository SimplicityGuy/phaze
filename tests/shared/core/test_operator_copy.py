"""Guard authored presentation copy; archive names and other caller data are never sanitized."""

import argparse
import ast
from pathlib import Path
import re
import shlex
import uuid

from jinja2 import Environment, FileSystemLoader
import pytest
from sqlalchemy import select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.cli import _build_parser
from phaze.config import AgentSettings, BaseSettings, ControlSettings
from phaze.main import app
from phaze.models.file import FileRecord
from phaze.services.pagination import paged_stmt, split_sentinel


ROOT = Path(__file__).resolve().parents[3]
# These are maintained user/operator guides, including adjacent deployment and telemetry guides.
# Developer workflows (AGF/gates), design decisions, spikes and dated audit/report artifacts are
# evidence and instructions for developers, not current product guidance.
CURRENT_DOCS = (
    "README.md",
    "docs/README.md",
    "docs/multi-compute.md",
    "docs/k8s-burst.md",
    "docs/tracklist-retirement.md",
    "docs/analysis-evaluation.md",
    "docs/architecture.md",
    "docs/api.md",
    "docs/stranded-analysis-recovery.md",
    "docs/essentia-analysis.md",
    "docs/runbook.md",
    "docs/quick-start.md",
    "docs/cloud-burst.md",
    "docs/deployment.md",
    "docs/configuration.md",
    "docs/configuration-inventory.md",
    "docs/global-tracklist-providers.md",
    "docs/analysis-evaluation-upcloud-runbook.md",
    "docs/agent-queue-lanes.md",
    "docs/arm64-agent-image.md",
    "docs/degenerate-audio.md",
    "docs/database.md",
    "docs/telemetry/metric-catalogue.md",
    "docs/telemetry/overhead.md",
    "docs/telemetry/alerting.md",
    "docs/telemetry/traces.md",
    "docs/telemetry/concurrent-identity.md",
    "docs/telemetry/exporter.md",
    "docs/operations/companion-viewing.md",
    "docs/operations/companion-availability.md",
    "docs/operations/companion-capture.md",
    "docs/project-structure.md",
    "docs/performance/files-workspace.md",
    "docs/ui-design-reference.md",
)
TRACKING = re.compile(
    r"\bphaze-[a-z0-9]{4,}(?:\.\d+)*\b|\bPhases?[ -]+\d+(?:\.\d+)?|\b(?:D|CR|T|L|SHELL|REVIEW|CUT|KJOB|FAIL|REG|DRILL|UI|RECORD|SCHED|KSTAGE|KSUBMIT|REQ|DIST|AUTH|CLOUDPIPE|SIDECAR|ENRICH|BEUI|OBS|WORK|MCOMP|MKUE|KDEPLOY|WR)-\d{2,}(?:[.-]\d+)*(?:[a-z])?\b|\bquick-\d{6}-[a-z0-9]+\b|(?<![\d-])35-05(?![\d-])",
    re.IGNORECASE,
)
# Product tokens are machine-consumed names, never genealogy. Do not rename or scrub them.
PRODUCT_TOKEN = re.compile(
    r"\bphaze-(?:agent|server|internal|stage|models|border|panel|theme|muted|accent|primary|secondary|danger|surface|analysis|pipeline|files|decode|push|committed|telemetry|staging|essentia|alerts|cloud|owned|controller|watcher|submitter|analyze|tracklist|quarantine)(?:[-_a-zA-Z0-9/<>{}.]*)"
)
EVIDENCE_TARGET = re.compile(r"\]\([^\n)]*\)|(?:docs/)?(?:spikes|design)/[^\s)`]+")
# Exact negotiated wire header introduced by the typed agent-config policy. Similar prose and
# different header names remain guarded; never rename this machine-consumed API identifier.
WIRE_TOKEN = re.compile(r"(?<![\w-])X-Phaze-Config-Version(?![\w-])")
# Exact historical record markers, each scoped to its paragraph rather than exempting a guide.
HISTORICAL_RECORDS = {
    "docs/telemetry/alerting.md": ("Question as put, with host identifiers replaced by role descriptions",),
    "docs/telemetry/overhead.md": ("Question as put, 2026-08-26",),
    "docs/degenerate-audio.md": ("> For phaze-nkucu",),
    "docs/analysis-evaluation.md": ("Question as put",),
}
# Exact existing machine references: these paths address the staged evaluation, not prose
# genealogy. No arbitrary phaze-* path or entire code block is exempt from the public guard.
SUPPORT_IDENTIFIERS = {
    "docs/analysis-evaluation.md": ("/home/datum/phaze-iq64v", "docs/evaluation-phaze-iq64v-corpus.json"),
    "docs/analysis-evaluation-upcloud-runbook.md": ("/home/datum/phaze-iq64v",),
}

# These named records are serialized into snapshots/retained observations. Changing their text
# can change persisted semantic identity; this cleanup preserves the exact historical records,
# independently pinned by provider tests. No other literal in either module is exempt.
CANONICAL_PROVENANCE = {
    "src/phaze/tracklist_providers/trackid.py": frozenset({"RIGHTS_EVIDENCE"}),
    "src/phaze/tracklist_providers/mixesdb.py": frozenset({"RIGHTS_EVIDENCE"}),
}


def leaks(text: str) -> list[str]:
    """Find tracking vocabulary in authored text, keeping evidence links and product tokens."""
    text = text.replace('<a id="removing-fingerprint-era-data-phaze-0jpe"></a>', "").replace("Phaze-representative", "")
    return TRACKING.findall(PRODUCT_TOKEN.sub("", EVIDENCE_TARGET.sub("", WIRE_TOKEN.sub("", text))))


def template_copy(text: str) -> str:
    """Ignore implementation comments, which do not supply text/help/attributes to the UI."""
    text = re.sub(r"{#.*?#}|<!--.*?-->|/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//[^\n]*", "", text)


def document_copy(name: str, paragraph: str) -> str:
    for identifier in SUPPORT_IDENTIFIERS.get(name, ()):
        paragraph = paragraph.replace(identifier, "support-reference")
    return template_copy(paragraph)


def authored_literals(text: str, *, record_assignments: frozenset[str] = frozenset()) -> list[str]:
    tree = ast.parse(text)
    docs = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value = [statement.target], statement.value
        else:
            continue
        if value is not None and any(isinstance(target, ast.Name) and target.id in record_assignments for target in targets):
            docs.update(id(node) for node in ast.walk(value) if isinstance(node, ast.Constant))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and id(node) not in docs
        and isinstance(node.value, str)
        and (re.search(r"\s", node.value) or TRACKING.search(node.value))
    ]


def test_current_authored_copy_has_no_tracking_labels() -> None:
    failures = []
    for path in (ROOT / "src/phaze").rglob("*.py"):
        for text in authored_literals(path.read_text(), record_assignments=CANONICAL_PROVENANCE.get(str(path.relative_to(ROOT)), frozenset())):
            if found := leaks(text):
                failures.append((str(path.relative_to(ROOT)), found, text))
    for path in (ROOT / "src/phaze/templates").rglob("*"):
        if path.suffix in {".html", ".svg"} and (found := leaks(template_copy(path.read_text()))):
            failures.append((str(path.relative_to(ROOT)), found))
    assert failures == []


def test_current_operator_guides_have_no_tracking_labels() -> None:
    failures = []
    for name in (*CURRENT_DOCS, ".env.example"):
        for paragraph in (ROOT / name).read_text().split("\n\n"):
            if any(marker in paragraph for marker in HISTORICAL_RECORDS.get(name, ())):
                continue
            if found := leaks(document_copy(name, paragraph)):
                failures.append((name, found, paragraph))
    assert failures == []


def _descriptions(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(item) for key, item in value.items() if key in {"description", "summary"}] + [
            text for item in value.values() for text in _descriptions(item)
        ]
    if isinstance(value, list):
        return [text for item in value for text in _descriptions(item)]
    return []


def _help(parser: argparse.ArgumentParser) -> list[str]:
    result = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                result.extend(_help(child))
    return result


def test_generated_schemas_settings_and_cli_help_are_clean() -> None:
    texts = _descriptions(app.openapi()) + _help(_build_parser())
    for settings in (BaseSettings, ControlSettings, AgentSettings):
        texts.extend(_descriptions(settings.model_json_schema()))
    assert [(text, leaks(text)) for text in texts if leaks(text)] == []


@pytest.mark.parametrize(
    "sample",
    [
        "(phaze-o0n6)",
        "(Phase 33)",
        "(phaze-hti8, mirrors agent_env D-06)",
        'title="Phase 33"',
        "KDEPLOY-01",
        "KDEPLOY-03",
        "KDEPLOY-06",
        "WR-01",
        "35-05",
        "quick-260707-dh1",
    ],
)
def test_guard_rejects_an_introduced_leak(sample: str) -> None:
    assert leaks(template_copy(sample))
    assert leaks("KJOB-02")
    assert any(leaks(text) for text in authored_literals(f"help_text = {sample!r}"))


def test_guard_preserves_comments_revision_numbers_and_user_names() -> None:
    assert leaks(template_copy("{# phaze-o0n6 Phase 33 D-06 #}<p>Migration 039, revision 1.0</p>")) == []
    assert not any(leaks(text) for text in authored_literals('# phaze-o0n6\n"""Phase 33 internal rationale."""\nhelp_text = "revision 1.0"'))
    record = 'RIGHTS_EVIDENCE: tuple[str, ...] = ("phaze-gyr3l",)\nhelp_text = "Phase 33"'
    assert authored_literals(record, record_assignments=frozenset({"RIGHTS_EVIDENCE"})) == ["Phase 33"]
    assert any(leaks(text) for text in authored_literals(record))
    env = Environment(loader=FileSystemLoader(ROOT / "src/phaze/templates"), autoescape=True)
    error = "User recording: phaze-o0n6 & Phase 33.mp3"
    rendered = env.get_template("pipeline/partials/scan_submit_error.html").render(error_message=error)
    assert "phaze-o0n6 &amp; Phase 33.mp3" in rendered
    assert "Phase 27" not in rendered
    # The guard reads authored source, never the rendered caller-supplied value above.
    assert leaks(template_copy((ROOT / "src/phaze/templates/pipeline/partials/scan_submit_error.html").read_text())) == []


def test_representative_empty_and_settings_fragments_render_clean() -> None:
    env = Environment(loader=FileSystemLoader(ROOT / "src/phaze/templates"), autoescape=True)
    env.filters["thousands"] = str
    empty = env.get_template("pipeline/partials/empty_state.html").render(agents=[])
    rows = [
        {"key": key, "value": field.default, "source": "default", "description": field.description}
        for key in ("active_reap_slack_seconds", "enable_saq_ui", "control_env")
        for field in (ControlSettings.model_fields[key],)
    ]
    table = env.get_template("admin/partials/_runtime_config_table.html").render(
        reloadable_keys=rows[:1], restart_only_keys=rows[1:], stale_override_keys=[]
    )
    assert "Point Phaze at your music" in empty
    assert "Reloadable runtime configuration" in table
    assert leaks(template_copy(empty + table)) == []


def fenced_blocks(name: str, language: str) -> list[str]:
    return re.findall(rf"(?m)^```{language}\n(.*?)^```", (ROOT / name).read_text(), re.DOTALL)


def test_evaluation_machine_references_are_real_and_not_tracking_exemptions() -> None:
    for name in SUPPORT_IDENTIFIERS:
        contents = (ROOT / name).read_text()
        for reference in re.findall(r"`(docs/[^`]+\.(?:json|md))`", contents):
            assert (ROOT / reference).is_file(), reference
        for line in contents.splitlines():
            if not line.startswith("```"):
                assert line.count("`") % 2 == 0, line
        assert leaks(document_copy(name, "The phaze-iq64v requirement")) == ["phaze-iq64v"]
        assert leaks(document_copy(name, "/unapproved/phaze-iq64v/code/start-analysis.sh")) == ["phaze-iq64v"]
    blocks = fenced_blocks("docs/analysis-evaluation-upcloud-runbook.md", "bash")
    commands = [shlex.split(block.replace("\\\n", " "), comments=True) for block in blocks]
    assert commands[0][-1] == "/home/datum/phaze-iq64v/code/start-analysis.sh"
    for command in commands[1:]:
        assert "/home/datum/phaze-iq64v/code/run-candidate-v2.sh" in command
        assert "/home/datum/phaze-iq64v/candidate-models" in command


async def test_documented_pagination_calls_execute(session: AsyncSession) -> None:
    example = next(block for block in fenced_blocks("docs/architecture.md", "python") if "stmt = paged_stmt(" in block)
    # The documented ellipsis stands for the caller's filter; use a real filter and model.
    example = example.replace(".where(...)", ".where(Thing.id.is_not(None))")
    namespace = {
        "paged_stmt": paged_stmt,
        "split_sentinel": split_sentinel,
        "select": select,
        "Thing": FileRecord,
        "page": 1,
        "page_size": 10,
        "session": session,
    }
    exec(  # noqa: S102 -- execute the repository-owned documentation example
        compile(
            "async def documented_page():\n" + "".join(f"    {line}\n" for line in example.splitlines()) + "    return rows, has_next\n",
            "<documentation pagination>",
            "exec",
        ),
        namespace,
    )
    assert await namespace["documented_page"]() == ([], False)


async def test_documented_sql_onboarding_and_quarantine_execute(session: AsyncSession) -> None:
    # Temporary shadows exercise the exact documented statements without touching application rows.
    await session.execute(
        sql_text("CREATE TEMP TABLE agents (id text, name text, token_hash text, scan_roots jsonb, created_at timestamptz) ON COMMIT DROP")
    )
    onboarding = next(block for block in fenced_blocks("docs/deployment.md", "sql") if "INSERT INTO agents" in block)
    await session.execute(sql_text(onboarding))
    created_at, token_hash = (await session.execute(sql_text("SELECT created_at, token_hash FROM agents"))).one()
    assert created_at is not None and len(token_hash) == 64
    await session.execute(
        sql_text("CREATE TEMP TABLE companion_junk_review (id uuid, status text, updated_at timestamptz, executed_at timestamptz) ON COMMIT DROP")
    )
    await session.execute(sql_text("CREATE TEMP TABLE saq_jobs (key text) ON COMMIT DROP"))
    await session.execute(sql_text("CREATE TEMP TABLE scheduling_ledger (key text) ON COMMIT DROP"))
    review_id = uuid.uuid4()
    await session.execute(sql_text("INSERT INTO companion_junk_review (id, status) VALUES (:id, 'executing')"), {"id": review_id})
    recovery = next(block for block in fenced_blocks("docs/runbook.md", "sql") if "UPDATE companion_junk_review AS review" in block)
    statement = re.search(r"UPDATE companion_junk_review AS review\n.*?RETURNING id, status;", recovery, re.DOTALL)
    assert statement is not None
    sql = statement[0].replace(":'review_id'::uuid", "CAST(:review_id AS uuid)")
    row = (await session.execute(sql_text(sql), {"review_id": review_id})).one()
    assert row == (review_id, "approved")
    assert await session.scalar(sql_text("SELECT updated_at FROM companion_junk_review")) is not None


@pytest.mark.parametrize(
    ("name", "expression"),
    [
        ("docs/architecture.md", "phaze.logging_config.configure_logging()"),
        ("docs/configuration.md", "derive_sizing().concurrency"),
        ("docs/database.md", "op.get_context().autocommit_block()"),
        ("docs/telemetry/concurrent-identity.md", "sum(increase())"),
    ],
)
def test_inline_callable_references_preserve_call_syntax(name: str, expression: str) -> None:
    assert f"`{expression}`" in (ROOT / name).read_text()
    assert any(isinstance(node, ast.Call) for node in ast.walk(ast.parse(expression, mode="eval")))


def test_agent_config_wire_header_is_an_exact_machine_identifier() -> None:
    # Same assignment shape as schemas/agent_config.py in the reviewed config assembly.
    declaration = 'CONFIG_VERSION_HEADER = "X-Phaze-Config-Version"'
    assert [(literal, leaks(literal)) for literal in authored_literals(declaration)] == [("X-Phaze-Config-Version", [])]
    assert leaks("Send X-Phaze-Config-Version: 2 to negotiate typed policy") == []
    assert leaks("X-Phaze-Config-Version-Extra") == ["Phaze-Config"]
    assert leaks("The phaze-config requirement") == ["phaze-config"]


def test_config_integration_preserves_reviewed_live_policy_semantics() -> None:
    guide = (ROOT / "docs/configuration.md").read_text()
    reload_paragraph = next(line for line in guide.splitlines() if line.startswith("> **Superseded in 2026.7.1:**"))
    assert "remain supported environment inputs; the reload timing above identifies which controls apply without restart" in reload_paragraph
    batch_row = next(line for line in guide.splitlines() if line.startswith("| `PHAZE_ANALYSIS_TF_BATCH_SIZE` |"))
    assert "11 in the current default strategy; 33 in the full ensemble" in batch_row
    assert "exported process environment establishes the host ceiling" in batch_row
    assert "live `analysis_tf_batch_size` policy may lower it for new children" in batch_row
    assert "Raising beyond that ceiling requires a deployment change and restart" in batch_row
