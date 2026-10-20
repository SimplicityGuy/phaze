# phaze-di48m — MP3s misdetected by FFmpeg because of a junk prefix: a read-only corpus scan

- **Bead:** `phaze-di48m` (spike, child of epic `phaze-pjkbu`)
- **Date:** 2026-10-19
- **Code under test:** the deployed release image `ghcr.io/simplicityguy/phaze:2026.10.7`. Its own
  `ffprobe`, `mutagen` 1.48.1 and essentia `2.1-beta6-dev` were used, run unmodified.
- **Status:** measurement only. **No product code changed.** Nothing was written to any archive file,
  to the production database, or to Valkey (see [How read-only was enforced](#how-read-only-was-enforced)).
- **Identifiers:** file UUIDs are placeholders per `CONVENTIONS.md`. `<uuid-1>` is the trigger file
  named in this bead's acceptance criteria, and the real id is in the bead record. Hosts are named by
  role: `host-prod` runs the API and Postgres, and `host-store` holds the archive and runs the agent
  containers.

______________________________________________________________________

## Question

The trigger file `<uuid-1>` crashed analysis. Its ID3v2 tag ends at byte 4,096, non-MPEG junk follows
up to byte 3,444,524, and a valid MPEG-1 Layer 3 stream (44.1 kHz, 320 kbps) starts there. FFmpeg
probes the file as **Layer 1** (`mp1`, 32 kHz, `probe_score=1`, 15,560 s). Its real duration is about
3,027 s.

A misdetected MP3 that does **not** crash is the worse case. It gets analyzed as noise with a wrong
duration and leaves no error at all. This spike asks three things:

1. How many MP3s in the archive does FFmpeg detect as something other than the stream they actually
   contain?
2. How well do the cheap signals (`mutagen` `sketchy`, ffprobe `probe_score`) predict that?
3. How many of the affected files already have a completed analysis row that is silently wrong?

## Method

**Population.** One `SELECT` against production chose the files:
`files.file_type = 'mp3' AND missing_at IS NULL`. That gives **94,412** files totalling
**11,319,517,463,615 bytes**. All of them are held by the single agent on `host-store`; no other agent
holds an MP3 row. Another 27 MP3 rows have `missing_at` set and were not scanned.

**Per file**, in one throwaway container on `host-store` (2 worker processes):

| Probe | What was recorded |
|---|---|
| `ffprobe -show_entries stream=codec_name,sample_rate,bit_rate,duration:format=probe_score,duration,format_name` | first stream's codec, sample rate and duration, plus `format_name` and `probe_score`. In every file that had an audio stream, it was stream 0 (cover-art streams always followed it) |
| `mutagen.mp3.MP3` (header parse, no `save`) | `layer`, `version`, `sample_rate`, `length`, `bitrate`, `sketchy`, or the exception |
| frame-chain parse (numpy sync search, Python chain walk) | ID3v2 end offset, every chain of at least 2 consecutive valid frames before the real stream (offset, version, layer, sample rate, bitrate, length), and the **ground-truth stream** |
| `essentia.standard.MetadataReader` | `duration`, `bitrate`, `sampleRate`, `channels` |

**Frame-chain parse.** The parse skips any ID3v2 tag(s) and then visits every `0xFFE` sync
candidate. A candidate is a valid header unless any of these hold: MPEG version reserved, layer
reserved, bitrate index 0 or 15, sample-rate index 3, emphasis reserved, or computed frame length
under 21 bytes. Frame length is
`(12·br/sr + pad)·4` for Layer 1, `72·br/sr + pad` for MPEG-2/2.5 Layer 3, and `144·br/sr + pad`
otherwise. A chain is a run of frames, each starting exactly where the previous one ended, all with
the same version, layer and sample rate.

**Ground truth.** The real stream is the first chain of **at least 64 frames**, or a chain of at least
8 frames that ends exactly at EOF or at an ID3v1/APE/Lyrics tail tag (short files). The scan gives up
after 64 MiB past the ID3v2 tag. Chains are capped at 64 frames, and at most 400 chains are recorded
per file. Only one file hit that cap: it is the single "unresolved" entry at N≥48 in the table below.

**Classes**, over files that have a ground-truth stream:

- **(i)** the layer implied by ffprobe's codec (`mp3`→3, `mp2`→2, `mp1`→1, anything else → none)
  differs from the ground-truth layer;
- **(ii)** `mutagen` reports `sketchy=True`;
- **(iii)** both.

**Silently wrong.** For each class, analysis rows were joined with a `SELECT` on `analysis`
(`analysis_completed_at`, `failed_at`, `fine_windows_total`) and `metadata` (`duration`).

The scan ran from 17:55 to 19:24 on 2026-10-19 (≈ 89 min wall clock). Per-file probe time had a median
of 0.103 s and a maximum of 27.943 s, with 10,653 s of summed worker time.

### How read-only was enforced

- **Archive mounts.** Each of the six archive directories was bind-mounted into a throwaway container
  (`docker run --rm --network none --cpus 2 --memory 3g --user 1000:1000`, image
  `ghcr.io/simplicityguy/phaze:2026.10.7`) with **`:ro`**, at the same container paths the live agent
  uses, so `files.current_path` resolved unchanged. The live agent containers were never `exec`'d into.
- **Opens.** The only writable mount was a per-bead scratch directory on `host-store`'s tmpfs
  (`/out`), outside every archive mount, which held the input list and the result JSONL. Every archive
  open in the scan script was `O_RDONLY`. `mutagen` got a file object opened that way, never a path,
  and `save()` was never called. `ffprobe` and `MetadataReader` only read. `MetadataReader` is a
  TagLib read and writes nothing.
- **Database.** Every query ran via `docker exec -i phaze-api /app/.venv/bin/python -I -` on
  `host-prod`, inside an asyncpg transaction opened with `readonly=True`, so Postgres itself rejects
  any write. All queries were `SELECT`s. There were no API calls, nothing touched Valkey, and nothing
  was requeued, retried, skipped or scanned.
- **Cleanup.** The scratch directory on `host-store` was removed after the results were copied off.
  Both containers (the main scan and a follow-up byte sampler) ran with `--rm`.
- **atime caveat (stated, not hidden).** The archive filesystems are mounted `relatime`. The scan
  script opened with `O_NOATIME` where the kernel allowed it, but 4,086 files are not owned by uid
  1000 and fell back to a plain `O_RDONLY` open. `ffprobe` and `MetadataReader` open by path. So an
  access time may have been updated, as it is on every read of these files, including the live analyze
  worker's own. File contents, mtime and ctime were not touched.

## Evidence

### Population outcome

| Outcome | Files |
|---|---:|
| Scanned (present per DB) | **94,412** |
| `ENOENT`: DB row present, file gone from disk | 9 |
| Opened, **no ground-truth MPEG stream** | 1,663 |
| Opened, ground-truth stream found | **92,740** |

Ground-truth layer by ffprobe codec (92,740 files):

| ffprobe first-stream codec | Ground-truth layer | Files |
|---|---|---:|
| `mp3` | 3 | 92,699 |
| `mp2` | 2 | 37 |
| `vvc` (FFmpeg probed the whole file as raw H.266 video) | 3 | 3 |
| `mp1` | 3 | 1 |

The 37 `mp2` files are real MPEG Layer 2 audio with a `.mp3` extension. `mutagen` also reports layer 2
for all 37. They are correctly detected.

### Classes

| Class | Files |
|---|---:|
| **(i)** ffprobe codec ≠ frame-chain layer | **4** |
| **(ii)** `mutagen` sketchy | **1** |
| **(iii)** both | **1** |
| (i) only | 3 |
| (ii) only | 0 |

**The trigger file `<uuid-1>` is in class (i) and in (iii).** The scan reproduces every quantity
measured in the investigation:

- ID3v2 ends at 4,096, so the junk run is 3,440,428 bytes. The ground-truth stream starts at
  3,444,524: MPEG-1 Layer 3, 44,100 Hz, 320 kbps.
- ffprobe reports `mp1`, 32,000 Hz, 15,560.265875 s, `probe_score=1`.
- `mutagen` reports layer 1, 48,000 Hz, 15,567.243875 s, `sketchy=True`.
- `MetadataReader` reports 3,027 s. The size-based estimate from the real stream is also 3,027 s.
- The junk holds 145 chains of 2–3 frames, the longest being 3 frames of "Layer 1".

**The other three class-(i) files (`<uuid-2>`, `<uuid-3>`, `<uuid-4>`) show a different mechanism.**
Each has a large ID3v2 tag ending at byte 58,648, 58,804 and 90,791 respectively. That is followed by
562, 562 and 946 junk bytes, then a clean MPEG-1 Layer 3 48 kHz stream (192, 192 and 320 kbps). FFmpeg
probes all three as `format_name=vvc` at `probe_score=51` and reports **no audio stream at all**.
`mutagen` reads them correctly (layer 3, not sketchy, lengths 2,983.37 s, 7,854.68 s and 4,654.06 s).
`MetadataReader` agrees to the second.

### How well each signal predicts (i)

Computed over the 92,740 ground-truth files, of which 4 are positives:

| Signal | TP | FP | FN |
|---|---:|---:|---:|
| `mutagen` `sketchy` | 1 | 0 | 3 |
| `mutagen` layer ≠ ffprobe layer | 3 | 0 | 1 |
| `mutagen` raised (`can't sync to MPEG frame`) | 0 | 92 | 4 |
| ffprobe first-stream codec ≠ `mp3` (incl. no audio stream) | **4** | 37 | 0 |
| ffprobe codec not in {mp1, mp2, mp3} | 3 | 0 | 1 |

**`probe_score` bound.** A file is flagged when `probe_score ≤ b`:

| Bound `b` | TP | FP |
|---:|---:|---:|
| 1 | 1 | 98 |
| 2 | 1 | 98 |
| 3 | 1 | 98 |
| 5 | 1 | 98 |
| 10 | 1 | 98 |
| 25 | 1 | 107 |
| 50 | 1 | 7,378 |

Over the ground-truth files, `probe_score` takes the values 51 (85,341 files), 50 (7,271), 1 (99),
99 (18), 24 (4), 12 (3), 25 (2) and 100 (2). The three VVC misdetections score **51**, the same as a
healthy MP3. That is why no bound catches more than 1 of 4.

**Minimum chained frames `N`.** The candidate detector parses frame chains, takes the first chain of
at least `N` frames, and flags the file if that chain's layer differs from ffprobe's layer:

| `N` | TP | FP | FN | Ground-truth-less files flagged | First chain ≥ N starts before the real stream (same params) | …with wrong params |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 3 | 1 | 1 | 4 | 193 | 2 |
| 3 | 3 | 0 | 1 | 0 | 189 | 1 |
| **4** | 4 | 0 | 0 | 0 | 179 | 0 |
| 5 | 4 | 0 | 0 | 0 | 162 | 0 |
| 6 | 4 | 0 | 0 | 0 | 141 | 0 |
| **8** | **4** | **0** | **0** | **0** | 99 | 0 |
| 12 | 4 | 0 | 0 | 0 | 90 | 0 |
| 16 | 4 | 0 | 0 | 0 | 86 | 0 |
| 24 | 4 | 0 | 0 | 0 | 65 | 0 |
| 32 | 4 | 0 | 0 | 0 | 33 | 0 |
| 48 | 4 | 0 | 0 | 0 | 8 (1 unresolved: chain cap) | 0 |
| 64 | 4 | 0 | 0 | 0 | 0 (1 unresolved: chain cap) | 0 |

The "same params" column counts files where a shorter chain of the real stream's own
version/layer/rate precedes the 64-frame run. Those are real but briefly broken streams, and they are
harmless to a layer check. The column that matters is "wrong params": a chain whose layer, version or
rate differs from the real stream's. **The longest such foreign chain before a real stream, anywhere
in the corpus, is 3 frames**, and it is in the trigger file's junk. That chain is exactly why N=3
misses the trigger: its first ≥3 chain is that "Layer 1" junk run, which agrees with ffprobe's wrong
`mp1`.

### Silently wrong results

| Population | Files | Completed analysis row | Failed row | No row |
|---|---:|---:|---:|---:|
| Class (i) | 4 | **0** | 1 (the trigger: `crashed: analysis child exited -11 without a result`) | 3 |

The three VVC files have **no** analysis row. Under the deployed D-10 probe (`ffprobe -select_streams a`)
they would fail **loudly** (no audio stream, so no duration), not silently. No class-(i) file has a
silently wrong completed result, so **a re-analysis sweep is not warranted for this defect.** Note that
only 10,785 of the 94,412 MP3s have a completed analysis at all. The detector still matters going
forward for the remaining ~83,600.

### Adjacent populations found on the way (not this defect)

These are measured exactly, but they are a different mechanism from FFmpeg misdetecting the layer:

- **Partial/zero-filled downloads with a real stream later in the file: 92 files** with more than
  1 MiB between the ID3v2 end and the real Layer 3 stream, while ffprobe still says `mp3`. 90 of them
  have an all-zero first MiB and 1 is mostly zero. `mutagen` fails on 90 of the 92 (`can't sync to
  MPEG frame`). ffprobe gives no duration for 67 of the 92, a duration more than 10% off the real
  stream's size-based estimate for 18, and a duration within 10% for 7. **1** has a completed analysis
  row. That analysis completed before today's probe: ffprobe now returns no duration for that file,
  whose real stream is ≈ 2,995 s.
- **No MPEG stream at all: 1,663 files.** A 1 MiB sample at the head and at the midpoint shows:
  - 1,074 files zero in both samples, plus 6 more zero in one of the two samples;
  - 546 HTML documents (`<htm…` 382, `<!DO…` 164) saved under a `.mp3` name;
  - 20 empty files;
  - 10 MPEG-TS streams (ffprobe `aac`);
  - a handful of other containers (ASF/`wmav2` 1, AMR 1, …).

  **0** of the 1,663 have an analysis row.

Junk-prefix size across all 92,740 ground-truth files, measured from the ID3v2 end to the real stream:

| Bytes | Files |
|---|---:|
| 0 | 82,627 |
| 1–1,024 | 9,397 |
| 1,025–65,536 | 607 |
| 65,537–1,048,576 | 16 |
| > 1,048,576 | 93 (the 92 above + the trigger) |

## Verdict

- **Class (i) is real but tiny: 4 of 94,412 MP3s (92,740 with a real stream).** Two mechanisms cause
  it:
  - a long junk prefix that FFmpeg's mp3 probe resyncs into as Layer 1 (1 file, the trigger);
  - a few hundred junk bytes after a large ID3v2 tag, enough for FFmpeg's raw-VVC probe to outscore
    the mp3 probe (3 files).
- **None of the 4 is silently wrong today.** One crashed, and three have never been analyzed and would
  fail loudly at the probe. **No re-analysis sweep is warranted.**
- **`sketchy` is a poor predictor (1/4).** **`probe_score` is unusable as a gate**: it reaches 1/4
  recall at every bound, and costs 98 false positives already at `≤ 1`.
- A frame-chain check with **N ≥ 4** classifies all 4 positives correctly with **0** false positives.
  The worst foreign chain in the corpus is 3 frames.

## Recommendation

1. **Detector thresholds.**
   - **Minimum chained frames: N = 8.** It has 0 FP and 0 FN measured, and 2.7× margin over the
     longest foreign chain observed (3 frames). Any N from 4 to 64 measured identically (0 FP); 8 is
     chosen for margin at a cost of ≈ 8 frames (≈ 3–8 KB) read past the candidate.
   - **probe_score: do not use a bound as the gate.** Measured FP is 98 at `≤ 1`/`≤ 10`, 107 at
     `≤ 25` and 7,378 at `≤ 50`, with only 1 of 4 caught at every bound.
   - **Cheap prefilter instead:** flag when ffprobe's first audio stream codec is not `mp3`, or there
     is no audio stream. That catches 4 of 4, with 37 FP, all of them real MP2 audio. Then confirm with
     the N = 8 chain parse, which clears all 37 (layer 2 = `mp2`) and keeps all 4.
2. **The fix should restore the intended behaviour, not only flag it.** For a confirmed file, the
   decode and the duration should start at the ground-truth stream offset. One way to do that is to
   hand FFmpeg the offset (`-skip_initial_bytes`) or force the `mp3` demuxer. Merely skipping the file
   would leave the 4 files unanalyzed. That design choice belongs to the implementing bead.
3. **No re-analysis sweep** for class (i). There are 0 silently wrong completed rows.
4. **The partial-download population (92 + 1,663 files) is a separate defect** and is not this
   bead's fix. 1 file in it has a completed analysis row. It deserves its own triage bead (scan-time
   rejection of zero-filled/HTML/empty `.mp3` files). Left to the dispatcher to file; not filed here.

**Rule 5 (general form).** "ffprobe's verdict cannot be verified by ffprobe" is the
`docs/design/0012-verification-fidelity-and-operator-attribution.md` rule 3 lesson again, applied to
format detection. The ground truth here came from an independent parser that **would have rejected
the wrong answer**: a 64-frame chain cannot form by chance in junk, and the corpus maximum for a
foreign chain is 3 frames.
