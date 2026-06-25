---
phase: 47-official-arm64-essentia-agent-image
plan: 01
subsystem: infra
tags: [docker, arm64, aarch64, essentia, tensorflow, python313, saq, supply-chain, hadolint]

# Dependency graph
requires:
  - phase: spike/arm64-essentia-analysis
    provides: proven 4-fix source-build recipe (Python 3.13 + TF 2.20.0 + essentia-from-source), PASS 2026-06-23
provides:
  - Dockerfile.agent-arm64 — production arm64 essentia analysis agent build recipe
  - reproducible essentia pin (commit SHA) + TF ==2.20.0 pin
  - scoped Python-3.13 install mechanism that leaves the repo's requires-python >=3.14 contract untouched (D-47-PY)
  - baked OMP_NUM_THREADS=1 dual-OpenMP mitigation (fix #4)
affects: [47-02 (native arm64 build + boot + import-smoke), 47-03 (duration routing + backfill), 47-04 (real-audio numeric parity guard — owns fix #4 + ESSENTIA_SHA trust), 47-05 (deploy)]

# Tech tracking
tech-stack:
  added: [python:3.13-slim-bookworm base, tensorflow==2.20.0 (aarch64 wheel), essentia (source build @ pinned SHA), uv 0.11.23 as resolver-only]
  patterns: [single-toolchain ABI match for essentia #977, dangling-symlink repoint to libtensorflow_cc.so.2, post-compile ENV layers to preserve build cache, scoped --ignore-requires-python agent-image exception, runtime-libs-or-crash-loop final stage]

key-files:
  created: [Dockerfile.agent-arm64]
  modified: []

key-decisions:
  - "D-47-PY: scope the 3.13 exception to this image only — uv export+--system for the closure, pip --no-deps --ignore-requires-python . for phaze; pyproject requires-python >=3.14 left UNTOUCHED"
  - "Install and run both target /usr/local/bin/python3 — CMD uses python3 -m saq directly, NOT the uv launcher (which would re-validate requires-python >=3.14 against 3.13 and miss the --system packages)"
  - "essentia pinned to a hardcoded 40-hex commit SHA (b9fa6cb674ca43dfb94d28d293aeda441c6745db) — no moving master, no build-time remote resolution; TRUSTED only after the 47-04 parity guard"
  - "TensorFlow pinned ==2.20.0 (only 2.20.0/2.21.0 ship cp313 aarch64 wheels; 2.20.0 is spike-proven)"

patterns-established:
  - "Pattern: post-compile ENV layers (final LD_LIBRARY_PATH + OMP_NUM_THREADS) placed AFTER the ~324s waf compile so they never bust the compile cache"
  - "Pattern: agent-image bare-pip exceptions are each justified inline + hadolint-ignored (DL3013) — bounded to the TF toolchain install and the phaze project install"
  - "Pattern: runtime apt set kept in the final stage so a future multi-stage split that drops -dev packages cannot regress the v4.0.9/v4.1.1 crash-loop class"

requirements-completed: [CLOUDIMG-01]

# Metrics
duration: 18min
completed: 2026-06-24
---

# Phase 47 Plan 01: Official arm64 essentia agent image Summary

**Reproducible `Dockerfile.agent-arm64` that builds essentia from source on Python 3.13 + TF 2.20.0 with all four spike fixes baked in, scopes the 3.13 exception without touching the repo's 3.14 contract, and launches the agent role via `python3 -m saq` — hadolint-clean.**

## Performance

- **Duration:** ~18 min
- **Completed:** 2026-06-24
- **Tasks:** 3
- **Files modified:** 1 (created)

## Accomplishments
- Transcribed the PROVEN `spike/arm64-essentia-analysis` recipe into a hardened, reproducible `Dockerfile.agent-arm64` (NOT a branch merge): `python:3.13-slim-bookworm`, `ARG TF_VERSION=2.20.0`, single-toolchain `pip install numpy pyyaml tensorflow==${TF_VERSION}` (matches libstdc++ CXX11 ABI, avoids essentia #977).
- Pinned essentia to a hardcoded 40-hex commit SHA (`b9fa6cb674ca43dfb94d28d293aeda441c6745db`) with a FULL clone + `git checkout`; no moving master, no build-time remote SHA resolution (T-47-01).
- Baked spike fixes #1 (repoint dangling `setup_from_python.sh` symlinks → `libtensorflow_cc.so.2`), #2 (`LIBRARY_PATH`), #3 (`LD_LIBRARY_PATH` incl. `tensorflow.libs`, set after the compile layer), and #4 (`OMP_NUM_THREADS=1` dual-OpenMP mitigation).
- Implemented D-47-PY: scoped the Python-3.13 exception to this image only — `uv export`+`uv pip install --system` for the third-party closure, then `pip install --no-deps --ignore-requires-python .` for the phaze package; `pyproject.toml` `requires-python = ">=3.14,<3.15"` left untouched.
- Kept the runtime apt set (`libatomic1 ffmpeg libsndfile1 libchromaprint-tools libpq5`) in the final stage (v4.0.9/v4.1.1 crash-loop class), added the non-root uid/gid-1000 user, and set the agent CMD to `python3 -m saq phaze.tasks.agent_worker.settings` (direct interpreter, no uv launcher).

## Task Commits

Each task was committed atomically:

1. **Task 1: Transcribe the spike build recipe (3.13 + TF 2.20.0 + essentia from source + fixes #1-#3)** - `fe62d60` (feat)
2. **Task 2: Harden for production — runtime libs, scoped 3.13 install, non-root user, agent CMD** - `08497df` (feat)
3. **Task 3: Bake the dual-OpenMP fix (#4) env** - `c0b7b7b` (feat)

## Files Created/Modified
- `Dockerfile.agent-arm64` - production arm64 essentia-from-source agent build recipe (3.13 + TF 2.20.0 + 4 spike fixes + scoped 3.13 install + runtime libs + agent CMD).

## Decisions Made
- **3.13 reconciliation mechanism (D-47-PY):** the closure is installed via `uv export --frozen --no-dev --no-emit-project --format requirements-txt > /tmp/agent-reqs.txt` then `uv pip install --system --python /usr/local/bin/python3 -r /tmp/agent-reqs.txt`; the phaze package via `python3 -m pip install --no-cache-dir --no-deps --ignore-requires-python .`. essentia-tensorflow is marker-excluded on linux/aarch64 (built from source), so it is correctly absent from the export. Both install and run hit `/usr/local/bin/python3`.
- **ESSENTIA_SHA:** `b9fa6cb674ca43dfb94d28d293aeda441c6745db` — resolved once from MTG/essentia master on 2026-06-24 (the spike-era state; the spike's `--depth 1` master clone recorded no SHA). It is trusted only after the 47-04 parity guard catches any fresh-master divergence on real audio.
- **CMD:** `["python3", "-m", "saq", "phaze.tasks.agent_worker.settings"]` — the direct equivalent of the `saq` console_script (`saq.__main__:main`); deliberately NOT the uv launcher (which re-validates requires-python >=3.14 against 3.13 and would miss the `--system` packages). Agent role only (D-25: must not import phaze.database).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Reworded inline comments that collided with the plan's negative content gates**
- **Found during:** Task 1 and Task 2 (verification)
- **Issue:** The plan's verify steps assert `! grep -q 'ls-remote'` and `! grep -q 'uv run'`. My explanatory comments literally contained the strings "ls-remote" (in "NO build-time `git ls-remote`") and "uv run" (in "never `uv run`"), which made the negative gates fail even though the Dockerfile never *uses* those mechanisms.
- **Fix:** Reworded the comments to "NO build-time remote SHA resolution" and "never the uv launcher" / "The uv launcher ..." while preserving the documented intent.
- **Files modified:** Dockerfile.agent-arm64
- **Verification:** `! grep -q 'ls-remote'` and `! grep -q 'uv run'` both pass; hadolint clean; all positive gates still pass.
- **Committed in:** `fe62d60` (Task 1) and `08497df` (Task 2)

---

**Total deviations:** 1 auto-fixed (1 blocking — comment wording vs. verification gates)
**Impact on plan:** Cosmetic comment wording only; no change to build behavior or scope. The Dockerfile still never performs remote SHA resolution and never invokes the uv launcher.

## Issues Encountered
- The native arm64 C++ compile (~324s) and a full `docker build` were NOT run here — by design. This plan ships the Dockerfile as a hadolint-clean recipe; the actual native arm64 build + boot + `import essentia`/`import phaze` smoke is owned by plan 47-02, and real-audio numeric parity (which also proves fix #4) by plan 47-04. Validated locally via hadolint (`--failure-threshold error`, exits 0) plus all content gates.

## Fix #4 (dual-OpenMP) runtime validation — HAND-OFF
The OpenMP mitigation (`OMP_NUM_THREADS=1`, with a documented `LD_PRELOAD` fallback to a single `tensorflow.libs/libomp-*.so`) is baked and documented, but it CANNOT be proven by hadolint or a synthetic `np.sin` signal. Its real validation is a real-audio analyze through `phaze.services.analysis.analyze_file` on a native arm64 runner — **explicitly owned by plan 47-04** (Assumption A1: do NOT assume benign).

## Next Phase Readiness
- `Dockerfile.agent-arm64` exists and is hadolint-clean; ready for plan 47-02 to wire the native `ubuntu-24.04-arm` build/publish job + import-smoke and plan 47-04 to add the numeric parity guard.
- Open follow-ups carried forward: (1) plan 47-02 must actually build the image natively and run the import smoke; (2) plan 47-04 must validate fix #4 on real audio and confirm the pinned ESSENTIA_SHA matches x86 outputs within tolerance.

## Self-Check: PASSED
- FOUND: `Dockerfile.agent-arm64`
- FOUND: `.planning/phases/47-official-arm64-essentia-agent-image/47-01-SUMMARY.md`
- FOUND commits: `fe62d60`, `08497df`, `c0b7b7b`

---
*Phase: 47-official-arm64-essentia-agent-image*
*Completed: 2026-06-24*
