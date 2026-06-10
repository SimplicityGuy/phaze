---
phase: quick-260610-fp9
plan: 01
subsystem: build/docker
tags: [docker, essentia, audio-pipeline, apt, runtime-deps]
requires: []
provides: [audio-runtime-system-libs]
affects: [Dockerfile, src/phaze/services/analysis.py]
tech-stack:
  added: [libatomic1, ffmpeg, libsndfile1, libchromaprint-tools]
  patterns: [single-RUN-apt-layer, no-install-recommends, apt-list-cleanup]
key-files:
  created: []
  modified: [Dockerfile]
decisions:
  - "Single apt layer with all four packages to avoid a second redeploy"
  - "Layer placed after WORKDIR /app, before uv COPY, for cache stability and root context"
  - "hadolint DL3008 (pin apt versions) intentionally ignored — versions tracked via base-image tag"
  - "Apt packages kept on one install line to satisfy the plan's line-based verify regex"
metrics:
  duration: ~4m
  completed: 2026-06-10
  tasks: 2
  files: 1
---

# Quick 260610-fp9: Add Audio System Deps to Dockerfile Summary

Added a single root `apt-get` layer to the shared `Dockerfile` installing `libatomic1`, `ffmpeg`, `libsndfile1`, and `libchromaprint-tools` so essentia-tensorflow's native `_essentia` extension imports at runtime and the decode/fingerprint toolchain (ffmpeg/ffprobe/fpcalc) is on PATH — unblocking the analysis pipeline that was dead-lettering all 11,428 files at `import essentia` with `ImportError: libatomic.so.1`.

## What Was Done

- **Task 1** — Inserted a new layer immediately after `WORKDIR /app` and before the `COPY --from=...uv` line:
  - `RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 ffmpeg libsndfile1 libchromaprint-tools && rm -rf /var/lib/apt/lists/*`
  - Layer runs as root (before `USER phaze`), uses `--no-install-recommends`, and cleans the apt cache in the same layer.
  - Added an explanatory comment block mapping each package to the shared object / binary it provides.
- **Task 2** — Ran `pre-commit run --files Dockerfile` (no `--no-verify`). All hooks pass.

## Commits

- `f5fb6e7` — fix(quick-260610-fp9): add audio system deps to Dockerfile so essentia imports

## Verification

- Task 1 automated check passed: `grep -nE 'apt-get install.*libatomic1.*ffmpeg.*libsndfile1.*libchromaprint-tools' Dockerfile` matches line 16, and the `awk` ordering check confirms the apt layer precedes `USER phaze`.
- Task 2 automated check passed: `pre-commit run --files Dockerfile` — all applicable hooks (`Lint Dockerfiles`/hadolint, EOF, trailing whitespace, mixed line ending, large files, merge conflicts, AWS creds, private key) Passed.
- **Runtime proof — empirically confirmed against the live v4.0.8 image (2026-06-10).** Rather than defer entirely, the fix was proven on `nox` using a throwaway root container from `ghcr.io/simplicityguy/phaze:v4.0.8`:
  1. `ldd` on the deployed `_essentia.cpython-314-x86_64-linux-gnu.so` showed the prebuilt `essentia-tensorflow` wheel **bundles all heavy deps** (tensorflow, libdrm, codec/fft/taglib libs) in `essentia_tensorflow.libs/`. The **only** unbundled external library reported `not found` was `libatomic.so.1`; all others (`libstdc++`, `libm`, `libgcc_s`, `libc`, `libdl`, `librt`) are already present in `python:3.14-slim`.
  2. `docker run --rm --user root ... -c "apt-get install -y libatomic1 && python -c 'import essentia, essentia.standard; from essentia.standard import MonoLoader, TensorflowPredictMusiCNN'"` → **`ESSENTIA_IMPORT_OK 2.1-beta6-dev`**. So `libatomic1` alone is sufficient for the full essentia import incl. the TF model classes. (`libcuda.so.1` warnings are expected/harmless — CPU-only host.)
- **Dependency-completeness audit (user request — essentia install page):** The essentia *source-build* dependencies (`libfftw3-dev`, `libavcodec-dev`, `libsamplerate0-dev`, `libtag1-dev`, `libchromaprint-dev`, etc.) **do NOT apply** to our prebuilt-wheel install — the wheel bundles/statically links them. Installing the `-dev` packages would be incorrect bloat. `ffmpeg`/`ffprobe` + `fpcalc` (`libchromaprint-tools`) stay in the layer because phaze's broader pipeline needs them as subprocesses (video metadata via `ffprobe`, pyacoustid fingerprinting via `fpcalc`), not because essentia links them. The 4-package layer is therefore complete and minimal.
- Still deferred to CI build + v4.0.9 redeploy: the end-to-end proof that analysis rows climb and files leave `discovered` in the running stack. There is no unit/integration test for Dockerfile contents; this is expected.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] hadolint hook present and failing (DL3008)**
- **Found during:** Task 2 (`pre-commit run --files Dockerfile`).
- **Issue:** The plan's Task 2 note stated "hadolint is NOT in the project hook set per CLAUDE.md." That is incorrect — `.pre-commit-config.yaml` (line 57-60) wires `hadolint/hadolint@v2.14.0` as the `Lint Dockerfiles` hook. It failed with `DL3008 warning: Pin versions in apt get install`, blocking the pre-commit gate.
- **Fix:** Added a `# hadolint ignore=DL3008` directive (with a 3-line rationale comment) directly above the `RUN` instruction. Pinning Debian-slim apt versions is brittle — versions shift on every base-image refresh and pinning would break builds on each upstream security update; the base-image tag controls the package snapshot instead.
- **Files modified:** Dockerfile
- **Commit:** f5fb6e7

### Other Adjustments

**2. [Formatting] apt packages kept on a single install line**
- The plan's automated verify regex (`apt-get install.*libatomic1.*ffmpeg.*libsndfile1.*libchromaprint-tools`) is line-based and requires all four packages, in order, on the same line. A multi-line `\`-continuation package list (initially written for readability) would not match a line-based grep. Reformatted to a single `apt-get install` line so the plan's stated verify gate passes exactly as written. Still a single `RUN` layer with `--no-install-recommends` and cache cleanup.

## Notes for Redeploy

- This change only takes effect once the shared image `ghcr.io/simplicityguy/phaze` is rebuilt (CI on merge) and the homelab containers (phaze-api, phaze-worker, phaze-agent-worker) are redeployed onto the new tag (v4.0.9). Until then the running containers still lack the libs.

## Self-Check: PASSED

- FOUND: Dockerfile (modified, apt layer at lines 5-17, `USER phaze` at line 41)
- FOUND: commit f5fb6e7 in `git log`
