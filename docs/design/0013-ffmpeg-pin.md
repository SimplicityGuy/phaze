# ADR-0013 — Unify ffmpeg at 7.1.5 across every surface

| | |
| --- | --- |
| **Status** | Accepted — decided 2026-08-20 |
| **Date** | 2026-08-20 |
| **Bead** | `phaze-b62ri` (rescoped 2026-08-20; the earlier 8.1 implementation is scrapped, preserved at tag `salvage/phaze-b62ri-8.1`) |
| **Applies to** | `Dockerfile`, `Dockerfile.agent-arm64`, `Dockerfile.job`, `.github/workflows/tests.yml` |
| **Supersedes** | the tech-stack table's "FFmpeg (system) 8.x", which was wrong for both images |

## Decision

1. **Pin ffmpeg at `7:7.1.5-0+deb13u1`** in every image, from **Debian trixie apt**, via one
   `ARG FFMPEG_APT_VERSION` per Dockerfile that also drives the arm64 `libav*-dev` set.
2. **Move the arm64 agent base from bookworm to trixie**, because bookworm cannot serve 7.1.5.
   **Python stays 3.13** there.
3. **Scope: the CLI plus, on arm64, the libav libraries essentia compiles against.** Both are
   driven from the same string, so they cannot skew (§3).
4. **CI is knowingly left on 8.1 for now**, recorded rather than silently tolerated, with a
   recommendation put to the operator (§8).

### Operator attribution (ADR-0012 rule 2)

| | |
| --- | --- |
| **Question as put** | Should the bead's 8.1 pin stand, given that the essentia wheel links ffmpeg 7.1 and trixie already ships 7.1.5? |
| **Answer as given** | *"i think this bead was mis-filed. let's scrap the work entirely and change the scope to pin the CLI to 7.1"* |
| **Date** | 2026-08-20 |
| **Durable record** | the `phaze-b62ri` bead comment thread |

The **trixie base move** is recorded in that same thread as operator-approved. It was relayed to
the implementer without a verbatim quote, so none is invented here: treat it as an approved scope
item evidenced by the bead thread, not as a quoted decision.

The **earlier 8.1 decision** (*"lock to the latest ffmpeg 8.x, same as in github workflows"*,
2026-08-20) is **superseded** and must not be re-derived.

## 1. What was actually installed before this (measured)

The bead originally assumed unpinned drift. Measurement found something worse — the two images
were not on the same **major**, and neither was on the 8.x the tech-stack table claimed:

| Image | Base | Debian | apt `ffmpeg` |
| --- | --- | --- | --- |
| app (amd64) | `python:3.14-slim` | 13 trixie | **7.1.5** (`7:7.1.5-0+deb13u1`) |
| agent (arm64) | `python:3.13-slim-bookworm` | 12 bookworm | **5.1.9** (`7:5.1.9-0+deb12u1`) |
| CI | `ubuntu-latest` | — | pinned BtbN **8.1.2** (phaze-m3qte) |

`Dockerfile.job` installs nothing at all — it is `FROM ${BASE_IMAGE}` and inherits the api image's
ffmpeg. (The rescoped bead text says it "also installs ffmpeg"; it does not, and the distinction
matters, because the correct fix there is a guard against it *ever* installing one, not a pin.)

## 2. Why 7.1 — and why this is a pin, not an upgrade

**7.1 is what the analysis library already uses.** The `essentia-tensorflow` manylinux x86_64
wheel **statically links ffmpeg 7.1** (libavformat 61.7.100) into
`_essentia.cpython-314-x86_64-linux-gnu.so`: no bundled `libav*.so`, no soname references, no
dynamic seam to repoint. That is what decodes audio for **analysis** on amd64. Upstream essentia
builds against 7.1; 8.x is territory upstream has not walked.

**The CLI and the library are not independent.** For video containers the CLI extracts an audio
artifact (`-c:a copy` → `.mka`) which essentia's libav then decodes — a live producer/consumer
seam, and the same seam that cost 11.5 hours in `phaze-3ea41`. On amd64 the two were *already*
aligned at 7.1, by accident of Debian's version rather than by design. **Pinning to 8.1 would have
introduced a skew that did not previously exist**, which is why the first implementation was
scrapped.

So on the app image this changes **no version at all** — it freezes 7.1.5 so a future base refresh
cannot move it. On the agent image it is a real upgrade, 5.1.9 → 7.1.5, which is what brings that
image onto the same line as everything else.

## 3. Why apt, and why the pin covers libav too

**apt, not a downloaded build.** First-party, security-updated within the `-0+deb13uN` line, no
download step, no checksum to maintain, no third-party host. `apt-get install ffmpeg=<version>` is
itself an assertion: it **fails** if the base's suite stops serving that version, so a base bump
that moves ffmpeg breaks the build loudly at the layer that named it. BtbN is the documented
fallback only — it **dropped the 7.x line** between 2026-08-16 and 2026-08-17 (7.x is EOL
upstream), so `autobuild-2026-08-16-13-00` is a frozen asset on a host that has stopped building
it. Both sources converge on upstream 7.1.5.

**One string drives both surfaces.** The two images consume ffmpeg differently, and the pin has to
follow that:

- **amd64:** the wheel is self-contained, so only the CLI is pinned. Verified: `ldd` on the
  wheel's `_essentia` shows **no `libav*` link**, and in a container with neither the ffmpeg
  binaries nor any system libav, `essentia.standard` imported and decoded `.mp3`, `.mp4` and
  `.mkv` correctly.
- **arm64:** no cp314 aarch64 wheel exists, so essentia is compiled from source against
  `libavcodec-dev`, `libavformat-dev`, `libavutil-dev`, `libswresample-dev`. Those are pinned to
  the **same** `ARG FFMPEG_APT_VERSION` as the CLI. That is the substantive difference from the
  scrapped 8.1 attempt, which pinned the CLI and left the libraries on Debian's version — a
  CLI-vs-library skew inside a single image.

Verified on the built trixie image: the from-source `_essentia` links `libavformat.so.61`,
`libavcodec.so.61`, `libavutil.so.59`, `libswresample.so.5` — the trixie 7.1.5 sonames — with
**zero unresolved symbols**.

## 4. The arm64 base move, and the risk that actually mattered

`python:3.13-slim-bookworm` → `python:3.13-slim-trixie`. **Python 3.13 stays**: TensorFlow ships no
cp314 aarch64 wheel and dependabot PR #326 already proved 3.14 breaks this build. Base suite,
Python, TF and the pinned essentia commit are **one combination, not four independent knobs**.

glibc 2.36 → 2.41 was never the risk. The risk was the **C++ ABI**: this image requires system gcc
and the pip manylinux wheels to share one libstdc++ CXX11 ABI, and mixing them is the essentia
[#977](https://github.com/MTG/essentia/issues/977) `undefined symbol: _ZTINSt6thread6_StateE`
failure. trixie moves system gcc **12 → 14**, changing one half of that matched pair. A #977
mismatch is a **runtime** symbol resolution failure, so a green `docker build` proves nothing —
which is exactly the producer's-seam trap ADR-0012 rule 3 is about.

Two risks the bead did not name also had to clear: essentia recompiles against **libav 7.1 rather
than 5.1** (two majors), and trixie ships **TagLib 2.0.2** where bookworm had 1.x — an API major
bump under a pinned 2019-era essentia commit.

**All three cleared, verified at runtime on native arm64 hardware** (not emulation):

- `import phaze.tasks.agent_worker` and `import essentia.standard` both succeed.
- essentia links `libstdc++.so.6.0.33` (gcc 14) with **no unresolved symbols**.
- A **real decode producing real descriptors** ran on three fixtures. §4b has the numbers.

## 4a. Probe-surface parity against phaze's *actual* usage

The call sites, which were read rather than taken from the bead's list:

- `video_audio.py::probe_container_streams` —
  `ffprobe -v error -print_format json -show_entries stream=index,codec_name,codec_type,channels,sample_rate:stream_disposition=default,attached_pic`
- `video_audio.py::extract_audio_track` —
  `ffmpeg -y -nostdin -loglevel error -nostats -i <in> -map 0:<idx> -vn -sn -dn -c:a copy [-progress pipe:1] <out.mka>`
- `analysis.py::_probe_duration_sec` (D-10) —
  `ffprobe -v error -print_format json -show_entries format=duration:stream=duration -select_streams a`

The **real phaze module** was run against five fixtures — one-audio-track video; **no-audio**
video; video with **three audio tracks, `default` on the second**; plain `.mp3`; `.mp3` with
`attached_pic` — on each ffmpeg version, on its real base image.

**Every correctness-critical decision is identical** across 5.1.9, 7.1.5 and 8.1.2: audio-stream
count, `is_already_plain_audio`, selected track index and codec, the other-track list, and the
`NoAudioTrackError` outcome. The two paths `phaze-3ea41` names both hold — **no-audio** raises
`NoAudioTrackError` everywhere, and **multi-track** selects index 2 (the `default`-flagged ac3)
over the lowest index everywhere. `-c:a copy` produced a non-empty `.mka` and `-progress pipe:1`
emitted heartbeat ticks on every version.

Only one difference is in scope for this change: **`stream_groups: []`**, an additive top-level key
absent in 5.1.9 and present from 7.1.5. phaze reads only `streams` and `format` via `.get()`, so it
is inert — but it is a live demonstration that probe JSON shape *does* move between majors, which
is what makes the pin worth having.

**A difference that this change deliberately avoids:** 8.x began reporting mp3 durations
gapless-trimmed (5.041633 → 5.000000 on a fixture holding exactly 5.000 s — the 1836-sample LAME
delay + padding). **5.1.9 and 7.1.5 both report 5.041633**, so the 7.1 target does not move
duration at all, on either image. This is the concrete sense in which 7.1 has a smaller blast
radius than 8.1.

## 4b. Checked at the REAL consumer, not with the muxer's own tooling

§4a validated the extraction output with `ffprobe` — **the muxer's own tooling**, which on its own
proves round-tripping rather than compatibility. That is the shape of `phaze-3ea41`, where the
extracted `.mka` was asserted *"decodable by ffprobe"* while `es.MetadataReader`, the consumer that
could not read its duration, was never handed the file, and 11,428 files analyzed to zero windows.
This change swaps the muxer on arm64, so the check is owed (ADR-0012 rule 3).

In production the extracted file goes to `run_analysis_subprocess` → **essentia**, and its duration
comes from `_probe_duration_sec`. Both were run against `.mka` files muxed by each version, **same
architecture, byte-identical fixtures**:

| muxer (arm64) | essentia decode of the extracted `.mka` | `_probe_duration_sec` `format.duration` |
| --- | --- | --- |
| bookworm apt 5.1.9 | 221 184 samples | 5.032000 |
| BtbN 8.1.2 *(scrapped attempt)* | 221 184 samples | 5.000000 |
| **trixie apt 7.1.5** | **221 184 samples** | **5.038000** |

**Both results that matter hold.** Every muxer yields **byte-identical decoded audio** at the
consumer — `-c:a copy` really is lossless across the version change — and `format.duration` is
**non-zero and usable on every lane**, with per-stream duration `N/A` for Matroska exactly as
`_duration_from_ffprobe_payload`'s docstring predicts, so the `format` fallback carries it. **The
`phaze-3ea41` zero-duration failure mode is confirmed absent.**

**The negative result, recorded rather than smoothed over.** On ac3-in-Matroska the container
duration is an approximation on every version: essentia decodes 5.01551 s, while the probe reports
5.032 (5.1.9), 5.038 (7.1.5) and 5.000 (8.1.2). The 7.1.5 reading over-reports by **+22 ms**,
slightly worse than 5.1.9's +17 ms. Over-reporting is the direction
`_duration_from_ffprobe_payload`'s docstring actually warns about — windows past the last sample —
so this is the less comfortable direction, and it is stated plainly rather than omitted. It is
inert at phaze's scale: 22 ms against a 60 s fine / 30 s coarse window can only change the window
count when a duration lands within 22 ms of a boundary, and the fine tier already drops a short
trailing window. It is noted because it is real, not because it is thought to matter.

## 5. Blast radius (ADR-0012 rule 4)

**This changes the analysis path for every file processed by the arm64 agent lane.** The amd64 app
and job images are unaffected in behaviour — their ffmpeg version does not move (7.1.5 → 7.1.5) and
their essentia is untouched. The arm64 agent, by contrast, gets a recompiled essentia against a new
compiler, a new libc, a new libav major pair and a new TagLib major.

**What could break:** essentia failing to load (#977), or loading and producing different numbers.
**What proves it still works:** a native-arm64 runtime comparison of real descriptors over
byte-identical fixtures, old image vs new.

| descriptor | bookworm / gcc 12 / libav 5.1 | trixie / gcc 14 / libav 7.1 |
| --- | --- | --- |
| decoded samples | 220 500 / 221 184 / 222 208 | **identical** |
| RMS, loudness | — | **identical to 9 dp** |
| BPM, beat count, beat confidence | 48.075169 / 85.285599 / 85.003197 | **identical to 6 dp** |
| key, key strength | A minor | **identical** |
| spectral centroid | 0.028425029 | 0.028425058 — **differs, 2.9e-08** |

**Decoded PCM is bit-identical** (equal sample counts *and* equal RMS/loudness), so the libav major
change does not alter what the analyzer sees. The lone difference is a **2.9e-08 absolute** drift in
spectral centroid — floating-point scheduling from gcc 12 → 14, downstream of an unchanged decode.
The 47-04 parity gate compares with `math.isclose(abs_tol=1e-4)`, so this sits **~3450× inside
tolerance** and cannot trip it. The population is stated as the arm64 lane rather than a file count
because the lane's share of the 11,428-file corpus is not something this bead measured.

## 6. The guards

| Guard | Where | Catches |
| --- | --- | --- |
| `tests/agents/deployment/test_ffmpeg_pin.py` | pytest, every CI run | The two declared pins disagreeing; the major drifting off 7; **any** ffmpeg/libav package installed unpinned in any image; a literal version repeated at an install site; `Dockerfile.job` growing its own apt layer; the agent base reverting off trixie; the agent moving off Python 3.13; the CI divergence note being deleted |
| `apt-get install ffmpeg=${FFMPEG_APT_VERSION}` | `docker build` | The base no longer serving the pinned version — the install fails outright rather than silently resolving something else |
| `ffmpeg -version` / `ffprobe -version` in the same layer | `docker build` | A pinned install that nonetheless left a different binary on `PATH` |

The pytest guard is mutation-tested: nine independent breakages were introduced one at a time and
every one was caught, including two bugs it found in **itself** on first run — a scanner that read
Dockerfile *comments* containing `apt-get install` as real install lines, and a `bookworm` check so
broad it would have deleted the accurate historical citation of PR #326.

## 7. The pin lives in two files, and that is not fixable

`Dockerfile` and `Dockerfile.agent-arm64` each declare `ARG FFMPEG_APT_VERSION`; `Dockerfile.job`
inherits through `FROM ${BASE_IMAGE}` and must never declare one. Docker has no include directive,
and plumbing `--build-arg` from every call site (both publish workflows, `docker-validate.yml`,
four compose files, the justfile, any hand-run `docker build`) would trade one visible duplication
for a silent-default failure at whichever site was missed. The guard converts "one source of
truth", which is unreachable, into "two copies, mechanically proven identical".

## 8. CI still tests 8.1 — DECIDED: the split stands

**DECISION (operator, 2026-08-20): leave CI on 8.1.x and the containers on 7.1.5.** The
recommendation below was put to the operator and **declined, with reasons**. This section is a
closed decision, not an open question — do not re-open it as a cleanup task.

> "leave CI on 8.1.x and the containers on 7.1.5. Yes, this is a discrepancy, but for CI ffmpeg is
> used only for the audio extract path. this is sufficiently isolated that this discrepancy is fine."

**The rationale was verified before being recorded**, because it is what justifies accepting a
measured difference. Eleven test files reference `ffmpeg`/`ffprobe`; two could have falsified it:

- `test_analysis_probe_duration.py` — where the 7.1.5-vs-8.1.2 mp3 duration difference would land —
  is **fully mocked** and never invokes a real `ffprobe`. The difference cannot reach it. This is
  the strongest support for the operator's claim.
- `test_extraction_analysis_handoff.py` runs **real** ffmpeg and essentia, and is the extract path
  itself — consistent with the rationale.

The claim is accurate as stated.

**What the split actually costs**, drawn explicitly so nobody re-derives it: in CI the handoff test
extracts with 8.1 and analyses with the wheel's static 7.1; in production it extracts with 7.1.5 and
analyses with 7.1. CI's *ongoing* coverage of that seam is therefore on the wrong producer version,
and a regression specific to 7.1.5's extraction output would not be caught. That residual is already
discharged, just not by CI: §4a checked 7.1.5's extraction output at its real consumer
(`es.MetadataReader`) and found byte-identical decoded audio. The production combination is verified
by direct measurement in this bead rather than by ongoing regression coverage.

---

**The declined recommendation, preserved for the record: move CI to 7.1.5.** The divergence is not theoretical — §4a measured 7.1.5 and
8.1.2 differing on mp3 duration (5.041633 vs 5.000000) and on the presence of `stream_groups`. A
duration-sensitive regression could pass CI on 8.1 and behave differently in production.

| Option | Gets | Costs |
| --- | --- | --- |
| **A — repoint the existing `FFMPEG_*` block** at `autobuild-2026-08-16-13-00` / `ffmpeg-n7.1.5-16-g9a4bb2c579-linux64-gpl-7.1.tar.xz` *(recommended)* | Same upstream release line as production; smallest diff; reuses the existing cache design | **Not the same build** — BtbN is 16 commits past the 7.1.5 tag with a different codec set; Debian adds its own patches. A frozen asset on a line BtbN no longer builds (fine for a test toolchain, which ships nothing) |
| B — run the suite in a trixie job container and apt-pin | **Byte-identical** to production | Restructures the test job's Python/uv/service-container plumbing |
| C — leave split | Nothing | Keeps the gap this bead is about |

Ubuntu 24.04 (`ubuntu-latest`) serves only ffmpeg 6.1.1, so runner apt is not an option.

**Outcome: option C.** If this is ever revisited, the trigger to watch for is a regression that
reproduces on 7.1.5 extraction output but not on 8.1 — that is the specific failure the split
leaves uncovered, and it is the evidence that would change the decision.

## What this does **not** cover

- **9.x.** Out of scope by operator decision, with a **stated suspicion** of essentia
  incompatibility — recorded as the hedge it was, not as a finding. Nobody has tested it. The 7.1
  evidence does make it less attractive (upstream builds against 7.1, so 9.x is two majors past
  validated), but that is an argument, not a measurement.
- **Forking or patching essentia** — `phaze-a51jo`.
- **The amd64 wheel's static ffmpeg.** There is no dynamic seam to pin; changing it means a source
  build of essentia (`phaze-han03` / `phaze-a51jo`). Note the wheel is **platform-skewed**: the
  macOS wheel bundles ffmpeg 8.x dylibs *and* libx264/libx265, where the linux wheel statically
  links 7.1 with neither. **Local macOS behaviour is therefore not evidence about deployed linux
  behaviour** for anything touching libav.
- **Codec-level behaviour beyond the five fixtures.** The comparison covers the probe surface and
  extraction paths phaze uses, on the container/codec mix it uses.
- **A libav follow-up framed as "pinning".** If one is filed, the right frame is **reproducibility**:
  make the arm64 agent's libav an explicit, reproducible input. `python:3.13-slim-trixie` is a
  moving tag, so point releases still change contents under a fixed ABI. Three guards already front
  it — the pinned essentia commit, the exact TF pin, and the 47-04 parity gate — so P3 unless the
  parity gate has actually been tripped by it.

## Appendix — GPL/LGPL, since it will be asked

The images have **always** shipped GPL ffmpeg; this bead does not introduce it. Debian's plain
`ffmpeg` package is built `--enable-gpl` **without** `--enable-version3`, so both before and after
this change the images carry **GPL-2+**. (The scrapped BtbN `gpl` asset would have moved that to
GPL-3+; the apt route does not.) phaze's own MIT source is unaffected either way — it invokes
`ffmpeg` as a separate process across a CLI boundary, which is aggregation, not linking — and the
images already ship essentia, which is **AGPL-3.0**.

One correction worth recording so it is not re-derived wrongly: **libx264/libx265 are encoders.**
Decoding H.264/HEVC uses ffmpeg's own native LGPL decoders. Analyzing video — probing, decoding,
extracting audio — does not touch them; only *encoding* to those codecs does, and phaze's single
`ffmpeg` invocation is `-vn -sn -dn -c:a copy`. Measured, not argued: the same harness run against
BtbN's `lgpl` build produced **byte-identical output** to the `gpl` build across all five fixtures.
This does not reopen anything — the apt route is GPL-2+ regardless — but a future reader should not
conclude that video analysis requires GPL codecs. It does not.
