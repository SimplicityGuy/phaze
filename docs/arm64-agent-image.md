<!-- generated-by: gsd-doc-writer -->
# 🦾 arm64 essentia Agent Image

`Dockerfile.agent-arm64` builds the production **linux/arm64 (aarch64)** essentia
analysis agent. It is the foundation of the v5.0 Cloud Burst path — the perpetual-free
OCI Always-Free **Ampere A1** (4 cores / 24 GB) compute agent that drains the long-set
analysis backlog the homelab can't keep up with.

The recipe transcribes the proven spike (`spike/arm64-essentia-analysis`, PASS
2026-06-23) into a hardened, reproducible build: **Python 3.13 + TensorFlow 2.20.0 +
essentia at a pinned commit SHA**, with all four spike integration fixes baked in.

---

## Why Python 3.13 (when the rest of phaze is 3.14)

phaze is **Python 3.14 exclusively** — `essentia-tensorflow` ships `cp314` wheels for
x86, so the main app and the api image stay on 3.14. But on **linux/aarch64 there is no
`cp314` TensorFlow/essentia wheel** (the aarch64 TF wheels top out at `cp313`), so the
arm64 agent image **must** run on Python 3.13 and build essentia **from source** against
the AWS-maintained aarch64 `tensorflow` wheel.

### The scoped `requires-python` reconciliation (decision D-47-PY)

The repo's `requires-python = ">=3.14,<3.15"` contract is **never relaxed**. The 3.13
exception lives **only inside this Dockerfile**, mirroring the existing
`pyproject.toml` essentia arch-gate style. The mechanism, all targeting the base
`/usr/local/bin/python3` (3.13):

1. **Closure install (`--system`):** `uv export --frozen ...` writes the locked
   third-party requirements (essentia-tensorflow is marker-excluded on linux/aarch64 —
   we built it from source), then `uv pip install --system --python
   /usr/local/bin/python3` installs that closure into the system interpreter.
2. **phaze itself:** one justified `pip install --no-deps --ignore-requires-python .`
   (uv has no `--ignore-requires-python`) installs the pure-Python phaze package on the
   same 3.13 interpreter, bypassing the 3.14 gate for this one image only.

### Install and run hit the *same* interpreter — `python3`, never `uv run`

Because packages are installed `--system` on `/usr/local/bin/python3`, the runtime
**must** launch via that same system interpreter:

```dockerfile
CMD ["python3", "-m", "saq", "phaze.tasks.agent_worker.settings"]
```

`python3 -m saq` is the exact equivalent of the `saq` console-script and hits the
interpreter the `--system` install populated. **Never `uv run`** inside this image: the
uv launcher resolves against a project `.venv` and re-validates `requires-python >=3.14`
against the 3.13 interpreter, so it would neither see the `--system` packages nor
resolve the interpreter — the image would fail its own boot/import-smoke. This is why
every tool run *inside* the arm64 image (import-smoke, parity dump) uses `python3`
directly, while the x86 (uv-based, 3.14) api image uses `uv run python`.

The agent role is `phaze.tasks.agent_worker.settings` (D-25: must **not** import
`phaze.database`) — not the api uvicorn entry.

---

## The pins (supply-chain integrity)

| Knob | Value | Why |
|------|-------|-----|
| Base | `python:3.13-slim-bookworm` | only `cp313` has aarch64 TF wheels |
| TensorFlow | **`2.20.0`** (exact, `--build-arg TF_VERSION`) | 2.19 has **no** `cp313` aarch64 wheel; 2.20.0 is the spike-proven glue combo |
| essentia | pinned commit `b9fa6cb674ca43dfb94d28d293aeda441c6745db` | hardcoded SHA — **no** floating `master`/`HEAD`, **no** build-time remote SHA resolution (T-47-01). MTG/essentia master resolved 2026-06-24, the spike-era state the TF-2.20.0 glue was proven against |

The pinned essentia SHA is **trusted only once it passes the parity guard** (below): a
fresh-master numeric divergence is caught there on real audio, never silently shipped.

A **single toolchain** (system gcc + pip manylinux wheels) keeps the libstdc++ CXX11 ABI
matched between essentia and libtensorflow — mixing ABIs is the essentia #977
`undefined symbol: _ZTINSt6thread6_StateE` segfault. The bare-pip TF install is the
research-granted agent-image exception: the TF wheel **must** be the toolchain essentia
links against.

---

## The four spike fixes (essentia's stale TF glue)

Everything needed is prebuilt; the friction is all in essentia's `setup_from_python.sh`
(written for TF ~2.5–2.12):

1. **Dangling TF symlinks.** The setup script hardcodes TF's old `tensorflow_core`
   pywrap name and assumes `libtensorflow_framework.so.2` lives in `/usr/local/lib`. On
   modern TF both symlinks dangle (`ld: cannot find -lpywrap_tensorflow_internal /
   -ltensorflow_framework`). **Fix:** repoint both at the real wheel `.so` files — the
   C++ runtime symbols moved to `libtensorflow_cc.so.2` (the 211 KB `_pywrap` is now a
   shim), so the pywrap slot maps to `libtensorflow_cc.so.2`.
2. **Linker search path (`LIBRARY_PATH`).** `/usr/local/lib` isn't on Debian gcc's
   default `-l` search; `LIBRARY_PATH=/usr/local/lib` covers the link-time `-L` search.
3. **Vendored libomp (`LD_LIBRARY_PATH`).** `libtensorflow_cc.so.2` has a transitive
   `DT_NEEDED` on the pip-wheel-vendored `libomp-<hash>.so.5` in the sibling
   `tensorflow.libs/` dir → add that dir to `LD_LIBRARY_PATH` or import segfaults.
4. **Dual-OpenMP runtime conflict.** Importing numpy's C extensions *after* essentia/TF
   loads in the same process segfaults — TF's LLVM `libomp` vs numpy's OpenBLAS `libgomp`
   colliding. The spike smoke test dodged this with `np.sin`; production drives numpy
   heavily (`np.mean`, aggregation) through the real `analyze_file` path. **Primary
   mitigation:** `OMP_NUM_THREADS=1` (serializes the OpenMP runtimes to one thread,
   defusing the collision). **Documented fallback** if `=1` alone is insufficient:
   `LD_PRELOAD` a single OpenMP runtime, e.g.
   `LD_PRELOAD=/usr/local/lib/python3.13/site-packages/tensorflow.libs/libomp-*.so`.
   Fix #4 cannot be proven by hadolint or a synthetic signal — its **real validation is
   the parity-guard run** driving real audio through `analyze_file` on a native arm64
   runner (below).

### Runtime-libs-or-crash-loop rule (T-47-06)

The final image **must** carry these runtime native libs or the agent crash-loops on
import (the v4.0.9 / v4.1.1 incident class): `libatomic1` (essentia's `_essentia` links
`libatomic.so.1`), `ffmpeg` (decode + `ffprobe`), `libsndfile1`, `libchromaprint-tools`
(the runtime `fpcalc` binary — the source build uses the `-dev` headers, the runtime
needs the `-tools` binary), and `libpq5` (backs psycopg's SAQ `PostgresQueue` broker).
They are installed explicitly so a future multi-stage split that drops the `-dev`
packages can't silently regress the crash-loop. Models (frozen TF1 graphs,
arch-independent) are **mounted at `/models` at runtime, never baked** — keeping the
build independent of the flaky `essentia.upf.edu` download.

---

## Build commands

Native arm64 host required (`ubuntu-24.04-arm` in CI, or Apple Silicon / colima) — the
essentia C++ compile is ~324 s cold; **no QEMU** (a cross-compile is prohibitively slow).

```bash
# Operator fallback (mirrors the CI build-arm64 job):
just image-build-arm64            # builds ghcr.io/<owner>/phaze:latest-arm64
just image-build-arm64 2026.7.0   # builds ...:2026.7.0-arm64

# Raw docker:
docker build --build-arg TF_VERSION=2.20.0 -f Dockerfile.agent-arm64 \
  -t ghcr.io/<owner>/phaze:<tag>-arm64 .
```

**CI:** the `build-arm64` job in `.github/workflows/docker-publish.yml` builds + `load`s
+ import-smokes the image on a native `ubuntu-24.04-arm` runner and warms the shared
`type=gha,scope=arm64` build cache. It does **not** push — the registry push is gated on
the parity guard (below).

---

## Parity workflow (CLOUDIMG-03) — the gate before publish

The arm64 image is built, parity-checked against an x86 golden, and pushed to GHCR
**only after parity passes**. A parity-divergent image never reaches the registry.

| Tool | What it does |
|------|--------------|
| `scripts/parity/dump_analysis.py` | shared CLI — runs the real `analyze_file` over `reference.wav` + `/models` and emits the parity-projected JSON (run **inside** each image) |
| `scripts/parity/compare_analysis.py` | compares golden vs actual — **BPM/key exact**, model scores within `--atol` epsilon; **non-zero exit on any break** |
| `scripts/parity/reference.wav` | committed deterministic synthetic reference clip (the shared input both sides analyze) |
| `just parity-dump IMAGE [MODELS] [OUT] [INTERP]` | the shared dump path both CI jobs delegate to; `INTERP` selects `uv run python` (x86) vs `python3` (arm64 `--system` 3.13) |
| `just parity-check [TAG]` | operator mirror of the CI parity-guard (provision models → dump arm64 actual → compare against golden) |
| `just parity-golden-regen [TAG]` | regenerate `golden-x86.json` from the x86 api image (CI is authoritative) |

### The CI gate (`docker-publish.yml`)

1. **`parity-golden-x86`** (x86, `ubuntu-latest`) — runs `dump_analysis.py` inside the
   freshly-built x86 api image over `reference.wav` and uploads `golden-x86.json`. This
   is the authoritative golden producer.
2. **`parity-guard`** (native `ubuntu-24.04-arm`, `needs: [build-arm64,
   parity-golden-x86]`) — rebuilds the arm64 image from the warmed `scope=arm64` cache
   (`load: true`, identical digest), runs the **same** `dump_analysis.py` (via
   `python3`) over `reference.wav`, and compares against the downloaded golden. A
   non-zero `compare_analysis.py` exit **fails the build before the push step runs**, so
   a divergent image is never pushed (CLOUDIMG-03 / T-47-08).
3. **Gated push** (a later step in the same job, `if: github.event_name !=
   'pull_request'`) — pushes the validated image with `push: true`, `provenance: true`,
   `sbom: true`, the same `-arm64` tags **and** the OCI source/revision/version
   **labels** the x86 image carries (`needs.build-arm64.outputs.labels` — T-47-04).
   Reached only when the compare passes.

Because this guard drives **real audio** through the actual `analyze_file`
(MonoLoader + RhythmExtractor2013 + KeyExtractor + musicnn/vggish/effnet + numpy
aggregation), a green run is the runtime proof that **fix #4** (dual-OpenMP) holds on
real workloads — not the spike's `np.sin` (T-47-09 / CLOUDIMG-01). If it SIGSEGVs
(faulthandler native stack names `libgomp`/`libomp`), apply the `LD_PRELOAD` fallback
from fix #4 above.

**Epsilon:** BPM and musical key must be **exactly** equal across architectures (frozen
TF1 graphs + deterministic DSP); only model scores need a tolerance. The `--atol`
starts at the comparator default and is tuned just above the observed model-score noise
floor from the first real x86↔arm64 run. A BPM/key difference is a **real divergence to
investigate**, not a tolerance to widen.

---

## Tag naming (consumed by Phase 51)

The image is published as `ghcr.io/<owner>/phaze:<tag>-arm64` — `latest-arm64` on the
default branch and `<version>-arm64` on a release tag (via
`flavor: suffix=-arm64,onlatest=true`). The **Phase 51 cloud-agent compose** pins
`<version>-arm64` to run this image on the OCI A1 compute agent.
</content>
</invoke>
