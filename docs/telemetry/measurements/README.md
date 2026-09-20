# Historical Telemetry Measurements

This directory contains dated measurement inputs and verification records. Values and conclusions
are immutable point-in-time evidence from the environment named in each record; they do not define
the current telemetry contract. Use the maintained [telemetry documentation](../metric-catalogue.md)
for shipped behavior.

Repository-local links and Mermaid diagrams preserve the repository shape known when a measurement
was captured. `scripts/audit_historical_evidence.py` checks every local target and Mermaid fence. A
missing target reported as `historical_by_boundary` is intentionally retained as point-in-time
provenance, not promised as current navigation.

**`analysis-run.json` is `{}` and stays that way.** It was written by
`scripts/measure_metric_contract.py --out .` on 2026-08-26, in the same invocation that produced
`metric-contract-2026-08-26.md`. That invocation used `--scrape-only` — reading the series the
collector already exposed rather than driving a fresh analysis — and `--scrape-only` passes an
empty `report` dict into the writer by construction (see `_report`'s `scrape-only` call site), so
the file it serializes to is genuinely empty; it is not a truncated or corrupted capture. It is
retained as an accurate record of what that exact command produced, under the same point-in-time
rule as everything else in this directory: a real result would require re-running a live
`otel-collector-contrib` against a real multi-model analysis (`docker compose -f
docker-compose.telemetry.example.yml up -d` plus `measure_metric_contract.py --minutes 30`), which
this directory's historical entries are not re-run to backfill. `docs/telemetry/metric-catalogue.md`
does not cite it as a source of data.
