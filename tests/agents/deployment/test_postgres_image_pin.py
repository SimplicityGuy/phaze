"""phaze-tcqq: mechanical guard against the pinned Postgres image diverging.

The Postgres tag is pinned in three places, only one of which can share a single
source of truth:

1. ``justfile``'s ``postgres_image :=`` variable, interpolated at every justfile
   site that launches a Postgres container (``test-db``, ``integration-test``,
   ``perf-db-up``) -- including the four ``echo`` lines that only *claim* the
   version. That interpolation is what collapsed the former 8 separate literal
   pins to one.
2. ``docker-compose.yml``'s ``postgres`` service ``image:``.
3. ``.github/workflows/tests.yml``'s ``postgres`` service container ``image:``.

Compose files and GitHub Actions workflows cannot read a justfile variable (no
``build:`` equivalent for Actions service containers either -- they can only
reference a published image), so (2) and (3) stay literal. This test is the
mechanical check that a version bump touching only one or two of the three
pins fails loudly instead of silently diverging the harness from production, or
CI from both.

Deliberately parses raw text/YAML rather than shelling out to `just` -- no
docker daemon, no `just` binary dependency, safe to run in any environment.
"""

from pathlib import Path
import re

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
JUSTFILE_PATH = REPO_ROOT / "justfile"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
TESTS_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# Every literal `postgres:<tag>` reference anywhere in the justfile is expected to have
# been collapsed to this one interpolation site.
_JUSTFILE_VAR_RE = re.compile(r'^postgres_image\s*:=\s*"([^"]+)"\s*$', re.MULTILINE)
_LITERAL_PIN_RE = re.compile(r"\bpostgres:[0-9A-Za-z_.\-]+alpine\b")


def _justfile_text() -> str:
    assert JUSTFILE_PATH.exists(), f"justfile missing at {JUSTFILE_PATH}"
    return JUSTFILE_PATH.read_text()


def test_justfile_declares_one_postgres_image_variable() -> None:
    """The justfile must declare exactly one ``postgres_image :=`` variable."""
    text = _justfile_text()
    matches = _JUSTFILE_VAR_RE.findall(text)
    assert len(matches) == 1, f"expected exactly one `postgres_image :=` declaration in the justfile; found {matches!r}"


def test_justfile_has_no_leftover_literal_postgres_pins() -> None:
    """Every Postgres reference in the justfile must go through ``{{postgres_image}}``.

    A literal ``postgres:18-alpine`` (in an echo string or a `docker run` argument)
    left over after the ``postgres_image`` variable was introduced is exactly the
    partial-bump hazard this bead fixes: it would print/run the old tag while every
    other site moved to the new one.
    """
    text = _justfile_text()
    # Strip the variable declaration line itself before scanning for literal pins --
    # that line's right-hand side legitimately names the tag once.
    text_without_declaration = _JUSTFILE_VAR_RE.sub("", text)
    offenders = _LITERAL_PIN_RE.findall(text_without_declaration)
    assert not offenders, (
        f"found literal postgres:*-alpine pin(s) outside `postgres_image :=` in the justfile: {offenders!r}. Use {{{{postgres_image}}}} instead."
    )


def _justfile_postgres_image() -> str:
    text = _justfile_text()
    match = _JUSTFILE_VAR_RE.search(text)
    assert match, 'justfile must declare `postgres_image := "..."`'
    return match.group(1)


def _compose_postgres_image() -> str:
    assert COMPOSE_PATH.exists(), f"docker-compose.yml missing at {COMPOSE_PATH}"
    data = yaml.safe_load(COMPOSE_PATH.read_text())
    image = data["services"]["postgres"]["image"]
    assert isinstance(image, str), f"docker-compose.yml postgres service image must be a string; got {image!r}"
    return image


def _tests_workflow_postgres_image() -> str:
    assert TESTS_WORKFLOW_PATH.exists(), f".github/workflows/tests.yml missing at {TESTS_WORKFLOW_PATH}"
    data = yaml.safe_load(TESTS_WORKFLOW_PATH.read_text())
    services = data["jobs"]["test"]["services"]
    image = services["postgres"]["image"]
    assert isinstance(image, str), f"tests.yml postgres service image must be a string; got {image!r}"
    return image


def test_postgres_image_pins_agree_across_justfile_compose_and_ci() -> None:
    """The three independent pins must name the exact same image tag.

    docker-compose.yml and .github/workflows/tests.yml cannot interpolate the
    justfile's `postgres_image` variable, so they carry their own literal pins.
    This is the guard that catches the moment they drift instead of a reviewer
    happening to notice a stale tag in a diff.
    """
    justfile_image = _justfile_postgres_image()
    compose_image = _compose_postgres_image()
    workflow_image = _tests_workflow_postgres_image()

    assert justfile_image == compose_image == workflow_image, (
        "Postgres image pin has diverged across sources:\n"
        f"  justfile (postgres_image :=):       {justfile_image!r}\n"
        f"  docker-compose.yml (postgres.image): {compose_image!r}\n"
        f"  .github/workflows/tests.yml:         {workflow_image!r}\n"
        "Bump all three together (and re-check any other compose overlay that defines "
        "the postgres service)."
    )


def test_tests_workflow_documents_service_container_build_constraint() -> None:
    """tests.yml must record, in a comment, that service containers can't be built.

    GitHub Actions service containers can only REFERENCE a published image -- there
    is no `build:` equivalent. If production's Postgres ever becomes a custom image
    (e.g. the pgvector adoption gated on the phaze-ytgo verdict matrix), CI cannot
    follow it in place without first publishing that image. This is documentation of
    a constraint, not a functional check, but a comment can be deleted silently by a
    future edit, so pin the assertion to durable phrasing rather than exact wording.
    """
    text = TESTS_WORKFLOW_PATH.read_text()
    lower = text.lower()
    assert "service container" in lower, "tests.yml must comment on the Actions service-container constraint near the postgres service"
    assert "can only" in lower or "cannot be built" in lower or "cannot build" in lower, (
        "tests.yml's service-container comment must state that service containers can only reference a published image (cannot be built)"
    )
