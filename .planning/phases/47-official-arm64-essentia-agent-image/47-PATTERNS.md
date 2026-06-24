# Phase 47: Official arm64 essentia agent image - Pattern Map

**Mapped:** 2026-06-24
**Files analyzed:** 7 (5 new, 2 modified)
**Analogs found:** 7 / 7 (every new file has a strong in-repo or spike-branch analog)

> No CONTEXT.md yet (research ran before discuss-phase). File list extracted from
> 47-RESEARCH.md §"Recommended file layout" + §"Wave 0 Gaps". The spike branch
> `spike/arm64-essentia-analysis` is the **source-of-truth pattern** for the
> Dockerfile but is NOT a file in this branch — read it via
> `git show spike/arm64-essentia-analysis:spike/arm64-essentia/Dockerfile`.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|-------------------|------|-----------|----------------|---------------|
| `Dockerfile.agent-arm64` (NEW) | config / build artifact | batch (image build) | `spike/.../Dockerfile` (pattern) + root `Dockerfile` (runtime layer) | exact (spike) + role-match (runtime) |
| `.github/workflows/docker-publish.yml` (MODIFY) | config / CI | event-driven (release/push) | existing `build-and-push` job in same file | exact |
| `.github/workflows/docker-validate.yml` (MODIFY) | config / CI | event-driven (PR) | existing hadolint matrix in same file | exact |
| `justfile` (MODIFY) | config / build | request-response (operator CLI) | existing `image-push` + `docker-validate` recipes | exact |
| `scripts/parity/compare_analysis.py` (NEW) | utility | transform (JSON compare) | `src/phaze/scripts/download_models.py` (module shape) + `analyze_file` output contract | role-match |
| `tests/test_parity/test_compare_analysis.py` (NEW) | test | request-response (assert) | `tests/test_scripts/test_download_models.py` (unit) + `tests/test_deployment/test_agent_compose.py` (YAML/CI assert) | exact |
| `scripts/parity/golden-x86.json` + `reference.<ext>` (NEW) | config / fixture | file-I/O | spike `run_test.py` model-run pattern; `analyze_file` return dict = JSON schema | role-match |
| README / `docs/` arm64 agent doc (NEW/MODIFY) | doc | — | root `Dockerfile` header comments (incident provenance style) | role-match |

---

## Pattern Assignments

### `Dockerfile.agent-arm64` (config, batch build)

**Primary analog (the build recipe):** `spike/arm64-essentia-analysis:spike/arm64-essentia/Dockerfile` — read with
`git show spike/arm64-essentia-analysis:spike/arm64-essentia/Dockerfile`. This is the PROVEN 4-fix build. Transcribe it; do NOT merge the branch.

**Secondary analog (runtime hardening + uv + non-root user):** root `/Users/Robert/Code/public/phaze/Dockerfile`.

**Base + TF pin pattern** (from spike Dockerfile, lines verified this session):
```dockerfile
FROM python:3.13-slim-bookworm          # NOT 3.14 — no cp314 TF wheel exists
ARG TF_VERSION=2.20.0                    # spike used build-arg; 2.20.0 is proven (2.19 has no cp313 aarch64 wheel)
```

**Source-build apt deps** (spike Dockerfile — `-dev` packages, compile-time only):
```dockerfile
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates git wget pkg-config \
        libeigen3-dev libyaml-dev libfftw3-dev \
        libavcodec-dev libavformat-dev libavutil-dev libswresample-dev \
        libsamplerate0-dev libtag1-dev libchromaprint-dev \
    && rm -rf /var/lib/apt/lists/*
```

**The 4 spike fixes (bake ALL — quoted exactly from spike Dockerfile):**
```dockerfile
# Single toolchain so libstdc++ CXX11 ABI matches (avoids essentia #977 segfault)
# hadolint ignore=DL3013
RUN python3 -m pip install --no-cache-dir numpy pyyaml "tensorflow==${TF_VERSION}"

# FIX #1 — repoint dangling setup_from_python.sh symlinks at the real wheel .so files
RUN bash src/3rdparty/tensorflow/setup_from_python.sh \
    && TF=$(python3 -c "import tensorflow,os;print(os.path.dirname(tensorflow.__file__))") \
    && ln -sf "$TF/libtensorflow_cc.so.2"        /usr/local/lib/libpywrap_tensorflow_internal.so \
    && ln -sf "$TF/libtensorflow_framework.so.2" /usr/local/lib/libtensorflow_framework.so \
    && ldconfig

# FIX #2 (LIBRARY_PATH = link-time -L) + FIX #3 partial (LD_LIBRARY_PATH = runtime)
ENV LIBRARY_PATH=/usr/local/lib \
    LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/python3.13/site-packages/tensorflow

RUN python3 waf configure --build-static --with-python --with-tensorflow \
    && python3 waf -j"$(nproc)" && python3 waf install && ldconfig

# FIX #3 final — add vendored libomp dir AFTER the compile layer (so it doesn't bust the compile cache)
ENV LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/python3.13/site-packages/tensorflow:/usr/local/lib/python3.13/site-packages/tensorflow.libs
```
> **FIX #4 (dual-OpenMP) is NOT in the spike Dockerfile** — it is the OPEN production blocker. The planner MUST add a dedicated task to resolve it (`OMP_NUM_THREADS=1` env / single-libomp `LD_PRELOAD` / aligned numpy) and verify on a REAL concert file through `phaze.services.analysis.analyze_file`, not `np.sin`. See RESEARCH Pitfall 2 / Assumption A1.

**Runtime-libs hardening pattern — COPY FROM root `Dockerfile` lines 5-21** (this is the v4.0.9/v4.1.1 incident class; the final stage MUST keep these or the agent crash-loops on `import essentia`/`import phaze`):
```dockerfile
# essentia links libatomic.so.1; decode/fingerprint needs ffmpeg+ffprobe, libsndfile.so.1,
# fpcalc+libchromaprint.so.1; libpq5 backs psycopg's SAQ PostgresQueue broker.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 ffmpeg libsndfile1 libchromaprint-tools libpq5 \
    && rm -rf /var/lib/apt/lists/*
```
> Note: source build uses `libchromaprint-dev` (headers); runtime needs `libchromaprint-tools` (the `fpcalc` binary). A multi-stage build that drops `-dev` packages MUST still install the runtime set above in the final stage.

**uv install pattern — COPY FROM root `Dockerfile` lines 23-39** (caveat: `requires-python` blocks 3.13 — see Shared Patterns / Pitfall 1):
```dockerfile
COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
ENV UV_NO_SYNC=1
```

**Non-root uid-1000 pattern — COPY FROM root `Dockerfile` lines 41-45** (the scan-uid incident fix; keep uid 1000 for media-read parity):
```dockerfile
RUN groupadd -g 1000 phaze && useradd -m -u 1000 -g 1000 phaze
USER phaze
```

**Hadolint-ignore comment style — COPY FROM root `Dockerfile` lines 15-18**: justify every `# hadolint ignore=` inline (DL3008 unpinned-apt is the established exception). Spike already uses `DL3008` + `DL3013`.

**Models-mounted-not-baked pattern — FROM spike Dockerfile**: `RUN mkdir -p /models` only; never download `.pb` files at build (production uses `ensure_models_present`, imported in `src/phaze/tasks/agent_worker.py:61`).

---

### `.github/workflows/docker-publish.yml` — add `build-arm64` + `parity-guard` jobs (config, event-driven CI)

**Analog:** the existing `build-and-push` job in the SAME file (`/Users/Robert/Code/public/phaze/.github/workflows/docker-publish.yml` lines 19-145). Mirror it, changing only runner, dockerfile, platform, and tag suffix.

**Runner + permissions** (NEW job — RESEARCH §Code Examples, verified `ubuntu-24.04-arm` label):
```yaml
  build-arm64:
    runs-on: ubuntu-24.04-arm        # native aarch64, free for public repos, NO QEMU
    timeout-minutes: 60              # essentia C++ compile is ~324s cold (vs the x86 job's 30)
    permissions:
      contents: read
      packages: write
```

**GHCR login — COPY FROM lines 80-86** (frozen-SHA action, `GITHUB_TOKEN`, `if: github.event_name != 'pull_request'`):
```yaml
      - name: "🔒 Log in to the GitHub Container Registry"
        if: github.event_name != 'pull_request'
        uses: docker/login-action@650006c6eb7dba73a995cc03b0b2d7f5ca915bee  # v4.2.0
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
```

**Tag strategy — COPY FROM `meta` step lines 96-115, ADD `-arm64` suffix.** The existing step (and the test that guards it) is the canonical pattern:
```yaml
      - name: "📊 Extract metadata (tags, labels) for Docker"
        id: meta
        uses: docker/metadata-action@80c7e94dd9b9319bd5eb7a0e0fe9291e23a2a2e9  # v6.1.0
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          flavor: suffix=-arm64,onlatest=true      # → latest-arm64, <ver>-arm64
          tags: |
            type=raw,value=latest,enable={{is_default_branch}}
            type=semver,pattern={{version}}
            type=ref,event=tag
```
> Verify `flavor: suffix` semantics against docker/metadata-action docs at plan time (RESEARCH A5); fallback is explicit `type=raw` `-arm64` tags. Note the `IMAGE_NAME` lowercasing happens in the "🏷️ Set lowercase image name" step (lines 52-57) — replicate it in the arm64 job.

**Build-push — COPY FROM lines 125-145, add provenance/sbom parity** (RESEARCH Security §V14):
```yaml
      - uses: docker/build-push-action@f9f3042f7e2789586610d6e8b85c8f03e5195baf  # v7.2.0
        with:
          context: .
          file: Dockerfile.agent-arm64
          platforms: linux/arm64
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          provenance: true
          sbom: true
          cache-from: type=gha,scope=arm64       # NOT the local-cache action (that's x86 runner.temp)
          cache-to: type=gha,scope=arm64,mode=max
          build-args: TF_VERSION=2.20.0
```
> Cache divergence: the existing x86 job uses `./.github/actions/docker-build-cache` (`type=local`, see `.github/actions/docker-build-cache/action.yml`). For arm64, RESEARCH recommends `type=gha,scope=arm64` (survives runner churn, no `Move cache` shuffle). Don't reuse the local-cache action verbatim.

**`parity-guard` job:** no exact analog — it `docker run`s the built arm64 image over `scripts/parity/reference.<ext>` + `/models`, dumps `analyze_file` output to JSON, and shells out to `scripts/parity/compare_analysis.py` against `scripts/parity/golden-x86.json`. Closest shape: the import-smoke step in `docker-validate.yml` lines 64-72 (a `docker run` that asserts the image behaves). Fail the build on non-zero exit (CLOUDIMG-03).

---

### `.github/workflows/docker-validate.yml` — extend hadolint matrix (config, event-driven CI)

**Analog:** the existing matrix + steps in the SAME file (`/Users/Robert/Code/public/phaze/.github/workflows/docker-validate.yml` lines 22-72).

**Matrix-add pattern — COPY FROM lines 22-33** (add an `agent-arm64` entry):
```yaml
    strategy:
      matrix:
        include:
          - name: api
            dockerfile: Dockerfile
            context: .
          - name: agent-arm64                 # NEW
            dockerfile: Dockerfile.agent-arm64
            context: .
```

**Hadolint gate — COPY FROM lines 42-46** (frozen-SHA, `failure-threshold: error`):
```yaml
      - name: 🔍 Validate Dockerfile with hadolint
        uses: hadolint/hadolint-action@2332a7b74a6de0dda2e2221d575162eba76ba5e5  # v3.3.0
        with:
          dockerfile: ${{ matrix.dockerfile }}
          failure-threshold: error
```
> Caveat: the `🐳 Test Docker build` step (lines 48-56) runs on `ubuntu-latest` (x86). An arm64-only Dockerfile can be hadolint-linted there, but a full `docker build` of the C++ compile under QEMU is forbidden/slow — gate the actual build in the native-arm64 `docker-publish.yml` job instead, and consider `if: matrix.name != 'agent-arm64'` on the build/import-smoke steps here (lint-only for arm64).

**Import-smoke analog — lines 64-72** (the v4.1.0 regression guard): adapt for the agent entrypoint. The api image imports `phaze.main`; the agent image's entry is `saq phaze.tasks.agent_worker.settings` (see `src/phaze/tasks/agent_worker.py:1`), so smoke `uv run python -c "import phaze.tasks.agent_worker; import essentia.standard"` inside the built arm64 image (runs in the native arm64 publish job).

---

### `justfile` — add `image-build-arm64`, `image-push-arm64`, `parity-check`, `parity-golden-regen` (config, operator CLI)

**Analog:** existing `image-push` recipe (lines 248-268) + `docker-validate` recipe (lines 237-246) in `/Users/Robert/Code/public/phaze/justfile`.

**Image build/push recipe pattern — COPY FROM `image-push` lines 248-268** (bash shebang, `set -e`, derive OWNER/REPO from git remote, lowercase):
```just
[doc('Build the arm64 essentia agent image (native arm64 host)')]
[group('docker')]
image-build-arm64 TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}-arm64"
    docker build --build-arg TF_VERSION=2.20.0 -f Dockerfile.agent-arm64 -t "${IMAGE}" .
```
> Per project rule (MEMORY: workflows delegate to just): the CI `build-arm64` job MAY call `just image-build-arm64`, OR keep `docker/build-push-action` for the CI build (matching the existing x86 job style) while the `just` recipe is the operator fallback — RESEARCH §Project Constraints leans toward the latter (mirror existing `image-push`).

**`parity-check` recipe pattern — model on `download-models` (lines 310-313, delegates to a script) + `pip-audit` (lines 192-206, bash with logic):**
```just
[doc('Run the arm64↔x86 numeric parity check against the committed golden')]
[group('docker')]
parity-check TAG="latest":
    #!/usr/bin/env bash
    set -e
    # docker run the arm64 image over scripts/parity/reference.<ext>, dump JSON,
    # then: uv run python scripts/parity/compare_analysis.py golden-x86.json actual.json
```

---

### `scripts/parity/compare_analysis.py` (utility, transform)

**Analog (module shape, header-comment provenance style, `from __future__`, typed helpers):** `src/phaze/scripts/download_models.py` (a self-contained script with unit-tested pure functions). The compare logic operates on the `analyze_file` output dict.

**Output contract to compare against — `src/phaze/services/analysis.py` `analyze_file` return, lines 575-588** (THIS is the JSON schema the golden + actual share):
```python
return {
    "bpm": aggregate_bpm(fine_windows),          # float|None, rounded 0.1 — EXACT match
    "musical_key": aggregate_key(fine_windows),  # str|None  — EXACT match
    "mood": aggregate_dominant(coarse_windows, "mood"),      # str|None — exact (or epsilon on underlying scores)
    "style": aggregate_dominant(coarse_windows, "style"),    # str|None
    "danceability": aggregate_danceability(coarse_windows),  # float|None — epsilon
    "features": _representative_features(coarse_windows),    # dict model scores — epsilon (atol)
    "windows": windows, ...
}
```
> Classifier families the parity guard MUST cover (RESEARCH Pitfall 3) — from `analysis.py` lines 56-92: `musicnn` (msd + mtt variants), `vggish`, and `effnet_discogs`. The spike only ran musicnn+effnet; production also runs VGGish via every `_make_standard_set` (line 54). BPM comes from `RhythmExtractor2013` (aggregated at line 465-466), key from `KeyExtractor`.

**Comparator shape — RESEARCH §Code Examples** (exact for bpm/key, `math.isclose(atol=...)` for scores; `atol` picked empirically):
```python
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
> Ruff/style note: `target-version = py313`, line-length 150, double quotes, `from __future__ import annotations`, type hints on all functions (mypy strict, though `scripts/` may sit outside the `^(tests/|prototype/|services/)` mypy exclude — confirm it's typed). `T201` (print) is allowed in CLI/entry points per pyproject per-file-ignores; if this script prints, add it to `[tool.ruff.lint.per-file-ignores]`.

---

### `tests/test_parity/test_compare_analysis.py` (test, request-response)

**Analog (unit-test structure, docstring-rich, `from __future__`, fixtures):** `tests/test_scripts/test_download_models.py`. Imports the script's pure functions directly and asserts behavior with no I/O.

**Pattern — import the comparator, table-test pass/fail cases:**
```python
from __future__ import annotations
from scripts.parity.compare_analysis import compare   # adjust import path to chosen layout

def test_exact_bpm_mismatch_fails() -> None:
    fails = compare({"bpm": 120.0, ...}, {"bpm": 120.1, ...})
    assert any("bpm" in f for f in fails)

def test_model_score_within_epsilon_passes() -> None:
    g = {"bpm": 120.0, "musical_key": "C major", "model_scores": {"mood_happy": 0.80}}
    a = {"bpm": 120.0, "musical_key": "C major", "model_scores": {"mood_happy": 0.80005}}
    assert compare(g, a, atol=1e-3) == []
```

**CI-config-assertion analog (if the planner adds a `-arm64` tag-strategy test):** `tests/test_deployment/test_agent_compose.py` lines 165-198 — `test_docker_publish_workflow_tags_both_latest_and_version` parses the workflow YAML and asserts the tag block. Extend this exact pattern to assert the new `build-arm64` job emits `-arm64`-suffixed `latest` + version tags (RESEARCH §Wave 0: "Tag-strategy test extended for `-arm64`"). Helpers `_extract_api_metadata_action_step` / `_metadata_action_tag_lines` (lines 160-162) are reusable.

---

### `scripts/parity/golden-x86.json` + `reference.<ext>` (config / fixture, file-I/O)

**Analog:** spike `run_test.py` (model-run + finite-check shape) — read via `git show spike/arm64-essentia-analysis:spike/arm64-essentia/run_test.py`. Golden is generated ONCE from the x86 image by running `analyze_file` over `reference.<ext>` and serializing the dict above.

**Generation recipe:** a `just parity-golden-regen` recipe (model on `download-models` line 310-313) runs the x86 `Dockerfile` image over the reference clip and writes `golden-x86.json`. Reference clip: short (~30-60s), license-clean/CC0 or deterministic-synthesized but NON-degenerate (a pure 440Hz sine is too degenerate for model-score parity — RESEARCH Open Q2).

---

## Shared Patterns

### Python-3.13 vs `requires-python = ">=3.14,<3.15"` reconciliation (THE #1 decision)
**Source of conflict:** `pyproject.toml` line 10 (`requires-python = ">=3.14,<3.15"`) hard-fails a `uv sync` inside a `python:3.13` image.
**Apply to:** `Dockerfile.agent-arm64` (its uv layer) and the build.
**Existing arch-gate precedent to mirror:** the essentia platform marker at `pyproject.toml` line 16 (`; sys_platform != 'linux' or platform_machine == 'x86_64'`) and the `[tool.uv] environments` list at lines 175-183 (already enumerates `linux aarch64`). RESEARCH Pitfall 1 options: (a) relax to `>=3.13,<3.15`, (b) scoped `[tool.uv]` override / separate constraints file, (c) `--python-version` override at install. **discuss-phase must lock this (Assumption A2).**

### Frozen-SHA GitHub Actions
**Source:** every `uses:` in `docker-publish.yml` / `docker-validate.yml` carries a `# vX.Y.Z` frozen-SHA comment (e.g. `actions/checkout@9c091bb...  # v7.0.0`).
**Apply to:** every action in the new `build-arm64` / `parity-guard` jobs. Project mandate (MEMORY: precommit/actions frozen).

### Runtime-libs-or-crash-loop (v4.0.9 / v4.1.1 incident class)
**Source:** root `Dockerfile` lines 5-21 (the apt runtime set + the comment block explaining each `.so`).
**Apply to:** the FINAL stage of `Dockerfile.agent-arm64`. A multi-stage build that drops `-dev` packages MUST re-install `libatomic1 ffmpeg libsndfile1 libchromaprint-tools libpq5`. Gate with an import-smoke `docker run` before publish (mirrors `docker-validate.yml` lines 64-72).

### Image-build provenance/SBOM
**Source:** `docker-publish.yml` lines 134-135 (`provenance: true`, `sbom: true`).
**Apply to:** the `build-arm64` build-push step (RESEARCH Security §V14 — parity with x86).

### Pin-for-reproducibility (supply-chain integrity)
**Source:** `pyproject.toml` litellm cap comment (lines 19-22) — the repo's established "pin exact, justify inline" style.
**Apply to:** `Dockerfile.agent-arm64` — pin `tensorflow==2.20.0` exactly and essentia to a commit SHA (spike used `master`; RESEARCH §V6 + Open Q4).

---

## No Analog Found

| File / concern | Role | Data Flow | Reason |
|------|------|-----------|--------|
| `parity-guard` CI job (full image-run numeric compare) | CI | event-driven | No existing job runs a built image and diffs numeric output. Closest *shape* is the import-smoke step (`docker-validate.yml` lines 64-72) — a `docker run` assertion, but it checks import success, not numeric parity. New pattern; build from the analysis output contract + the comparator helper. |
| Dual-OpenMP fix #4 (env/LD_PRELOAD) | config | — | No prior art in repo (spike deliberately dodged it with `np.sin`). Net-new; must be validated on real audio (Assumption A1). |
| `type=gha` buildx cache | CI | — | Repo only uses `type=local` (`.github/actions/docker-build-cache`). The gha-scope cache for the arm64 runner is new; no in-repo analog. |

## Metadata

**Analog search scope:** repo root (`Dockerfile`), `.github/workflows/`, `.github/actions/docker-build-cache/`, `justfile`, `pyproject.toml`, `src/phaze/services/analysis.py`, `src/phaze/scripts/download_models.py`, `src/phaze/tasks/agent_worker.py`, `tests/test_scripts/`, `tests/test_deployment/`, `docker-compose.agent.yml`, plus the `spike/arm64-essentia-analysis` branch (`Dockerfile`, `README.md`, `run_test.py`) read via `git show`.
**Files scanned:** ~14 (in-branch) + 3 (spike branch via git show)
**Pattern extraction date:** 2026-06-24
