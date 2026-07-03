"""Regression tests for the CI doc-only change-gate classifier (Phase 63-04, CI-04 / D-09).

``scripts/classify-changed-files.sh`` decides whether a PR's changed files are
documentation-only (skip the heavy security/test/docker jobs) or contain code (run
the full pipeline). ``ci.yml``'s ``detect-changes`` job delegates to it via
``just detect-code-changes``; ``aggregate-results`` then reports SUCCESS on a
doc-only change so branch protection stays satisfied (skip-with-success).

These tests feed crafted changed-file lists through the *real* script over a
subprocess (the same interface CI uses) and assert the printed ``code-changed``
value. The load-bearing case is :func:`test_mixed_doc_and_code_is_conservative`:
a change set mixing docs with a single ``.py`` file MUST classify as
``code-changed=true``. That is the security property (T-63-04-01) — a code change
can never ride a doc-only skip past the security scans — so it gets an explicit,
non-parametrised positive test rather than hiding in a table.

An empty or whitespace-only file list is treated as ``code-changed=true`` (fail
safe): a spurious-empty diff (e.g. a broken diff base) must never silently skip
CI. ``code-changed=false`` is reserved for "at least one path changed, all docs".
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


# tests/shared/test_change_gate.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLASSIFIER = _REPO_ROOT / "scripts" / "classify-changed-files.sh"


def _classify(changed_files: str) -> str:
    """Run the real classifier with ``changed_files`` on stdin, return its stdout line.

    ``changed_files`` is the newline-delimited path list exactly as ``ci.yml`` pipes
    ``${CHANGED_FILES}`` into ``just detect-code-changes``.
    """
    assert _CLASSIFIER.is_file(), f"classifier script missing: {_CLASSIFIER}"
    result = subprocess.run(  # noqa: S603 - fixed, in-repo executable; input is a test literal
        [str(_CLASSIFIER)],
        input=changed_files,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("changed_files", "expected"),
    [
        # Doc-only shapes (at least one changed path, all docs) -> skip.
        (".planning/STATE.md\n.planning/phases/63/63-04-PLAN.md\n", "code-changed=false"),
        ("LICENSE\n", "code-changed=false"),
        ("docs/architecture.md\ndocs/notes.txt\nrelease-notes.txt\n", "code-changed=false"),
        ("README.md\nsrc/phaze/foo.md\n", "code-changed=false"),
        # Code shapes -> run the full pipeline (code-changed=true).
        ("src/phaze/main.py\n", "code-changed=true"),
        (".github/workflows/ci.yml\n", "code-changed=true"),
        ("pyproject.toml\n", "code-changed=true"),
        # Empty / whitespace-only file list -> fail safe, run everything (WR-01).
        # A spurious-empty diff must never silently skip CI.
        ("", "code-changed=true"),
        ("\n\n", "code-changed=true"),
        ("   \n", "code-changed=true"),
    ],
)
def test_classifier_maps_change_sets(changed_files: str, expected: str) -> None:
    """Doc-only change sets skip; any code/workflow/config path runs the full pipeline."""
    assert _classify(changed_files) == expected


def test_mixed_doc_and_code_is_conservative() -> None:
    """A change set mixing docs with a single .py MUST classify as code (T-63-04-01).

    This is the security invariant: a code change can never be hidden behind a
    doc-only skip and thereby bypass the security/test/docker jobs. The positive
    assertion here proves the conservative classifier is not vacuously permissive.
    """
    assert _classify(".planning/STATE.md\nLICENSE\nsrc/phaze/pipeline.py\n") == "code-changed=true"
