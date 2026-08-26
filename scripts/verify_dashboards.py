#!/usr/bin/env python
"""Import the committed dashboards into a RUNNING Grafana and check every query.

phaze-m1drf.4 acceptance 1, 2 and 5. ADR-0012 rule 3 decides the shape: *"verify with the
artifact's real consumer"*. The real consumer of Grafana dashboard JSON is **Grafana**, not
a JSON-schema validator -- a schema check would accept a dashboard whose datasource uid
does not exist in the target instance, whose panel type Grafana does not have, or whose
PromQL does not parse, and all three are exactly what breaks an import.

So this script:

1. **creates a second Prometheus datasource with a deliberately unrelated uid**, because
   acceptance 2 is *"verify by importing into an instance whose datasource uid differs"* --
   importing against the uid the example compose happens to provision would prove nothing;
2. **POSTs each dashboard through the real import API** and fails on Grafana's own verdict;
3. **reads each one back** and asserts every datasource reference is the template variable;
4. **runs every panel's PromQL through Grafana's query API against the unrelated uid**, so a
   query that does not parse, or names a metric that does not exist, is a failure here
   rather than an empty panel someone notices in a month;
5. **reports which panels returned real data**, which is acceptance 5 -- a dashboard that
   renders only in theory is not done.

Not a pytest: it needs a live Grafana and a Prometheus holding a real analysis run, which
CI has neither of. ``tests/shared/telemetry/test_dashboards.py`` holds the CI-runnable half
(generator drift, no hard-coded uids, no local identifiers, catalogued metric names only).

    docker compose -f docker-compose.telemetry.example.yml up -d
    uv run python scripts/verify_dashboards.py --grafana http://localhost:3000
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request


DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboards"

#: Deliberately unlike anything this repo provisions. The point of the exercise is that the
#: dashboards carry no uid of their own, so the one they land on is the operator's.
UNRELATED_UID = "unrelated-grafana-9f2c41ab"
UNRELATED_NAME = "phaze-import-check-prometheus"


def emit(text: str = "") -> None:
    """The script's entire output is what it prints; it is the evidence a bead cites."""
    print(text, flush=True)  # noqa: T201


def _request(base: str, path: str, payload: dict | None = None, method: str | None = None) -> tuple[int, dict]:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    if not base.startswith(("http://", "https://")):
        # Bandit B310's actual concern: urlopen honours file:/ and custom schemes. The base
        # comes from --grafana on this operator's own command line, so refusing anything but
        # http(s) here is the check rather than a suppression of it.
        msg = f"--grafana must be an http(s) URL, got {base!r}"
        raise SystemExit(msg)
    request = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))  # noqa: S310  # nosec B310 - scheme checked immediately above
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310 - scheme checked above
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"raw": body}


def ensure_unrelated_datasource(base: str, prometheus_url: str) -> None:
    status, _ = _request(base, f"/api/datasources/uid/{UNRELATED_UID}")
    if status == 200:
        emit(f"# datasource {UNRELATED_UID} already present")
        return
    status, body = _request(
        base,
        "/api/datasources",
        {"name": UNRELATED_NAME, "type": "prometheus", "access": "proxy", "url": prometheus_url, "uid": UNRELATED_UID, "isDefault": False},
    )
    emit(f"# created datasource {UNRELATED_UID}: HTTP {status} {body.get('message', '')}")
    if status >= 300:
        raise SystemExit(f"could not create the unrelated datasource: {body}")


def _panel_iter(dashboard: dict) -> list[dict]:
    panels: list[dict] = []
    for panel in dashboard.get("panels", []):
        panels.append(panel)
        panels.extend(panel.get("panels", []))
    return panels


def check_dashboard(base: str, path: Path) -> tuple[int, int, list[str], list[str]]:
    """Import one dashboard and query every panel.

    Returns ``(panels, with-data, problems, empty-panel-titles)``. The empty list is reported
    rather than swallowed: acceptance 5 is *"every panel renders with real data"*, and a bare
    count says nothing about WHICH panel is empty or whether its emptiness is legitimate
    (a "windows skipped" panel is correctly empty when nothing was skipped).
    """
    source = json.loads(path.read_text(encoding="utf-8"))
    status, body = _request(base, "/api/dashboards/db", {"dashboard": source, "overwrite": True, "folderUid": ""})
    if status >= 300:
        return 0, 0, [f"IMPORT REJECTED by Grafana: HTTP {status} {body}"], []
    uid = body["uid"]

    status, body = _request(base, f"/api/dashboards/uid/{uid}")
    if status >= 300:
        return 0, 0, [f"could not read back {uid}: HTTP {status}"], []
    stored = body["dashboard"]

    problems: list[str] = []
    rendered = json.dumps(stored)
    for hardcoded in re.findall(r'"uid":\s*"([^"$][^"]*)"', rendered):
        if hardcoded != uid:
            problems.append(f"HARD-CODED datasource/panel uid survived the round trip: {hardcoded!r}")

    panels = [panel for panel in _panel_iter(stored) if panel.get("type") != "row"]
    with_data = 0
    empty: list[str] = []
    for panel in panels:
        panel_had_data = False
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if not expr:
                continue
            # Substitute the dashboard variables Grafana would substitute at render time.
            resolved = expr.replace("$__rate_interval", "5m").replace('{job=~"$job"}', "").replace('job=~"$job", ', "").replace('job=~"$job"', "")
            status, result = _request(
                base,
                "/api/ds/query",
                {
                    "queries": [
                        {
                            "refId": target.get("refId", "A"),
                            "expr": resolved,
                            "datasource": {"type": "prometheus", "uid": UNRELATED_UID},
                            "instant": True,
                        }
                    ],
                    "from": "now-6h",
                    "to": "now",
                },
            )
            if status >= 300:
                problems.append(f"{panel['title']!r}: query API HTTP {status}: {result}")
                continue
            frames = result.get("results", {}).get(target.get("refId", "A"), {})
            if frames.get("error"):
                problems.append(f"{panel['title']!r}: PromQL rejected: {frames['error']}")
                continue
            rows = 0
            for frame in frames.get("frames", []):
                values = frame.get("data", {}).get("values") or []
                rows += len(values[0]) if values and values[0] else 0
            if rows:
                with_data += 1
                panel_had_data = True
                break
        if not panel_had_data:
            empty.append(str(panel.get("title", "?")))
    return len(panels), with_data, problems, empty


def warm_service_metrics(otlp_endpoint: str, instance: str) -> None:
    """Drive the REAL api, database and SAQ hooks so the service-health panels have data.

    phaze-m1drf.4 acceptance 5 is *"every panel renders with real data"*, and an analysis run
    alone leaves the HTTP, database and SAQ panels empty -- the analysis child emits none of
    those. So this drives the real surfaces:

    * **HTTP** -- real requests through ``phaze.main.create_app()``'s real router, INSIDE the
      real lifespan, so the route labels are the app's own templates. A handler that still
      raises is useful rather than a problem: it exercises the ``error`` and ``5xx`` status
      classes alongside ``/health``'s ``2xx``.
    * **database** -- real statements through a real ``AsyncEngine`` against the test
      Postgres, so the events fire under the async driver rather than a stand-in.
    * **SAQ** -- the real ``before_process`` / ``after_process`` hooks with the context shape
      ``saq.worker.Worker.process`` builds.

    Everything here is synthetic TRAFFIC against REAL code. It proves the panels resolve; it
    is not a claim about production volumes.
    """
    import asyncio  # noqa: PLC0415

    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = otlp_endpoint
    os.environ["PHAZE_TELEMETRY_INSTANCE"] = instance
    os.environ.setdefault("PHAZE_ROLE", "api")

    from phaze.telemetry import (  # noqa: PLC0415
        configure_telemetry,
        pipeline as telemetry_pipeline,
        saq as telemetry_saq,
        shutdown_telemetry,
    )

    if not configure_telemetry("api"):
        raise SystemExit("telemetry did not configure; is OTEL_EXPORTER_OTLP_ENDPOINT reachable-looking?")

    from fastapi.testclient import TestClient  # noqa: PLC0415

    from phaze.main import create_app  # noqa: PLC0415

    async def _database_and_queue() -> None:
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415

        from phaze.telemetry.db import instrument_engine  # noqa: PLC0415

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tests.db_guard import resolve_test_dsn  # noqa: PLC0415

        engine = create_async_engine(resolve_test_dsn())
        instrument_engine(engine)
        async with engine.begin() as conn:
            for _ in range(20):
                await conn.execute(text("SELECT 1"))
            await conn.execute(text("CREATE TABLE IF NOT EXISTS phaze_dashboard_probe (id int)"))
            await conn.execute(text("INSERT INTO phaze_dashboard_probe (id) VALUES (1)"))
            await conn.execute(text("UPDATE phaze_dashboard_probe SET id = 2"))
            await conn.execute(text("DELETE FROM phaze_dashboard_probe"))
            await conn.execute(text("DROP TABLE phaze_dashboard_probe"))
        await engine.dispose()

        class _Job:
            def __init__(self, function: str, status: str) -> None:
                self.function, self.status, self.key, self.attempts = function, status, f"{function}:probe", 1

        for function, status in (
            ("process_file", "Status.COMPLETE"),
            ("extract_file_metadata", "Status.COMPLETE"),
            ("generate_proposals", "Status.FAILED"),
        ):
            ctx: dict[str, object] = {"job": _Job(function, status)}
            await telemetry_saq.before_process(ctx)
            await telemetry_saq.after_process(ctx)

    app = create_app()
    paths = ["/health", "/s/analyze", "/s/summary", "/pipeline/stats", "/record/00000000-0000-0000-0000-000000000000", "/definitely-not-a-route"]

    # EVERYTHING happens INSIDE the lifespan, and that is not a stylistic choice. LEAVING the
    # TestClient context runs the app's lifespan shutdown, which calls `shutdown_telemetry()`
    # -- correct product behaviour -- and tears the providers down. A first version of this
    # harness opened and closed the context and then drove its requests, and recorded NOTHING:
    # the only metrics that survived were the database statements the lifespan's own
    # `run_migrations` + `SELECT 1` issued while telemetry was still alive.
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in paths * 3:
            with contextlib.suppress(Exception):
                client.get(path)
        emit(f"# drove {len(paths) * 3} HTTP requests through the real router, inside the real lifespan")
        asyncio.run(_database_and_queue())
        emit("# drove real SQL statements and SAQ hooks")
        telemetry_pipeline.record_backlog(
            {"awaiting_cloud": 8079, "analyzing_cloud": 4, "pushing": 0, "inadmissible": 0, "analysis_failed": 4, "analysis_stalled": 1459}
        )
        telemetry_pipeline.record_stage_inflight({"analyze": {"queued": 9, "active": 4}, "metadata": {"queued": 0, "active": 0}})
        for _ in range(5):
            telemetry_pipeline.record_transition("process_file", "scheduled")
            telemetry_pipeline.record_transition("process_file", "resolved")
        emit("# published pipeline backlog, stage inflight and ledger transitions")

    # The lifespan's own shutdown has already flushed everything above. This is the belt for
    # anything a future edit records outside the block, and it is harmless when there is none.
    shutdown_telemetry(10000)
    emit("# flushed; waiting for the collector's batch processor")
    time.sleep(20)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--grafana", default="http://localhost:3000")
    parser.add_argument("--prometheus", default="http://prometheus:9090", help="as GRAFANA sees it, not as this shell does")
    parser.add_argument(
        "--warm-service-metrics", action="store_true", help="drive the real api/database/SAQ paths first so the service-health panels have data"
    )
    parser.add_argument("--otlp-endpoint", default="http://localhost:4318")
    parser.add_argument("--instance", default="measurement-host")
    args = parser.parse_args()

    status, body = _request(args.grafana, "/api/health")
    if status != 200:
        raise SystemExit(f"Grafana is not answering at {args.grafana}: HTTP {status} {body}")
    emit(f"# Grafana {body.get('version')} at {args.grafana}")

    ensure_unrelated_datasource(args.grafana, args.prometheus)

    if args.warm_service_metrics:
        warm_service_metrics(args.otlp_endpoint, args.instance)

    failures = 0
    emit()
    emit("| dashboard | panels | panels returning real data | verdict |")
    emit("| --- | ---: | ---: | --- |")
    all_empty: dict[str, list[str]] = {}
    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        panels, with_data, problems, empty = check_dashboard(args.grafana, path)
        verdict = "OK" if not problems else f"{len(problems)} PROBLEM(S)"
        failures += len(problems)
        emit(f"| {path.name} | {panels} | {with_data} | {verdict} |")
        for problem in problems:
            emit(f"|   | | | {problem} |")
        if empty:
            all_empty[path.name] = empty
    emit()
    if all_empty:
        emit("## Panels that returned no data at this instant")
        emit()
        emit("A panel can be legitimately empty -- a 'windows skipped' panel is correct to be empty")
        emit("when nothing was skipped, and every query here is a rate() that needs the series to be")
        emit("live NOW. Read this list, do not just count it.")
        emit()
        for name, titles in all_empty.items():
            emit(f"- **{name}**")
            for title in titles:
                emit(f"  - {title}")
        emit()
    emit(f"# unrelated datasource uid used for every query: {UNRELATED_UID}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
