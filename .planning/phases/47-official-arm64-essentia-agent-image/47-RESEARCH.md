# Phase 47: Official arm64 essentia agent image - Research

**Researched:** 2026-06-24
**Domain:** Cross-arch container builds, essentia-from-source on aarch64, GitHub-hosted arm64 CI, numeric parity guards
**Confidence:** HIGH (the hardest unknowns are settled by a PROVEN spike + verified registry facts)

## Summary

The viability question is already answered: the `spike/arm64-essentia-analysis` branch **proved** (2026-06-23) that essentia builds from source on `linux/aarch64` against the AWS-maintained `tensorflow` wheel and runs the full ML stack (TensorflowPredictMusiCNN + TensorflowPredictEffnetDiscogs) without the `essentia #977` libstdc++ dual-ABI segfault. Phase 47 is therefore an **engineering/productionization** phase, not a research gamble: take the spike's exact 4 fixes, harden them into a production agent Dockerfile, build+push on a native GitHub-hosted arm64 runner, and gate it with a numeric parity guard.

Three facts dominate planning and are all VERIFIED against PyPI this session:
1. **Python on the arm64 agent image MUST be 3.13, not 3.14.** No `cp314` TensorFlow wheel exists on *any* platform yet (latest TF is 2.21.0, zero cp314 wheels), and aarch64 TF wheels top out at `cp313`. The project's Python-3.14 mandate cannot reach the arm64 agent. The agent is a standalone image/role, so this divergence is acceptable — but the repo's `requires-python = ">=3.14,<3.15"` will **block** a 3.13 install and must be worked around for the agent build.
2. **GitHub-hosted arm64 runners are free for public repos** (`phaze` is PUBLIC) under the `ubuntu-24.04-arm` label, GA since 2025-08-07. No QEMU needed, satisfying CLOUDIMG-02 natively.
3. **A parity golden does not exist yet** and the spike under-tested the production surface (it ran MusiCNN + Effnet only; production also uses `TensorflowPredictVGGish`, `RhythmExtractor2013`, and `KeyExtractor`). The parity guard must cover *all* production code paths and a golden reference must be created.

**Primary recommendation:** Build a dedicated `Dockerfile.agent-arm64` from `python:3.13-slim-bookworm` that bakes the spike's 4 fixes, pins essentia to a specific commit + TF to `2.20.0`, resolves the dual-OpenMP conflict (spike fix #4 — the one true open blocker), and publishes a **separate single-arch arm64 tag** (multi-arch manifest is explicitly deferred to CLOUDIMG-04). Gate every build with a parity job that compares full-analysis output against an x86-generated golden within tolerance.

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CLOUDIMG-01 | Official arm64 essentia agent image on GHCR, essentia **built from source** (wheel is x86-only) with spike fixes baked in | Spike Dockerfile + README give the exact apt deps, the 4 integration fixes, TF 2.20.0, and the `setup_from_python.sh` symlink repointing. essentia-tensorflow wheel confirmed x86-only (no linux aarch64 wheel). See "Standard Stack" + "The Four Spike Fixes". |
| CLOUDIMG-02 | Built + pushed by CI on a **native arm64 runner** (no QEMU), same release triggers + matching tags as x86 | `ubuntu-24.04-arm` GitHub-hosted runner, free for public repos, GA. Add an arm64 job to `docker-publish.yml` reusing the existing semver tag strategy with an `-arm64` suffix. See "Architecture Patterns" → CI. |
| CLOUDIMG-03 | CI guard runs full analysis (MusiCNN + discogs-effnet) and confirms arm64 results match x86 within tolerance (BPM/key exact; model scores within epsilon) | Production analysis surface enumerated (BPM/key/MusiCNN/VGGish/Effnet). Golden-reference comparison pattern. See "Validation Architecture" + "Parity Guard". |

*No CONTEXT.md exists yet (standalone research, run before `/gsd:discuss-phase`). The "Project Constraints" section below stands in for locked decisions until discuss-phase produces them.*
</phase_requirements>

## Project Constraints (from CLAUDE.md + MEMORY)

These have the authority of locked decisions. Research recommendations below comply with all of them.

| Constraint | Implication for Phase 47 |
|-----------|--------------------------|
| **Python 3.14 exclusively** (main app) | The arm64 **agent** image is the documented exception — it must run 3.13 (no cp314 TF wheel exists). Frame this as "agent image pins 3.13; main/api image stays 3.14." Do **not** attempt to drag the whole repo to 3.13. |
| **uv only** (`uv run`, never bare pip) | The agent image installs deps via uv where possible. BUT the from-source essentia build uses `python3 waf` + a system/venv interpreter; the TF wheel + numpy/pyyaml are installed before the waf build. Document where uv applies (phaze package + pure-Python deps) vs. where the source build steps run. |
| **CI delegates to `just`** (workflows call just recipes, not inline shell) | Add `just` recipes (e.g. `image-build-arm64`, `image-push-arm64`, `parity-check`) and have the new workflow job call them. The current `docker-publish.yml` uses `docker/build-push-action` directly — match existing CI style for the build step but keep an operator-facing `just` recipe (mirrors the existing `image-push` recipe). |
| **Pre-commit with frozen SHAs; add hooks as needed** | New Dockerfile must pass `hadolint` (already a hook + `docker-validate.yml`). Keep `# hadolint ignore=` comments minimal and justified, matching the existing root Dockerfile style. |
| **README per service, kept current** | The arm64 agent image is effectively a new build artifact — add/extend a README (e.g. under the agent image dir or `docs/`) documenting the 3.13 pin, the 4 fixes, build + parity commands. |
| **85% coverage, Codecov flags** | The parity guard is an integration check (runs a built image), not unit-testable Python — it lives in CI, not pytest coverage. Any new *Python* helper (golden compare script) should have unit tests. |
| **Worktree branch + PR per phase; never push to main** | Phase 47 lands as its own PR. The spike branch stays unmerged; cherry-pick/transcribe its contents into the production Dockerfile rather than merging `spike/arm64-essentia-analysis` as-is (README says "do not merge as-is"). |
| **Frequent commits during execution** | Standard. |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| essentia native build (C++/TF link) | Build (Dockerfile, arm64 runner) | — | Compiled once at image build; runtime only imports the result |
| Python/dep resolution for agent | Build (uv + TF wheel install) | — | 3.13 pin + TF 2.20.0 fixed at build time |
| Image publish + tagging | CI (GitHub Actions, `docker-publish.yml`) | `just` recipe (operator manual path) | CLOUDIMG-02 mandates CI on native arm64; just recipe is the human fallback |
| Numeric parity verification | CI (new parity job) | golden-compare Python helper | CLOUDIMG-03 gate; helper is reusable + unit-testable |
| Model `.pb` provisioning | Runtime (volume mount at `/models`) | `ensure_models_present` bootstrap | Models are arch-independent frozen graphs; never baked into image (flaky upstream + image bloat) |
| Analysis execution (BPM/key/ML) | Runtime (agent process pool) | — | Same `phaze.services.analysis` code path as x86; only the native libs differ |

## Standard Stack

The arm64 agent image is a **distinct build target** from the existing api/agent image. It does not (and cannot) reuse `Dockerfile` verbatim because that pins `python:3.14-slim`.

### Core (arm64 agent image)
| Component | Version | Purpose | Why / Provenance |
|-----------|---------|---------|------------------|
| Base image | `python:3.13-slim-bookworm` | Runtime + toolchain | aarch64 TF wheels top out at cp313 `[VERIFIED: PyPI tensorflow 2.20.0/2.21.0 — only cp39–cp313 aarch64 wheels]` |
| TensorFlow | `2.20.0` (or `2.21.0`) | C++ runtime essentia links against | TF 2.19 has **no** cp313 aarch64 wheel; 2.20.0 + 2.21.0 do `[VERIFIED: PyPI]`. Spike used 2.20.0 `[CITED: spike README]`. Pin exactly. |
| essentia | git, built `--with-tensorflow`, **pinned to a commit SHA** | BPM/key/ML analysis | Wheel is x86-only `[VERIFIED: PyPI essentia-tensorflow dev1438 — cp314 macos arm64/x86 + manylinux x86_64 only, NO linux aarch64]`. Build from source. Spike used `master`; pin a SHA for reproducibility (README "remaining work" #4). Latest upstream tag is `v2.1_beta5` (old); prefer a recent master SHA. |
| numpy | float to match TF | audio array handling | Spike lets it float with the TF wheel; production pins via lockfile. **Note dual-OpenMP risk** (fix #4). |
| pyyaml | float to match TF | essentia config | Spike installs alongside numpy/TF before the waf build. |
| uv | `0.11.23` (match root Dockerfile) | phaze package + pure-Python dep install | Project mandate. Used for the phaze layer, not the C++ build. |

**essentia source build apt dependencies** (all have aarch64 packages — quoted from spike Dockerfile):
```
build-essential ca-certificates git wget pkg-config
libeigen3-dev libyaml-dev libfftw3-dev
libavcodec-dev libavformat-dev libavutil-dev libswresample-dev
libsamplerate0-dev libtag1-dev libchromaprint-dev
```

**Runtime apt dependencies** (must match the x86 image's runtime needs — from root `Dockerfile`):
```
libatomic1 ffmpeg libsndfile1 libchromaprint-tools libpq5
```
> Plan note: the source-build deps include the `-dev` packages (compile-time); a multi-stage build can drop them from the final layer, but the runtime set above (ffmpeg/fpcalc/libsndfile/libatomic/libpq) MUST remain or the agent crash-loops on `import essentia` / `import phaze` — this is the exact class of incident that produced v4.0.9 and v4.1.1. `chromaprint-tools` provides `fpcalc`; `libchromaprint-dev` provides headers — the build needs both, runtime needs the tool.

### The Four Spike Fixes (bake ALL of these — quoted precisely)

From `spike/arm64-essentia/README.md` and Dockerfile `[CITED: spike branch]`:

1. **Dangling TF symlinks.** `src/3rdparty/tensorflow/setup_from_python.sh` (written for TF ~2.5–2.12) hardcodes TF's old internal package name `tensorflow_core` for the pywrap lib and assumes `libtensorflow_framework.so.2` lives in `/usr/local/lib`. On modern TF both symlinks dangle → `ld: cannot find -lpywrap_tensorflow_internal / -ltensorflow_framework`. **Fix:** repoint them at the real wheel files — the C++ runtime symbols moved to `libtensorflow_cc.so.2` (the 211 KB `_pywrap` is now a shim), so the pywrap slot maps to `libtensorflow_cc.so.2`:
   ```dockerfile
   RUN bash src/3rdparty/tensorflow/setup_from_python.sh \
       && TF=$(python3 -c "import tensorflow,os;print(os.path.dirname(tensorflow.__file__))") \
       && ln -sf "$TF/libtensorflow_cc.so.2"        /usr/local/lib/libpywrap_tensorflow_internal.so \
       && ln -sf "$TF/libtensorflow_framework.so.2" /usr/local/lib/libtensorflow_framework.so \
       && ldconfig
   ```
2. **Linker search path.** `/usr/local/lib` isn't on Debian gcc's default `-l` search. **Fix:** `ENV LIBRARY_PATH=/usr/local/lib` (belt-and-suspenders with the generated `tensorflow.pc`).
3. **Vendored libomp.** `libtensorflow_cc.so.2` has a transitive `DT_NEEDED` on the pip-wheel-vendored `libomp-<hash>.so.5` in the sibling `tensorflow.libs/` dir. **Fix:** add it to `LD_LIBRARY_PATH`:
   ```dockerfile
   ENV LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/python3.13/site-packages/tensorflow:/usr/local/lib/python3.13/site-packages/tensorflow.libs
   ```
4. **Dual-OpenMP runtime conflict — OPEN PRODUCTION BLOCKER.** Importing `numpy.random`'s C extensions *after* essentia/TF is loaded segfaults — TF's LLVM `libomp` vs numpy's OpenBLAS `libgomp` in one process. The spike smoke test sidestepped it with `np.sin`. **This MUST be resolved for production** because the real `phaze.services.analysis` path uses numpy heavily (`np.mean`, aggregation) and essentia `MonoLoader`/`EasyLoader`. Candidate fixes to evaluate (planner should make this a dedicated task with a verification step on real audio): `OMP_NUM_THREADS=1`, preload a single libomp via `LD_PRELOAD`, align numpy build to use the same OpenMP runtime, or confirm the real `MonoLoader` path never triggers `numpy.random`. **Do not assume it's benign** — verify with a real concert file through the actual analysis function, not a synthetic signal. `[ASSUMED]` that one of these fixes works — needs execution-time validation.

### Build order (from spike Dockerfile, in sequence)
1. apt install source-build deps
2. `pip install numpy pyyaml tensorflow==2.20.0` (single toolchain — system gcc + manylinux wheels — so the libstdc++ CXX11 ABI matches between essentia and libtensorflow; mixing ABIs is the #977 segfault)
3. `git clone --depth 1` essentia at pinned SHA
4. `setup_from_python.sh` + symlink repoint (fix #1) + ldconfig
5. `ENV LIBRARY_PATH` (fix #2) + `ENV LD_LIBRARY_PATH` (fix #3, partial)
6. `python3 waf configure --build-static --with-python --with-tensorflow && python3 waf -j$(nproc) && python3 waf install && ldconfig` (~324 s compile per spike)
7. `ENV LD_LIBRARY_PATH` final (fix #3, add `tensorflow.libs`) — placed AFTER the compile layer so it doesn't bust the compile cache
8. Resolve fix #4 (OpenMP)
9. Install phaze package + pure-Python deps (uv)

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Separate `-arm64` single-arch tag | Multi-arch manifest (amd64+arm64 under one tag) | **Explicitly deferred to CLOUDIMG-04.** Phase 47 ships a separate arm64 tag. Don't build the manifest now. |
| Build essentia from source | Wait for an upstream aarch64 essentia-tensorflow wheel | None exists (verified); no ETA. Source build is the only path. |
| TF 2.20.0 | TF 2.21.0 | Both have cp313 aarch64. 2.20.0 is spike-proven; 2.21.0 uses `manylinux_2_27` (newer glibc floor) — fine on bookworm but unproven with essentia's glue. Prefer 2.20.0 unless a security fix forces 2.21.0. |
| `python:3.13-slim-bookworm` | `ubuntu:24.04` + deadsnakes | slim-bookworm is spike-proven and matches the project's slim base philosophy. |
| Native arm64 runner | QEMU emulation on x86 runner | **Forbidden by CLOUDIMG-02** (and ~10× slower for a C++ compile). |

**Installation (image build, native arm64 host):**
```bash
# CI: runs-on: ubuntu-24.04-arm  → native, no buildx --platform needed
docker build --build-arg TF_VERSION=2.20.0 -f Dockerfile.agent-arm64 -t <ghcr>/phaze:<tag>-arm64 .
```

## Package Legitimacy Audit

All packages are pre-existing, long-established project dependencies or OS packages — no new npm/PyPI packages are introduced by this phase. slopcheck not run (no new dependency surface).

| Package | Registry | Age | Source Repo | Disposition |
|---------|----------|-----|-------------|-------------|
| tensorflow `2.20.0` | PyPI | mature, AWS-maintained aarch64 builds | github.com/tensorflow/tensorflow | Approved `[VERIFIED: PyPI]` |
| essentia (source) | github.com/MTG/essentia | mature (MTG/UPF) | github.com/MTG/essentia | Approved — pin to a commit SHA `[VERIFIED: spike build PASS]` |
| numpy, pyyaml | PyPI | mature | — | Approved (already project deps) |
| apt deps (eigen/fftw/libav/taglib/chromaprint/ffmpeg/...) | Debian bookworm | distro-maintained | — | Approved (distro packages, aarch64 available) |

**Packages removed due to slopcheck [SLOP]:** none
**Packages flagged [SUS]:** none

## Architecture Patterns

### System Architecture Diagram (build + verify flow)

```
                          ┌─────────────────────────────────────────────┐
   git tag v5.x / push    │  ci.yml (on: push main + tags v*.*.*)        │
   ───────────────────────▶  detect-changes ─▶ aggregate-results ─▶ ...  │
                          └───────────────┬─────────────────────────────┘
                                          │ uses: docker-publish.yml (secrets: inherit)
                                          ▼
        ┌──────────────────────────────────────────────────────────────────────┐
        │ docker-publish.yml                                                     │
        │                                                                        │
        │  job: build-and-push (EXISTING)        job: build-arm64 (NEW)          │
        │  runs-on: ubuntu-latest                runs-on: ubuntu-24.04-arm       │
        │  platforms: linux/amd64                native aarch64, NO QEMU         │
        │  → ghcr.io/.../phaze:<ver>             → Dockerfile.agent-arm64        │
        │  → ...:latest                          → ghcr.io/.../phaze:<ver>-arm64 │
        │      │                                     → ...:latest-arm64          │
        │      │ (produces/loads golden)              │                          │
        │      ▼                                       ▼                          │
        │  job: parity-guard (NEW) ─────────────────────────────────────────┐    │
        │   run full analysis on arm64 image over reference audio           │    │
        │   compare → BPM exact, key exact, model scores |Δ| ≤ epsilon       │    │
        │   FAIL build if parity breaks (CLOUDIMG-03)                        │    │
        └───────────────────────────────────────────────────────────────────┘    │
                                          │                                       │
                                          ▼                                       │
                           ghcr.io/simplicityguy/phaze:<tag>-arm64  ◀── compute agent pulls (Phase 51)
```

### Recommended file layout
```
Dockerfile.agent-arm64          # NEW — 3.13 + essentia-from-source + 4 fixes
scripts/parity/
├── reference.<ext>             # small committed reference audio clip (or generator)
├── golden-x86.json             # golden analysis output from the x86 image
└── compare_analysis.py         # arch-agnostic compare: exact vs epsilon, unit-tested
.github/workflows/docker-publish.yml   # +job build-arm64, +job parity-guard
justfile                        # +image-build-arm64, +image-push-arm64, +parity-check
docs/ or README                 # 3.13-pin rationale + 4 fixes + build/parity commands
```

### Pattern 1: Separate single-arch arm64 tag (not multi-arch manifest)
**What:** Reuse the existing `docker/metadata-action` semver tag strategy, append `-arm64` (e.g. `latest-arm64`, `5.0.0-arm64`, `v5.0.0-arm64`). The compute-agent compose (Phase 51) pins `PHAZE_IMAGE_TAG` to an `-arm64` tag.
**When to use:** Now (Phase 47). Multi-arch manifest = CLOUDIMG-04, deferred.
**Why:** The current agent compose pulls `ghcr.io/simplicityguy/phaze:${PHAZE_IMAGE_TAG:-latest}` — x86 hosts keep `:latest`, arm64 cloud hosts use `:<tag>-arm64`. No manifest complexity, no risk of an arm64 layer leaking onto x86 pulls.

### Pattern 2: Golden-reference parity (avoid cross-arch runtime)
**What:** Generate `golden-x86.json` once from the x86 image (full analysis over a committed reference clip). The arm64 parity job runs the same analysis on the arm64 image and asserts equality against the golden. Optionally the x86 job re-asserts the golden each run (drift guard).
**When to use:** CLOUDIMG-03.
**Why:** Running the x86 image on the arm64 runner would require QEMU (forbidden) and is slow. A committed golden is deterministic for frozen TF1 graphs + deterministic DSP. Store the golden as a checked-in artifact and regenerate it via a documented `just` recipe when models/essentia version change.

### Pattern 3: Models mounted, never baked
**What:** `.pb` model files mount at `/models` at runtime (spike) / are fetched by `ensure_models_present` (production `model_bootstrap.py`). The image build does NOT download them.
**Why:** `essentia.upf.edu` is flaky; baking ~30+ models bloats the image and couples the build to a download. Frozen graphs are arch-independent so x86 and arm64 share identical `.pb` files — which is *why* numeric parity is even expected.

### Anti-Patterns to Avoid
- **Reusing the 3.14 root `Dockerfile`/`pyproject` requires-python as-is for arm64** — `requires-python = ">=3.14,<3.15"` will hard-fail a 3.13 `uv sync`. Must be addressed (see Pitfall 1).
- **Baking models into the image** — bloat + flaky build + breaks the "identical graphs ⇒ parity" guarantee.
- **Trusting the spike's `np.sin` smoke test as production-ready** — it deliberately dodges the dual-OpenMP segfault (fix #4) and never exercises VGGish, RhythmExtractor2013, or KeyExtractor.
- **QEMU multi-arch build** — forbidden by CLOUDIMG-02; also unworkably slow for the C++ compile.
- **Letting essentia float on `master`** — pin a commit SHA for reproducible builds + cacheable clone layer.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| arm64 CI runner | self-hosted Ampere box / QEMU | `runs-on: ubuntu-24.04-arm` | Free for public repos, GA, native, 4 vCPU |
| Image tag/label generation | bespoke tag shell | existing `docker/metadata-action` (already in `docker-publish.yml`) + `-arm64` suffix | Verified tag-strategy test already asserts latest+version |
| Build layer cache | hand-rolled tar cache | buildx cache (`type=gha` or `type=registry`) + pinned essentia SHA for clone-layer reuse | Existing `docker-build-cache` action uses `type=local`; a registry/gha cache survives across native arm64 runners better |
| essentia↔TF linking | manual `.so` hunting | the spike's exact `setup_from_python.sh` + 3 symlink/env fixes | Already solved and proven |
| Float comparison in parity | `==` on model scores | `numpy.isclose` / `math.isclose` with explicit `atol`/`rtol` | Frozen graphs are deterministic but tiny FP differences across BLAS/arch are normal; exact-equal only for BPM (rounded 0.1) + key (string) |

**Key insight:** Nearly every hard problem in this phase is *already solved by the spike or by GitHub's platform.* The net-new engineering is: (a) the Python-3.13/requires-python reconciliation, (b) the dual-OpenMP fix validated on real audio, (c) full-surface parity coverage, and (d) CI wiring + caching so the C++ compile is tractable.

## Common Pitfalls

### Pitfall 1: `requires-python = ">=3.14,<3.15"` blocks the 3.13 agent build
**What goes wrong:** A `uv sync` / `uv pip install .` inside a `python:3.13` image fails immediately — the project metadata forbids 3.13.
**Why it happens:** The repo is intentionally 3.14-only for the main app; the agent image is the first 3.13 consumer.
**How to avoid:** Decide the reconciliation strategy in discuss-phase. Options for the planner to weigh:
  - (a) Relax `requires-python` to `>=3.13,<3.15` and keep the essentia platform marker doing the arch gating. Simplest; widens the whole project's declared support to 3.13 (mostly harmless, but mypy/ci still target 3.14).
  - (b) A dedicated agent dependency manifest / `uv` override scoped to the arm64 image (separate constraints file or a `[tool.uv]` environment) so the main `requires-python` is untouched.
  - (c) Install the phaze package into the arm64 image with `--python-version`/marker overrides.
**Warning signs:** `error: Package 'phaze' requires Python >=3.14` during the image build.
`[ASSUMED]` that option (a) or (b) is acceptable to the operator — this is the #1 discuss-phase decision.

### Pitfall 2: Dual-OpenMP segfault on real analysis (spike fix #4)
**What goes wrong:** Image imports essentia fine, smoke test passes, but the real `process_file` → `analyze` path segfaults when numpy's OpenBLAS `libgomp` and TF's LLVM `libomp` collide in one process.
**Why it happens:** Two OpenMP runtimes loaded in the same process; triggered by numpy C-extension paths (notably `numpy.random`) after TF/essentia load.
**How to avoid:** Resolve fix #4 (env `OMP_NUM_THREADS=1` / single-libomp `LD_PRELOAD` / aligned numpy build) AND verify by running the *actual* `phaze.services.analysis` analyze function over a real concert file in the arm64 image — not `np.sin`.
**Warning signs:** Clean import + smoke pass, then SIGSEGV (faulthandler native stack mentions `libgomp`/`libomp`) under real workload.

### Pitfall 3: Parity guard under-covers the production surface
**What goes wrong:** Guard only checks MusiCNN + Effnet (the spike's two), arm64 passes, but `TensorflowPredictVGGish` (used by every `_make_standard_set` variant), `RhythmExtractor2013(method="multifeature")` (BPM), or `KeyExtractor(profileType="edma")` (key) diverge or crash on arm64 and ship undetected.
**Why it happens:** The spike's `run_test.py` only exercised 2 of the 3 classifier types and zero of the DSP extractors.
**How to avoid:** The parity golden must include every production output: aggregated `bpm` (exact, rounded 0.1), `musical_key` (exact string), and the mood/style/danceability model scores (epsilon). Drive it through `phaze.services.analysis` so the real code path is what's compared.
**Warning signs:** Parity green but production analysis returns nulls or crashes on the agent.

### Pitfall 4: C++ compile blows the CI time budget / no cache reuse
**What goes wrong:** ~324 s essentia compile (plus TF download) on every build; `docker-publish.yml` job `timeout-minutes: 30` plus disk pressure.
**Why it happens:** No layer cache reuse; essentia on `master` (clone layer changes); large TF wheel.
**How to avoid:** Pin essentia to a SHA (stable clone layer), order Dockerfile so the heavy compile layer only busts when TF/essentia version or build flags change, and use a persistent buildx cache (`type=gha` or `type=registry,ref=ghcr.io/.../phaze:buildcache-arm64`). Consider `ccache` mounted via `RUN --mount=type=cache` for incremental C++ rebuilds. Raise the arm64 job timeout if needed (it's a separate job, won't slow the x86 build).
**Warning signs:** Every run recompiles essentia from scratch; job approaches/exceeds 30 min.

### Pitfall 5: Missing runtime libs reproduce the v4.0.9 / v4.1.1 incidents
**What goes wrong:** A multi-stage build that drops `-dev` packages also accidentally drops a runtime `.so` (libatomic1/ffmpeg/libsndfile1/fpcalc/libpq5) → `import essentia` or `import phaze` fails only at container start (build passes).
**Why it happens:** Build succeeds without proving the image boots; exactly the v4.1.0 psycopg/libpq regression.
**How to avoid:** Keep the runtime apt set from the root Dockerfile in the final stage, and add an **import-smoke step** (mirror `docker-validate.yml`'s `import phaze.main` check, adapted to the agent entrypoint `saq phaze.tasks.agent_worker.settings` import graph) on the arm64 image before publish.
**Warning signs:** Green build, container exits at startup with `ImportError`/missing `.so`.

## Code Examples

### New arm64 build job (sketch for `docker-publish.yml`)
```yaml
# Source: derived from existing docker-publish.yml build-and-push job + verified ubuntu-24.04-arm label
  build-arm64:
    runs-on: ubuntu-24.04-arm        # native aarch64, free for public repos, NO QEMU
    timeout-minutes: 60              # essentia C++ compile is ~5+ min cold
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0   # v7.0.0
      - name: Log in to GHCR
        if: github.event_name != 'pull_request'
        uses: docker/login-action@650006c6eb7dba73a995cc03b0b2d7f5ca915bee  # v4.2.0
        with: { registry: ghcr.io, username: ${{ github.actor }}, password: ${{ secrets.GITHUB_TOKEN }} }
      - uses: docker/metadata-action@80c7e94dd9b9319bd5eb7a0e0fe9291e23a2a2e9  # v6.1.0
        id: meta
        with:
          images: ghcr.io/${{ env.IMAGE_NAME }}
          flavor: suffix=-arm64,onlatest=true     # latest-arm64, <ver>-arm64
          tags: |
            type=raw,value=latest,enable={{is_default_branch}}
            type=semver,pattern={{version}}
            type=ref,event=tag
      - uses: docker/setup-buildx-action@d7f5e7f509e45cec5c76c4d5afdd7de93d0b3df5  # v4.1.0
      - uses: docker/build-push-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf  # v7.2.0
        with:
          context: .
          file: Dockerfile.agent-arm64
          platforms: linux/arm64
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          cache-from: type=gha,scope=arm64
          cache-to: type=gha,scope=arm64,mode=max
          build-args: TF_VERSION=2.20.0
```
> Verify `flavor: suffix` semantics against `docker/metadata-action` docs at plan time `[CITED: github.com/docker/metadata-action]` — the suffix/onlatest behavior is the cleanest way to get `latest-arm64` + `<ver>-arm64`.

### Parity comparison helper (shape)
```python
# Source: derived from src/phaze/services/analysis.py output contract
# BPM exact (rounded 0.1), key exact (string), model scores within epsilon.
import json, math, sys

def compare(golden: dict, actual: dict, *, atol: float = 1e-4) -> list[str]:
    fails: list[str] = []
    if golden["bpm"] != actual["bpm"]:
        fails.append(f"bpm {golden['bpm']} != {actual['bpm']}")
    if golden["musical_key"] != actual["musical_key"]:
        fails.append(f"key {golden['musical_key']!r} != {actual['musical_key']!r}")
    for name, gv in golden["model_scores"].items():
        av = actual["model_scores"].get(name)
        if av is None or not math.isclose(gv, av, abs_tol=atol):
            fails.append(f"score {name}: {gv} vs {av} (atol={atol})")
    return fails
```
> `atol` is a placeholder — the planner should pick epsilon empirically from a few real files (CLOUDIMG-03 says "small epsilon"). BPM/key are exact.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| No arm64 essentia (wheel x86-only) | Build essentia from source vs AWS `tensorflow` aarch64 wheel | Proven 2026-06-23 (spike) | Unlocks OCI Always-Free A1 perpetual compute |
| QEMU multi-arch only | Free native `ubuntu-24.04-arm` GH runners | GA 2025-08-07 | Native arm64 CI builds at no cost (public repo) |
| essentia-tensorflow wheel (cp314 x86) | Source build pins Python 3.13 + TF 2.20.0 | n/a | Agent image diverges from 3.14 main app — by design |

**Deprecated/outdated:**
- TF 2.19.0 for this purpose — no cp313 aarch64 wheel (verified). Use 2.20.0+.
- The spike's `np.sin` smoke test — superseded by a real-audio parity guard for production.

## Runtime State Inventory

Not a rename/refactor phase — section is largely N/A, but two cross-cutting state items matter:
| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Build artifacts | No existing arm64 image/tag on GHCR; `spike/arm64-essentia-analysis` branch holds the proof (unmerged) | New `Dockerfile.agent-arm64`; transcribe spike, do not merge branch as-is (README directive) |
| Config/tag surface | `docker-compose.agent.yml` pins `PHAZE_IMAGE_TAG` (default `latest`) | Phase 51 will point cloud agent at `<tag>-arm64`; Phase 47 only produces the tag |
| Stored data | None | None — frozen `.pb` models are arch-independent and already provisioned by `ensure_models_present` |
| OS-registered state | None | None |
| Secrets/env vars | `GITHUB_TOKEN` (GHCR push, already wired in `docker-publish.yml`) | None new |

## Validation Architecture

> nyquist_validation is enabled (config.json) → section included.

### Test Framework
| Property | Value |
|----------|-------|
| Unit framework | pytest + pytest-asyncio (existing) — for the golden-compare helper only |
| Image validation | CI-level (built-image run), not pytest coverage |
| Quick run (unit) | `uv run pytest tests/test_parity/ -x` (new helper tests) |
| Full image guard | new `parity-guard` job in `docker-publish.yml` |
| hadolint gate | existing `docker-validate.yml` (extend matrix to include `Dockerfile.agent-arm64`) |

### Observable signals that prove the phase works
| Signal | How observed | Pass condition | Requirement |
|--------|--------------|----------------|-------------|
| arm64 image exists + boots | `docker run <tag>-arm64` import-smoke (agent_worker import graph) | exit 0, no `ImportError`/missing `.so` | CLOUDIMG-01 |
| essentia imports on arm64 (no #977 segfault) | run import inside image | no SIGSEGV, faulthandler clean | CLOUDIMG-01 |
| Built on native arm64, no QEMU | CI job `runs-on: ubuntu-24.04-arm`; `uname -m`=aarch64 in a step | job green; matching `-arm64` tag pushed on release | CLOUDIMG-02 |
| All 3 classifier families run | parity job runs MusiCNN + **VGGish** + Effnet over reference audio | finite arrays, expected ranks | CLOUDIMG-03 |
| BPM parity | RhythmExtractor2013 multifeature on reference, aggregated | **exact** match to golden (rounded 0.1) | CLOUDIMG-03 |
| Key parity | KeyExtractor edma on reference, aggregated | **exact** string match to golden | CLOUDIMG-03 |
| Model-score parity | mood/style/danceability scores | `|Δ| ≤ epsilon` (atol picked empirically) | CLOUDIMG-03 |
| Dual-OpenMP fix holds | real-audio analyze (not np.sin) on arm64 | no segfault, returns full result | CLOUDIMG-01 (fix #4) |
| Build fails on parity break | inject a mismatch / tolerance test | parity job exits non-zero, blocks publish | CLOUDIMG-03 |

### Requirements → Test Map
| Req | Behavior | Test Type | Automated Command (sketch) | Exists? |
|-----|----------|-----------|----------------------------|---------|
| CLOUDIMG-01 | image boots + essentia imports on arm64 | image smoke | `docker run <tag>-arm64 python -c "import essentia.standard"` | ❌ Wave 0 |
| CLOUDIMG-01 | real-audio analyze (OpenMP fix) | image integration | `docker run -v models -v ref <tag>-arm64 <analyze cmd>` | ❌ Wave 0 |
| CLOUDIMG-02 | native arm64 publish on release | CI job presence + tag assertion | extend `tests/test_deployment/test_agent_compose.py` style tag test for `-arm64` | ❌ Wave 0 |
| CLOUDIMG-03 | numeric parity vs golden | CI parity job + helper unit test | `uv run pytest tests/test_parity/` + parity-guard job | ❌ Wave 0 |

### Wave 0 Gaps
- [ ] `Dockerfile.agent-arm64` — the build artifact itself
- [ ] `scripts/parity/reference.<ext>` — small committed reference clip (license-clean) or deterministic generator
- [ ] `scripts/parity/golden-x86.json` — golden output captured from the x86 image
- [ ] `scripts/parity/compare_analysis.py` + `tests/test_parity/` — unit-tested comparator
- [ ] `parity-guard` + `build-arm64` jobs in `docker-publish.yml`; extend `docker-validate.yml` hadolint matrix
- [ ] `just` recipes: `image-build-arm64`, `image-push-arm64`, `parity-check`, `parity-golden-regen`
- [ ] Tag-strategy test extended for `-arm64`

## Security Domain

> security_enforcement absent in config → treated as enabled. This is a build/supply-chain phase, so the relevant controls are V6 (crypto/integrity) and V14 (configuration/build), not app-runtime auth.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|------------------|
| V2 Authentication | no | No new auth surface (agent token is Phase 48) |
| V5 Input Validation | minimal | Parity helper validates JSON shape; reference audio is trusted |
| V6 Cryptography / Integrity | yes | Pin essentia to a commit SHA; pin TF `==2.20.0`; keep frozen-SHA GitHub Actions (already the repo norm); GHCR push uses `GITHUB_TOKEN` least-priv (`packages: write` only) |
| V14 Build / Config | yes | hadolint gate on the new Dockerfile; provenance/SBOM already enabled in `docker-publish.yml` (extend to the arm64 job); no secrets baked into image |

### Threat patterns for this build
| Pattern | STRIDE | Mitigation |
|---------|--------|------------|
| Supply-chain (malicious essentia/TF version) | Tampering | Pin TF exact + essentia SHA; verify TF wheel hashes; build from official MTG repo only |
| Compromised GH Action | Tampering/Elevation | Continue frozen-SHA pinning (repo mandate) for any new action |
| Token over-scope | Elevation | Job `permissions: { contents: read, packages: write }` only (matches existing) |
| Image provenance gap | Repudiation | Enable `provenance: true` + `sbom: true` on the arm64 build (parity with x86 job) |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | One of the candidate dual-OpenMP fixes (OMP_NUM_THREADS=1 / LD_PRELOAD / aligned numpy) makes real-audio analysis stable on arm64 | Spike Fix #4 / Pitfall 2 | HIGH — if none work, the agent segfaults on real workloads; may need deeper numpy/TF rebuild work. Must be validated in execution, not assumed. |
| A2 | Relaxing `requires-python` to include 3.13 (or a scoped uv override) is acceptable to the operator | Pitfall 1 | MEDIUM — blocks the agent build until resolved; pure decision, needs discuss-phase. |
| A3 | TF 2.20.0 (spike-proven) is preferred over 2.21.0 absent a forcing security need | Standard Stack | LOW — both have cp313 aarch64; 2.21.0 just unproven with essentia glue. |
| A4 | Frozen TF1 `.pb` graphs + deterministic DSP yield BPM/key *exactly* equal across arch (only model scores need epsilon) | Validation Architecture | MEDIUM — if BPM/key differ slightly on arm64, the "exact" requirement (CLOUDIMG-03) needs a tolerance carve-out; confirm empirically when generating the golden. |
| A5 | `docker/metadata-action` `flavor: suffix=-arm64,onlatest=true` produces `latest-arm64` + `<ver>-arm64` cleanly | Code Examples | LOW — verify against action docs; fallback is explicit `type=raw` tags. |

## Open Questions

1. **Python-3.13 reconciliation (Pitfall 1 / A2)** — Relax `requires-python`, scope a uv override, or maintain a separate agent manifest? *Recommendation:* decide in discuss-phase; a scoped override keeps the main app's 3.14 contract pristine.
2. **Reference audio for the golden** — What clip? Needs to be license-clean to commit, short (fast CI), and exercise BPM/key/all-3-model-families meaningfully. *Recommendation:* a short (~30–60 s) CC0/self-generated clip, or a deterministic synthesized signal that still produces non-degenerate model outputs. A pure 440 Hz sine (spike) is too degenerate for meaningful model-score parity.
3. **Epsilon value (A4/CLOUDIMG-03)** — pick `atol` from observed x86↔arm64 deltas on a few real files. BPM/key expected exact; confirm.
4. **essentia pin** — specific master SHA (latest tag `v2.1_beta5` is old and predates the TF glue maturity). *Recommendation:* pin the spike-tested master SHA or a recent verified master commit.
5. **Cache backend** — `type=gha` (simple, per-repo cache) vs `type=registry` (survives runner churn, shareable). *Recommendation:* start with `type=gha,scope=arm64`; escalate to registry cache if the cold compile dominates.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| `ubuntu-24.04-arm` GH runner | CLOUDIMG-02 native build | ✓ (public repo) | GA 2025-08-07, 4 vCPU Cobalt 100 | none needed |
| TF aarch64 cp313 wheel | essentia source build | ✓ | 2.20.0 / 2.21.0 | none (2.19 lacks cp313) |
| essentia source (MTG) | CLOUDIMG-01 | ✓ | git master (pin SHA) | none |
| aarch64 apt build/runtime deps | image build | ✓ | bookworm | none |
| cp314 TF wheel (any arch) | (would enable 3.14 agent) | ✗ | — | **Pin Python 3.13** — this is the whole reason for the 3.13 agent image |
| GHCR | publish | ✓ | — | none |

**Missing dependencies with no fallback:** none block the phase.
**Missing dependencies with fallback:** cp314 TF wheel absent → fall back to Python 3.13 (the core design decision).

## Sources

### Primary (HIGH confidence)
- PyPI JSON API `pypi.org/pypi/tensorflow/{2.19.0,2.20.0,2.21.0,latest}/json` — aarch64 wheel matrix; confirmed cp313 ceiling, zero cp314 wheels `[VERIFIED]`
- PyPI JSON API `pypi.org/pypi/essentia-tensorflow/json` — dev1438 wheels are macos arm64/x86 + manylinux x86_64 only, **no linux aarch64** `[VERIFIED]`
- `spike/arm64-essentia-analysis` branch: `spike/arm64-essentia/{Dockerfile,README.md,run_test.py}` — the proof + 4 fixes + working combo `[CITED]`
- Repo: root `Dockerfile`, `.github/workflows/{docker-publish,docker-validate,ci}.yml`, `.github/actions/docker-build-cache/action.yml`, `pyproject.toml`, `docker-compose.agent.yml`, `src/phaze/services/analysis.py`, `src/phaze/tasks/agent_worker.py`, `justfile` `[VERIFIED: codebase]`
- GitHub Changelog: "arm64 hosted runners for public repositories are now generally available" (2025-08-07) — `ubuntu-24.04-arm` label, free for public repos `[CITED]`

### Secondary (MEDIUM confidence)
- GitHub Changelog "Linux arm64 hosted runners ... public preview" (2025-01-16); InfoQ coverage — corroborate runner specs/labels
- `gh repo view` — `SimplicityGuy/phaze` visibility PUBLIC (enables free runners) `[VERIFIED]`

### Tertiary (LOW confidence)
- `docker/metadata-action` `flavor: suffix` exact semantics — to be confirmed against action docs at plan time

## Metadata

**Confidence breakdown:**
- Standard stack (3.13 / TF 2.20.0 / essentia source): HIGH — spike-proven + registry-verified
- arm64 CI runner: HIGH — GA, verified label, public repo confirmed
- Build caching: MEDIUM — known patterns, not yet exercised on this compile
- Parity guard: MEDIUM — pattern is clear, but golden + epsilon + full-surface coverage are net-new and need empirical tuning
- Dual-OpenMP fix (#4): MEDIUM — flagged by spike as the one open production blocker; resolution unverified

**Research date:** 2026-06-24
**Valid until:** ~2026-07-24 (TF wheel availability + GH runner offering are the fastest-moving facts; re-check if a cp314 aarch64 TF wheel appears, which would reopen the 3.13-vs-3.14 question)
