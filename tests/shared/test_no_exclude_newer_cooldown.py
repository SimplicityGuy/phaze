"""phaze-d4eiq: guard against `exclude-newer` reappearing in `[tool.uv]`.

Operator decision 2026-08-03 removed the `[tool.uv] exclude-newer` cooldown from
``pyproject.toml`` on purpose (see the ``# nosemgrep`` comment and rationale directly
above ``[tool.uv]``) — the ``litellm`` version-cap comment in the same file documents
that its exact-minor pin is now the ONLY control on that package's supply-chain risk,
so the cooldown's absence is load-bearing, not an oversight.

The key has reappeared THREE times through three different routes before this guard
existed: an epic branch, a merge artifact (PR #406), and ``scripts/update-project.sh``
re-inserting a zero-length ``"0 days"`` window on every run. This test is the fourth
guard: it fails loudly, citing the decision, the moment the key reappears by ANY route
— a hand-edit, a merge, or a future regression in the updater script.

DB-free and subprocess-free: parses ``pyproject.toml`` as TOML. Lives in
``tests/shared/`` so it rides the ``shared`` bucket (see ``test_partition_guard.py``
for why bucket placement matters).
"""

from __future__ import annotations

from pathlib import Path
import tomllib


# tests/shared/test_no_exclude_newer_cooldown.py -> parents[2] == repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"


def test_no_exclude_newer_cooldown_in_tool_uv() -> None:
    """`[tool.uv]` must never carry `exclude-newer` — operator decision 2026-08-03 (phaze-d4eiq)."""
    assert _PYPROJECT_PATH.is_file(), f"missing {_PYPROJECT_PATH}"
    data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    tool_uv = data.get("tool", {}).get("uv", {})
    assert "exclude-newer" not in tool_uv, (
        f"pyproject.toml [tool.uv] carries `exclude-newer` = {tool_uv.get('exclude-newer')!r}. "
        "Operator decision 2026-08-03 removed this cooldown deliberately (see the `# nosemgrep` "
        "comment in [tool.uv]); it has already reverted itself three times (phaze-d4eiq) via a "
        "branch, a merge artifact, and scripts/update-project.sh. Do not re-add it — if the "
        "decision changes, update the [tool.uv] comment and this test together."
    )


def test_nosemgrep_suppression_documents_the_operator_decision() -> None:
    """The paired `# nosemgrep` suppression comment must stay in `[tool.uv]`.

    Direct verification (2026-08-09, `semgrep scan --config auto`) against this file
    shows the `uv-missing-dependency-cooldown` registry rule does not currently fire on
    this file's `[tool.uv]` shape whether or not the suppression comment is present —
    so the comment is not load-bearing for CI today. It stays anyway: it is the durable,
    human-readable record of the 2026-08-03 decision, and removing it would strip that
    context the next time someone edits `[tool.uv]` or the rule's matching behavior
    changes upstream.
    """
    text = _PYPROJECT_PATH.read_text(encoding="utf-8")
    assert "nosemgrep: package_managers.uv.uv-missing-dependency-cooldown" in text, (
        "the `# nosemgrep` suppression comment for the uv dependency-cooldown rule is missing "
        "from pyproject.toml's [tool.uv] table — it documents operator decision 2026-08-03 "
        "(phaze-d4eiq) and must stay even though the registry rule does not currently fire "
        "against this file's [tool.uv] shape either way."
    )
    assert "Operator decision 2026-08-03" in text, "the [tool.uv] comment must keep citing the 2026-08-03 operator decision by date"
