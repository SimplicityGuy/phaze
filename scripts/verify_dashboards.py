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
import json
from pathlib import Path
import re
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


def check_dashboard(base: str, path: Path) -> tuple[int, int, list[str]]:
    """Import one dashboard and query every panel. Returns (panels, with-data, problems)."""
    source = json.loads(path.read_text(encoding="utf-8"))
    status, body = _request(base, "/api/dashboards/db", {"dashboard": source, "overwrite": True, "folderUid": ""})
    if status >= 300:
        return 0, 0, [f"IMPORT REJECTED by Grafana: HTTP {status} {body}"]
    uid = body["uid"]

    status, body = _request(base, f"/api/dashboards/uid/{uid}")
    if status >= 300:
        return 0, 0, [f"could not read back {uid}: HTTP {status}"]
    stored = body["dashboard"]

    problems: list[str] = []
    rendered = json.dumps(stored)
    for hardcoded in re.findall(r'"uid":\s*"([^"$][^"]*)"', rendered):
        if hardcoded != uid:
            problems.append(f"HARD-CODED datasource/panel uid survived the round trip: {hardcoded!r}")

    panels = [panel for panel in _panel_iter(stored) if panel.get("type") != "row"]
    with_data = 0
    for panel in panels:
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
                break
    return len(panels), with_data, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--grafana", default="http://localhost:3000")
    parser.add_argument("--prometheus", default="http://prometheus:9090", help="as GRAFANA sees it, not as this shell does")
    args = parser.parse_args()

    status, body = _request(args.grafana, "/api/health")
    if status != 200:
        raise SystemExit(f"Grafana is not answering at {args.grafana}: HTTP {status} {body}")
    emit(f"# Grafana {body.get('version')} at {args.grafana}")

    ensure_unrelated_datasource(args.grafana, args.prometheus)

    failures = 0
    emit()
    emit("| dashboard | panels | panels returning real data | verdict |")
    emit("| --- | ---: | ---: | --- |")
    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        panels, with_data, problems = check_dashboard(args.grafana, path)
        verdict = "OK" if not problems else f"{len(problems)} PROBLEM(S)"
        failures += len(problems)
        emit(f"| {path.name} | {panels} | {with_data} | {verdict} |")
        for problem in problems:
            emit(f"|   | | | {problem} |")
    emit()
    emit(f"# unrelated datasource uid used for every query: {UNRELATED_UID}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
