# Producer → consumer artifact seam inventory

**Bead:** `phaze-d2hgv.6` (ADR-0012 §7 R4) · **Date:** 2026-08-20 · **Commit surveyed:** `d0805b02`

This is the inventory [ADR-0012](../design/0012-verification-fidelity-and-operator-attribution.md)
guardrail **G3** is owed. G3 says a change producing an artifact *names its real consumer, and the
test calls that consumer*; `phaze-l832u.3` discharged that for exactly one seam — extraction →
analysis. This document enumerates the rest.

It is investigation output. **It fixes nothing.** Findings are filed as their own beads.

> **If you saw an earlier verbal report from this investigation, two of its headline claims were
> wrong and are corrected here.** The Redis Lua scripts were first reported as executed by *no test
> at any fidelity*; **4 of 6 in fact reach a real interpreter** (E2). The `exec:{batch_id}` Redis
> hash was first reported *not crossed*; it **is** crossed (E3). Both were corrected by measurement
> after the first report went out — §6 records how, and why the correction direction matters.

______________________________________________________________________

## 1. The shape being hunted

`phaze-3ea41` extracted audio to a Matroska `.mka` and shipped real-`ffmpeg`, real-container tests
asserting the artifact was *"decodable by `ffprobe`"*. `ffprobe` reads Matroska duration correctly.
`es.MetadataReader` — the consumer that actually received the file — does not. Zero duration
produced zero natural windows for all 11,428 files in the corpus.

Both tools were right. The artifact was still unusable. So the question this inventory asks at every
seam is not "is there a test" but:

> **What does the CONSUMER read that the PRODUCER never writes — and what would the producer's own
> tooling report correctly that the consumer would not?**

## 2. Verdict vocabulary

| verdict | means |
| --- | --- |
| **crossed** | A test hands the **producer's actual output** to the **real consumer** across the real boundary. |
| **proxy** | A test exists, but something stands in for the producer, the consumer, or the boundary. The row **names the proxy** and states what it *structurally cannot exhibit*. |
| **not crossed** | Nothing connects producer output to real consumer. |

"Proxy" is not a synonym for "mock". `phaze-3ea41`'s proxy was **real `ffmpeg` output read by real
`ffprobe`** — the fidelity was high and the *seam* was still uncrossed. A row is a proxy whenever the
thing under test is not the seam.

## 3. Method, and what an absent seam means (G5)

**How seams were found.** Four parallel sweeps over the tree at `d0805b02`, one per cluster
(cloud/transport, tag writes, CUE/companions/filesystem, IPC/queue/wire), each seeded from the three
candidates ADR-0012 §7 R4 names and instructed to treat that list as a starting point. Seeding
greps: `write_text` / `write_bytes` / `open("w"` / `NamedTemporaryFile` / `mkstemp` / `shutil.copy` /
`shutil.move` / `os.replace` / `.rename(` across `src/phaze/**`, plus every `model_dump(mode="json")`,
`json.dumps`, `register_script`, and `subprocess`/`create_subprocess_exec` call site.

**How verdicts were established.** Every **not crossed** verdict and every row marked ✔ in the
*verified* column was re-derived by me directly from the named source lines, not accepted from a
sweep. Test bodies were read; test names were never trusted. Three sweep verdicts were **overturned**
this way — see §6, which is the best evidence for why the column exists.

**One verdict was settled by measurement rather than reading.** For the Redis Lua scripts, static
reading is unreliable (`register_script` is indirected through accessor functions and a
`DispatchScripts` container). I ran `tests/review/routers/test_execution_dispatch.py` and
`test_agent_exec_batches.py` on an isolated seat with `redis-cli monitor` capturing, then matched the
observed `EVALSHA` digests against `hashlib.sha1` of each script's source. That is a direct
observation of which scripts reach a real Lua interpreter — see E2.

**Coverage limits — what an absent seam does NOT mean.** Per G5, an absence finding names its
sources. This sweep covered:

- `src/phaze/**` for filesystem writes, serialization, subprocess spawns, and Redis scripting;
- `tests/**` for the corresponding consumers;
- `git log -S` archaeology on two rows (C1, and the `_EXTRACTABLE` filter's history).

It did **not** cover:

- **The live archive.** No query was run against a production database. Every population figure below
  is measured **in code** (e.g. "12 of the 28 extensions in `EXTENSION_MAP`"), never in data. Rows
  needing a data measurement say so and give the query. For the duration/size-distribution ones,
  **`just corpus-distribution`** (`phaze-d2hgv.5`, commit `225716dd`) answers *"what fraction of the
  corpus exceeds \<duration | size\>"* in one command — it is ADR-0012 §7 R3, and it is what
  discharges G3's distribution clause. It is **not** on this document's base (`d0805b02`); it arrives
  with the molecule merge, so the rows below name it as the tool rather than restating the task.
- **Templates and static assets.** `src/phaze/templates/**` → browser, and the Tailwind build →
  served CSS, were not examined. A Jinja/HTMX contract is a genuine producer→consumer seam and is
  simply out of this pass's scope.
- **Alembic migrations → SQLAlchemy models.** Not examined; `MIGRATIONS_TEST_DATABASE_URL` suggests
  coverage exists, but this pass did not confirm it.
- **Docker image build → runtime.** Not examined.
- **Log/metric emission → any consumer.** Deliberately excluded as not artifact-shaped.

An absent seam therefore means *"not found by the searches above"*, not *"does not exist"*. The four
areas named in this list are the ones where a second pass has the best odds.

______________________________________________________________________

## 4. The inventory

`✔` in **verified** = re-derived by me from source, not taken from a sweep.

### A — Audio and analysis artifact chain

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | `services/video_audio.py::extract_audio_track` | extracted audio container | `services/analysis.py::analyze_file` → `_probe_duration_sec` → `es.MetadataReader` | process + format | **crossed** — `tests/analyze/services/pipeline/test_extraction_analysis_handoff.py`, real `ffmpeg` + real essentia. This is the reference row; `phaze-l832u.3` closed it. | ✔ |
| A2 | `analysis_child.py::_emit` | line-oriented JSONL over an OS pipe | `services/analysis_exec.py::run_analysis_subprocess._pump_stdout` → `json.loads` | process + format | **crossed** — `tests/analyze/services/pipeline/test_analysis_exec.py` spawns the real `python -m phaze.analysis_child` ("Every test here runs the REAL … subprocess", line 3) and lets the real parser read its real bytes. | ✔ |
| A3 | same seam, at **real payload scale and type** | one JSONL line carrying `analyze_file`'s actual result (thousands of windows, essentia-derived floats) | same | process + format | **proxy** — every real-child test carries `tests/analyze/_child_stubs.py::_result`, a hand-built dict of plain Python floats. `_emit` is `json.dumps(obj)` with **no `default=`**, and its own docstring at `analysis_child.py:123` says the strictness is deliberate. The stub structurally cannot exhibit a numpy scalar leaf (→ `TypeError` after hours of work), a `NaN`, or a multi-MB line through a 64 KiB pipe. **Owes a distribution measurement** — how large the real result line gets is a function of the corpus's duration distribution: `just corpus-distribution`. | ✔ |
| A4 | `services/agent_s3_reports.py::process_uploaded` → `complete_multipart_upload` | assembled S3 object at `phaze-staging/<file_id>`, **no extension, no content-type** | `job_runner.py::_download_to` → `compute_sha256` → `extract_audio_track` → essentia | process + machine + network + format | **not crossed** — the halves are covered separately. `tests/analyze/services/backends/test_s3_staging.py::test_multipart_round_trip_assembles_object` does a real moto round trip with the *test's* httpx and one 37-byte part; every `tests/analyze/core/test_job_runner.py` test mocks the GET with respx and patches out both `extract_audio_track` and `run_analysis_subprocess`. | |
| A5 | `routers/agent_files.py::presign_download` | `PresignDownloadResponse` (`download_url`, `expected_sha256`, `audio_ext`) | `services/agent_client.py::request_download_url` → `job_runner._temp_suffix` (names the temp file `<file_id>.<audio_ext>`, which is what essentia's format detection reads) | process + network + format | **proxy** — `tests/agents/routers/test_agent_presign_download.py` runs a real `ThreadedMotoServer` but **never PUTs an object and never fetches the URL**; assertions are `str(file.id) in body["download_url"]`. An empty-bucket moto cannot exhibit a dead URL or wrong bytes. | |
| A6 | `tasks/push.py::push_file` → `rsync` over SSH | the media file on a **different machine's** filesystem | `tasks/functions.py::process_file` reading `payload.scratch_path`, a path built independently at `routers/agent_push.py:226` | process + machine + network + fs | **not crossed**, self-declared — `tests/analyze/core/test_push_pipeline.py:12`: *"The subprocess is mocked everywhere"*. Producer side asserts argv; consumer side asserts a separate literal. **Mitigated**: `process_file` sha256-verifies the landed copy (`tasks/functions.py:298`), so this seam fails loudly rather than silently. | |
| A7 | `tasks/functions.py::_build_analysis_write_payload` / `job_runner.py::_build_payload` | HTTP JSON `AnalysisWritePayload` + up to 50,000 windows | `routers/agent_analysis.py` (FastAPI/pydantic parse) | process + network + format | **proxy on both halves** — client half is respx (`tests/agents/services/test_agent_client_endpoints.py`); server half is ~20 hand-written `json={...}` literals (`tests/agents/routers/test_agent_analysis.py`). The producer's actual bytes have never met the real router. **Owes a distribution measurement**: `AnalysisWindowPayload` count is bounded `le=50000` and `fine_windows_total` is derived from duration, so "can any real file exceed the bound" is a question about the archive's duration distribution, not a test — `just corpus-distribution`. | |

### B — Tag writes

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | `services/tag_write_disk.py::write_tags` → `_write_id3` | ID3v2.4 tag block on an MP3 | `verify_write` → `extract_tags` → `mutagen.File` | format | **crossed** — `tests/review/services/test_tag_writer.py::TestVerifyWrite` genuinely writes and re-reads a real MP3. But the consumer is the producer's own library; see B2 for what this does not establish. | ✔ |
| B2 | same | same | `es.MetadataReader` / TagLib in the analysis pod; external players | process + format | **proxy — mutagen** reading mutagen. Exactly the `ffprobe`-checking-`ffmpeg` shape: mutagen round-trips its own output perfectly. Cannot exhibit a v2.3-only reader seeing nothing after `audio.save()` silently rewrites the tag as v2.4, or `TDRC` being invisible to a `TYER`-only reader. **`tests/**` contains zero invocations of any non-mutagen tag reader.** | ✔ |
| B3 | `tag_write_disk.py::_write_vorbis` | Vorbis comment block in FLAC/Ogg/Opus | `metadata_parsing.parse_format_tags`; essentia/TagLib; players | process + format | **not crossed** — `TestWriteVorbisFormat`'s own class docstring says *"via mock"*; every test is `MagicMock()` + `assert_any_call`. No `.flac`/`.ogg`/`.opus` file is constructed anywhere in `tests/`. | |
| B4 | `tag_write_disk.py::_write_mp4` | iTunes-style MP4 atoms, `trkn` as `[(n, total)]` | `parse_format_tags` `_MP4_MAP`; essentia/TagLib; players | process + format | **not crossed** — `TestWriteMP4Format` is all `MagicMock`. The `elif isinstance(audio, MP4)` dispatch at `tag_write_disk.py:97` is a branch no test takes. | |
| B5 | `tag_write_disk.py::write_tags` line 99 `else: _write_vorbis(...)` | for `.wma`/`.wav`/`.aiff`/`.aac` — Vorbis-shaped keys written into a non-Vorbis container | any format-correct reader | process + format | **not crossed, and self-consistently masked.** `EXTENSION_MAP` admits `.wma .wav .aiff .aac` as MUSIC; none is `ID3` or `MP4`, so all fall to `_write_vorbis`. `extract_tags`' own `_VORBIS_MAP` fallback then reads the same lowercase keys back, so `verify_write` returns `{}` and the write is recorded COMPLETED. **The verifier confirms a tag no format-correct reader will see.** | ✔ |
| B6 | `write_tags` → `audio.save()` | **the audio file's bytes** — sha256 changes on every successful tag write | `FileRecord.sha256_hash` readers: `routers/agent_files.py:275` → `expected_sha256` → `job_runner.py:485` (**exit 11, fail-fast, no retry**); `tasks/execution.py::_verify_hash_or_raise`; dedup grouping | process + format | **not crossed** — `sha256` appears **nowhere** in `tag_write_disk.py`, `tag_writer.py`, `tasks/tag_write.py` or `routers/agent_tag_writes.py`. `sha256_hash` is written only at ingest (`tasks/scan.py:218`, the `agent_files` upsert); nothing re-hashes or invalidates it after a tag write. **Pushed to a firm verdict — see §5.1; the sharpest consumer is not the cloud gate but ordinary re-execution.** | ✔ |
| B7 | `_extract_before_tags` → `TagWriteLog.before_tags` JSONB | undo snapshot; values `str \| int \| list[str] \| None` | `routers/tags.py::undo_tag_write` → `enqueue_tag_write` → `write_tags` | process + JSONB + a second process hop | **proxy** — `TestExtractBeforeTagsRawFidelity` / `TestUndoDeletesAddedTags` capture-and-re-apply in one process against a real MP3, **with no DB and no JSONB in the loop**. | |
| B8 | `tag_proposal.compute_proposed_tags` → `WriteFileTagsPayload(tags=...)` | JSON in the SAQ broker payload | `tasks/tag_write.py::write_file_tags` → `model_validate` | process + format | **proxy** — tests call `write_file_tags(ctx, **_kwargs(...))` with an in-process dict; the payload is never serialized and back. Cannot exhibit an explicit `None` (phaze-52qd's entire delete mechanism) being dropped rather than preserved — a dropped key is a silent no-op in `_write_id3`, turning an undo into a lie. | |

### C — Companions and CUE

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| **C1** | **ingestion — `tasks/scan.py` / `agent_watcher/observer.py` → `POST /api/internal/agent/files`** | **a `FileRecord` row whose `file_type` is a COMPANION extension** | **`services/companion.py::associate_companions` (line 79, `FileRecord.file_type.in_(COMPANION_TYPES)`)** | **process + format** | **NOT CROSSED — AND THE PRODUCER DOES NOT EXIST. See §5. Escalated on discovery; filed as `phaze-j8hjn` (P1).** | ✔ |
| C2 | `services/cue_generator.py::write_cue_file` | `.cue` — UTF-8 **with BOM**, **bare LF** line endings, quotes stripped rather than escaped, `INDEX 01 MM:SS:FF` @75 fps | external CUE parsers and players | process + format | **not crossed** — every assertion is a substring/line-count check on the returned Python string. **No CUE parser exists in the tree or in `pyproject.toml`.** A string assertion cannot exhibit a parser rejecting CRLF-less records or a leading BOM. | |
| C3 | `write_cue_file` — `content.encode("utf-8-sig")` | file whose first character decodes as `U+FEFF` | `services/companion_read.py::read_companion_bounded_sync` — `open(encoding="utf-8", ...)`, **not `utf-8-sig`** | process + format | **not crossed** — every readback test uses `read_text(encoding="utf-8-sig")`, which strips the BOM *by definition* and therefore cannot exhibit the mismatch. **Currently unreachable in production because C1 means no `.cue` is ever ingested** — it becomes live the moment C1 is fixed. | |
| C4 | `routers/cue.py::generate_cue` → `WriteCueSheetPayload` | rendered CUE string over SAQ JSON | `tasks/cue_write.py::write_cue_sheet` → `model_validate` → `write_cue_file` | process + format | **proxy** — `tests/review/tasks/test_cue_write.py` builds `_kwargs()` from a module-level literal `_CUE`, never from `generate_cue_content`. No test carries a real generated sheet across this hop. | |
| C5 | `routers/cue.py:149` — `FILE "<basename>"` computed from the **unresolved** `current_path` | the CUE's relative FILE reference | a player resolving it beside the `.cue`, which `tasks/cue_write.py::_write_sync` places using the **`Path.resolve()`d** path | process + format | **not crossed** — no test pairs a symlinked or stale `current_path` with the resolved write target. | |

### D — Filesystem execution

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | `tasks/execution.py::_atomic_cross_fs_copy` → `_streamed_copy` + `shutil.copystat` | the file at its destination — bytes, mode, mtime, ownership, xattrs | the filesystem; a later re-scan's `stat` + `compute_sha256`; the operator's players | process + fs | **proxy** — *every* cross-filesystem test monkeypatches `_same_filesystem` to `False` inside **one** `tmp_path`. Cannot exhibit real `EXDEV`, `copystat` silently no-op'ing on SMB/CIFS/exFAT (the mounts this archive lives on), or ENOSPC on a genuinely different device. `test_streamed_copy_preserves_content_and_mtime` asserts `st_mtime` only. | |
| D2 | `tasks/execution.py:920` — `ProposalStatePatch(current_path=str(proposed))` | a path string crossing HTTP into `FileRecord.current_path` | `routers/agent_proposals.py` (assigns verbatim), then `routers/cue.py`, `tasks/cue_write.py`, `routers/tags.py`, pipeline file views | process + format (NFC vs NFD) | **not crossed** — `current_path` is NFC-normalized at ingest (`tasks/scan.py:224`, `agent_watcher/poster.py:101`) and the codebase calls that "Pitfall 3". The execute path is the **one** writer of `current_path` that skips it (`grep unicodedata.normalize src/phaze/` — 18 hits, none in `tasks/execution.py`). `proposed_filename` reaches it from LLM JSON via `sanitize_pg_text` only (`services/proposal.py:487`), which strips Postgres-unsafe text and does **not** normalize Unicode. **Latent, not certain** — it fires only when the LLM or an operator edit emits a non-NFC form; graded P3 on that basis rather than P1. | ✔ |
| D3 | `services/collision.py::_dest_key_columns` — destination predicted as SQL text | a "no collision" verdict gating the whole approved batch | `tasks/execution.py::_resolve_destination` → `Path.resolve()` | process + format (SQL string vs POSIX resolution) | **proxy** — collision tests are pure DB-row assertions; the executor is never invoked on a pair the detector cleared. `Path.resolve()` collapses symlinks, case, and NFC/NFD form that a SQL byte-comparison treats as distinct. | |
| D4 | `tasks/execution.py:808` — `_committed_copy_marker_path(...).write_text(...)` | sibling marker file; **not fsynced**, while the copy it certifies is | `_check_replay_corroborated` / `_reclaim_or_refuse_existing_destination`, via `.exists()` only | process (a retry in a different worker after a crash) | **proxy** — the marker's entire stated purpose is to survive a hard kill; every test creates it inside the same live process, which no in-process test can exercise. | |

### E — Queue, cache, and control plane

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| E1 | every `queue.enqueue(...)` call site | `Job.to_dict()` → `json.dumps` → `.encode("utf-8")` → `saq_jobs.job` BYTEA | the SAQ worker's `deserialize` → the task function's `**kwargs` | process + format + network | **proxy** — `tests/_queue_fakes.py::FakeQueue.enqueue` records the kwargs dict **in-process, unserialized** (line 179-187); it never calls a serializer. The only real-broker tests (`tests/integration/test_pg_dedup.py`, `test_pg_queue_priority.py`, `test_pg_active_reap.py`) enqueue **with no payload kwargs at all**. Cannot exhibit a `TypeError` from a `UUID`/`datetime`/`Path`/enum kwarg, or JSON round-trip type drift. | ✔ |
| E2 | six Redis Lua scripts (`register_script`) | Lua source + KEYS/ARGV, EVALSHA'd | the Redis server's Lua 5.1 interpreter | process + format + network | **mixed — measured, not read.** With `redis-cli monitor` capturing an isolated seat, 109 `EVALSHA` calls resolved to **4 distinct digests**, matched by `sha1` against source: `_CLAIM_DISPATCH_LUA`, `_PROMOTE_STATUS_LUA`, `_APPLY_INCREMENTS_LUA`, `_PERSIST_SORT_IF_EXISTS_LUA` all **crossed** against a real interpreter. **`_RELEASE_DISPATCH_LUA` and `_RATE_LIMIT_LUA` (the LLM rate limiter, `services/proposal.py:327`) are never executed by any test.** Both `SCRIPT LOAD` cleanly, so the residual risk is runtime semantics (`tonumber(nil)`, KEYS arity, type coercion), **not** syntax. | ✔ |
| E3 | `services/execution_dispatch_protocol.py::_init_fields` / `_seed_batch_hash` | Redis HASH `exec:{batch_id}`, including `dispatch_summary` as an embedded JSON string | `routers/execution.py` → `json.loads(data.get("dispatch_summary", "[]"))` | process + format + network | **crossed** — measured: 95 `HSET` / 55 `HGETALL` against `exec:<uuid>` on a real Redis, 24 lines carrying `dispatch_summary`. **Residual, narrower gap:** both sides of that test use `decode_responses=True`, while production's writer may be `queue.cache_redis` (`queue_factory.py`, **no** `decode_responses`). The bytes-vs-str client boundary is not crossed. | ✔ |
| E4 | `services/pipeline_counters.py::incr_enqueued` / `incr_completed` via a byte-mode `cache_redis` | Redis string counters | `read_counters` → `mget` → `_to_int` → the dashboard | process + format + network | **proxy** — `tests/_queue_fakes.py::FakeRedis`, a dict. The integration conftest builds a real `cache_redis` handle but no integration test asserts a counter. `_to_int` is defensively bimodal, which **masks** a real client-mode mismatch rather than surfacing it. | |
| E5 | `tasks/companion_read.py::read_companion_files` return value | task result → `json.dumps` → `saq_jobs.job` BYTEA | `services/proposal.py::fetch_companion_contents` → `queue.apply(...)` → `entry["content"]` | process + format + network | **not crossed** — the task is called directly in-process in its tests; `queue.apply` is an `AsyncMock` in the proposal tests. The failure mode is a swallowed exception that silently degrades every proposal to an empty companion context. **Currently unreachable in production for the same reason as C3** — C1 means no companion is ever associated, so this RPC never fires. | |
| E6 | a live LLM via `litellm.acompletion` | `response.choices[0].message.content` — a JSON string | `BatchProposalResponse.model_validate_json(...)` (`services/proposal.py:289`) | process + format + network | **proxy — the consumer generating its own input.** The fixture is literally `BatchProposalResponse(proposals=[...]).model_dump_json()`. Cannot exhibit markdown fences, a prose preamble, `content=None` (→ `TypeError`, not `ValidationError`), an empty `choices` list, or truncation at `max_tokens`. There is no fence-stripping, no `content is None` guard, and no per-item salvage — one malformed field kills the whole batch. | |

### F — Infrastructure manifests

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| F1 | `services/kube_staging.py::build_job_manifest` | a suspended `batch/v1` Job JSON | the Kubernetes apiserver (schema validation, **Quantity parsing**), the Kueue admission controller, the kubelet | process + network + format | **proxy — respx returning a canned `201`.** The route never inspects the body. All other manifest tests assert on the producer's **own dict**. There is **no `kubeconform` / `kubeval` / `kubectl --dry-run` anywhere in the repo.** `cpu_request` / `memory_request` / `memory_limit` are free-form operator strings checked only for truthiness; **only the apiserver parses them as Quantities**, and no test ever does. | |
| F2 | `build_job_manifest`'s `envFrom` — a ConfigMap/Secret referenced **by name only** (`kube_staging.py:407-408`) | the burst pod's process environment | `job_runner.py::run` → `get_settings()` + `os.environ.get(...)` | process + format | **not crossed** — phaze never enumerates the ConfigMap's keys, so a missing `PHAZE_AGENT_TOKEN` / `PHAZE_AGENT_API_URL` / `PHAZE_MODELS_DIR` is invisible until the pod exits 20. The `/models` mountPath ↔ ConfigMap `PHAZE_MODELS_DIR` invariant is flagged as drift-prone in the code's own comment (`kube_staging.py:308-309`) with no test comparing the two halves. | |
| F3 | the Kubernetes apiserver and Kueue (external producers) | `Job.status`, `Workload.status.conditions[]`, `Pod.status` | `tasks/reconcile_cloud_jobs.py` — branches on literal reason strings (`"Inadmissible"`, `"WorkloadInactive"`, `"Unschedulable"`, `"NodeShutdown"`, `"DeletionByTaintManager"`) | process + network + format | **proxy — `tests/kube_fakes.py` `SimpleNamespace` factories**, whose own docstring says "ZERO HTTP". Nothing pins the reason vocabulary to a Kueue/k8s version; a drift makes every branch fall through to "hold" and strand the in-flight registry, with no failing test. | |

______________________________________________________________________

## 5. C1 — the live finding, escalated

**COMPANION `FileRecord` rows have no producer. The companion chain is dead for the whole archive.**

This is the `phaze-3ea41` shape, live, on a path that serves every file that reaches the propose
stage. It was escalated to the dispatcher on discovery rather than filed as routine, independently
re-verified there, and filed as **`phaze-j8hjn`** (P1, bug), which owns the fix. **Not fixed here.**

**The gate.** `tasks/scan.py:55` and `agent_watcher/observer.py:75` both define
`_EXTRACTABLE = frozenset({MUSIC, VIDEO})`. `scan.py`'s own docstring states it: *"COMPANION
extensions (`.cue`, `.nfo`, `.txt`, images, playlists, …) are deliberately excluded"*. Those two are
the only clients of `POST /api/internal/agent/files`, which is the only `FileRecord` producer in the
tree — `routers/agent_files.py:146` holds the sole `pg_insert(FileRecord)`, and grepping `FileRecord(`
across `src/` returns only the model definition. The router applies no category filter of its own, so
ingestion's filter is the entire gate.

**The consumers, all live.**

1. `associate_companions` (`services/companion.py:79`) selects on
   `FileRecord.file_type.in_(COMPANION_TYPES)` — so `file_companions` is permanently empty and
   `POST /associate` (`routers/companion.py:27`) returns 0 forever.
2. `services/proposal.py:223` puts `"companions": companion_contents` into the LLM naming context.
   `src/phaze/prompts/naming.md:106` documents the field; **`:54` and `:56` make companion presence an
   explicit confidence driver**, so every rename proposal in the archive is generated at the
   "no companion files → low confidence" tier.
3. `services/tracklist_candidate_queue.py:161-164` — the `has_cue` `EXISTS(...)` over `FileCompanion`
   is always false, so the `.cue` tracklist-candidate source never fires and `stats.skipped_cue` is
   structurally always 0.
4. Everything phaze itself writes as a companion: `cue_generator` emits `.cue` sidecars that
   ingestion then refuses to ingest. **Phaze cannot read back its own output.**

**Archaeology — why nobody noticed.** Phase 3 (`9995b2e4`, 2026-03-28) built companion association
against COMPANION `FileRecord`s. The pre-Phase-27 ingester
(`git show 4efb4a48^:src/phaze/services/ingestion.py:57-58`) ingested every classified category and
rejected only `UNKNOWN`; its `extractable_categories` frozenset at `:159` was the **auto-enqueue**
gate, a separate thing. Phase 27 (`4efb4a48`, 2026-05-14, *"Watcher Service & User-Initiated Scan"*)
rewrote ingestion and applied the **enqueue** filter as the **ingest** filter. The commit's stated
intent — make the manual-scan set identical to the watcher set — is satisfied. Killing the companion
producer was collateral and is recorded nowhere.

**Why the suite is green — the G3 mechanism exactly.** Every test exercising a companion consumer
inserts the row by hand: `tests/identify/services/test_tracklist_priority.py:259`
(`make_file(original_filename="companion.cue", file_type="cue")` plus `session.add(FileCompanion(...))`),
`test_tracklist_candidate_queue.py:225-226`, `test_scan_deletion.py:138`. A directly-inserted row
**structurally cannot exhibit "no ingestion path writes this row"**. The producer's own tooling —
`associate_companions` run against hand-seeded rows — reports correct behaviour on an artifact
production never creates.

**Population.** Measured in code, not in data: `EXTENSION_MAP` holds **28** entries — 9 MUSIC, 7
VIDEO, **12 COMPANION** — and none of the 12 can be ingested. The affected consumer population is
every file reaching the propose stage — the same 11,428-file corpus ADR-0012 measures. The data-side
measurement is one query and should be run before the fix is sized:

```sql
SELECT count(*) FROM files
WHERE file_type IN ('cue','nfo','txt','jpg','jpeg','png','gif','m3u','m3u8','pls','sfv','md5');
```

The prediction is **0**, and confirming that prediction *is* the measurement.

**A question for the operator, not a corollary — asked and answered.** Re-enabling COMPANION ingest
changes what `scan_directory` writes for the whole archive and mints a large backlog of new
`FileRecord` rows on the next scan. Per G4 that owes a blast-radius statement; and **which** of the
12 companion extensions should be ingested (images? `.md5`?) does not follow from the bug — the bug
establishes that the row cannot be created, not which rows are wanted. That question was put to the
operator and answered on 2026-08-21.

**Ownership.** This finding is filed as **`phaze-j8hjn`** (P1, bug), which owns the fix and carries
the operator citation with its scope limit, per G2. It is deliberately **not** restated here: a
citation belongs at one durable site, and copying it into a spike doc is how a decision's conditions
get separated from it. Read the bead for the decision; read this section for the defect.

______________________________________________________________________

## 5.1 B6 — the stale `sha256_hash`, carried to a firm verdict

B6 was pushed past "not crossed" on request, because its blast radius was not obvious from the row.
It is **not** whole-archive — it does not warrant C1's escalation — but its sharpest consumer is not
the one the row first named, and the difference matters for how the fix is scoped.

**The mechanism.** `write_tags` ends in `audio.save()` (`tag_write_disk.py:102`), and
`write_and_verify_sync`'s own docstring concedes mutagen *"rewrites the whole file when the tag area
must grow"*. The file's sha256 therefore changes. `FileRecord.sha256_hash` is written **only** at
ingest and is never refreshed: the string `sha256` does not appear anywhere in the tag-write path.

**The consumer that matters is ordinary re-execution, not the cloud gate.** Tracing the chain:

```
services/execution_dispatch.py:106   sha256_hash=file_record.sha256_hash
  -> ExecuteFilesPayload item        (schemas/agent_tasks.py:215)
  -> tasks/execution.py:1121-1125    if not already_moved:
                                         if item.sha256_hash is not None:
                                             await _verify_hash_or_raise(original, item.sha256_hash, ...)
```

Line 1125 sits under `if not already_moved:` — it is the **fresh-execution** pre-copy verify, not a
replay-only branch. So: a file is renamed (executed), then tag-written (bytes change, hash goes
stale), then renamed **again** — an ordinary archive-organizing action, and one the product exists to
support. The second execution fails at `verify` with `sha256 mismatch for <path>`, and it fails
**permanently**: every retry recomputes the same real hash and compares it to the same stale column.
Nothing in the system can clear the condition, because nothing re-hashes.

**Two further consumers, same root cause, narrower reach:**

- `routers/agent_files.py:275` → `expected_sha256` → `job_runner.py:485` → `sys.exit(EXIT_INTEGRITY)`
  — fail-fast, no retry. Reachable when an already-analyzed file is re-analyzed on the cloud lane.
  The routine `POST /api/v1/analyze` trigger selects only DISCOVERED files and so cannot reach it,
  but `services/reanalysis_backfill.py` selects on incomplete window coverage **without reference to
  execution or tag state**, and `routers/agent_push.py:233` pins the same value for the rsync lane.
- Dedup groups by `sha256_hash`, so a tag-written file silently leaves its duplicate group.

**Why no test catches it.** There is no seam test because there is no seam *code*: producer and
consumer were written in different phases and share only a column. This is the sub-case of G3 worth
naming — **an uncrossed seam whose two halves never reference each other is invisible to a search for
the seam**, because there is no call site to grep for. It was found by asking what `write_tags`
changes about the artifact *besides* the tags, which is the G3 question applied to a side effect
rather than to the payload.

**Population.** Measured in code: the failure is **certain**, not probabilistic, for any file that is
tag-written and subsequently re-executed. The data-side measurement:

```sql
-- files whose on-disk bytes no longer match files.sha256_hash
SELECT count(DISTINCT file_id) FROM tag_write_logs WHERE status = 'completed';
```

That count is the population already carrying a stale hash; the subset that has since been
re-executed is the population already broken. Both should be measured before the fix is sized —
whether the fix re-hashes after a write, or clears the column, or writes tags without a full rewrite,
depends on which of those numbers is large.

______________________________________________________________________

## 6. Three verdicts the first pass got wrong

Recorded because they calibrate how much the rest of this table should be trusted, and because two of
the three errors were in the *pessimistic* direction — a seam-hunting pass is biased toward finding
gaps, and that bias needs stating.

1. **E2, Redis Lua — reported "not crossed", actually 4 of 6 crossed.** The first pass read
   `tests/review/services/test_execution_dispatch_protocol.py`, where all three `DispatchScripts`
   entry points are `AsyncMock`s, and concluded no Lua ever reaches an interpreter. It missed
   `tests/review/routers/test_execution_dispatch.py`, which installs a **real** Redis client as
   `app.state.redis`. Static reading could not settle this; the monitor capture did. The corrected
   finding — two named scripts, both syntactically valid — is much narrower and much more actionable.
2. **E3, the `exec:{batch_id}` hash — reported "not crossed", actually crossed.** Same root cause:
   the `_RecordingRedis` command log was mistaken for the whole picture. Measurement showed 55 real
   `HGETALL`s. What survives is a narrower and more interesting gap: the writer/reader **client decode
   mode** boundary.
3. **B2 vs B1, tag writes — the first pass called the mutagen readback "not crossed".** It is
   crossed; the seam it fails to cross is a *different* one. Splitting the row into B1 (mutagen →
   mutagen, genuinely crossed, and a legitimate check) and B2 (mutagen → TagLib/essentia, a proxy)
   is the honest form, and it is the same distinction `phaze-3ea41` turned on.

The general lesson, in G3's own vocabulary: **"a mock is present" is not the test.** The test is
whether the artifact under scrutiny reaches its real consumer. A seam can be crossed by a suite full
of mocks (A2) and uncrossed by a suite full of real infrastructure (A5, F1).

______________________________________________________________________

## 7. Beads proposed

Ordered by blast radius. Populations are code-measured; each names the data query it still owes.

| priority | seam | proposed bead |
| --- | --- | --- |
| **P1** | **C1** | **Filed: `phaze-j8hjn`.** Restore a producer for COMPANION `FileRecord` rows. Blast radius: every file reaching the propose stage; 12 of the 28 `EXTENSION_MAP` extensions unreachable. The §5 operator question on ingest scope is answered and cited on the bead. |
| **P1** | **B6** | Tag writes invalidate `FileRecord.sha256_hash` and nothing updates it. **Sharpest consumer is `tasks/execution.py:1125`, the fresh-execution pre-copy verify** — a re-rename of a tag-written file fails `sha256 mismatch` permanently, on every retry. Cloud gate (`job_runner.py:485`, exit 11) and dedup grouping are the narrower consumers. See §5.1; population query given there. |
| **P2** | **B5** | `.wma`/`.wav`/`.aiff`/`.aac` route through `_write_vorbis` and are then *self-consistently verified* by a reader using the same wrong keys. Population: `SELECT count(*) FROM files WHERE file_type IN ('wma','wav','aiff','aac')`. |
| **P2** | **B3, B4** | Give the Vorbis and MP4 tag writers real containers instead of `MagicMock`. Also crosses the never-taken `elif isinstance(audio, MP4)` dispatch branch. |
| **P2** | **B2** | One test that hands a phaze-written tag to a **non-mutagen** reader (`es.MetadataReader` is already a test dependency via A1). Closes the whole-cluster gap in one test. |
| **P2** | **E2** | Exercise `_RELEASE_DISPATCH_LUA` and `_RATE_LIMIT_LUA` against a real interpreter — the other four already are, so this is a two-script gap, not a cluster-wide one. Both `SCRIPT LOAD` cleanly, so the risk is runtime semantics only. The rate limiter gates every LLM call. |
| **P3** | **D2** | NFC-normalize `current_path` on the execute path, matching ingest. **Re-graded P2→P3**: latent, not certain — `proposed_filename` reaches it from LLM JSON unnormalized, so it fires only on a non-NFC emission. Population: every executed proposal is exposed; an unknown subset is affected. |
| **P2** | **A4, A5** | One test that assembles an object through the real producer and hands it to `job_runner`'s real download → ffprobe → essentia chain. This is A1's test, one boundary further out. |
| **P3** | **E1** | Round-trip at least one representative payload per task through a real SAQ serializer. |
| **P3** | **E6** | Guard the LLM→pydantic seam against fences, `content=None`, and truncation; add per-item salvage. |
| **P3** | **F1, F2** | Validate the Job manifest with `kubeconform` (schema + Quantity parsing) and assert the ConfigMap-key ↔ `job_runner` env contract. |
| **P3** | **C2, C3, C5** | Parse a generated `.cue` with a real CUE parser; reconcile the BOM against `read_companion_bounded_sync`. Sequence **after** C1 — C3 and E5 are unreachable until then. |
| **P3** | **A3** | Make one real-child test carry a real-`analyze_file` result, so `_emit`'s strict `json.dumps` meets a real essentia value. |
| **P3** | **D1, D3, D4** | Cross the filesystem seams for real: a genuine second device, a resolve-colliding pair, an out-of-process marker. |
| **P3** | §3 gaps | A second pass over the four uncovered areas: templates→browser, migrations→models, image build→runtime. |
