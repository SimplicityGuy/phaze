"""CI workflow wiring guard (Phase 63-02/63-03, CI-02 matrix wiring + CI-03 gate deferral).

Two Phase 63 invariants are structurally fragile and had NO automated guard before this
file: the parallel-CI matrix staying wired to the canonical bucket list (CI-02), and the
combine/gate deferral protocol that makes the fan-out possible at all (CI-03).

**CI-03 is the highest-value assertion here.** The `just test-bucket` recipe MUST defer
pytest-cov's `fail_under` gate (`--cov-fail-under=0`) because a single bucket only
exercises a fraction of ``phaze`` — enforcing the pyproject-wide 85% gate against one
bucket's PARTIAL coverage fails every matrix leg (exit 1) before its shard is uploaded,
which starves the ``combine`` job (``needs: [test]``) of any input and the whole gate
never runs. **This exact regression already happened once during this phase** — the
verifier caught every matrix leg exiting 1 for precisely this reason before the fix
landed. ``test_bucket_recipe_defers_the_coverage_gate`` below is a unit-speed tripwire
for that regression: it reads the ``test-bucket`` recipe body directly out of the
justfile and fails loud if ``--cov-fail-under=0`` is ever dropped.

The remaining tests assert the rest of the combine/gate protocol (the 85% gate is
enforced exactly once, on the COMBINED number) and the CI-02 matrix-to-``buckets.json``
wiring (the matrix is derived via ``fromJSON`` of the setup job's output — not a
hardcoded, driftable bucket list inline in the workflow — and the token used for the
Codecov upload never leaks into a per-bucket matrix leg).

This guard is DB-free and subprocess-free: it parses ``justfile`` as text and
``.github/workflows/tests.yml`` as YAML. It lives in ``tests/shared/`` so it rides the
``shared`` bucket (see ``test_partition_guard.py`` for why bucket placement matters).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import yaml


# tests/shared/test_ci_workflow_wiring.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _REPO_ROOT / "justfile"
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "tests.yml"
_BUCKETS_JSON = _REPO_ROOT / "tests" / "buckets.json"


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


def test_bucket_recipe_defers_the_coverage_gate() -> None:
    """`just test-bucket` MUST pass --cov-fail-under=0 (CI-03, the regression tripwire).

    Without this flag, pytest-cov enforces pyproject's fail_under=85 against a single
    bucket's PARTIAL coverage, failing every matrix leg before its shard is uploaded —
    this happened once already this phase (verifier-caught) and starved `combine`.
    """
    recipe_body = _extract_recipe(_JUSTFILE.read_text(encoding="utf-8"), "test-bucket")
    assert "--cov-fail-under=0" in recipe_body, f"test-bucket recipe lost its gate deferral:\n{recipe_body}"
    # Sanity: the recipe still runs pytest against a bucket-scoped path (not vacuous).
    assert "pytest tests/" in recipe_body


def test_coverage_combine_recipe_enforces_the_gate_exactly_once() -> None:
    """`coverage-combine` merges shards and enforces the 85% gate on the COMBINED number.

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


def test_setup_job_reads_the_canonical_buckets_json() -> None:
    """CI-02: setup's `buckets` output is read from tests/buckets.json, the single source
    of truth also consumed by `just test-bucket` and the partition guard — never a
    copy hardcoded into the workflow.
    """
    jobs = _load_workflow()["jobs"]
    setup_job = jobs["setup"]

    assert setup_job["outputs"]["buckets"] == "${{ steps.buckets.outputs.buckets }}"

    run_steps = [s.get("run", "") for s in setup_job["steps"] if "run" in s]
    assert any("tests/buckets.json" in run for run in run_steps), (
        f"setup job must read tests/buckets.json (single source of truth), steps were: {run_steps}"
    )

    # And that source of truth must actually be the 9-bucket canonical list the
    # matrix and `just test-bucket` both key off of (drift-proofing the drift-proofer).
    assert _BUCKETS_JSON.is_file(), f"missing canonical bucket list: {_BUCKETS_JSON}"
    canonical_buckets = json.loads(_BUCKETS_JSON.read_text(encoding="utf-8"))
    assert len(canonical_buckets) == 9
