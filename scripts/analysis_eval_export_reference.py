"""Freeze SHA-addressed homelab analysis results and source genre tags.

Runs one read-only, repeatable-read transaction through lux's local Postgres
container. The output contains no archive path, filename, artist, or title.
Run on a trusted workstation, then copy the private JSON to the benchmark host.
"""

# ruff: noqa: S608 -- SQL literals come only from UUID/SHA-validated manifest values.

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from scripts.analysis_eval import load_manifest


def export(manifest: Path, output: Path) -> tuple[str, int]:
    items = load_manifest(manifest)
    values = ",\n".join(f"('{item['file_id']}'::uuid, '{item['sha256']}')" for item in items)
    sql = f"""
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH sample(file_id, sha256) AS (VALUES {values}),
snapshot AS (
  SELECT s.sha256, s.file_id,
    jsonb_build_object(
      'bpm', a.bpm, 'musical_key', a.musical_key, 'mood', a.mood,
      'style', a.style, 'dominant_style', a.dominant_style,
      'fine_windows_total', a.fine_windows_total,
      'fine_windows_analyzed', a.fine_windows_analyzed,
      'coarse_windows_total', a.coarse_windows_total,
      'coarse_windows_analyzed', a.coarse_windows_analyzed,
      'analysis_completed_at', a.analysis_completed_at
    ) AS analysis,
    m.genre AS source_genre_tag,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'tier', w.tier, 'window_index', w.window_index,
      'start_sec', w.start_sec, 'end_sec', w.end_sec,
      'bpm', w.bpm, 'musical_key', w.musical_key,
      'mood', w.mood, 'style', w.style, 'danceability', w.danceability,
      'mood_scores', w.mood_scores, 'energy', w.energy
    ) ORDER BY w.tier, w.window_index), '[]'::jsonb)
     FROM analysis_window w WHERE w.file_id = s.file_id) AS windows
  FROM sample s
  JOIN analysis a ON a.file_id = s.file_id
  LEFT JOIN metadata m ON m.file_id = s.file_id
)
SELECT jsonb_build_object('schema_version', 1,
  'snapshot_at_utc', transaction_timestamp(),
  'manifest_sha256', '{hashlib.sha256(manifest.read_bytes()).hexdigest()}',
  'items', (SELECT jsonb_agg(to_jsonb(snapshot) ORDER BY sha256) FROM snapshot));
ROLLBACK;
"""
    proc = subprocess.run(
        ["/usr/bin/ssh", "-o", "BatchMode=yes", "datum@lux", "docker exec -i postgres psql -X -q -At -U phaze -d phaze -v ON_ERROR_STOP=1"],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError("read-only homelab reference export failed; inspect lux")
    data = json.loads(proc.stdout)
    rows = data.get("items")
    if not isinstance(rows, list) or len(rows) != len(items) or {row["sha256"] for row in rows} != {item["sha256"] for item in items}:
        raise RuntimeError("homelab reference does not cover the manifest exactly")
    if any(row["analysis"]["analysis_completed_at"] is None or not row["windows"] for row in rows):
        raise RuntimeError("homelab reference contains an incomplete analysis")
    if output.exists():
        raise FileExistsError(output)
    payload = (json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    output.write_bytes(payload)
    output.chmod(0o600)
    return hashlib.sha256(payload).hexdigest(), len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest, count = export(args.manifest, args.output)
    print(json.dumps({"items": count, "sha256": digest, "output": str(args.output)}))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
