"""Guard: an ``asyncio.gather`` of HTTP requests inside a test holding the hermetic ``session``
fixture must cite ``docs/design/0015-shared-session-gather.md`` (phaze-7w9dv).

Cited by filename throughout this module, deliberately, never by a bare "ADR-00NN" number: a
renumbered ADR leaves a bare-number reference resolving SILENTLY to the wrong document (0014 is
``0014-tracklist-candidate-sets.md``, unrelated), where a filename reference just breaks on a
rename -- loud, not silent. See phaze-x2z38 for the tree-wide sweep of the former.

THE SHAPE THIS CATCHES. ``docs/design/0015-shared-session-gather.md`` (phaze-4tch9) establishes
that ``asyncio.gather`` over one ``AsyncSession`` is unsupported (Fact A, upstream's documented
contract) and, on the pinned SQLAlchemy 2.0.52, silently serializes rather than raising (Fact B --
measured, not a licence). ``phaze-4tch9``'s sweep read all 28 ``asyncio.gather`` call sites in
``src/`` and ``tests/`` and found exactly ONE gathering HTTP requests through a client built over
the single-connection, per-test ``session`` fixture:
``tests/review/routers/test_agent_exec_batches.py::test_concurrent_sub_batch_terminals_keep_status_consistent_with_failed``.
phaze-7w9dv re-checked that count (see the bead comment) and confirmed it. This guard mechanizes
the check so a NEW site cannot reintroduce the shape silently -- CLAUDE.md rule 5 (a lesson states
its general form) -- while still allowing the one known, deliberately-retained site: phaze-7w9dv's
disposition was RETENTION with an explicit citation, not a fixture rewrite (see the comment at
that test's call site), so the guard requires the citation rather than forbidding the shape
outright.

WHY ``session`` (THE PARAMETER NAME) IS THE SIGNAL, NOT ``lambda: session`` (THE OVERRIDE TEXT).
An earlier draft of this detector scanned for the literal override text ``lambda: session``
co-occurring with ``asyncio.gather`` in the same file. That over-matches: five OTHER files pair
those two substrings (``test_agent_scan_batches.py``, ``test_agent_tag_writes.py``,
``test_agent_execution.py``, ``test_agent_s3_concurrency.py``, ``test_agent_push_concurrency.py``)
because each has a module-level smoke-app helper used by its OTHER, non-concurrent tests, while
its actual concurrency test builds an independent app locally (either a fresh session per request
via a generator override, or one client per branch off the real-PG ``committed_db`` fixture) and
therefore does NOT take the ``session`` fixture as a parameter at all. Checking the CONTAINING
TEST FUNCTION's own parameter list instead of file-wide text co-occurrence removes that false-
positive class entirely: in this codebase's convention, ``session`` (never ``session_a``,
``session_b``, nor a fixture named ``committed_db``/``async_engine``) is the hermetic, single
``_db_connection``-bound fixture (``tests/conftest.py::session``), and it is the ONLY session an
in-scope test can hand to ``get_session`` without building its own engine/fixture. A test function
that gathers HTTP requests while holding that parameter has no way to give each request its own
session without a fixture change -- which is exactly the tradeoff phaze-7w9dv's citation names.
Verified by direct AST scan of the whole ``tests/`` tree (2026-08-24): exactly one function
matches "takes `session` as a parameter AND calls `asyncio.gather`/`gather` whose arguments
include an HTTP-verb call" -- the known site, and none of the five lookalikes above.

SCANNING SURFACE. Every ``tests/**/*.py`` file (this module excluded by path, the same way
``test_cluster_wide_catalog_scoping.py`` excludes itself). ``src/`` is not scanned: production
code has no ASGI test client, so the detector's HTTP-verb-call requirement cannot match there
regardless of parameter names -- verified directly (2026-08-24): 3 of the 10 ``src/`` gather sites
DO take a ``session`` parameter (``dashboard_stats.py::pipeline_stats_partial``,
``summary.py::_build_summary_context``, ``stages.py::get_stage_progress``), and all three are the
CLEAN pattern ``docs/design/0015-shared-session-gather.md`` section 4 names as the model to copy --
each gathered branch reads through its own session via ``_read_in_own_session``, not the outer
parameter, and none issues an HTTP call. The other three prose sites
(``proposal.py``/``cue_review.py``/``companion.py``) carry the
citation already and are not a gather at all.

CITATION TOKEN. The literal substring ``0015-shared-session-gather`` (the ADR's filename, no
extension) must appear anywhere in the flagged function's own source span -- comment or docstring,
before or after the gather call; unlike ``test_operator_attribution_citations.py``'s paragraph
window this token has no internal whitespace, so it cannot be split by a line wrap and needs no
wrap-tolerant normalization.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOT = REPO_ROOT / "tests"
_THIS_FILE = Path(__file__).resolve()

_HTTP_VERBS = frozenset({"post", "get", "put", "patch", "delete", "request"})
_CITATION_TOKEN = "0015-shared-session-gather"

# The known, deliberately-retained site (phaze-7w9dv). Recorded here, not to silence the guard for
# it, but so `test_the_known_site_carries_the_citation` can name it explicitly: this repo's
# convention (`test_cluster_wide_catalog_scoping.py`, `test_operator_attribution_citations.py`) is
# an allowlist that still requires the real evidence (the citation) at the site, never a bare skip.
_KNOWN_SITE = (
    "tests/review/routers/test_agent_exec_batches.py",
    "test_concurrent_sub_batch_terminals_keep_status_consistent_with_failed",
)


def _is_gather_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "gather") or (isinstance(func, ast.Name) and func.id == "gather")


def _is_http_verb_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _HTTP_VERBS


def _takes_session_param(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    params = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
    return "session" in params


def _gathers_http_requests(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if `node`'s body contains a gather call whose descendants include an HTTP-verb call.

    Walks the WHOLE function body rather than requiring the HTTP call to be a direct argument of
    the gather call, so both shapes the tree actually uses are caught: a generator expression
    (``gather(*(ac.post(...) for b in bodies))``, the known site) and literal positional arguments
    (``gather(ac_a.patch(...), ac_b.patch(...))``, the clean two-branch shape -- which this
    function alone would flag, and which `_takes_session_param` is what rules it out, since those
    tests never bind a fixture literally named ``session``).
    """
    gather_calls = (n for n in ast.walk(node) if _is_gather_call(n))
    return any(any(_is_http_verb_call(n) for n in ast.walk(gather_call)) for gather_call in gather_calls)


def _offending_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every top-level-or-nested function in `tree` matching the risky shape, regardless of citation."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _takes_session_param(node) and _gathers_http_requests(node):
            found.append(node)
    return found


def _uncited_offenders(text: str) -> list[str]:
    """Return ``"<lineno>:<name>"`` for every offending function in `text` lacking the citation token."""
    tree = ast.parse(text)
    offenders = []
    for node in _offending_functions(tree):
        span = ast.get_source_segment(text, node) or ""
        if _CITATION_TOKEN not in span:
            offenders.append(f"{node.lineno}:{node.name}")
    return offenders


def _sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.exists())


def _uncited_files(root: Path | None = None) -> dict[str, list[str]]:
    """Return ``{relative_path: [offenders]}`` for every scanned file with at least one uncited offender."""
    offenders: dict[str, list[str]] = {}
    for path in _sources(root if root is not None else SCANNED_ROOT):
        if path.resolve() == _THIS_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover -- no such file in this repo today
            continue
        try:
            found = _uncited_offenders(text)
        except SyntaxError:  # pragma: no cover -- a broken tests/ file is a separate, louder failure
            continue
        if found:
            try:
                label = str(path.relative_to(REPO_ROOT))
            except ValueError:
                label = str(path)
            offenders[label] = found
    return offenders


def test_no_test_gathers_http_requests_over_the_shared_session_fixture_without_citing_adr_0015() -> None:
    """The real guard. A hit means a NEW (or re-broken) site needs the phaze-7w9dv citation.

    Fix by adding a comment inside the offending test naming ``docs/design/0015-shared-session-
    gather.md`` and arguing the disposition -- retention (like the known site) or a fixture change
    that gives each gathered request its own session (per CLAUDE.md's acceptance-criteria rule 4,
    with the blast-radius statement that requires: how many tests share the fixture being changed).
    """
    offenders = _uncited_files()
    assert offenders == {}, f"gather-over-shared-session sites missing the docs/design/0015-shared-session-gather.md citation: {offenders}"


def test_the_known_site_still_carries_the_shape_and_the_citation() -> None:
    """Positive control: the known site is still findable by the detector (not vacuously exempt) and
    carries its citation -- so it does NOT appear in `_uncited_files()`'s output, distinct from
    "the detector never looked".
    """
    rel_path, func_name = _KNOWN_SITE
    text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    matches = [n for n in _offending_functions(tree) if n.name == func_name]
    assert matches, f"detector no longer recognises the known site {rel_path}::{func_name} -- guard may be vacuous"
    span = ast.get_source_segment(text, matches[0]) or ""
    assert _CITATION_TOKEN in span, (
        f"the known, deliberately-retained site lost its docs/design/0015-shared-session-gather.md citation: {rel_path}::{func_name}"
    )


def test_the_scan_reaches_the_tests_tree() -> None:
    """The detector must be looking at real files -- an over-tight scan would pass vacuously."""
    matched = {str(p.relative_to(REPO_ROOT)) for p in _sources(SCANNED_ROOT) if _offending_functions(ast.parse(p.read_text(encoding="utf-8")))}
    assert _KNOWN_SITE[0] in matched, f"{_KNOWN_SITE[0]} has the offending shape but the detector missed it"


def test_the_detector_flags_the_offending_shape_and_clears_it_once_cited() -> None:
    """Pin the shape/citation logic directly, independent of any file on disk."""
    uncited = """
async def test_something(session: AsyncSession, redis_client) -> None:
    async with _make_client(session, redis_client) as ac:
        responses = await asyncio.gather(*(ac.post(url, json=b) for b in bodies))
"""
    cited = """
async def test_something(session: AsyncSession, redis_client) -> None:
    # see docs/design/0015-shared-session-gather.md
    async with _make_client(session, redis_client) as ac:
        responses = await asyncio.gather(*(ac.post(url, json=b) for b in bodies))
"""
    assert _uncited_offenders(uncited) == ["2:test_something"]
    assert _uncited_offenders(cited) == []


def test_the_detector_does_not_flag_the_known_clean_shapes() -> None:
    """Regression pin for the five real lookalikes this detector must NOT flag.

    Each mirrors the actual shape of one of ``test_agent_scan_batches.py``,
    ``test_agent_tag_writes.py``, ``test_agent_execution.py``,
    ``test_agent_s3_concurrency.py`` and ``test_agent_push_concurrency.py``'s concurrency
    tests: no parameter literally named ``session`` (a per-branch ``session_a``/``session_b``, a
    bare ``async_engine``, or a real-PG ``committed_db`` fixture instead), even though each
    gathers HTTP requests. ``lambda: session`` may still appear elsewhere in the SAME real file
    (in a module-level helper used by other, non-concurrent tests) -- irrelevant here, because
    this detector reasons about one function's own parameter list, not file-wide text.
    """
    generator_override_shape = """
async def test_concurrent_patch_does_not_regress_terminal_status(async_engine) -> None:
    async def _override_session():
        async with factory() as s:
            yield s
    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(transport=ASGITransport(app=app)) as ac_a, AsyncClient(transport=ASGITransport(app=app)) as ac_b:
        r_a, r_b = await asyncio.gather(ac_a.patch(url, json=body_a), ac_b.patch(url, json=body_b))
"""
    per_branch_committed_db_shape = """
async def test_mismatch_concurrent_no_lost_update(committed_db, monkeypatch, backends_toml_env) -> None:
    async with session_factory() as session_a, session_factory() as session_b:
        client_a = _make_client(session_a, raw_token)
        client_b = _make_client(session_b, raw_token)
        async with client_a, client_b:
            resp_a, resp_b = await asyncio.gather(task_a, task_b)
"""
    assert _uncited_offenders(generator_override_shape) == []
    assert _uncited_offenders(per_branch_committed_db_shape) == []


def test_a_fixture_module_with_the_offending_shape_and_no_citation_fails_the_guard(tmp_path: Path) -> None:
    """RED proof at the `_uncited_files` (whole-guard) level, on a real file on disk."""
    fixture = tmp_path / "fixture_uncited_gather_over_session.py"
    fixture.write_text(
        "async def test_x(session, redis_client) -> None:\n"
        "    async with _make_client(session, redis_client) as ac:\n"
        "        r = await asyncio.gather(*(ac.post(url, json=b) for b in bodies))\n",
        encoding="utf-8",
    )

    offenders = _uncited_files(tmp_path)

    assert list(offenders.keys()) == [str(fixture)], f"the fixture's uncited gather-over-session must be flagged, got: {offenders}"
    assert offenders[str(fixture)] == ["1:test_x"]


def test_a_fixture_module_with_the_offending_shape_and_a_citation_passes_the_guard(tmp_path: Path) -> None:
    """GREEN proof: the same fixture, with the citation added, clears the guard.

    Together with the previous test this is the FAIL-before-PASS pair CLAUDE.md's acceptance
    rule 4 requires of any guard added under this bead: this exact fixture text failed the guard
    above, and adding one line -- the citation -- is what makes it pass, mirroring the real fix
    applied at the known site.
    """
    fixture = tmp_path / "fixture_cited_gather_over_session.py"
    fixture.write_text(
        "async def test_x(session, redis_client) -> None:\n"
        "    # docs/design/0015-shared-session-gather.md\n"
        "    async with _make_client(session, redis_client) as ac:\n"
        "        r = await asyncio.gather(*(ac.post(url, json=b) for b in bodies))\n",
        encoding="utf-8",
    )

    assert _uncited_files(tmp_path) == {}
