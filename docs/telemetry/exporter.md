# Pointing phaze at a collector

**Audience: whoever is wiring this into homelab.** Nothing here requires reading phaze's
source. The metric contract itself — every metric, its labels and each label's bound — is
[`metric-catalogue.md`](metric-catalogue.md); the topology decision and its alternatives
are [`docs/design/0017-telemetry-export-topology.md`](../design/0017-telemetry-export-topology.md).

**phaze does not own the observability stack.** It emits. The collector, Prometheus,
Grafana and any Alertmanager are homelab's, and this repo makes no decision about
retention, storage sizing, scrape topology or alert routing.

______________________________________________________________________

## 1. Turning it on

Telemetry is **OFF unless an OTLP endpoint is configured**. With none set, no SDK provider
is installed at all and every instrumentation call site in phaze resolves to the OpenTelemetry
API's no-op.

```bash
# The one required variable. Point it at the collector's OTLP/HTTP receiver.
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.internal:4318

# Optional. Only if the collector wants auth.
OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer%20…

# Optional, and READ SECTION 3 BEFORE SETTING IT PER POD.
PHAZE_TELEMETRY_INSTANCE=host-prod
```

Signal-specific endpoints (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`,
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`) work too, and either one alone is enough to turn
telemetry on.

**`OTEL_SDK_DISABLED=true` turns it off again, and phaze says so.** That is the OpenTelemetry
SDK's own kill switch, so it is the right way to disable telemetry for a process without
touching the endpoint configuration — a CI runner, a one-off script, a tool that wraps phaze.
phaze checks it explicitly and logs `telemetry_off_sdk_disabled` rather than installing
providers that would accept every call and record nothing: under the kill switch the SDK hands
out no-op meters and tracers from providers that construct perfectly happily, so "configured"
and "recording" would otherwise part company silently. Only a case-insensitive `true` counts,
matching the SDK's own parsing.

Every phaze process configures itself independently, because each is its own OS process:
the api through its FastAPI lifespan, the control and agent workers through their SAQ
startup hooks, and the exec'd analysis child through its `main`. **A k8s analyze Job
inherits the endpoint through its pod environment and configures its own SDK**; nothing
extra is needed for the burst node beyond the variable being present in the Job's env.

## 2. Protocol and port

| | |
| --- | --- |
| protocol | **OTLP over HTTP/protobuf** (`opentelemetry-exporter-otlp-proto-http`) |
| default port | **4318** — the OTLP/HTTP convention. phaze does NOT speak gRPC (4317) |
| paths | the SDK's own: **`/v1/traces`** and `/v1/metrics`, appended to the endpoint |
| traces only | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` overrides the generic endpoint for spans alone, and setting it ALONE is enough to turn telemetry on |
| compression | the SDK default (`OTEL_EXPORTER_OTLP_COMPRESSION` if you want gzip) |
| direction | **outbound from phaze only.** Nothing scrapes phaze; phaze pushes |

HTTP/protobuf rather than gRPC is deliberate: the gRPC exporter needs `grpcio`, a heavy
binary wheel that would have to build for every platform in the essentia/TensorFlow matrix,
and its channel keeps reconnection backoff across a shutdown call — the exact shape §4 has
to bound for a pod that must exit.

**Network reality for the burst node.** `vox` reaches the home server over **Tailscale**, so
the endpoint given to an analyze Job is the collector's tailnet address. When the tailnet is
down the export fails and the analysis is unaffected — see §4.

## 3. How phaze identifies itself

Resource attributes on **metrics** — these become the Prometheus `job` and `instance`
labels:

| attribute | value |
| --- | --- |
| `service.name` | `phaze-api`, `phaze-controller`, `phaze-agent`, `phaze-analysis`, `phaze-watcher` |
| `service.namespace` | `phaze` |
| `service.version` | the installed package version |
| `service.instance.id` | **`PHAZE_TELEMETRY_INSTANCE`, defaulting to the service name** |
| `phaze.role` | `api` / `controller` / `agent` / `analysis` / `watcher` |
| `deployment.environment.name` | `PHAZE_DEPLOYMENT_ENVIRONMENT`, when set |

> **Do not set `PHAZE_TELEMETRY_INSTANCE` per pod.** `service.instance.id` becomes the
> Prometheus `instance` label and it multiplies **every** series the service emits. The
> analysis role emits ~1,700 series and runs as a k8s Job whose pod name is unique per
> analyzed file, so a per-pod instance id would mint a fresh ~1,700-series block for each of
> the archive's **11,428** files. Set it to the **HOST** (`host-prod`, `vox`) or leave it
> alone.

Per-process identity is not thrown away — it is carried on **spans**, where it is stored
per-occurrence and aged out rather than forever and per-series: `process.pid`, `host.name`,
and `k8s.pod.name` / `k8s.node.name` / `phaze.backend.id` from `PHAZE_POD_NAME` /
`PHAZE_NODE_NAME` / `PHAZE_BACKEND_ID` when the Job sets them.

**One file's analysis is one trace**, across the process boundary. `services/analysis_exec.py`
passes `env=telemetry.child_environment()` when it execs `python -m phaze.analysis_child` — a
COPY of the environment carrying this span's W3C `TRACEPARENT` — and the child extracts it
before importing essentia and continues the same trace. Copying rather than mutating
`os.environ` matters: a mutated environment would leak the span context into every other
subprocess the worker ever spawns. Both directions are total — an absent, empty or malformed
`TRACEPARENT` simply starts a new trace rather than raising.

**Where the spans go is `docs/design/0017-telemetry-export-topology.md` §7**, and what one
looks like is [`traces.md`](traces.md). Two things to know here:

- **The span queue is bounded at 2,048 and drops oldest-first** when no backend is listening,
  exactly like the metric path. A single file cannot fill it — the worst case in the corpus, a
  12 h 04 m set, emits **388** spans (18.9%) — so what is lost with a dead collector is older
  files' traces, never a truncated current one.
- **Sampling is always-on.** No head or tail sampling, because spans are per CHUNK rather than
  per window: ~49.6 spans per file, **~566,073 for a whole-corpus re-analysis**, 0.034
  spans/second at the measured throughput. The arithmetic is in ADR-0017 §7d.

## 4. What happens when the collector is down, slow or absent

**Nothing that matters.** This is a hard requirement, not a best effort: a dropped metric is
acceptable, an analyze job that fails because homelab was rebooting is not — at 1.4951× the
file's own duration, a job lost that way is hours of burst-node time.

| situation | what happens |
| --- | --- |
| no endpoint configured | no SDK provider is installed; instrumentation is a no-op |
| endpoint malformed | logged once at WARNING, telemetry stays off, the process runs |
| collector unreachable | exports fail on a background thread and are dropped; the queue is bounded (2,048 spans) and drops when full |
| collector slow | each export attempt is deadlined at **5 s** (`OTEL_EXPORTER_OTLP_TIMEOUT`, in **seconds**), retries included |
| collector down at process exit | teardown is abandoned after **3,000 ms** (`PHAZE_TELEMETRY_FLUSH_TIMEOUT_MS`) and the process exits |

> **The bound is enforced by phaze, not by the SDK's own timeouts, and it had to be.**
> `TracerProvider.shutdown()` takes no timeout argument at all and `MeterProvider.shutdown()`
> defaults to 30,000 ms. A first implementation that bounded only `force_flush` was measured
> at **40.3 s** against a black-holed collector while asking for 3 — which for a k8s analyze
> Job is 40 s of a pod refusing to die with a Kueue slot behind it, once per file. Teardown
> now runs in a joined daemon thread and is abandoned at the deadline; both SDK worker
> threads are daemons, so the interpreter does not wait for them either.

This is verified by running the real thing, not by reading the settings:
`tests/shared/telemetry/test_telemetry_never_breaks_analysis.py` runs a **real essentia
analysis to completion** against (a) an unroutable address and (b) a real HTTP listener that
accepts and then stalls past every timeout, and compares the result to the telemetry-off run.

### What is actually lost

Metrics are **cumulative**, so each export carries the running total rather than a delta.

- **A job that has been exporting successfully and loses only its final export** loses at
  most the increments since the previous one — bounded by `OTEL_METRIC_EXPORT_INTERVAL`,
  default **15 s**.
- **A job whose collector was unreachable for the whole run** loses **everything**, not just
  a tail. Nothing was ever delivered. This is the accepted trade, and it is why the alert
  rules phaze ships are built to be silent rather than to fire on absence.

`shutdown_telemetry()` returns `False` when the teardown was **abandoned at the deadline**,
so an over-budget shutdown is visible rather than silent.

**A `True` return does not prove delivery, and phaze does not claim it does.** The SDK's
`force_flush` reports that its own queue drained, not that the collector accepted anything:
measured against a listener that accepts and then stalls for 30 s, both providers returned
`True` while nothing had left the process, because the periodic worker had already taken the
batch out of the queue and was sitting on a failing export. The SDK exposes no delivery
signal. **Whether homelab received anything is a question for homelab's collector** — its
own `otelcol_exporter_sent_metric_points` / `otelcol_receiver_accepted_metric_points`
counters are the authority, and they are on homelab's side of the line on purpose.

## 5. Verifying it works

```bash
# 1. Bring up a collector locally (DEVELOPMENT ILLUSTRATION -- not the production topology).
docker compose -f docker-compose.telemetry.example.yml up -d

# 2. Point phaze at it and do something.
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318

# 3. Read the collector's own Prometheus exposition -- these are the exact series
#    homelab's Prometheus will scrape.
curl -s http://localhost:8889/metrics | grep '^phaze_'
```

**One collector behaviour worth knowing before you size a scrape interval.** The collector's
Prometheus exporter drops a series it has not seen an update for within its
`metric_expiration` (**default 5 minutes**), so an analyze Job's final counters disappear
from the scrape endpoint five minutes after the pod exits. Observed directly while building
this: 203 phaze series at the endpoint, then zero, with the collector healthy and the
Prometheus target still `up`. At a 15 s scrape interval Prometheus still catches roughly
twenty scrapes of the final value, so the default is fine — but a long scrape interval and
this default together lose whole jobs silently.

If nothing appears, check in this order: the endpoint variable is set **in the process that
does the work** (the worker and the analysis child, not only the api); the collector's log
for `failed to convert metric` (a label collision drops a metric silently at the scrape
endpoint while logging there); and that at least one export interval (15 s) has elapsed.

## 5a. Dashboards and alert rules

Both are artifacts you may adopt; neither is deployed from this repo.

- **Dashboards** — `dashboards/*.json`, four of them. Import through Grafana's normal
  Dashboards → Import flow and pick your own Prometheus: every panel references a
  `${datasource}` **template variable** and no hard-coded uid appears anywhere. Verified by
  importing all four into a running **Grafana 12.3.1** against a datasource whose uid was
  chosen to be unrelated to anything this repo provisions, and running every panel's PromQL
  through the query API: **30 of 30 panels returned real data**
  (`measurements/dashboard-verification.md`).
- **Alert rules** — `alerts/phaze-alerts.yml`, three of them, with four candidates
  documented for *not* being shipped. `docs/telemetry/alerting.md` has the reasoning and the
  `promtool` commands.

## 6. What is explicitly NOT decided here

Retention. Storage sizing. Scrape intervals. High availability. Alert routing. Dashboard
provisioning in production. Those are homelab's, and this repo contains no configuration for
any of them. The compose file and the YAML under `deploy/telemetry/` are labelled
development illustrations in their own headers and exist so a developer can see the data and
so this repo's measured figures could be taken against a real collector.
