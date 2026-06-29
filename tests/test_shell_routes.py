"""Wave-1 collectible stubs for the v7.0 shell routes (SHELL-01..04).

These six test functions are declared here, body-less, so that the ``-k`` selectors
and ``::test_name`` node-ids referenced by later plans resolve against an existing,
collectible file the moment those plans run. (pytest given a missing file path exits
with a usage error before any body could be filled — seeding the stub now removes
that ordering hazard.)

Plan 57-02 (Task 3) and Plan 57-03 (Task 3) REPLACE the bodies below with the real
assertions; they do NOT redeclare these functions. Do not add fixtures, imports, or
``async`` here — the file must collect and pass trivially until then, and must not
add any template files (which would perturb the dead-template guard).

Function → requirement map (see 57-VALIDATION.md "Per-Task Verification Map"):
    test_root_renders_shell_analyze_default  → SHELL-01
    test_stage_fragment_is_bare              → SHELL-02
    test_rail_nodes_wired                    → SHELL-02
    test_unknown_stage_404                   → SHELL-02 (negative)
    test_tabbar_removed_header_present       → SHELL-03
    test_theme_and_store_preserved           → SHELL-04
"""


def test_root_renders_shell_analyze_default() -> None:
    """SHELL-01 — filled by Plan 57-02 Task 3."""
    ...


def test_stage_fragment_is_bare() -> None:
    """SHELL-02 — filled by Plan 57-02 Task 3."""
    ...


def test_unknown_stage_404() -> None:
    """SHELL-02 (negative) — filled by Plan 57-02 Task 3."""
    ...


def test_rail_nodes_wired() -> None:
    """SHELL-02 — filled by Plan 57-03 Task 3."""
    ...


def test_tabbar_removed_header_present() -> None:
    """SHELL-03 — filled by Plan 57-03 Task 3."""
    ...


def test_theme_and_store_preserved() -> None:
    """SHELL-04 — filled by Plan 57-03 Task 3."""
    ...
