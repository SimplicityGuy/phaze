# Producer → consumer artifact seams: the second pass

**Bead:** `phaze-5jo4c` · **Date:** 2026-10-14 · **Commit surveyed:** `a0348a3f` ·
**Predecessor:** [`phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md`](phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md)

The first inventory stated its own coverage limits under ADR-0012 (verification fidelity and operator
attribution) guardrail **G5**. It did not examine four areas, and it ran no query against a live
database. This pass covers exactly those gaps.

It is investigation output. **It fixes nothing.** Findings are filed as their own beads (§8).

> **Headline.** No seam on a path serving the whole archive was found uncrossed, so nothing here met
> the bead's escalation bar. The closest is **D1**, essentia's native load in the x86 image. It is a
> proxy before publication and is crossed only after the image is in the registry (§5). One **data**
> finding needs an explanation the code cannot give: 31,703 companion rows that the
> `phaze-8eq1i` backfill does not account for (§6.1, `phaze-c2885`). The dispatcher was told about
> both mid-flight.

______________________________________________________________________

## 1. The question, unchanged

The predecessor's §1 question still applies at every row: **what does the consumer read that the
producer never writes, and what would the producer's own tooling report correctly that the consumer
would not?** The verdict vocabulary is the predecessor's §2:

- **crossed**: a test hands the producer's real output to the real consumer across the real boundary.
- **proxy**: a test exists, but something stands in for the producer, the consumer or the boundary.
  The row names the proxy.
- **not crossed**: nothing connects them.

This pass adds one qualifier, used where it applies: *measured clean*. It means "no test crosses this
seam, but a measurement this pass ran found no instance of the defect today". It is not a verdict
upgrade. A measurement is a snapshot; a test is a guard.

## 2. Method, and what an absent seam means (G5)

**Inherited from the predecessor:** where indirection hides a seam, measure rather than reason. Every
row with ✔ in the **verified** column was re-derived from source by this seat, or settled by a
measurement this seat ran. None was accepted from a search hit or a test name.

**Measurements run by this seat**, all on base `a0348a3f`, all reproducible from the commands named
in the rows:

1. **CSS, two producers.** I compiled `assets/src/app.css` with the pinned Tailwind `v4.3.2`
   standalone binary and `--minify` twice. The first run was from the repository root (what
   `just tailwind` does). The second was from an empty directory holding only `assets/` and
   `src/phaze/templates/` (what the Dockerfile `css-builder` stage copies). I then diffed the class
   selectors.
2. **CSS, unresolved template classes.** I extracted every static class token from
   `src/phaze/templates/**` and checked which have no selector in the image-equivalent build.
3. **htmx targets.** Every static `#id` in `hx-target` / `hx-swap-oob` / `hx-select` /
   `hx-indicator` / `hx-include` was checked against the `id` literals in templates, Python and JS.
4. **Schema, server defaults.** I ran `alembic upgrade head` (`091`) on an isolated seat
   (`just test-db-for phaze_5jo4c`). Then I ran `compare_metadata` twice: once exactly as
   `test_autogenerate_drift_is_frozen` does, and once with `compare_server_default=True`.
5. **Schema, definitions.** I built two schemas on the same seat, the migrated one and the one
   `Base.metadata.create_all` produces (the unit suite's schema). I compared every CHECK/UNIQUE/
   EXCLUDE constraint by `pg_get_constraintdef`, and every index by `pg_get_indexdef`.
6. **Image import graph.** In the project venv, I listed which native-backed modules
   `import phaze.main` actually loads.
7. **Runtime dependency closure.** I mapped every third-party top-level import in `src/phaze/**`
   (via AST) to its distribution, and checked it against `uv export --no-dev`.
8. **Telemetry label matchers.** Every label-bearing `phaze_*` selector in `alerts/` and
   `dashboards/` was checked against the label names and value domains declared in
   `phaze.telemetry.catalogue`.

**Measurements this seat did NOT run.** The three production-database SELECTs in §6 were run by the
dispatcher of this bead, read-only, under an authorisation the operator gave the dispatcher on
2026-10-14. This seat had no archive access and sought none. The authorisation reached this seat as
the dispatcher's relay; this seat did not witness the exchange.

**Coverage limits: what an absent seam does NOT mean.** This pass covered the four areas in its
scope, using the searches above plus reading `Dockerfile`, `Dockerfile.job`, `.dockerignore`,
`.github/workflows/{docker-validate,docker-publish,tests,ci}.yml`, the `justfile` Tailwind and
browser recipes, `tests/browser/conftest.py`, `tests/integration/test_migrations/**`, and
`tests/shared/telemetry/**`. It did **not**:

- **Build or run any production image.** No `ldd` was run inside the image. So the consumers of
  `libsndfile1` and `libpq5` (D4) are inferred, not observed. The image-side half of D1 rests on
  reading the workflow plus the import-graph measurement in the venv.
- **Run the browser suite**, or render any page with `StrictUndefined` (T4). T4 is a reading, not a
  measurement.
- **Examine the production telemetry stack.** The OTel collector and Prometheus versions that
  production runs live outside this repository. Only the `.example` compose pins them (L1).
- **Re-examine the predecessor's 30 rows.** Where this pass learned something about one (B5, B6, C1),
  §6 says so. Otherwise those rows stand as written on 2026-08-20.
- **Look for a fifth area.** None was sought. That is a limit, not a finding.

An absent seam in the tables below therefore means *"not found by the searches above"*.

______________________________________________________________________

## 3. The inventory

`✔` in **verified** = re-derived from source or settled by a measurement this seat ran (§2).

### T: Templates and static assets → the browser

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | Jinja templates + router context; 4 Python modules emitting HTML fragments directly (`routers/execution.py`, `routers/pipeline/_common.py`, `routers/pipeline/skip.py`, `routers/remote_lookup_ui.py`) | rendered HTML carrying `hx-*` / Alpine contracts | htmx 2 + Alpine.js in a real browser | process + network + format | **crossed** for the flows the real-browser suite drives: `tests/browser/` (38 test modules) boots real uvicorn on a **migrated** database, drives Chromium with Playwright, and has been a blocking CI job since 2026-08-21. **Proxy** elsewhere: the server-side suite asserts `hx-*` attributes as strings, which `tests/browser/conftest.py` itself names as the gap it exists to close. **Measured:** 44 static `#id` targets in `hx-target`/`hx-swap-oob`/`hx-select`/`hx-indicator`/`hx-include`, 0 dangling. 43 resolve to an `id` literal; `#changes-workspace-list` resolves through the `CHANGES_LIST_CONTAINER_ID` constant (`routers/shell/stage_maps.py:173`). | ✔ |
| T2 | **two** producers of `static/css/app.css`: the Dockerfile `css-builder` stage (image) and the `just tailwind` recipe (CI and dev) | compiled Tailwind CSS | the browser rendering the **image's** CSS | process + format | **proxy: the browser suite runs against the `just tailwind` build, not the image's.** Tailwind v4 auto-detects class candidates from the cwd as well as from the explicit `@source "../../src/phaze/templates"`. The recipe runs at the repo root, while `css-builder` copies only `assets/` + `templates/`. **Measured:** repo-root build 99,755 B and 1,266 class selectors; image-equivalent build 70,877 B and 899. 367 selectors exist only in the CI build, none only in the image. Exactly **one** of them is emitted by runtime code: `dark:ring-phaze-border`, on the two toasts `routers/pipeline/skip.py:139,168` builds in Python. Production renders them without their dark-mode ring; the browser suite renders them with it. Every other Python-emitted class appears in some template too, which is coverage by coincidence. `app.css`'s comment that *"all class strings are literal in these files"* (the templates) is false: the four modules in T1 emit `class="…"` markup from Python. The proxy cannot exhibit a class emitted only from Python or JS. **`phaze-oicyy`.** | ✔ |
| T3 | template authors | a class token in `class="…"` | Tailwind's generator, which emits **nothing** for an unknown utility | format | **not crossed.** No check that a template class resolves to a rule. The attribute-quote lint (`phaze-e1zby`) checks syntax, not resolution. **Measured:** of 632 distinct static template class tokens, 6 resolve to no selector in the image build. 3 are state or JS hooks (`open`, `selected`, `rail-node`). 3 are dead styling: `dark:ring-phaze-border-lg` in 8 templates (no `--color-phaze-border-lg` token has ever existed, per `git log --all -S` = 0 hits, so these toasts have never had their dark ring since `747bf369`), and `input` / `input-sm` on the two ordinal inputs in `record/companions/_observation.html`. **`phaze-zrkcb`.** | ✔ |
| T4 | each router's template context dict | Jinja variables | the template, via 17 independent `Jinja2Templates` environments | format | **proxy: lenient `Undefined`.** No environment sets `StrictUndefined`, and neither `src/` nor `tests/` mentions it. A context key that is renamed or dropped renders as `""`, indistinguishable from an intentionally empty value unless an assertion checks the value. In-process TestClient rendering crosses the seam for *exceptions* only. **Not measured, and not filed:** `StrictUndefined` also raises on the legitimate `{{ x or '' }}` idiom, so a strict-mode run yields a count that cannot be read without per-template triage. No instance was found by reading. | ✔ (the 17 environments and the absence of `StrictUndefined`) |
| T5 | external CDNs (unpkg, jsDelivr) | htmx 2.0.10, htmx-ext-sse 2.2.4, Alpine 3.15.12 + focus, SRI-pinned in `shell/shell.html` | the browser's SRI check, then the page | network + format | **crossed** by the real-browser suite loading the real `<script>` tags, with pinning guarded by `tests/shared/core/test_base_html_sri.py`. Not re-derived here. | |
| T6 | `static/js/analysis_timeline.js` | state classes `timeline-can-scroll-{left,right}`, `is-current` toggled at runtime | hand-written rules in `assets/src/app.css:240-241,513,520` | format | **crossed**: the rules exist (read), and `tests/browser/test_analysis_timeline.py` drives the toggles in Chromium. These are hand-written CSS, so T2's divergence cannot reach them. | ✔ (rules exist) |

### M: Alembic migrations → SQLAlchemy models

The G3 question the bead poses is: *what does the ORM read that a migration may never create (a column
default, a constraint, an index the planner assumes)?* The structural fact underneath every row: **the
unit suite's schema is `Base.metadata.create_all`** (`tests/conftest.py:363`). **Production's schema
is Alembic's**: the `039` baseline plus 52 revisions, head `091`. Only the migration tests and the
browser harness run on a migrated schema.

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| M1 | `alembic/versions/**` | production schema: tables, columns, types, nullability, index and constraint **names** | the ORM's mapped queries | process + format | **crossed**: `tests/integration/test_migrations/test_baseline_schema.py::test_autogenerate_drift_is_frozen` runs `compare_metadata` on a real migrated Postgres and asserts the drift equals a frozen 25-entry set. Any new drift, or any silent resolution of old drift, fails the test. Re-measured here: 25 entries, as frozen. | ✔ |
| M2 | same | **server defaults** | any INSERT that omits a column | format | **proxy, measured clean.** The drift test passes `compare_server_default=False` explicitly. **Measured with it `True`:** 6 extra entries (`proposals.status`, `scan_batches.{status,total_files,processed_files}`, `tracklists.auto_linked`, `tracklist_tracks.is_mashup`). Every one has a DB server default where the ORM declares only a Python `default=` of the same value. That is the **safe** direction: the ORM always supplies the value, and a core INSERT omitting it fails loudly under `create_all`. There are **0** entries in the dangerous direction (an ORM `server_default` the migrations never create). **`phaze-lw5pr`.** | ✔ |
| M3 | same | CHECK / UNIQUE constraint **bodies**, index definitions | Postgres enforcement on every write | format | **proxy, measured clean.** alembic matches constraints by name, so a same-named CHECK whose body changed would pass. **Measured by definition:** all 47 of 47 CHECK/UNIQUE/EXCLUDE constraints are byte-identical between the migrated and `create_all` schemas. For indexes, 17 exist only in the migrated schema (GIN/trigram/partial/btree, plus `alembic_version_pkc` and `files_state_archive_pkey`) and 1 only in `create_all`, all as frozen. **None of the differing indexes is unique**, so no uniqueness behaviour differs between the schema tests use and the one production uses. **`phaze-lw5pr`.** | ✔ |
| M4 | same | an index the ORM declares | the production query planner | format | **crossed, benign.** `ix_analysis_window_file_id` is declared by the ORM and **never created** by any migration (frozen `add_index`). Its column is the leading column of `ix_analysis_window_file_tier_idx (file_id, tier, window_index)`, which production has, so a `file_id` lookup has an index either way. Answers the bead's "index the planner assumes" clause: one exists, and it is covered. | ✔ |
| M5 | the unit suite's schema (`create_all`) | the schema ~all DB tests run against | test assertions about production behaviour | format | **proxy: `create_all` stands in for the migrated schema.** It structurally cannot exhibit the 17 migration-only indexes, the generated `search_vector` columns or `files_state_archive`. Measured consequence: performance-only. M3 shows no constraint or uniqueness difference, and the search queries compute `to_tsvector` inline rather than reading `search_vector` (`services/search_queries.py:140,212,246`). Not filed. | ✔ |

### D: Docker image build → runtime

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| **D1** | `Dockerfile` apt layer: `libatomic1` | `libatomic.so.1` in the x86 api image, which is also the analyze agent's image and the base of `Dockerfile.job` | essentia-tensorflow's native `_essentia` extension, at `import essentia`, on the analysis path for the whole archive | process + format | **proxy before publication; crossed only after it.** The only pre-merge check is `docker-validate.yml`'s `import phaze.main`. Its own output line claims "native backends (psycopg/essentia) load". **Measured:** after `import phaze.main`, `essentia`, `essentia.standard` and `tensorflow` are all absent from `sys.modules`. The proxy cannot exhibit a missing `libatomic1`. The genuine crossing is `docker-publish.yml`'s `parity-golden-x86`, which runs the real `analyze_file` inside the pushed image. It is skipped on PRs, and it is `needs: build-and-push`, so the image (and the moving `latest` tag on `main`) is already in GHCR, and `build-job-runner` has already built the Job image from it, before essentia is ever loaded there. The arm64 image, by contrast, is smoked with `import essentia.standard` **before** its gated push. **Not broken today.** See §5 for why it was not escalated. **`phaze-mtt0v`.** | ✔ |
| D2 | `Dockerfile` apt layer: `ffmpeg` | `ffmpeg` / `ffprobe` on `PATH` | `services/video_audio.py:154,312`, `services/analysis_probe.py:120` (subprocess argv) | process | **crossed for presence at build time**: the same `RUN` executes `ffmpeg -version` and `ffprobe -version`, so a missing binary fails the build. Version policy is `docs/design/0013-ffmpeg-pin.md`'s and is not re-examined. | ✔ |
| D3 | `Dockerfile` apt layer: `rsync`, `openssh-client` | `rsync` / `ssh` on `PATH` | `tasks/push.py:190` | process | **crossed for presence at build time** (`command -v rsync ssh` in the same `RUN`). Their *behaviour* is the predecessor's A6 (not crossed, mocked), unchanged. | ✔ |
| D4 | `Dockerfile` apt layer: `libsndfile1`, `libpq5`, `libchromaprint-tools` | shared libraries | inferred: essentia's linkage (`libsndfile1`); psycopg's pure-Python fallback (`libpq5`); none known (chromaprint) | process + format | **consumer not verified.** No `src/phaze` module imports `soundfile`. psycopg loads its **binary** implementation (measured `psycopg.pq.__impl__ == "binary"`, which bundles its own libpq), so `libpq5` is the fallback the Dockerfile comment calls it. chromaprint's retention was decided on 2026-07-29 and is recorded in `docs/design/0002-fingerprint-removal.md` and `phaze-0jpe.6`; it is not reopened here. Settling the other two needs `ldd` inside the built image, which this pass did not run (§2). | |
| D5 | `uv sync --frozen --no-dev` | the image's Python environment | every third-party import in `src/phaze/**` | process | **not crossed by a test, measured clean.** 29 distinct third-party top-level imports, all in the `uv export --no-dev` closure, so no dev-only package is imported at runtime. `docker-validate`'s smoke crosses only the subset `phaze.main` imports. | ✔ |
| D6 | `css-builder` stage | `static/css/app.css` | the browser | process + format | = **T2**. | ✔ |
| D7 | `.dockerignore` (allow-list: `!src/`, `!alembic/`, `!assets/`, …) + `COPY src/` | non-Python runtime files: `prompts/naming.md`, templates, static assets, `py.typed` | the running app (editable install, reads from `/app/src`) | process + fs | **not crossed by a test; low risk by construction.** The allow-list admits `src/` whole and excludes only bytecode, caches and the generated `app.css` (rebuilt by `css-builder`). No image test renders a template or reads `naming.md`. | ✔ |
| D8 | `Dockerfile.agent-arm64` (essentia built from source) | the arm64 agent image | essentia + TF at runtime on arm64 | process + format | **crossed before publication**: an `import essentia.standard` smoke, then a parity guard comparing real `analyze_file` output against the x86 golden, gating the push (`docker-publish.yml:311-326`, `parity-guard`). | ✔ |
| D9 | `Dockerfile.job` (`FROM` the api image, no apt layer) | the Kubernetes Job image | `phaze.job_runner` in the burst pod | process + format | **inherits D1**: no smoke of its own, built after `build-and-push` and independently of `parity-golden-x86`. Covered by `phaze-mtt0v`'s fix, since the base is where the libraries live. | ✔ |

### L: Log and metric emission → any consumer

| # | producer | artifact | real consumer | boundary | verdict | verified |
| --- | --- | --- | --- | --- | --- | --- |
| L1 | `telemetry/instruments.py` (OTel SDK) | OTLP metrics | the OTel collector's Prometheus translation (unit suffixes, `_total`, label collisions) | process + network + format | **crossed once, by measurement; proxy thereafter.** `docs/telemetry/metric-catalogue.md` §5 measured the translation against a live `otel/opentelemetry-collector-contrib` 0.140.0 (2026-08-26) and caught three defects that reading the rules would have shipped. That result is now encoded in a hand-written function, `tests/shared/telemetry/_prometheus_translation.py`, which cannot exhibit a translation change in a different collector version. Production's collector version is outside this repo; only the `.example` compose pins 0.140.0. | |
| L2 | `telemetry/catalogue.py` | Prometheus **family names** | `alerts/phaze-alerts.yml` exprs; the 4 Grafana dashboards | format | **crossed at name level** (static): `test_alert_rules.py::test_every_referenced_metric_family_is_catalogued`, `test_dashboards.py::test_every_metric_referenced_is_catalogued`, and `test_metric_catalogue.py::test_every_instrument_is_catalogued_and_every_catalogue_entry_is_built`, all through the one shared translation. | ✔ (read) |
| L3 | same | **label names and values** | PromQL label matchers and `by()` / `without()` clauses | format | **not crossed, measured clean.** A renamed label or value gives an **empty vector**, not an error, which is the very failure L2 exists to stop for names. **Measured:** 50 label-bearing `phaze_*` selectors across alerts + 4 dashboards. Every matched label is declared on its metric, every literal value is in the declared domain, and every grouping label is declared on some metric. **0 problems**, and nothing pins it. **`phaze-5rvda`.** | ✔ |
| L4 | `alerts/phaze-alerts.yml` | rule logic | `promtool test rules` (the real consumer) | process + format | **crossed for rule logic**: `just alerts-test` locally (loud skip without `promtool`), and for real in `code-quality.yml`. Its input series are hand-written in `phaze-alerts.test.yml`, so it cannot exhibit emission shape. L2 bridges the names. | |
| L5 | structlog call sites | two log strings: `"stranded 'active' jobs reaped…"` (`tasks/active_reaper.py:144`) and `"schema at head"` (`database.py:128`) | `docs/runbook.md:318,482`, which tells the operator to `grep` for them | format (a human consumer) | **not crossed, measured clean.** Both strings resolve today. No test references either, so a reworded log line silently breaks a recovery step. **`phaze-5rvda`.** | ✔ |
| L6 | every log call | log lines on container stdout | **a machine consumer: none exists.** No Loki or LogQL in `dashboards/`, `alerts/` or `deploy/`, no OTel log exporter or `LoggerProvider`, no code parsing log output. | — | **no seam.** The sole consumer is the operator reading logs (L5). | ✔ |

## 4. The log/metric exclusion, re-examined

The predecessor excluded log and metric emission as *"not artifact-shaped"*. This pass **reverses
that for metrics and upholds it for logs, for a stated reason.**

- **Metrics are artifact-shaped.** A family name, a label set and a unit form a wire contract read by
  machine consumers. The consumers are PromQL in alert rules that page the operator, and dashboards.
  A break is silent by construction: an absent series is an empty vector, not an error. The
  repository already treats it that way (L1, L2, L4), and this pass found the one dimension it does
  not yet guard (L3).
- **Logs are not, here, because there is no machine consumer** (L6, searched). Upholding the
  exclusion is the right call *for machine seams*. It does not make logs consumer-free: a runbook
  `grep` is a consumer with a human at the end (L5), and this pass records it rather than inheriting
  the exclusion silently. If a log pipeline (Loki, an OTel log exporter) is ever added, this
  exclusion stops holding and L6 must be re-run.

## 5. Escalation check

The bead requires any seam found **uncrossed on a path serving the whole archive** to be escalated
immediately. None met that bar:

- **D1 is the closest.** essentia's native load *is* on the whole-archive analysis path. But the seam
  **is crossed**, by `parity-golden-x86` running real `analyze_file` in the real image, and
  `libatomic1` is installed today. The defect is *ordering*: the crossing happens after publication.
  It is not *absence*. That is a P3 gap in what would catch a future regression, filed as
  `phaze-mtt0v`. It is not a live whole-archive failure of C1's kind.
- **The companion count (§6.1)** is a data anomaly, not a seam, and its cause is unknown. It was
  reported to the dispatcher mid-flight with D1 and filed at P2 (`phaze-c2885`). Whether it is a
  defect depends on the queries that bead names.

## 6. Data measurements

### What the predecessor's figures were

**Every population figure in the predecessor was measured in CODE, not in data.** It said so itself
(§3: *"No query was run against a production database"*). Some of those figures have since been
measured in data, partly by other beads before this pass ran and partly by this pass:

| predecessor figure (code-measured, 2026-08-20) | measured in data, and when |
| --- | --- |
| C1: "12 of the 28 `EXTENSION_MAP` extensions" cannot be ingested; predicted 0 companion rows | `phaze-j8hjn`, 2026-08-24: 0 rows for the **six** later-approved extensions, plus a disk walk (15,198 companion files on disk). `phaze-8eq1i`, 2026-09-14: 14,438 rows after backfill. **This pass, 2026-10-14: the full twelve-extension query, 46,141** (§6.1). |
| B6: population "certain" for any file tag-written then re-executed | `phaze-2zeu0`, 2026-08-22: 0 and 0. **This pass, 2026-10-14: 0 and 0** (§6.2). |
| B5: `.wma` / `.wav` / `.aiff` / `.aac` admitted as MUSIC | `phaze-wt9vw`, 2026-08-22: 20 (all `.wav`). **This pass, 2026-10-14: 198** (§6.3). |
| D2: "every executed proposal is exposed" | **This pass, 2026-10-14: `execution_log` has 0 rows**, so the exposed population is currently 0 (§6.2). |
| A3, A7: owe a duration-distribution measurement (`just corpus-distribution`) | **Still not measured in data.** This pass had no archive access, and the dispatcher's three queries were categorical. |

**All 2026-10-14 figures are a point-in-time snapshot of that date.** §6.3 shows how quickly such a
snapshot goes stale on this archive.

**Provenance of the 2026-10-14 figures.** These are read-only SELECTs against the production Postgres.
They were run by this bead's dispatcher under an authorisation the operator gave the dispatcher on
2026-10-14, and relayed to this seat as counts only. This seat did not witness the authorisation and
did not run the queries. The durable records are comments on `phaze-j8hjn`, `phaze-2zeu0` and
`phaze-wt9vw`, written by this seat.

### 6.1 `phaze-j8hjn`: the companion rows

```sql
SELECT count(*) FROM files
WHERE file_type IN ('cue','nfo','txt','jpg','jpeg','png','gif','m3u','m3u8','pls','sfv','md5');
```

**46,141** = nfo 20,445 · m3u 16,706 · txt 6,619 · cue 2,368 · pls 3. The other seven extensions are 0.

**The prediction of 0 is refuted as stated.** It was a prediction about the pre-fix code. Since then
`phaze-dtz33` restored the producer (and `phaze-j8hjn` was closed as superseded by it), and
`phaze-8eq1i` backfilled the archive. The pre-fix measurement on 2026-08-24 covered only the six
approved extensions, so the twelve-extension query as written ran for the first time here.

**Attributing the rows to the restored producer: what can and cannot be shown.**

- **Shown:** the extension signature is exactly the set decided as D6 on `phaze-j8hjn` (2026-08-24;
  the question, answer and conditions are recorded there and deliberately not restated here).
  All five non-zero types are in `INGESTIBLE_COMPANION_EXTENSIONS` (`src/phaze/constants.py`), the
  one constant both producers derive their filter from (`tasks/scan.py:59`,
  `agent_watcher/observer.py:85`). All six excluded extensions read 0, although 1,385 such files
  were on disk on 2026-08-24. So the rows were minted by a path enforcing D6.
- **Shown:** they are not legacy rows. The pre-restore baseline was 0 on both 2026-08-24 and
  2026-09-14 (`phaze-8eq1i`, pre-scan baseline).
- **Shown:** 14,438 of them are the backfill, verified on 2026-09-14 (cue 35, m3u 7,130, nfo 7,139,
  pls 2, txt 132). That figure reconciled exactly with the disk walk minus the 760 orphans the scan's
  sibling rule skips.
- **NOT shown:** where the other **31,703 (69%)** came from. Growth since the backfill is very uneven
  by type: cue ×67.7, txt ×50.1, nfo ×2.9, m3u ×2.3. The archive did grow, as §6.3's media counts
  show, but that does not size this. The candidates are:
  - watcher arrivals;
  - watcher orphans (D8, also recorded on `phaze-j8hjn`, deliberately skips the sibling rule at the
    watcher);
  - phaze-written `.cue` sidecars re-ingested by the watcher, which the ×67 cue growth makes worth
    checking first;
  - further scans;
  - duplicate rows for one physical file under a different `original_path` form or `agent_id` (the
    conflict key is `(agent_id, original_path)`, `routers/agent_files.py:139`).

  **None is adopted.** Filed as **`phaze-c2885`** (P2) with the queries that would tell them apart.

### 6.2 B6 (`phaze-2zeu0`): the stale-hash population

**Correction to the predecessor's query.** §5.1 names `tag_write_logs`. The real table is
**`tag_write_log`** (singular, `src/phaze/models/tag_write_log.py`), and the query as written
errors rather than returning 0. (`phaze-2zeu0` recorded the same correction on 2026-08-22.) The
corrected query:

```sql
SELECT count(DISTINCT file_id) FROM tag_write_log WHERE status = 'completed';
```

| measurement (2026-10-14) | result |
| --- | ---: |
| files with a completed tag write | **0** |
| of those, re-executed later (a later `execution_log` row for the same file, via `proposals`) | **0** |
| `execution_log` rows, total | **0** |

The dispatcher relays that the operator confirms **no renames and no metadata writes have been done
yet**. So both B6 populations are 0 because neither producer (tag write, execute) has ever run on the
live archive, **not** because a fix made them 0. The fix (`routers/agent_tag_writes.py` refreshing
`FileRecord.sha256_hash`, `phaze-2zeu0`) is present at `a0348a3f` and has not yet met real data.
The same 0 bounds the predecessor's D1–D4 (filesystem execution): every execution-path seam has met
zero real executions.

### 6.3 B5 (`phaze-wt9vw`): the non-Vorbis containers

```sql
SELECT file_type, count(*) FROM files WHERE file_type IN ('wma','wav','aiff','aac') GROUP BY 1;
```

| | 2026-08-22 (`phaze-wt9vw`) | **2026-10-14** |
| --- | ---: | ---: |
| wma | 0 | **152** |
| wav | 20 | **41** |
| aiff | 0 | **2** |
| aac | 0 | **3** |
| total | 20 | **198** |

`phaze-wt9vw` sized its defect at **zero** on 2026-08-22, on the grounds that no `.wma` existed.
That was true then. Seven weeks later there are 152 `.wma` files, exactly the container class it
found broken. They meet the **fixed** writer (`tag_write_disk._write_asf`) and the fixed reader
(`metadata_parsing._ASF_MAP`, completed by `phaze-prla2`), so this is not a live defect, and tag
writes have still never run. But the fix landing before the population arrived was timing, not
anything enforced. **A zero-population sizing is a property of a date, not of the archive.**

## 7. Findings, ranked, and what they establish together

1. **The real-browser suite was validating a different CSS artifact from the one production
   serves (T2).** This is the G3 shape in miniature. The proxy has high fidelity (real Tailwind, real
   Chromium), and the seam is still uncrossed, because the two artifacts come from two producers.
   It was found only because the build was **run** in the image's configuration. A reader of the
   `@source` line and the comment beside it would have concluded the two builds were identical.
2. **A smoke test whose message claims more than it checks (D1).** "native backends
   (psycopg/essentia) load" was true of the author's intent and false of the import graph, and only a
   measurement could show it. This repeats the predecessor's §6 lesson in its optimistic direction:
   that pass found gaps by reading which a measurement then closed, and this one found a guard by
   reading which a measurement then showed absent.
3. **The schema bridge is sound where it looks, and blind where it does not look (M2, M3).** Both
   blind spots measured clean today. That is why they are P4 pins and not bugs.

## 8. Beads filed

| priority | seam | bead |
| --- | --- | --- |
| P2 | §6.1 | **`phaze-c2885`**: explain the companion growth 14,438 → 46,141; discriminating queries given. |
| P3 | T2, D6 | **`phaze-oicyy`**: one producer for `app.css`; browser CI must test the image's CSS; Python HTML emitters into the Tailwind source set. |
| P3 | D1, D9 | **`phaze-mtt0v`**: load `essentia.standard` in the pre-publish image smoke; prove it red on an image without `libatomic1`. |
| P4 | T3 | **`phaze-zrkcb`**: 3 dead template utilities; a guard that every template class resolves. |
| P4 | M2, M3 | **`phaze-lw5pr`**: pin constraint definitions and server defaults (measured clean). |
| P4 | L3, L5 | **`phaze-5rvda`**: pin label matchers and runbook log strings (measured clean). |

Not filed, with reasons: **T4** (no instance found; no interpretable measurement method), **D4**
(needs `ldd` in a built image; no defect suspected), **D5/D7** (measured clean; construction makes a
regression unlikely), **M5** (performance-only), **L1** (production collector version is outside
the repo, so the fix is not a phaze change).

## 9. Blast radius and general form

**G4.** This bead changes no production path. It adds this one document. What it could break:
nothing at runtime. The test that proves that: `just check-fast`'s docs-only arm (the prose guards
in `tests/docs_floor.txt`), run before submit.

**No back-reference from the predecessor, and why.** The bead's design asks for this document to be
cross-referenced *from* the predecessor. That turned out to conflict with a guard added after the
bead was written. `docs/spikes/` is a historical archive (`docs/spikes/README.md`), and
`tests/shared/test_historical_evidence_audit.py` holds every file in it to an exact-transform and
numeric check against a fixed checkpoint. The guard landed in `19102e5a` (2026-09-11), and its
current checkpoint is `phaze-tqoty` / `phaze-3p7s7` (2026-10-08). A five-line pointer appended
to the predecessor's §3 was measured to fail that test (`exact_transform_mismatches` and
`numeric_mismatches`, both naming the predecessor). So the link runs one way only: from this
document's header to the predecessor. Whether to add a back-reference, and where, is left to the
dispatcher and operator. It is not worked around here.

**G5 / rule 5, the general form of the lessons here.**

- **T2:** *two producers of one artifact make a test of either one a proxy for the other*. It applies
  wherever a build step exists twice, in a recipe and in a Dockerfile stage. Written down here and in
  `phaze-oicyy`.
- **D1:** *a check's self-description is a claim, and only a measurement verifies it*. This is
  already the general form of CLAUDE.md's "a test's name can carry a lesson its assertions do not
  check", extended to CI step names and echo text. No new site is needed.
- **§6.3:** *a population sized at zero is a snapshot*. Its general form is CLAUDE.md's existing "a
  count is a property of a run". Specific to this document otherwise.
