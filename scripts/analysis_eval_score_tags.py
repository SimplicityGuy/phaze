"""Score ranked styles against reviewable homelab source-tag family mappings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any


def score(reference: dict[str, Any], run_dir: Path, mapping: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for item in reference["items"]:
        tag = item.get("source_genre_tag")
        if not tag:
            continue
        sha = item["sha256"]
        patterns = mapping["patterns"]
        if tag in mapping["excluded"]:
            rows.append({"sha256": sha, "tag": tag, "status": "excluded", "reason": mapping["excluded"][tag]})
            continue
        if tag not in patterns:
            rows.append({"sha256": sha, "tag": tag, "status": "unmapped"})
            continue
        paths = list(run_dir.glob(f"{sha}-*/prediction.json"))
        if len(paths) != 1:
            raise ValueError(f"expected one prediction for {sha[:12]}")
        prediction = json.loads(paths[0].read_text())
        styles = [row["name"] for row in prediction.get("style") or []]
        if not styles:
            raise ValueError(f"missing ranked styles for {sha[:12]}")
        pattern = re.compile(patterns[tag])
        rows.append(
            {
                "sha256": sha,
                "tag": tag,
                "status": "scored",
                "top1": bool(pattern.search(styles[0])),
                "top5": any(pattern.search(name) for name in styles[:5]),
            }
        )
    scored = [row for row in rows if row["status"] == "scored"]
    return {
        "interpretation": mapping["interpretation"],
        "source": mapping["source"],
        "run_dir": run_dir.name,
        "summary": {
            "source_tags": len(rows),
            "scored": len(scored),
            "excluded_or_unmapped": len(rows) - len(scored),
            "top1": sum(row["top1"] for row in scored) / len(scored) if scored else None,
            "top5": sum(row["top5"] for row in scored) / len(scored) if scored else None,
        },
        "per_file": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    reference = json.loads(args.reference.read_text())
    mapping = json.loads(args.mapping.read_text())
    if reference.get("schema_version") != 1 or mapping.get("schema_version") != 1:
        parser.error("expected schema_version=1 reference and mapping")
    result = score(reference, args.run_dir, mapping)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(result["summary"], sort_keys=True))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
