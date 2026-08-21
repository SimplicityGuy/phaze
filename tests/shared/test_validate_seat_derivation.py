"""Regression tests for `just test-validate`'s per-worktree seat derivation (phaze-bk9el.23).

THE DEFECT. ``just test-validate`` -- what ``just check`` / ``just check-all``, and therefore
``bh work check`` and ``bh work submit``, all ultimately run -- used to fall back to the SHARED
``phaze_test`` / ``phaze_migrations_test`` pair and Redis logical DB 0 whenever the caller had
not exported ``TEST_DATABASE_URL``. That contradicts CLAUDE.md's standing rule ("Never share
Postgres OR Redis between concurrent agents") for the one command every bead is required to run.

The failure it produced is the bad shape, not the loud one: phaze-ieqg's session advisory lock
refuses the second pytest rather than corrupting both runs, so a concurrent seat that forgot to
export its own rig sees A RED RUN THAT PASSES ON ISOLATED RE-RUN -- which CLAUDE.md names as the
worst possible shape, because it trains reviewers to dismiss reds.

IT DERIVES; IT DOES NOT REFUSE. Refusing when ``TEST_DATABASE_URL`` is unset was proposed and
rejected: the auto-provision exists so a fresh worktree with no seat still works, and a refusal
fixes the concurrent case by breaking the solo case. The fallback therefore provisions a seat
derived from the worktree instead of landing on a shared one.

These tests are deliberately split by cost. The recipe-shape assertions are static reads of the
justfile (no harness, no Docker, safe in CI); the derivation tests drive the real
``scripts/derive-validate-seat-name.sh`` over a subprocess -- the same interface the recipe uses --
against throwaway directories, so they cannot drift from what the recipe actually does. Nothing
here provisions a real seat: that needs the shared Docker harness, which CI does not run.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE_PATH = REPO_ROOT / "justfile"
DERIVE_SCRIPT = REPO_ROOT / "scripts" / "derive-validate-seat-name.sh"
PROVISION_SCRIPT = REPO_ROOT / "scripts" / "provision-test-seat.sh"
# scripts/derive-seat-name.sh's validation rule; the derived raw name must satisfy it or
# `test-db-for`/`test-validate` die at CREATE DATABASE with a raw Postgres parse error.
SEAT_NAME_RULE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _recipe_body(name: str) -> str:
    """Return the raw justfile text of one recipe, from its header to the next top-level line."""
    justfile = JUSTFILE_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}:\n((?: +.*\n|\n)+)", justfile, re.MULTILINE)
    assert match is not None, f"recipe {name} not found in the justfile"
    return match.group(1)


def _derive(cwd: Path) -> str:
    assert DERIVE_SCRIPT.is_file(), f"seat-name derivation script missing: {DERIVE_SCRIPT}"
    result = subprocess.run(  # noqa: S603 - fixed, in-repo executable; no caller-supplied argv
        [str(DERIVE_SCRIPT)],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_test_validate_no_longer_falls_back_to_the_shared_seat() -> None:
    """The literal shared `phaze_test` / Redis DB 0 fallback must not come back.

    This is the whole bug. It is asserted on the recipe text rather than on a run because the
    run that would expose it needs a second concurrent worktree and twenty minutes.
    """
    body = _recipe_body("test-validate")

    assert "/phaze_test" not in body, body
    assert "/phaze_migrations_test" not in body, body
    assert not re.search(r"PHAZE_REDIS_URL=\"redis://[^\"]*/0\"", body), body


def test_test_validate_derives_its_seat_through_the_shared_provisioning_script() -> None:
    """Criterion 5: one copy of the provisioning body, not a hand-built DSN.

    `phaze-fmfk` records that a seat name is NOT used verbatim -- hyphens become underscores plus
    a hash of the raw name -- so a hand-constructed DSN and the recipe's agree only when the name
    happens to contain no hyphen. `test-db-for` and `test-validate` must therefore reach the same
    script, or the gate's seat and the operator's seat for one worktree silently diverge.
    """
    validate = _recipe_body("test-validate")

    assert "scripts/derive-validate-seat-name.sh" in validate
    assert "scripts/provision-test-seat.sh" in validate
    assert "scripts/provision-test-seat.sh" in _recipe_body("test-db-for name")


def test_test_validate_respects_an_exported_seat_verbatim() -> None:
    """Criterion 3: CI exports its own DSN against a 5432 service container.

    Re-pointing that at the local 5433 harness would break CI outright, so provisioning must stay
    inside the `TEST_DATABASE_URL` guard rather than running unconditionally.
    """
    body = _recipe_body("test-validate")
    guard = 'if [ -z "${TEST_DATABASE_URL:-}" ]; then'

    assert guard in body
    provisioning = body.index("scripts/provision-test-seat.sh")
    assert body.index(guard) < provisioning < body.index("fi\n")


def test_the_provisioning_script_emits_only_exports_on_stdout() -> None:
    """`test-validate` ``eval``s this script's stdout, so a stray human line would be executed."""
    source = PROVISION_SCRIPT.read_text(encoding="utf-8")
    # Every echo outside the final export block must be redirected to stderr.
    unredirected = [line.strip() for line in source.splitlines() if line.strip().startswith("echo ") and ">&2" not in line]

    assert unredirected == [
        'echo "export TEST_DATABASE_URL=\\"postgresql+asyncpg://phaze:phaze@localhost:${pg_port}/${main_db}\\""',
        'echo "export MIGRATIONS_TEST_DATABASE_URL=\\"postgresql+asyncpg://phaze:phaze@localhost:${pg_port}/${migrations_db}\\""',
        'echo "export PHAZE_REDIS_URL=\\"redis://localhost:${redis_port}/${redis_db}\\""',
    ], unredirected
    # The two sub-scripts it drives are chatty on stdout; both must be redirected.
    assert "ensure-pg-database.sh" in source and re.search(r"ensure-pg-database\.sh[^\n]*>&2", source)


def test_the_derived_name_satisfies_the_seat_name_rule(tmp_path: Path) -> None:
    """A derived name that `derive-seat-name.sh` rejects would fail the gate at provisioning."""
    derived = _derive(tmp_path)

    assert SEAT_NAME_RULE.match(derived), derived
    assert derived.startswith("auto-"), derived


def test_the_derived_name_is_stable_for_one_worktree(tmp_path: Path) -> None:
    """Idempotency: a fresh name per gate run would mint a database and burn a Redis index each time."""
    assert _derive(tmp_path) == _derive(tmp_path)


def test_two_worktrees_derive_different_seats(tmp_path: Path) -> None:
    """The property the whole bead exists for. Two trees, no exported seat, no shared database."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    assert _derive(first) != _derive(second)


def test_two_worktrees_with_the_same_basename_still_derive_different_seats(tmp_path: Path) -> None:
    """Uniqueness rides on the ABSOLUTE path, not the directory name.

    Deriving from the branch alone is nearly enough -- git refuses to check one branch out in two
    worktrees -- but two CLONES can both sit on `main`, and a detached HEAD has no branch at all.
    Both of those collapse onto one seat if the directory name is the only discriminator.
    """
    first = tmp_path / "a" / "phaze"
    second = tmp_path / "b" / "phaze"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    assert _derive(first) != _derive(second)


def test_derivation_survives_a_directory_that_is_not_a_git_checkout(tmp_path: Path) -> None:
    """A tarball export or a container COPY has no git metadata; the gate must still run."""
    derived = _derive(tmp_path)

    assert SEAT_NAME_RULE.match(derived), derived
