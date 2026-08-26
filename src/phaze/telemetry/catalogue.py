"""THE metric contract -- every metric phaze emits, its labels, and each label's bound.

This module is the single source of truth for three consumers that would otherwise
drift apart:

1. :mod:`phaze.telemetry.instruments` builds the real OpenTelemetry instruments FROM
   these specs, so an instrument that is not catalogued cannot be created;
2. ``tests/shared/telemetry/test_metric_catalogue.py`` enforces the cardinality budget
   (phaze-m1drf.3 acceptance 4) -- it fails the build when a label is added without a
   bound, when a forbidden identifier-shaped label appears, or when the documented
   catalogue drifts from this file;
3. ``docs/telemetry/metric-catalogue.md`` is generated from it, so the artifact homelab
   wires against cannot describe metrics phaze does not emit.

WHY THE BUDGET IS A HARD CONSTRAINT, not a style preference. phaze does not own the
Prometheus that scrapes this. A high-cardinality label here damages a SHARED homelab
instance, and the obvious labels are exactly the dangerous ones: the archive holds
11,428 files, a file is analyzed in up to 34 model x N window combinations, and a single
``file_id`` label would therefore mint series in the millions. File id, window index and
chunk index are SPAN ATTRIBUTES -- spans are sampled, dropped and aged out, and nothing
downstream indexes them into a time series.

MODEL IDENTITY IS THE ONE UNBOUNDED-LOOKING LABEL THAT IS ACTUALLY BOUNDED. There are
exactly 34 models (11 characteristic sets x 3 variants + 1 genre model), enumerated in
``services/analysis_models.py`` and pinned here by
``tests/shared/telemetry/test_metric_catalogue.py::test_model_label_bound_matches_the_registry``.
It is also the dimension the whole epic exists to expose, because
``classifier_type`` (musicnn / vggish / effnet_discogs) is what phaze-8ifq8 needs
per-model cost broken down by.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


InstrumentKind = Literal["counter", "histogram", "updowncounter", "gauge"]

#: UNITS ARE PART OF THE PROMETHEUS NAME, and a dimensionless "1" is not neutral: measured
#: against the real collector, a GAUGE with ``unit="1"`` is exported as
#: ``phaze_saq_queue_depth_ratio`` -- a queue depth is not a ratio, and the suffix would be
#: baked into every dashboard query and alert rule homelab writes. An OTel UCUM ANNOTATION
#: (braces, e.g. ``{jobs}``) is dropped by the translation instead, so the name stays
#: ``phaze_saq_queue_depth``. Counters were not affected -- they take ``_total`` -- which is
#: exactly the kind of inconsistency that is only findable by looking at the real output.


# Label names that MUST NEVER appear on a metric. Enforced by the catalogue guard test
# rather than by convention, because the failure is invisible until it has already been
# scraped into someone else's storage.
#
# Two families, and the second is the one that gets missed: identifiers of a THING
# (file/path/digest/uuid) and identifiers of a POSITION within a run (window/chunk index).
# The second family looks bounded per file -- a 12-hour set has 1,449 fine windows -- but
# it is unbounded across the archive, and a Prometheus series lives across files.
#: Label names Prometheus reserves. A metric carrying one of these is not "slightly wrong"
#: -- the OTLP -> Prometheus translation FAILS THE WHOLE METRIC and it never appears at the
#: scrape endpoint at all. Measured against the real collector
#: (otel/opentelemetry-collector-contrib 0.140.0): a label named ``job`` collides with the
#: ``job`` label the exporter derives from ``service.name``, and the collector logged
#: ``duplicate label names in constant and variable labels`` and dropped
#: ``phaze_saq_job_duration_seconds`` and ``phaze_saq_jobs_total`` entirely -- silently, as
#: far as anything downstream could see. ``le`` is the histogram bucket label and ``quantile``
#: the summary one; ``__``-prefixed names are Prometheus-internal.
RESERVED_LABEL_NAMES: frozenset[str] = frozenset({"job", "instance", "le", "quantile"})

FORBIDDEN_LABEL_SUBSTRINGS: tuple[str, ...] = (
    "file_id",
    "file_path",
    "filename",
    "path",
    "digest",
    "sha",
    "uuid",
    "window_index",
    "chunk_index",
    "url",
    "query",
    "sql",
    "user",
    "title",
    "artist",
)


@dataclass(frozen=True)
class LabelSpec:
    """One metric label, with the bound that makes it safe to scrape.

    ``cardinality`` is the number of DISTINCT values this label can ever take, over the
    whole life of the deployment -- not the number seen so far. ``values``, when given,
    is that exact set and the guard test checks the two agree; a label whose value set is
    computed at runtime (an HTTP route table) states its bound and leaves ``values`` None.
    """

    name: str
    cardinality: int
    description: str
    values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class MetricSpec:
    """One metric: what it is, what it is in, and how many series it can mint."""

    name: str
    kind: InstrumentKind
    unit: str
    description: str
    labels: tuple[LabelSpec, ...] = ()
    buckets: tuple[float, ...] | None = None
    #: Set when the realized combination count is SMALLER than the cartesian product --
    #: e.g. model name x variant is 12 x 4 = 48 on paper and exactly 34 in the registry.
    realized_combinations: int | None = None

    @property
    def label_names(self) -> frozenset[str]:
        return frozenset(label.name for label in self.labels)

    @property
    def cartesian_cardinality(self) -> int:
        """Series-generating label combinations, before histogram buckets."""
        total = 1
        for label in self.labels:
            total *= label.cardinality
        return total

    @property
    def bounded_combinations(self) -> int:
        """The number this metric is BUDGETED at -- realized when known, cartesian otherwise."""
        return self.realized_combinations if self.realized_combinations is not None else self.cartesian_cardinality

    def series_count(self) -> int:
        """Prometheus time series this metric mints at its bound.

        A histogram is not one series: the OTLP -> Prometheus translation emits one
        ``_bucket`` series per boundary PLUS the ``+Inf`` bucket, plus ``_sum`` and
        ``_count``. Costing a histogram as one series is the arithmetic error that makes
        a cardinality budget read as safe when it is not.
        """
        # buckets + the implicit +Inf bucket, plus _sum and _count.
        per_combination = (len(self.buckets or ()) + 1) + 2 if self.kind == "histogram" else 1
        return self.bounded_combinations * per_combination


# ---------------------------------------------------------------------------
# Shared label specs
# ---------------------------------------------------------------------------

TIER = LabelSpec(
    name="tier",
    cardinality=2,
    description="Which analysis pass -- the 44.1 kHz BPM/key tier or the 16 kHz model tier.",
    values=("fine", "coarse"),
)

OUTCOME = LabelSpec(
    name="outcome",
    cardinality=2,
    description="Whether the unit of work completed or raised.",
    values=("ok", "error"),
)

WINDOW_OUTCOME = LabelSpec(
    name="outcome",
    cardinality=2,
    description="Whether a window produced a record or was skipped by per-window failure isolation.",
    values=("analyzed", "skipped"),
)

# The 34-model dimension. Three labels rather than one composite, so a dashboard can
# aggregate by classifier_type (the phaze-8ifq8 question) without string surgery in PromQL.
MODEL_NAME = LabelSpec(
    name="model_name",
    cardinality=12,
    description="Characteristic the model predicts (11 model sets + the genre model).",
    values=(
        "mood_acoustic",
        "mood_electronic",
        "mood_aggressive",
        "mood_relaxed",
        "mood_happy",
        "mood_sad",
        "mood_party",
        "danceability",
        "gender",
        "tonality",
        "voice_instrumental",
        "discogs_genre",
    ),
)
MODEL_VARIANT = LabelSpec(
    name="model_variant",
    cardinality=4,
    description="Embedding variant the classifier sits on.",
    values=("musicnn_msd", "musicnn_mtt", "vggish", "effnet"),
)
CLASSIFIER_TYPE = LabelSpec(
    name="classifier_type",
    cardinality=3,
    description="Inference family -- the dimension phaze-8ifq8 asks per-model cost to be split by.",
    values=("musicnn", "vggish", "effnet_discogs"),
)
MODEL_LABELS: tuple[LabelSpec, ...] = (MODEL_NAME, MODEL_VARIANT, CLASSIFIER_TYPE)

#: 11 sets x 3 variants + 1 genre model. Pinned against ``analysis_models.MODEL_SETS``.
MODEL_COMBINATIONS = 34


# ---------------------------------------------------------------------------
# Bucket boundaries
# ---------------------------------------------------------------------------
#
# phaze-m1drf.3 acceptance 5: buckets come from MEASURED distributions, not from the
# SDK default (5 ms .. 10 s), which is useless at both ends of this workload -- a graph
# release is sub-millisecond and a coarse tier is hours. The measurements behind each
# ladder are recorded in docs/telemetry/metric-catalogue.md section 4 and were taken from
# the run in docs/telemetry/measurements/.
#
# Every ladder is deliberately SHORT. A histogram costs (len(buckets) + 3) series per
# label combination, so the 34-model instruments pay 34x whatever is added here; a
# 20-bucket ladder on those three instruments alone would mint 2,346 series.

#: Milliseconds to a few seconds. GRAPH construction and release only.
#:
#: MEASURED (docs/telemetry/measurements/, 30-minute file, real 34-graph sweep): graph BUILD
#: is 61.8% at or under 20 ms and 97.1% at or under 250 ms; graph RELEASE is 79.4% under
#: 20 ms and 97.1% under 50 ms. Both distributions live almost entirely inside what the
#: original shared ladder covered with a single 0.005 -> 0.02 -> 0.05 step, so this ladder is
#: densest exactly there. It stops at 10 s: a graph construction that takes longer than that
#: is a fault, not a distribution.
BUCKETS_GRAPH_OP: tuple[float, ...] = (0.002, 0.005, 0.01, 0.02, 0.035, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 10.0)

#: Tens of milliseconds to a minute. Per-INFERENCE and per-fine-window work.
#:
#: MEASURED: inference is 2.6% at or under 1 s, 94.7% at or under 2.5 s and 100% at or under
#: 5 s -- i.e. the whole distribution sat in ONE bucket of the original ladder, so its p95 was
#: a bucket boundary rather than a number. Fine-window measurement was 100% inside
#: (0.25, 0.5]: also one bucket. Split off from the graph ladder because the two populations
#: are two orders of magnitude apart and no single short ladder resolves both.
BUCKETS_INFERENCE: tuple[float, ...] = (0.05, 0.1, 0.25, 0.4, 0.6, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 30.0, 60.0)

#: Seconds to tens of minutes. For a chunk decode or a whole-model sweep over a chunk.
#:
#: MEASURED: a model sweep over a 10-window chunk was 2.9% at or under 10 s and 100% at or
#: under 30 s -- again a single bucket. Production chunks hold up to 30 windows, so a sweep
#: there is around 3x longer; 15 / 45 / 90 give the 10-90 s band the resolution the measured
#: shape says it needs, without lengthening a ladder that three 34-model instruments pay for.
BUCKETS_CHUNK: tuple[float, ...] = (0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 45.0, 90.0, 180.0, 300.0, 600.0, 1800.0)

#: Seconds to twelve hours. For a whole tier or a whole file -- the archive's longest
#: file is 43,466.880 s (12 h 04 m), so the ladder must reach past it or the top bucket
#: is +Inf for every long set and the p99 is unreadable exactly where it matters.
#: MEASURED: on a 30-minute file the fine tier landed in (60, 300] and the coarse tier in
#: (300, 900]. Production's own split is 378.266 s fine against 6,741.207 s coarse
#: (phaze-zaf2l section 3b), so 150 and 600 are added to resolve the fine tier, which would
#: otherwise share one bucket across a 5x range.
BUCKETS_RUN: tuple[float, ...] = (10.0, 30.0, 60.0, 150.0, 300.0, 600.0, 900.0, 1800.0, 3600.0, 7200.0, 14400.0, 28800.0, 43200.0, 86400.0)

#: Milliseconds to ten seconds. For HTTP handlers and database statements. The admin UI's
#: two heavy partials measure 534.0 ms and 1,378.6 ms (phaze-zaf2l section 4), so the ladder
#: is dense across 100 ms -- 2 s rather than across the SDK default's 5 ms -- 100 ms.
BUCKETS_REQUEST: tuple[float, ...] = (0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0, 10.0)

#: Milliseconds to an hour. A SAQ job is anything from an 11 ms dequeue to a multi-hour
#: process_file, so this ladder spans both rather than splitting the instrument in two.
BUCKETS_JOB: tuple[float, ...] = (0.01, 0.1, 0.5, 1.0, 5.0, 30.0, 120.0, 600.0, 1800.0, 3600.0, 14400.0, 43200.0)

#: Bytes. Peak RSS per chunk, against the 4Gi (4,294,967,296 B) pod limit. The ladder
#: brackets the limit on both sides so a breach is visible as a bucket crossing rather
#: than inferred from a mean.
BUCKETS_RSS: tuple[float, ...] = (
    536_870_912.0,
    1_073_741_824.0,
    1_610_612_736.0,
    2_147_483_648.0,
    2_684_354_560.0,
    3_221_225_472.0,
    3_758_096_384.0,
    4_294_967_296.0,
    6_442_450_944.0,
    8_589_934_592.0,
)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

HTTP_ROUTE = LabelSpec(
    name="http_route",
    # MEASURED against the real app, not estimated: `create_app()` declares 105 distinct
    # route templates and 106 distinct (method, template) pairs today
    # (POST 51 / GET 42 / PATCH 10 / PUT 2 / DELETE 1). 160 is that with headroom for growth;
    # it is a BOUND on a closed set declared in this repo's source, not a hope about traffic.
    cardinality=160,
    description=(
        "The matched route TEMPLATE -- '/record/{file_id}', never '/record/<a-real-uuid>'. "
        "A request matching no route reports the literal '__unmatched__', which is what keeps a 404 "
        "scan from minting one series per probed path. Bounded by the app's own route table: 105 "
        "templates measured against phaze.main.create_app()."
    ),
)
HTTP_METHOD = LabelSpec(
    name="http_method",
    cardinality=8,
    description="HTTP method, or 'OTHER' for anything outside the standard set.",
    values=("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"),
)
HTTP_STATUS_CLASS = LabelSpec(
    name="http_status_class",
    cardinality=6,
    description="Response status rounded to its class, plus 'error' for an unhandled exception.",
    values=("1xx", "2xx", "3xx", "4xx", "5xx", "error"),
)

SAQ_JOB = LabelSpec(
    name="saq_function",
    cardinality=64,
    description=(
        "Registered SAQ function name. Bounded by the union of the controller and agent function lists. "
        "NOT spelled 'job': that name is reserved by Prometheus for the target derived from service.name, "
        "and the collector drops the entire metric rather than reporting the collision (see RESERVED_LABEL_NAMES)."
    ),
)
STAGE_INFLIGHT_STATUS = LabelSpec(
    name="status",
    cardinality=2,
    description="Whether the stage's SAQ rows are waiting or being worked.",
    values=("queued", "active"),
)

DB_OPERATION = LabelSpec(
    name="db_operation",
    cardinality=8,
    description=(
        "First keyword of the statement -- SELECT / INSERT / UPDATE / DELETE / and so on. "
        "The statement TEXT is never a label: it is unbounded and it carries operator data."
    ),
    values=("SELECT", "INSERT", "UPDATE", "DELETE", "COMMIT", "ROLLBACK", "DDL", "OTHER"),
)

PIPELINE_STAGE = LabelSpec(
    name="stage",
    cardinality=64,
    description=(
        "The scheduling ledger's own unit of work: the Stage enum's name where the stage maps to one "
        "(metadata, analyze -- see stage_control.STAGE_TO_FUNCTION), else the SAQ function name, which "
        "is bounded by the union of the registered controller and agent function lists."
    ),
)
PIPELINE_TRANSITION = LabelSpec(
    name="transition",
    cardinality=2,
    description="Which side of the ledger row's life this is.",
    values=("scheduled", "resolved"),
)
BACKLOG_QUEUE = LabelSpec(
    name="backlog",
    cardinality=12,
    description="Which pipeline waiting-room is being counted.",
    values=(
        "awaiting_cloud",
        "analyzing_cloud",
        "queued_behind_quota",
        "admitted",
        "running",
        "finished",
        "pushing",
        "inadmissible",
        "analysis_failed",
        "analysis_stalled",
        "queued_analyze",
        "unrouted_queued_analyze",
    ),
)


CATALOGUE: tuple[MetricSpec, ...] = (
    # --- analysis: the 94.69% blind spot this epic exists to open up --------------
    MetricSpec(
        name="phaze.analysis.run.duration",
        kind="histogram",
        unit="s",
        description="Wall clock of one whole analyze_file call, from probe to assembled result.",
        labels=(OUTCOME,),
        buckets=BUCKETS_RUN,
    ),
    MetricSpec(
        name="phaze.analysis.tier.duration",
        kind="histogram",
        unit="s",
        description=(
            "Wall clock of one complete tier. The fine/coarse split this measures is the "
            "5.31% / 94.69% figure phaze-zaf2l had to reconstruct from log timestamps."
        ),
        labels=(TIER,),
        buckets=BUCKETS_RUN,
    ),
    MetricSpec(
        name="phaze.analysis.chunk.decode.duration",
        kind="histogram",
        unit="s",
        description="Wall clock of one D-07 chunk's streaming decode pass, per tier.",
        labels=(TIER,),
        buckets=BUCKETS_CHUNK,
    ),
    MetricSpec(
        name="phaze.analysis.chunk.derive.duration",
        kind="histogram",
        unit="s",
        description="Wall clock of one chunk's derive-and-assemble phase, after the inference peak is released.",
        labels=(TIER,),
        buckets=BUCKETS_CHUNK,
    ),
    MetricSpec(
        name="phaze.analysis.chunk.peak_rss",
        kind="histogram",
        unit="By",
        description=(
            "Whole-process peak RSS observed at the end of each chunk. A histogram rather than a "
            "gauge on purpose: an analyze Job is short-lived and exits between scrapes, so a gauge's "
            "last value is simply lost, while a histogram's buckets survive in the counter."
        ),
        labels=(TIER,),
        buckets=BUCKETS_RSS,
    ),
    MetricSpec(
        name="phaze.analysis.fine_window.duration",
        kind="histogram",
        unit="s",
        description="Wall clock of one fine window's RhythmExtractor2013 + KeyExtractor measurement.",
        buckets=BUCKETS_INFERENCE,
    ),
    MetricSpec(
        name="phaze.analysis.model.inference.duration",
        kind="histogram",
        unit="s",
        description=(
            "One model's inference on ONE coarse window -- the per-model cost by classifier_type "
            "that phaze-8ifq8 asks for. Recorded per window and per model; the WINDOW is not a label."
        ),
        labels=MODEL_LABELS,
        buckets=BUCKETS_INFERENCE,
        realized_combinations=MODEL_COMBINATIONS,
    ),
    MetricSpec(
        name="phaze.analysis.model.graph.build.duration",
        kind="histogram",
        unit="s",
        description=(
            "Constructing one TensorflowPredict* graph. D-07 pays this once per model PER CHUNK "
            "rather than once per file, which is the cost the chunking trades for a duration-independent peak."
        ),
        labels=MODEL_LABELS,
        buckets=BUCKETS_GRAPH_OP,
        realized_combinations=MODEL_COMBINATIONS,
    ),
    MetricSpec(
        name="phaze.analysis.model.graph.release.duration",
        kind="histogram",
        unit="s",
        description="Releasing one TensorflowPredict* graph -- the phaze-15sw one-graph-resident invariant's other half.",
        labels=MODEL_LABELS,
        buckets=BUCKETS_GRAPH_OP,
        realized_combinations=MODEL_COMBINATIONS,
    ),
    MetricSpec(
        name="phaze.analysis.model.sweep.duration",
        kind="histogram",
        unit="s",
        description="Build + sweep across every live buffer of one chunk + release, for one model.",
        labels=MODEL_LABELS,
        buckets=BUCKETS_CHUNK,
        realized_combinations=MODEL_COMBINATIONS,
    ),
    MetricSpec(
        name="phaze.analysis.windows",
        kind="counter",
        unit="{windows}",
        description="Windows reaching a terminal state, per tier. 'skipped' is per-window failure isolation firing.",
        labels=(TIER, WINDOW_OUTCOME),
    ),
    MetricSpec(
        name="phaze.analysis.chunks",
        kind="counter",
        unit="{chunks}",
        description="D-07 chunks completed, per tier.",
        labels=(TIER,),
    ),
    MetricSpec(
        name="phaze.analysis.audio.duration",
        kind="counter",
        unit="s",
        description=(
            "Audio-seconds admitted for analysis. Divided by wall clock this is the "
            "audio-hours-per-wall-hour throughput phaze-zaf2l derived by joining "
            "analysis_completed_at to metadata.duration by hand."
        ),
        labels=(OUTCOME,),
    ),
    # --- HTTP ---------------------------------------------------------------------
    MetricSpec(
        name="phaze.http.server.request.duration",
        kind="histogram",
        unit="s",
        description="Server-side handler duration, by matched route template.",
        labels=(HTTP_METHOD, HTTP_ROUTE, HTTP_STATUS_CLASS),
        buckets=BUCKETS_REQUEST,
        # Cartesian is 8 x 160 x 6 = 7,680 and is not the number to budget. A series exists
        # only for a (method, route, status_class) combination actually OBSERVED, and a route
        # serves the one or two methods it declares, not eight. MEASURED: 106 distinct
        # (method, template) pairs in the real app. At two status classes each -- a success
        # and one error class -- that is 212; 320 carries headroom.
        realized_combinations=320,
    ),
    MetricSpec(
        name="phaze.http.server.active_requests",
        kind="updowncounter",
        unit="{requests}",
        description="Requests currently in flight. No route label: this is a whole-process saturation read.",
    ),
    # --- SAQ ----------------------------------------------------------------------
    MetricSpec(
        name="phaze.saq.job.duration",
        kind="histogram",
        unit="s",
        description="Wall clock of one SAQ job, by registered function name and outcome.",
        labels=(SAQ_JOB, OUTCOME),
        buckets=BUCKETS_JOB,
        realized_combinations=80,
    ),
    MetricSpec(
        name="phaze.saq.jobs",
        kind="counter",
        unit="{jobs}",
        description="SAQ jobs reaching a terminal state, by function name and outcome.",
        labels=(SAQ_JOB, OUTCOME),
        realized_combinations=80,
    ),
    MetricSpec(
        name="phaze.pipeline.stage.inflight",
        kind="gauge",
        unit="{jobs}",
        description=(
            "SAQ rows queued and active, by pipeline STAGE. phaze-zaf2l sampled this by hand from "
            "`saq_jobs` every 120 s for the length of a spike; it is the read that settled 'SAQ is not "
            "a bottleneck' (depth held at 9 across 28 samples against a burst capacity of 318.3 jobs/s). "
            "Labelled by STAGE and not by QUEUE deliberately: the only sampler phaze has is the stage "
            "activity snapshot, which groups by the SAQ function name, and publishing that under a "
            "`queue` label would be mislabelled data. "
            "POLL-DRIVEN, like `phaze.pipeline.backlog`: sampled by the admin UI's own /pipeline/stats "
            "read, so it goes stale when no admin tab is open. Dashboard material, never alert material."
        ),
        labels=(PIPELINE_STAGE, STAGE_INFLIGHT_STATUS),
        realized_combinations=12,
    ),
    # --- database -----------------------------------------------------------------
    MetricSpec(
        name="phaze.db.statement.duration",
        kind="histogram",
        unit="s",
        description="Time one SQL statement spent at the driver, by leading keyword only.",
        labels=(DB_OPERATION,),
        buckets=BUCKETS_REQUEST,
    ),
    MetricSpec(
        name="phaze.db.statements",
        kind="counter",
        unit="{statements}",
        description=(
            "Statements executed, by leading keyword. Divided by request count this is the "
            "per-request fan-out phaze-zaf2l measured with pg_stat_user_tables deltas."
        ),
        labels=(DB_OPERATION,),
    ),
    # --- pipeline -----------------------------------------------------------------
    MetricSpec(
        name="phaze.pipeline.stage.transitions",
        kind="counter",
        unit="{transitions}",
        description=(
            "Scheduling-ledger transitions: a stage SCHEDULED for a file, and that row RESOLVED. "
            "The ledger is the durable record recovery reads, so this is the transition that matters; "
            "per-attempt work is counted by phaze.saq.jobs instead."
        ),
        labels=(PIPELINE_STAGE, PIPELINE_TRANSITION),
        realized_combinations=64,
    ),
    MetricSpec(
        name="phaze.pipeline.backlog",
        kind="gauge",
        unit="{files}",
        description=(
            "Files waiting in each pipeline waiting-room -- the 8,079-row `cloud_job` awaiting "
            "backlog phaze-zaf2l had to count in psql, among others. "
            "POLL-DRIVEN, and that limitation is deliberate rather than hidden: it is sampled by the "
            "admin UI's own /pipeline/stats read, so it goes STALE when no admin tab is open. Read it "
            "on a dashboard, never alert on it -- and note that backlog DEPTH is a settled operator "
            "decision (repowise decision e1e3374e, the current drain rate is ACCEPTED) and is not a "
            "fault condition in the first place."
        ),
        labels=(BACKLOG_QUEUE,),
    ),
)


BY_NAME: dict[str, MetricSpec] = {spec.name: spec for spec in CATALOGUE}


def total_series() -> int:
    """Every time series the catalogue can mint at its stated bound.

    This is the number phaze-m1drf.3 budgets and homelab sizes retention against. It is a
    CEILING, not a prediction: a metric only mints a series once a label combination is
    actually observed, and the measured figure in
    ``docs/telemetry/metric-catalogue.md`` section 5 is well under it.
    """
    return sum(spec.series_count() for spec in CATALOGUE)
