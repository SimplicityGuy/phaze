"""Guard: ``just test-db-down`` must refuse while another seat is using the shared harness (phaze-ieqg).

``phaze-test-db`` and ``phaze-test-redis`` are deliberately ONE pair of containers shared by every
concurrent worktree -- ``just test-db-for <name>`` isolates seats by carving a database and a Redis
logical DB out of them, not by giving each seat its own container. That makes ``docker rm -f`` on
either container a cluster-wide destructive act.

WHAT HAPPENED, 2026-07-29 18:17 UTC
-----------------------------------

A ``test-db-down`` (plus the implicit recreate on the next ``test-db``) ran while five full suites
were in flight across five worktrees. Both containers came back brand new: 89 per-worktree
databases gone, and the Redis DB-index allocation registry (``phaze:test:redis-db-index`` /
``-counter`` on logical DB 0) reset, so already-issued indices became re-issuable. Every in-flight
suite went red with failures spread across modules its branch never touched, all green on isolated
re-run.

That is the same false-red signature as the one-database-two-processes collision guarded in
``tests/shared/test_test_db_session_exclusivity.py``, from a different cause, and the two together
are what made "the suite is unreliable under any concurrent suite" look like an unfindable third
shared surface: the observable symptom is identical no matter which of them fired.

WHY A DOC COMMENT WAS NOT ENOUGH
--------------------------------

The recipe's ``[doc(...)]`` string has warned "affects every concurrent worktree/session using
them" since phaze-20vd. It is displayed by ``just --list``, which is read at leisure -- never at
the moment somebody types ``just test-db-down`` to unstick something. The guard has to run inside
the recipe, and it has to name the seats it is protecting so the operator can decide.

This test parses the justfile as text: no docker daemon, no ``just`` binary, safe anywhere.
"""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE_PATH = REPO_ROOT / "justfile"

FORCE_ENV_VAR = "PHAZE_TEST_DB_FORCE_DOWN"


def _recipe_body(name: str) -> str:
    """Return the body of justfile recipe ``name`` (its indented block, attributes excluded)."""
    text = JUSTFILE_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}:\n((?:[ \t].*\n|\n)+)", text, re.MULTILINE)
    assert match is not None, f"recipe {name!r} not found in {JUSTFILE_PATH}"
    return match.group(1)


def test_test_db_down_probes_for_live_seats_before_removing_anything() -> None:
    """The recipe must query ``pg_stat_activity`` for other clients BEFORE any ``docker rm``.

    Ordering is the whole point: a check that runs after the removal is a post-mortem, not a
    guard. Asserted positionally rather than by mere presence.
    """
    body = _recipe_body("test-db-down")
    probe_at = body.find("pg_stat_activity")
    remove_at = body.find("docker rm -f")
    assert probe_at != -1, "test-db-down must probe pg_stat_activity for other seats"
    assert remove_at != -1, "test-db-down must still be able to remove the containers"
    assert probe_at < remove_at, "the live-seat probe must run BEFORE the containers are removed"


def test_the_probe_matches_per_worktree_test_databases() -> None:
    """The probe must look at every ``phaze%`` database, not only the default ``phaze_test``.

    Every isolated seat is named ``phaze_<name>_test`` by ``just test-db-for``, so a probe pinned
    to the literal default database would see none of the seats it exists to protect -- it would
    pass the check precisely when the most seats are at risk. The pattern is deliberately not
    narrowed to ``phaze%test``: a database misplaced on this shared container under a non-``test``
    name (e.g. a perf corpus seeded here by mistake, phaze-zpdyg) would otherwise be invisible to
    this guard even while it lives on the exact container the recipe is about to remove.
    """
    body = _recipe_body("test-db-down")
    assert "'phaze%'" in body, "the live-seat probe must match every phaze-prefixed database on the shared container"
    assert "client backend" in body, "the probe must exclude Postgres' own background workers, which are always present"


def test_the_refusal_is_overridable_but_not_by_accident() -> None:
    """An explicit env-var override exists, and the refusal message names it.

    Stale connections after a crashed run are real, so the guard must not be a dead end. It must
    also not be bypassable by re-running the same command, which is what a bare confirmation
    prompt degrades to.
    """
    body = _recipe_body("test-db-down")
    assert FORCE_ENV_VAR in body, "an escape hatch must exist for genuinely stale connections"
    assert body.count(FORCE_ENV_VAR) >= 2, "the refusal message must name the override, not just honour it"
    assert "exit 1" in body, "the guard must fail the recipe, not merely print a warning"


def test_the_refusal_explains_the_failure_mode_it_prevents() -> None:
    """The message must say WHY, because the cost is invisible at the point of decision.

    "Containers are in use" reads as a lock to be forced. "Forcing this makes five suites report
    branch-unrelated failures that pass on re-run" reads as a reason to wait.
    """
    body = _recipe_body("test-db-down")
    assert "isolated re-run" in body, "the refusal must describe the false-red symptom it prevents"
    assert "phaze-ieqg" in body, "the refusal must cite the bead so the evidence trail is reachable"
