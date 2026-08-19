"""CI workflow wiring guard (Phase 63-02/63-03, CI-02 matrix wiring + CI-03 gate deferral).

Two Phase 63 invariants are structurally fragile and had NO automated guard before this
file: the parallel-CI matrix staying wired to the canonical shard list (CI-02), and the
combine/gate deferral protocol that makes the fan-out possible at all (CI-03).

**CI-03 is the highest-value assertion here.** The `just test-bucket` recipe MUST defer
pytest-cov's `fail_under` gate (`--cov-fail-under=0`) because a single shard only
exercises a fraction of ``phaze`` — enforcing the pyproject-wide global coverage gate against
one shard's PARTIAL coverage fails every matrix leg (exit 1) before its shard is uploaded,
which starves the ``combine`` job (``needs: [test]``) of any input and the whole gate
never runs. **This exact regression already happened once during this phase** — the
verifier caught every matrix leg exiting 1 for precisely this reason before the fix
landed. ``test_bucket_recipe_defers_the_coverage_gate`` below is a unit-speed tripwire
for that regression: it reads the ``test-bucket`` recipe body directly out of the
justfile and fails loud if ``--cov-fail-under=0`` is ever dropped.

The remaining tests assert the rest of the combine/gate protocol (the global coverage gate
is enforced exactly once, on the COMBINED number) and the CI-02 matrix-to-``ci_shards.json``
wiring (the matrix is derived via ``fromJSON`` of the setup job's output — not a
hardcoded, driftable shard list inline in the workflow — and the token used for the
Codecov upload never leaks into a per-shard matrix leg).

phaze-crq9k split the flat ``tests/buckets.json`` bucket-name list (still the single source
of truth for the ``tests/<bucket>/`` DIRECTORY partition the ``test_partition_guard.py``
guard enforces) from a NEW ``tests/ci_shards.json`` — the CI matrix source. Every shard is a
``{"name": ..., "paths": ...}`` object; most shards map 1:1 onto a ``tests/buckets.json``
bucket directory, but a bucket whose wall time dominates the matrix (``analyze``, ``shared``
as of this split) can be represented by SEVERAL shards, each a space-separated subset of
that bucket's subdirectories. Splitting the file (rather than overloading buckets.json's
shape) keeps the partition guard's directory-membership invariant untouched.

This guard is DB-free and subprocess-free: it parses ``justfile`` as text and
``.github/workflows/tests.yml`` as YAML. It lives in ``tests/shared/`` so it rides the
``shared-rest`` shard (see ``test_partition_guard.py`` for why bucket placement matters).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
from typing import Any

import yaml


# tests/shared/test_ci_workflow_wiring.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _REPO_ROOT / "justfile"
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "tests.yml"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_BUCKETS_JSON = _REPO_ROOT / "tests" / "buckets.json"
_CI_SHARDS_JSON = _REPO_ROOT / "tests" / "ci_shards.json"


def _extract_recipe(justfile_text: str, name: str) -> str:
    """Return the indented body of the top-level `just` recipe `name`.

    `just` recipe headers start at column 0 (optionally followed by parameters, then a
    trailing colon); the recipe body is every following line indented with whitespace.
    Anchoring the header match to the start of a line (``re.MULTILINE`` + ``^``) is
    load-bearing: it is what stops a recipe *name* merely mentioned inside a comment
    (e.g. the backtick-quoted ``coverage-combine`` reference in the ``test-bucket``
    doc comment) from being mistaken for the recipe's own header.
    """
    pattern = re.compile(rf"^{re.escape(name)}\b[^\n]*:\n((?:[ \t]+.*\n?)*)", re.MULTILINE)
    match = pattern.search(justfile_text)
    assert match is not None, f"recipe {name!r} not found as a top-level header in {_JUSTFILE}"
    return match.group(1)


def _load_workflow() -> dict[str, Any]:
    assert _WORKFLOW_PATH.is_file(), f"missing workflow: {_WORKFLOW_PATH}"
    loaded: dict[str, Any] = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    return loaded


def _find_codecov_token_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every step in `job` whose text representation contains CODECOV_TOKEN."""
    hits: list[dict[str, Any]] = []
    for step in job.get("steps", []):
        if "CODECOV_TOKEN" in json.dumps(step):
            hits.append(step)
    return hits


def _browser_job() -> dict[str, Any]:
    """Return the non-blocking real-browser job from the canonical test workflow."""
    return _load_workflow()["jobs"]["browser"]


def _find_run_step(job: dict[str, Any], needle: str) -> dict[str, Any]:
    """Return the unique run step containing ``needle``."""
    hits = [step for step in job["steps"] if needle in step.get("run", "")]
    assert len(hits) == 1, f"expected one browser step containing {needle!r}, found {len(hits)}"
    return hits[0]


def test_bucket_recipe_defers_the_coverage_gate() -> None:
    """`just test-bucket` MUST pass --cov-fail-under=0 (CI-03, the regression tripwire).

    Without this flag, pytest-cov enforces pyproject's global fail_under gate against a single
    bucket's PARTIAL coverage, failing every matrix leg before its shard is uploaded —
    this happened once already this phase (verifier-caught) and starved `combine`.
    """
    recipe_body = _extract_recipe(_JUSTFILE.read_text(encoding="utf-8"), "test-bucket")
    assert "--cov-fail-under=0" in recipe_body, f"test-bucket recipe lost its gate deferral:\n{recipe_body}"
    # Sanity: the recipe still runs pytest against an explicit, caller-supplied shard path
    # (not vacuous). phaze-crq9k: PATHS is now a parameter (one or more `tests/...` paths
    # from tests/ci_shards.json) rather than always synthesizing `tests/<NAME>`, since a
    # single bucket directory can be split across several shards.
    assert "pytest {{PATHS}}" in recipe_body, f"test-bucket recipe must run pytest against the PATHS param:\n{recipe_body}"


def test_bucket_recipe_records_per_test_coverage_contexts() -> None:
    """Every binary coverage shard retains the contexts required by repowise.

    Cobertura XML carries per-file totals but cannot represent pytest's per-test execution
    contexts. The binary ``.coverage.<bucket>`` shards are therefore the only CI output that can
    build repowise's test-to-code map after ``coverage combine``. Keep the flag in the shared
    recipe so every matrix leg records compatible context-bearing data.
    """
    recipe_body = _extract_recipe(_JUSTFILE.read_text(encoding="utf-8"), "test-bucket")
    assert "--cov-context=test" in recipe_body, f"test-bucket recipe lost per-test coverage contexts:\n{recipe_body}"


def test_coverage_combine_recipe_enforces_the_gate_exactly_once() -> None:
    """`coverage-combine` merges shards and enforces the global gate on the COMBINED number.

    The per-bucket deferral in test-bucket only makes sense if something enforces the
    real gate afterward. This recipe is that "afterward": combine -> xml -> json ->
    report --fail-under, run once against the merged coverage data. Phase 64 raised the
    gate above the 90.38% baseline; test_coverage_gate.py owns the exact-value invariant,
    so here we only assert the recipe still enforces a global fail-under gate at all.
    """
    recipe_body = _extract_recipe(_JUSTFILE.read_text(encoding="utf-8"), "coverage-combine")
    assert "coverage combine" in recipe_body
    assert "coverage xml" in recipe_body
    assert re.search(r"coverage report --fail-under=\d+", recipe_body) is not None


def test_codecov_token_is_confined_to_the_combine_job() -> None:
    """CODECOV_TOKEN is used exactly once, in the combine job — never in a matrix leg.

    Uploading per-leg would be wasteful and wrong (9 partial-coverage uploads instead
    of 1 combined one); it would also mean the secret is exposed to every matrix leg
    instead of only the single post-fan-in job that needs it.
    """
    workflow_text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    # The single `CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}` line legitimately contains
    # the substring twice (the env var name, then the secret reference) - so 2 total
    # occurrences in the whole file is the "appears exactly once" invariant, not 1.
    # A third+ occurrence would mean a second env/step referencing the token elsewhere.
    assert workflow_text.count("CODECOV_TOKEN") == 2, (
        "CODECOV_TOKEN must appear on exactly one line in tests.yml (the combine job's upload step only)"
    )

    workflow = _load_workflow()
    jobs = workflow["jobs"]

    test_job_hits = _find_codecov_token_steps(jobs["test"])
    assert not test_job_hits, f"CODECOV_TOKEN leaked into a per-bucket matrix leg: {test_job_hits}"

    combine_job_hits = _find_codecov_token_steps(jobs["combine"])
    assert len(combine_job_hits) == 1, f"expected exactly one CODECOV_TOKEN-bearing step in combine, found {len(combine_job_hits)}"
    (codecov_step,) = combine_job_hits
    assert "codecov/codecov-action" in codecov_step.get("uses", ""), codecov_step


def test_combine_job_downloads_shards_and_runs_the_combine_recipe() -> None:
    """`combine` fans in every bucket's shard artifact then delegates to `just coverage-combine`.

    This is the other half of CI-03: a single combined upload requires the combine job
    to actually download every `coverage-*` artifact (not just the last one) before
    running the recipe that merges + gates them.
    """
    jobs = _load_workflow()["jobs"]
    combine_job = jobs["combine"]
    steps = combine_job["steps"]

    download_steps = [s for s in steps if "download-artifact" in s.get("uses", "")]
    assert len(download_steps) == 1, f"expected exactly one artifact-download step in combine, found {len(download_steps)}"
    download_with = download_steps[0].get("with", {})
    assert download_with.get("pattern") == "coverage-*", download_with
    assert download_with.get("merge-multiple") is True, download_with

    run_steps = [s.get("run", "") for s in steps if "run" in s]
    assert any(run.strip() == "just coverage-combine" for run in run_steps), run_steps

    # combine must wait on the full matrix fan-out, not run ahead of shard uploads.
    assert combine_job["needs"] == ["test"]


def test_combine_job_exports_both_repowise_coverage_inputs() -> None:
    """The run artifact exposes the contextual map and the per-file totals together.

    CI must not ingest into its ephemeral repowise index. It does need to preserve the combined
    binary report (per-test contexts) alongside ``coverage.xml`` (per-file totals), so a human can
    download a commit-paired report while the durable local refresh remains
    ``just repowise-coverage <seat>``.
    """
    combine_steps = _load_workflow()["jobs"]["combine"]["steps"]
    uploads = [step for step in combine_steps if "actions/upload-artifact" in step.get("uses", "")]
    combined_uploads = [step for step in uploads if step.get("with", {}).get("name") == "coverage-combined"]
    assert len(combined_uploads) == 1, f"expected one coverage-combined artifact upload, found: {combined_uploads}"

    upload_with = combined_uploads[0]["with"]
    uploaded_paths = set(upload_with["path"].splitlines())
    assert uploaded_paths == {".coverage", "coverage.xml"}, upload_with
    assert upload_with.get("include-hidden-files") is True, upload_with
    assert upload_with.get("if-no-files-found") == "error", upload_with


def test_matrix_bucket_list_is_derived_via_fromjson_not_hardcoded() -> None:
    """CI-02: the test job's matrix comes from setup's buckets output, not an inline list.

    A hardcoded bucket array in the matrix could silently drift from buckets.json (add
    a bucket to the json without ever adding it to the matrix -> a whole shard's worth
    of coverage silently stops running). Deriving the matrix via fromJSON of the setup
    job's output makes drift structurally impossible.
    """
    jobs = _load_workflow()["jobs"]
    test_job = jobs["test"]

    strategy = test_job["strategy"]
    assert strategy["fail-fast"] is False, "fail-fast must stay disabled so one bucket's failure doesn't hide others"

    matrix_bucket_expr = strategy["matrix"]["bucket"]
    assert matrix_bucket_expr == "${{ fromJSON(needs.setup.outputs.buckets) }}", (
        f"matrix.bucket must be sourced via fromJSON(needs.setup.outputs.buckets), got: {matrix_bucket_expr!r}"
    )
    assert test_job["needs"] == ["setup"], "test job must depend on setup to receive the buckets output"


def test_setup_job_reads_the_canonical_ci_shards_json() -> None:
    """CI-02: setup's `buckets` output is read from tests/ci_shards.json, the single source
    of truth also consumed by `just test-bucket` — never a copy hardcoded into the workflow.

    phaze-crq9k: this is deliberately `tests/ci_shards.json`, NOT `tests/buckets.json` (the
    latter stays the partition guard's fixed 8-directory-name list — see
    `test_partition_guard.py`). The two are related but distinct: every shard's `paths`
    must resolve under a known bucket directory, checked below.
    """
    jobs = _load_workflow()["jobs"]
    setup_job = jobs["setup"]

    assert setup_job["outputs"]["buckets"] == "${{ steps.buckets.outputs.buckets }}"

    run_steps = [s.get("run", "") for s in setup_job["steps"] if "run" in s]
    assert any("tests/ci_shards.json" in run for run in run_steps), (
        f"setup job must read tests/ci_shards.json (single source of truth), steps were: {run_steps}"
    )

    assert _CI_SHARDS_JSON.is_file(), f"missing canonical CI shard list: {_CI_SHARDS_JSON}"
    shards = json.loads(_CI_SHARDS_JSON.read_text(encoding="utf-8"))
    assert shards, "ci_shards.json parsed to an empty list"

    names = [shard["name"] for shard in shards]
    assert len(names) == len(set(names)), f"duplicate shard names in ci_shards.json: {names}"

    # Every shard's paths must be non-empty and resolve under a bucket the partition guard
    # actually knows about (drift-proofing the drift-proofer): a shard path of
    # "tests/shared/routers" or "tests/shared --ignore=tests/shared/core" both key off the
    # `shared` bucket; a typo'd bucket segment here would silently stop running real tests.
    assert _BUCKETS_JSON.is_file(), f"missing canonical bucket list: {_BUCKETS_JSON}"
    known_buckets = set(json.loads(_BUCKETS_JSON.read_text(encoding="utf-8")))
    for shard in shards:
        assert shard.get("paths"), f"shard {shard.get('name')!r} has empty paths"
        for token in shard["paths"].split():
            if token.startswith("--"):
                # A pytest flag (e.g. --ignore=tests/shared/core), not a collection path.
                continue
            assert token.startswith("tests/"), f"shard {shard['name']!r} path {token!r} must start with tests/"
            bucket = token.removeprefix("tests/").split("/")[0]
            assert bucket in known_buckets, f"shard {shard['name']!r} path {token!r} references unknown bucket {bucket!r}"


def test_browser_toolchain_is_exactly_pinned_in_an_optional_group() -> None:
    """The browser runner cannot float independently of the reviewed lockfile."""
    pyproject = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    assert pyproject["dependency-groups"]["browser"] == ["playwright==1.62.0"]

    justfile_text = _JUSTFILE.read_text(encoding="utf-8")
    install_recipe = _extract_recipe(justfile_text, "test-browser-install")
    ci_install_recipe = _extract_recipe(justfile_text, "test-browser-install-ci")
    test_recipe = _extract_recipe(justfile_text, "test-browser")
    for recipe in (install_recipe, ci_install_recipe, test_recipe):
        assert "--group browser" in recipe
        assert "--with playwright" not in recipe


def test_browser_ci_installs_headless_shell_without_apt() -> None:
    """Hosted CI must never enter Playwright's unconditional apt update path."""
    browser_job = _browser_job()
    browser_job_text = json.dumps(browser_job)
    assert browser_job["runs-on"] == "ubuntu-latest"
    assert "apt-get" not in browser_job_text
    assert "--with-deps" not in browser_job_text

    justfile_text = _JUSTFILE.read_text(encoding="utf-8")
    local_recipe = _extract_recipe(justfile_text, "test-browser-install")
    ci_recipe = _extract_recipe(justfile_text, "test-browser-install-ci")
    assert "playwright install --with-deps chromium" in local_recipe
    assert "playwright install --only-shell chromium" in ci_recipe


def test_browser_cache_key_tracks_locked_toolchain_platform_and_payload() -> None:
    """An incompatible browser build must not be restored across toolchain/platform changes."""
    browser_job = _browser_job()
    cache_steps = [step for step in browser_job["steps"] if "actions/cache" in step.get("uses", "")]
    assert len(cache_steps) == 1
    cache_step = cache_steps[0]
    assert cache_step["id"] == "chromium-cache"
    assert cache_step["with"]["key"] == ("playwright-${{ runner.os }}-${{ runner.arch }}-${{ steps.playwright.outputs.version }}-headless-shell")

    version_step = _find_run_step(browser_job, "playwright --version")
    assert "uv run --group browser" in version_step["run"]


def test_browser_install_timeout_preserves_the_real_exit_status() -> None:
    """The install is bounded and distinguishes timeout 124 from other failures."""
    install_step = _find_run_step(_browser_job(), "just test-browser-install-ci")
    run = install_step["run"]
    assert "timeout --kill-after=5s 300 just test-browser-install-ci" in run
    assert "status=$?" in run
    assert "if ! timeout" not in run
    assert 'if [ "${status}" -eq 124 ]' in run
    assert 'exit "${status}"' in run


def test_browser_launch_smoke_precedes_the_contract_suite() -> None:
    """A bounded real launch separates browser infrastructure failures from test failures."""
    browser_job = _browser_job()
    steps = browser_job["steps"]
    smoke_step = _find_run_step(browser_job, "playwright.chromium.launch()")
    suite_steps = [step for step in steps if step.get("run", "").strip() == "just test-browser"]
    assert len(suite_steps) == 1
    (suite_step,) = suite_steps
    assert "timeout --kill-after=5s 30" in smoke_step["run"]
    assert steps.index(smoke_step) < steps.index(suite_step)
