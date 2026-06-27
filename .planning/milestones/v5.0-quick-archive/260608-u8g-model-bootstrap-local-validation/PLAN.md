---
phase: quick-260608-u8g
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/phaze/scripts/download_models.py
  - src/phaze/tasks/_shared/model_bootstrap.py
  - src/phaze/tasks/agent_worker.py
  - tests/test_scripts/test_download_models.py
  - tests/test_services/test_model_bootstrap.py
autonomous: true
requirements: ["QUICK-260608-u8g"]

must_haves:
  truths:
    - "A worker boot with all 68 weight files present and correct-size makes ZERO network calls (pure os.stat)."
    - "A missing or wrong-size weight file triggers a GET for ONLY that one file via the existing atomic _download_one path."
    - "The healthy bootstrap path never blocks the async startup event loop; the scan_directory SAQ job is no longer starved."
    - "The baked-in MANIFEST is derived programmatically from CLASSIFIER_MODELS/GENRE_MODELS and covers exactly 68 files."
    - "model_bootstrap stays Postgres-free and the agent_worker import-boundary subprocess tests stay green."
  artifacts:
    - path: "src/phaze/scripts/download_models.py"
      provides: "Baked-in size MANIFEST + local-validation download_to; HEAD path removed."
      contains: "MANIFEST"
    - path: "src/phaze/tasks/agent_worker.py"
      provides: "Non-blocking startup call via asyncio.to_thread."
      contains: "asyncio.to_thread"
    - path: "tests/test_scripts/test_download_models.py"
      provides: "Manifest + local-validation + zero-network tests; _download_one repair tests retained."
  key_links:
    - from: "src/phaze/scripts/download_models.py::download_to"
      to: "MANIFEST"
      via: "per-file os.stat size compare; GET only on miss/mismatch"
      pattern: "stat\\(\\)\\.st_size"
    - from: "src/phaze/tasks/agent_worker.py::startup"
      to: "ensure_models_present"
      via: "await asyncio.to_thread(...)"
      pattern: "asyncio\\.to_thread\\(ensure_models_present"
---

<objective>
Rework the essentia model bootstrap from "always HEAD-validate all 68 weight files
against essentia.upf.edu on every worker boot" to LOCAL-VALIDATION-ONLY against a
baked-in byte-size manifest. The network is touched ONLY to repair a missing or
wrong-size file.

Root cause being fixed: `ensure_models_present` -> `download_to` issued a synchronous
`httpx.head` (+ `time.sleep` backoff) for every weight file inside an `async def
startup`, freezing the worker event loop for minutes whenever essentia.upf.edu's TLS
flaked. That starved and timed out the actual `scan_directory` SAQ job, leaving scans
stuck "RUNNING" forever.

Purpose: healthy boots become instant local `os.stat` checks with zero network and zero
event-loop blocking; the rare repair path keeps the atomic, retrying, integrity-checked
GET.
Output: rewritten `download_models.py` (manifest + local validation), updated
`model_bootstrap.py` contract docs, non-blocking `agent_worker.startup`, and rewritten
download-path tests.

Scope: model bootstrap ONLY. Do NOT touch the other 4 PRs (scan lifecycle/elapsed,
structlog, activity indicator, delete-scans).
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@/Users/Robert/Code/public/phaze-pr1-model-bootstrap/CLAUDE.md

<interfaces>
<!-- Authoritative facts the executor needs. No codebase exploration required. -->

CLASSIFIER_MODELS has 33 entries; GENRE_MODELS = ("discogs-effnet-bs64-1",). Each model
contributes a .pb AND a .json file -> (33 + 1) * 2 == 68 weight files total.

Classifier filename = model_path.rsplit("/", 1)[-1]
  e.g. "mood_acoustic/mood_acoustic-musicnn-msd-2" -> stem "mood_acoustic-musicnn-msd-2"
Genre filename = the model string itself (no "/" segment).

URL bases (unchanged):
  _CLASSIFIER_BASE = "https://essentia.upf.edu/models/classifiers"
  _GENRE_BASE = "https://essentia.upf.edu/models/music-style-classification/discogs-effnet"
  download_to builds "{base}/{model_path}.pb" / ".json".

EXISTING functions to KEEP unchanged (repair path):
  _with_retries[T](label, fn) -> T            # bounded backoff + jitter, time.sleep
  _download_one(url, dest) -> None            # atomic .part + os.replace, 4xx fail-fast /
                                              # 5xx+transient retry, validates streamed
                                              # bytes against server Content-Length
  _RetryableDownloadError, _TRANSIENT_ERRORS, _TIMEOUT, _MAX_ATTEMPTS, backoff constants

EXISTING functions to DELETE (steady-state HEAD path, no external consumers):
  _head_content_length(url) -> int | None
  _try_head_size(url, dest) -> int | None
  _ensure_present(url, dest) -> None

Authoritative server-Content-Length byte sizes (captured from the validated production
deployment; manifest size == server Content-Length at model-pin time):
  .pb files:
    - "voice_instrumental-musicnn-msd-1.pb"        = 3239625   (the ONLY musicnn outlier)
    - any "*-vggish-audioset-1.pb"                 = 288629030
    - "discogs-effnet-bs64-1.pb"                   = 18366619
    - every other "*-musicnn-{msd,mtt}-*.pb"       = 3239548
  .json files (keyed by stem, no extension):
    danceability-musicnn-msd-2=2677, danceability-musicnn-mtt-2=2688,
    danceability-vggish-audioset-1=2691, discogs-effnet-bs64-1=14990,
    gender-musicnn-msd-2=2664, gender-musicnn-mtt-2=2664, gender-vggish-audioset-1=2678,
    mood_acoustic-musicnn-msd-2=3078, mood_acoustic-musicnn-mtt-2=3079,
    mood_acoustic-vggish-audioset-1=3093, mood_aggressive-musicnn-msd-2=3085,
    mood_aggressive-musicnn-mtt-2=3085, mood_aggressive-vggish-audioset-1=3099,
    mood_electronic-musicnn-msd-2=3093, mood_electronic-musicnn-mtt-2=3093,
    mood_electronic-vggish-audioset-1=3107, mood_happy-musicnn-msd-2=3049,
    mood_happy-musicnn-mtt-2=3049, mood_happy-vggish-audioset-1=3063,
    mood_party-musicnn-msd-2=3049, mood_party-musicnn-mtt-2=3049,
    mood_party-vggish-audioset-1=3063, mood_relaxed-musicnn-msd-2=3062,
    mood_relaxed-musicnn-mtt-2=3063, mood_relaxed-vggish-audioset-1=3077,
    mood_sad-musicnn-msd-2=3034, mood_sad-musicnn-mtt-2=3034,
    mood_sad-vggish-audioset-1=3048, tonal_atonal-musicnn-msd-2=2680,
    tonal_atonal-musicnn-mtt-2=2681, tonal_atonal-vggish-audioset-1=2695,
    voice_instrumental-musicnn-msd-1=2712, voice_instrumental-musicnn-mtt-2=2712,
    voice_instrumental-vggish-audioset-1=2785

agent_worker.startup test contract (must stay green): tests patch `aw.ensure_models_present`
with a SYNCHRONOUS fake and `await aw.startup({})`. asyncio.to_thread accepts a sync
callable and propagates its return/exception, so those tests keep passing unchanged.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Baked-in size manifest + local-validation download path</name>
  <files>src/phaze/scripts/download_models.py, tests/test_scripts/test_download_models.py, tests/test_services/test_model_bootstrap.py</files>
  <behavior>
    - MANIFEST is a dict[str, int] built programmatically from CLASSIFIER_MODELS + GENRE_MODELS; len(MANIFEST) == 68 and equals len(CLASSIFIER_MODELS)*2 + len(GENRE_MODELS)*2.
    - MANIFEST spot sizes: "mood_acoustic-musicnn-msd-2.pb"==3239548; "voice_instrumental-musicnn-msd-1.pb"==3239625; "danceability-vggish-audioset-1.pb"==288629030; "discogs-effnet-bs64-1.pb"==18366619; "discogs-effnet-bs64-1.json"==14990; "mood_acoustic-musicnn-msd-2.json"==3078.
    - _ensure_present_local: dest exists AND st_size == expected_size -> return, issuing NO httpx call (pure stat). Missing file -> _download_one. Present wrong-size file -> _download_one (re-fetch), WARN naming on-disk vs manifest size.
    - download_to: every file present and size-correct -> ZERO httpx.head and ZERO httpx.stream calls. One file missing -> exactly one GET, only for that file.
    - _download_one repair behavior (atomic .part, 4xx fail-fast, 5xx/transient retry, truncated-read retry, exhaustion RuntimeError) is unchanged and still covered.
  </behavior>
  <action>
    In `download_models.py`:
    1. Add a module-level `_JSON_SIZES: dict[str, int]` literal keyed by stem (no extension), using the 34 sizes in the interfaces block. Add `_expected_pb_size(filename: str) -> int` implementing the rule set: exact-match "voice_instrumental-musicnn-msd-1.pb" -> 3239625; exact-match "discogs-effnet-bs64-1.pb" -> 18366619; endswith "-vggish-audioset-1.pb" -> 288629030; else 3239548. Add `_build_manifest() -> dict[str, int]` that iterates CLASSIFIER_MODELS then GENRE_MODELS, deriving each stem, and inserts both `"{stem}.pb": _expected_pb_size("{stem}.pb")` and `"{stem}.json": _JSON_SIZES[stem]`. Assign module constant `MANIFEST: dict[str, int] = _build_manifest()`. Build from the model tuples (not a hand-written 68-entry literal) so it cannot drift from the model list.
    2. Add `_ensure_present_local(url: str, dest: Path, expected_size: int) -> None`: if `dest.exists() and dest.stat().st_size == expected_size` return immediately (pure local stat, zero network). Otherwise, if the file exists log a WARNING naming on-disk size vs manifest size, and call `_download_one(url, dest)` (atomic os.replace overwrites in place; a failed repair leaves the stale file, which the next boot re-detects and re-repairs). If the file is simply missing, log an INFO and call `_download_one(url, dest)`.
    3. Rewrite `download_to(target_dir: Path) -> None` to mkdir the dir then, for each CLASSIFIER_MODELS entry (stem = rsplit) and each GENRE_MODELS entry, call `_ensure_present_local` for the .pb and .json with `MANIFEST["{stem}.{ext}"]`. No HEAD requests anywhere in this path.
    4. DELETE `_head_content_length`, `_try_head_size`, and `_ensure_present` entirely (no remaining consumers). Keep `_with_retries`, `_download_one`, `_RetryableDownloadError`, `_TRANSIENT_ERRORS`, `_TIMEOUT`, retry constants.
    5. Update the module docstring: new contract is "local size-manifest validation; network only to repair a missing/wrong-size file". State that manifest sizes equal the server Content-Length at model-pin time, that the repair GET still validates streamed bytes against server Content-Length, and that the old 260608-jbg always-remote-HEAD-validate rationale is superseded (it blocked the async startup event loop and depended on a flaky remote). Tag the new contract 260608-u8g.
    Preserve type hints on all new funcs, double quotes, 150-char lines, strict-mypy-clean.

    In `tests/test_scripts/test_download_models.py`:
    - KEEP all `_download_one`-focused tests (atomic stream, 4xx fail-fast, transient retry, 5xx retry, exhaustion RuntimeError, no-truncated-dest, truncated-read retry) and the `_patch_sleep` helper.
    - DELETE every test exercising `_head_content_length`, `_try_head_size`, the old `_ensure_present`, and the two HEAD-based `download_to` tests (`test_download_to_fetches_classifier_and_genre_urls`, `test_download_to_valid_set_issues_only_heads_no_get`). Remove those names from the import block.
    - ADD: (a) a MANIFEST test asserting len == 68 == len(CLASSIFIER_MODELS)*2 + len(GENRE_MODELS)*2 and the spot sizes from <behavior>; (b) `_ensure_present_local` keeps a correct-size file with zero GET (monkeypatch download_models.httpx.stream to raise if invoked, write a small file, pass its byte length as expected_size); (c) `_ensure_present_local` GETs a missing file (respx GET, small payload, pass len(payload) as expected_size); (d) `_ensure_present_local` re-downloads a wrong-size file (seed a short file, pass a larger expected_size matching the respx payload). (e) `download_to` zero-network: monkeypatch `download_models.MANIFEST` to a dict mapping every expected filename -> 1, write a 1-byte file for all 68 expected names into tmp_path, monkeypatch BOTH download_models.httpx.head and download_models.httpx.stream to raise AssertionError, call download_to, assert it returns without raising. (f) `download_to` repairs only the missing file: same patched 1-byte MANIFEST, seed all-but-one file, respx GET (1-byte payload) for the URL of the missing one, assert exactly that one GET and the file now exists.

    In `tests/test_services/test_model_bootstrap.py`:
    - Update `test_download_to_creates_pb_and_json_pairs` to patch `download_models._ensure_present_local` (signature `(url, dest, expected_size)`) instead of the deleted `_ensure_present`; record (url, dest) and write a sentinel byte; assertions on the 68-file pb/json split are unchanged.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr1-model-bootstrap && uv run pytest tests/test_scripts/test_download_models.py tests/test_services/test_model_bootstrap.py -q</automated>
  </verify>
  <done>download_models.py exposes MANIFEST (68 entries, correct sizes), _ensure_present_local, and a HEAD-free download_to; the three HEAD helpers are gone; download-path + model_bootstrap service tests pass.</done>
</task>

<task type="auto">
  <name>Task 2: Update model_bootstrap contract docs for local validation</name>
  <files>src/phaze/tasks/_shared/model_bootstrap.py</files>
  <action>
    Update the module docstring and `ensure_models_present` docstring so the contract reads
    "local size-manifest validation; network only to repair a missing/wrong-size file"
    (tag 260608-u8g), replacing the "always-validate / per-file HEAD" 260608-jbg language.
    Explain that the healthy path is now pure `os.stat` (zero network, near-instant) and that
    the old per-boot remote HEAD validation was removed because it blocked the async startup
    event loop and depended on a flaky remote. Keep behavior identical: `download_to` is still
    invoked unconditionally (it now does the local stat compare internally) and any failure is
    still wrapped in `RuntimeError("Model download failed: ...")` so the container exits
    non-zero and `restart: unless-stopped` retries. Keep the existing INFO log containing the
    "3.1 GB" estimate token (a fresh/repair download is still multi-GB) and the
    `_EXPECTED_MODEL_COUNT` operator estimate. Do NOT add any new import beyond stdlib +
    phaze.scripts.download_models (preserve the Postgres-free boundary).
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr1-model-bootstrap && uv run pytest tests/test_services/test_model_bootstrap.py tests/test_task_split.py::test_model_bootstrap_stays_postgres_free -q</automated>
  </verify>
  <done>model_bootstrap docstrings describe the local-validation contract; download_to is still called unconditionally; postgres-free import-boundary test passes.</done>
</task>

<task type="auto">
  <name>Task 3: Make startup models check non-blocking via asyncio.to_thread</name>
  <files>src/phaze/tasks/agent_worker.py</files>
  <action>
    Add `import asyncio` to the stdlib import group (asyncio is stdlib, so the Postgres-free /
    import-boundary invariant is unaffected). Change the Step 3a call at line ~101 from
    `ensure_models_present(Path(cfg.models_path))` to
    `await asyncio.to_thread(ensure_models_present, Path(cfg.models_path))` so the (rare)
    repair-path network/`time.sleep` work runs in a worker thread and cannot freeze the async
    startup event loop. Update the Step 3a comment to state the healthy path is a pure local
    `os.stat` size-manifest check (zero network) and that to_thread keeps even the repair path
    off the event loop, preventing the scan_directory job starvation/timeout that motivated
    this change (260608-u8g). The existing banner / phase04-gaps tests patch
    `aw.ensure_models_present` with a synchronous fake; `asyncio.to_thread` accepts a sync
    callable and propagates its return value and exceptions, so those tests stay green.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze-pr1-model-bootstrap && uv run pytest tests/test_tasks/test_agent_startup_banner.py tests/test_phase04_gaps.py tests/test_task_split.py -q</automated>
  </verify>
  <done>agent_worker.startup awaits ensure_models_present via asyncio.to_thread; banner, phase04-gaps, and all import-boundary tests pass.</done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|-------------|
| worker -> essentia.upf.edu | Untrusted remote serves weight files; touched ONLY on repair. |
| baked-in MANIFEST -> on-disk files | Local size assertion replaces per-boot remote validation. |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|-----------------|
| T-u8g-01 | Tampering | repaired weight file from remote | mitigate | Repair GET still streams to `.part` and validates byte count against server Content-Length before atomic os.replace (unchanged `_download_one`). |
| T-u8g-02 | Tampering | same-size bit-flip of a weight file | accept | No published checksums for essentia weights; size is the only authoritative signal, same as prior contract. Out of scope. |
| T-u8g-03 | Denial of Service | flaky remote TLS freezing async startup | mitigate | Healthy path is pure os.stat (no network); repair path runs under asyncio.to_thread so it cannot block the event loop or starve scan_directory. |
| T-u8g-04 | Tampering | MANIFEST drifting from the model list | mitigate | MANIFEST is built programmatically from CLASSIFIER_MODELS/GENRE_MODELS; a count test asserts exactly 68 entries. |
</threat_model>

<verification>
Full suite, lint, types, and coverage must pass from the worktree root:

```bash
cd /Users/Robert/Code/public/phaze-pr1-model-bootstrap
uv run ruff format .
uv run ruff check .
uv run mypy .
uv run pytest --cov --cov-report=term-missing
```

Behavioral checks:
- `uv run python -c "from phaze.scripts.download_models import MANIFEST, CLASSIFIER_MODELS, GENRE_MODELS; assert len(MANIFEST) == len(CLASSIFIER_MODELS)*2 + len(GENRE_MODELS)*2 == 68"`
- `grep -n "asyncio.to_thread(ensure_models_present" src/phaze/tasks/agent_worker.py`
- Confirm `_head_content_length`, `_try_head_size`, `_ensure_present` no longer appear in `src/phaze/scripts/download_models.py`.
- Coverage stays >= 85% (project gate).
</verification>

<success_criteria>
- Healthy boot (all 68 files present, correct size): ZERO httpx.head and ZERO httpx.stream calls, proven by test.
- Missing/wrong-size file: exactly one GET for that file via the atomic, retrying `_download_one`.
- `agent_worker.startup` invokes the models check via `await asyncio.to_thread(...)`; event loop is never blocked on the healthy path.
- MANIFEST is programmatically derived and covers exactly 68 files with the authoritative sizes.
- Postgres-free and import-boundary subprocess tests remain green.
- ruff, mypy, and >= 85% coverage all pass.
- Three atomic commits: (1) download_models manifest + local validation + tests, (2) model_bootstrap contract docs, (3) agent_worker non-blocking startup.
</success_criteria>

<output>
This is a single-PR quick task on branch `fix/model-bootstrap-local-validation`. After all
three tasks pass verification, the changes are ready for the PR covering PR1 of the 5-PR
series (model bootstrap ONLY).
</output>
