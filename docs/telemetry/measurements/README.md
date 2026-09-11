# Historical Telemetry Measurements

This directory contains dated measurement inputs and verification records. Values and conclusions
are immutable point-in-time evidence from the environment named in each record; they do not define
the current telemetry contract. Use the maintained [telemetry documentation](../metric-catalogue.md)
for shipped behavior.

Repository-local links and Mermaid diagrams preserve the repository shape known when a measurement
was captured. `scripts/audit_historical_evidence.py` checks every local target and Mermaid fence. A
missing target reported as `historical_by_boundary` is intentionally retained as point-in-time
provenance, not promised as current navigation.
