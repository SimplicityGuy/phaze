"""sccache on the arm64 essentia compile (phaze-jfhr0): the Dockerfile ↔ workflow contract.

``Dockerfile.agent-arm64`` runs the ~324 s essentia C++ build through ``sccache`` with the
object cache in a BuildKit cache mount, and ``docker-publish.yml``'s ``build-arm64`` job
restores that mount from ``actions/cache`` before the build and saves it after. The two
files agree on exactly one string — the mount target — and nothing else checks that they
do: buildkit-cache-dance injects into whatever path it is told, and a mismatch is not an
error, it is a cache that is silently never restored and a compile that silently always
runs cold. That shape (a green build produced by the condition it was meant to catch) is
why the target is pinned here rather than read from either file alone.

The digest check is the phaze-hvzd rule applied to a second downloaded binary: a release
tag can be moved and an asset replaced without the version string changing, so the sha256
must be verified *before* the file becomes executable. Everything here is text over the
tracked files — no docker, no network — so it runs in every bucket.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DOCKERFILE = REPO_ROOT / "Dockerfile.agent-arm64"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"

SCCACHE_MOUNT_RE = re.compile(r"--mount=type=cache,target=(?P<target>\S+?)(?:,|\s)")


def _dockerfile() -> str:
    return AGENT_DOCKERFILE.read_text(encoding="utf-8")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))


def _compile_instruction() -> str:
    """The single RUN that carries the waf build, with its continuation lines joined."""
    joined = _dockerfile().replace("\\\n", " ")
    runs = [ln for ln in joined.splitlines() if ln.startswith("RUN ") and "waf configure" in ln]
    assert len(runs) == 1, f"expected exactly one RUN carrying `waf configure` in Dockerfile.agent-arm64; found {len(runs)}"
    return runs[0]


def _dockerfile_mount_target() -> str:
    run = _compile_instruction()
    match = SCCACHE_MOUNT_RE.search(run)
    assert match, f"the waf compile RUN must carry a BuildKit `--mount=type=cache,target=…` for the sccache dir; got: {run!r}"
    return match.group("target")


def _build_arm64_steps() -> list[dict[str, Any]]:
    job = (_workflow().get("jobs") or {}).get("build-arm64")
    assert job, "docker-publish.yml has no `build-arm64` job (CLOUDIMG-02)"
    steps = job.get("steps") or []
    assert steps, "build-arm64 has no steps"
    return steps


def test_sccache_binary_is_digest_verified_before_it_is_executable() -> None:
    """phaze-hvzd for sccache: pinned tag, pinned sha256, checked before `install -m 0755`."""
    text = _dockerfile()
    assert re.search(r"^ARG SCCACHE_VERSION=v\d+\.\d+\.\d+$", text, re.M), "SCCACHE_VERSION must be an ARG pinned to an exact vX.Y.Z tag"
    assert re.search(r"^ARG SCCACHE_SHA256_LINUX_ARM64=[0-9a-f]{64}$", text, re.M), "SCCACHE_SHA256_LINUX_ARM64 must be an ARG carrying a full sha256"
    joined = text.replace("\\\n", " ")
    install_runs = [ln for ln in joined.splitlines() if ln.startswith("RUN ") and "sccache" in ln and "waf" not in ln]
    assert len(install_runs) == 1, f"expected exactly one sccache install RUN; found {len(install_runs)}"
    run = install_runs[0]
    check = run.find("sha256sum -c")
    promote = run.find("install -m 0755")
    assert check != -1, "the sccache install must verify the asset with `sha256sum -c`"
    assert promote != -1, "the sccache install must promote the binary with `install -m 0755`"
    assert check < promote, "the sha256 check must run BEFORE the binary is made executable (phaze-hvzd)"
    assert "exit 1" in run, "a checksum mismatch must fail the build explicitly, never fall through"


def test_waf_compile_runs_through_sccache_inside_the_cache_mount() -> None:
    """CC/CXX wrap gcc/g++ with sccache, in the same shell as `waf configure` (which stores them)."""
    run = _compile_instruction()
    assert 'CC="sccache gcc"' in run, 'the waf compile must export CC="sccache gcc"'
    assert 'CXX="sccache g++"' in run, 'the waf compile must export CXX="sccache g++"'
    target = _dockerfile_mount_target()
    assert f"SCCACHE_DIR={target}" in run, f"SCCACHE_DIR must point at the cache mount target {target!r}; otherwise the mount persists nothing"
    assert "sharing=locked" in run, "the sccache mount must be sharing=locked: the disk backend supports one server per directory"
    assert "sccache --show-stats" in run, "the compile layer must end with `sccache --show-stats` so every build log carries the hit/miss evidence"
    assert "sccache --stop-server" in run, "the compile layer must stop the sccache server so the cache is flushed inside the layer"


def test_workflow_restores_the_same_mount_target_the_dockerfile_uses() -> None:
    """The one string both files must agree on, checked from both sides."""
    steps = _build_arm64_steps()
    dance = [s for s in steps if str(s.get("uses", "")).startswith("reproducible-containers/buildkit-cache-dance@")]
    assert len(dance) == 1, "build-arm64 must carry exactly one buildkit-cache-dance step to persist the sccache cache mount across runs"
    with_ = dance[0].get("with") or {}
    cache_map = yaml.safe_load(with_.get("cache-map") or "{}")
    assert isinstance(cache_map, dict) and cache_map, f"cache-map must be a non-empty JSON object; got {with_.get('cache-map')!r}"
    targets = {v["target"] if isinstance(v, dict) else v for v in cache_map.values()}
    dockerfile_target = _dockerfile_mount_target()
    assert dockerfile_target in targets, (
        f"buildkit-cache-dance cache-map targets {sorted(targets)} do not include the Dockerfile's sccache mount target {dockerfile_target!r}. "
        "A mismatch is not an error at build time -- it is a cache that is never restored and a compile that always runs cold."
    )
    # The cache-map's host directories must be what actions/cache saves.
    cache_steps = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")]
    cache_paths = {str((s.get("with") or {}).get("path", "")).strip() for s in cache_steps}
    assert set(cache_map) <= cache_paths, (
        f"every cache-map host dir {sorted(cache_map)} must be an actions/cache `path` in build-arm64; found {sorted(cache_paths)}"
    )
    # The dance must be wired to the builder the build uses and run BEFORE the build.
    assert with_.get("builder"), (
        "buildkit-cache-dance must name the buildx `builder` (steps.<id>.outputs.name) so it injects into the builder the build uses"
    )
    names = [str(s.get("uses", "")) for s in steps]
    dance_idx = next(i for i, n in enumerate(names) if n.startswith("reproducible-containers/buildkit-cache-dance@"))
    build_idx = next(i for i, n in enumerate(names) if n.startswith("docker/build-push-action@"))
    assert dance_idx < build_idx, "the cache-dance inject step must precede the build-push-action step"


def test_new_actions_are_sha_pinned() -> None:
    """Every `uses:` this bead added is pinned to a full commit SHA, like the rest of the file."""
    for step in _build_arm64_steps():
        uses = str(step.get("uses", ""))
        if uses.startswith(("reproducible-containers/buildkit-cache-dance@", "actions/cache@")):
            ref = uses.split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{uses!r} must be pinned to a 40-hex commit SHA, not a tag"
