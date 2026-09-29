"""phaze-8o294: mechanical guard against the Redis-protocol server image diverging again.

The test harness and CI ran ``redis:7-alpine`` while production ran ``redis:8-alpine`` -- a version
skew the suite did not cover and CLAUDE.md carried as a "Known gap". phaze-8o294 moved every one of
them onto ONE ``valkey/valkey`` image. The pin lives in several places, only some of which can share
a source of truth:

1. ``justfile``'s ``test_redis_image :=`` variable, interpolated by ``test-db`` and passed to
   ``scripts/integration-test-harness.sh`` (whose own default must agree for direct invocations).
2. ``docker-compose.yml``'s ``redis`` service ``image:`` -- production.
3. Both ``redis`` service containers in ``.github/workflows/tests.yml``.
4. The throwaway containers ``tests/shared/test_redis_seat_registry.py`` and
   ``tests/shared/test_test_db_gc.py`` start -- the tests that exercise the seat registry's
   ``CLIENT LIST`` parse and in-Lua ``SELECT``/``FLUSHDB`` against a real server, so they must run
   the server the registry will actually meet.

Same shape as ``test_postgres_image_pin.py``: raw text/YAML, no docker daemon, no ``just`` binary.
"""

from pathlib import Path
import re

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
JUSTFILE_PATH = REPO_ROOT / "justfile"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
TESTS_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tests.yml"
HARNESS_PATH = REPO_ROOT / "scripts" / "integration-test-harness.sh"
THROWAWAY_FIXTURES = (
    REPO_ROOT / "tests" / "shared" / "test_redis_seat_registry.py",
    REPO_ROOT / "tests" / "shared" / "test_test_db_gc.py",
)

_JUSTFILE_VAR_RE = re.compile(r'^test_redis_image\s*:=\s*"([^"]+)"\s*$', re.MULTILINE)
_HARNESS_DEFAULT_RE = re.compile(r'^redis_image="\$\{PHAZE_INTEGRATION_REDIS_IMAGE:-([^}]+)\}"$', re.MULTILINE)
# A literal image reference: the official `redis:<tag>` image or `valkey/valkey:<tag>`. A tag must
# start with a digit (so the justfile's `redis:*)` case pattern is not one), and `@redis:6379` -- the
# compose service's host:port inside a DSN -- is excluded by the `@` in the lookbehind.
_LITERAL_IMAGE_RE = re.compile(r"(?<![\w/.@-])(?:redis:[0-9][0-9A-Za-z_.-]*|valkey/valkey:[0-9A-Za-z_.-]+)")


def _justfile_image() -> str:
    matches = _JUSTFILE_VAR_RE.findall(JUSTFILE_PATH.read_text())
    assert len(matches) == 1, f"expected exactly one `test_redis_image :=` declaration in the justfile; found {matches!r}"
    return matches[0]


def _workflow_images() -> list[str]:
    data = yaml.safe_load(TESTS_WORKFLOW_PATH.read_text())
    images = [job["services"]["redis"]["image"] for job in data["jobs"].values() if "redis" in job.get("services", {})]
    assert len(images) == 2, f"expected the two redis service containers in tests.yml (unit + browser jobs); found {images!r}"
    return images


def test_every_pin_names_the_same_valkey_image() -> None:
    """Production, CI, the local harness and the real-server test fixtures run ONE image."""
    image = _justfile_image()
    assert image.startswith("valkey/valkey:"), f"test_redis_image must be a valkey/valkey image (phaze-8o294); got {image!r}"

    compose_image = yaml.safe_load(COMPOSE_PATH.read_text())["services"]["redis"]["image"]
    harness_default = _HARNESS_DEFAULT_RE.findall(HARNESS_PATH.read_text())
    fixture_images = {path.name: _LITERAL_IMAGE_RE.findall(path.read_text()) for path in THROWAWAY_FIXTURES}

    assert compose_image == image, f"docker-compose.yml redis image {compose_image!r} != justfile test_redis_image {image!r}"
    assert _workflow_images() == [image, image], f"tests.yml redis service images {_workflow_images()!r} != {image!r}"
    assert harness_default == [image], f"integration-test-harness.sh default {harness_default!r} != {image!r}"
    for name, found in fixture_images.items():
        assert found == [image], f"{name} starts {found!r}; its real-server checks must run {image!r}"


def test_no_other_redis_image_is_pinned_anywhere_that_launches_one() -> None:
    """No literal image other than the one pin survives in the files that start a server.

    The justfile's own declaration is stripped first -- that line legitimately names the tag once.
    Every other site must go through ``{{test_redis_image}}``; a leftover literal is exactly the
    partial-bump hazard ``test_postgres_image_pin.py`` names: it would run the old tag while every
    other site moved on.
    """
    image = _justfile_image()
    sources = {
        JUSTFILE_PATH: _JUSTFILE_VAR_RE.sub("", JUSTFILE_PATH.read_text()),
        HARNESS_PATH: _HARNESS_DEFAULT_RE.sub("", HARNESS_PATH.read_text()),
        COMPOSE_PATH: COMPOSE_PATH.read_text(),
        TESTS_WORKFLOW_PATH: TESTS_WORKFLOW_PATH.read_text(),
    }
    offenders = {path.name: found for path, text in sources.items() if (found := [m for m in _LITERAL_IMAGE_RE.findall(text) if m != image])}
    justfile_literals = _LITERAL_IMAGE_RE.findall(sources[JUSTFILE_PATH])
    assert not offenders, f"image literals that are not {image!r}: {offenders!r}"
    assert not justfile_literals, f"justfile names {justfile_literals!r} outside `test_redis_image :=`; use {{{{test_redis_image}}}}"
