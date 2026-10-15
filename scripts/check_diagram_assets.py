"""Validate maintained diagram provenance, relationships and reproducible local assets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from scripts.diagram_inputs import MIGRATION_RECEIPT_SHA256, render_input_digest


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    """Hash exact bytes, including vendored upstream sources."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_diagram_assets(root: Path = ROOT) -> dict[str, Any]:
    """Check dynamic manifest coverage without imposing a fixed diagram count."""
    directory = root / "docs/diagrams"
    errors: list[str] = []
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return {"diagrams": 0, "relationships": 0, "errors": ["docs/diagrams/manifest.json: missing diagram manifest"]}
    manifest = json.loads(manifest_path.read_text())
    assets: set[str] = set()
    slugs: set[str] = set()
    relationship_count = 0
    all_edges = {edge["id"] for diagram in manifest["diagrams"] for edge in diagram["edges"]}
    for diagram in manifest["diagrams"]:
        slug = diagram["slug"]
        if diagram.get("render_input_sha256") != render_input_digest(root, diagram):
            errors.append(f"{slug}: editable semantics/profile/rendering inputs changed without regeneration")
        if slug in slugs:
            errors.append(f"{slug}: duplicate diagram identity")
        slugs.add(slug)
        nodes = {node["id"] for node in diagram["nodes"]}
        edges = {edge["id"] for edge in diagram["edges"]}
        if len(edges) != len(diagram["edges"]):
            errors.append(f"{slug}: duplicate relationship identity")
        relationship_count += len(edges)
        for edge in diagram["edges"]:
            if edge["source"] not in nodes or edge["target"] not in nodes:
                errors.append(f"{slug}/{edge['id']}: missing endpoint")
            if not edge["evidence"]:
                errors.append(f"{slug}/{edge['id']}: missing source evidence")
            for evidence in edge["evidence"]:
                # Evidence paths are repository-relative; line suffixes are optional.
                path = evidence.split(":", 1)[0]
                if path.startswith("/") or ".." in Path(path).parts or not (root / path).is_file():
                    errors.append(f"{slug}/{edge['id']}: missing source evidence {evidence}")
        for original in diagram.get("origin", {}).get("relationships", []):
            if not original["rationale"] or not original["edges"] or not set(original["edges"]) <= all_edges:
                errors.append(f"{slug}/{original['id']}: incomplete original relationship disposition")
        if diagram.get("preview_source_sha256") != diagram.get("source_sha256") or diagram.get("preview_svg_sha256") != diagram.get("svg_sha256"):
            errors.append(f"{slug}: preview was not verified against the current HTML/SVG")
        if set(diagram["artifacts"]) != {"html", "svg", "png"}:
            errors.append(f"{slug}: mandatory HTML/SVG/PNG artifact mapping is incomplete")
        for kind, relative in diagram["artifacts"].items():
            if relative in assets:
                errors.append(f"{slug}: duplicate artifact ownership {relative}")
            assets.add(relative)
            path = root / relative
            if path.parent != directory or not path.is_file():
                errors.append(f"{slug}: missing {kind} asset {relative}")
                continue
            key = {"html": "source_sha256", "svg": "svg_sha256", "png": "png_sha256"}[kind]
            if diagram.get(key) != digest(path):
                errors.append(f"{slug}: stale {kind} digest; regenerate and verify preview")
            if kind in {"html", "svg"}:
                text = path.read_text()
                if re.search(r"(?:src|href)=[\"\'](?:https?:)?//|@import|url\([\"\']?https?:", text):
                    errors.append(f"{slug}: mandatory external resource in {kind}")
                if f'id="{slug}-title"' not in text or f'id="{slug}-desc"' not in text:
                    errors.append(f"{slug}: missing accessible {kind} title/description")
                for edge in edges:
                    if f'id="{edge}"' not in text:
                        errors.append(f"{slug}: {kind} silently drops relationship {edge}")
    baseline = manifest.get("migration_baseline")
    receipt_path = directory / "migration.json"
    if not baseline or not receipt_path.is_file() or digest(receipt_path) != MIGRATION_RECEIPT_SHA256:
        errors.append("manifest: missing or unauthenticated immutable migration receipt")
    else:
        receipt = json.loads(receipt_path.read_text())
        if (
            baseline.get("revision") != receipt["revision"]
            or baseline.get("diagrams") != len(receipt["diagrams"])
            or baseline.get("relationships") != sum(len(item["relationships"]) for item in receipt["diagrams"])
        ):
            errors.append("manifest: migration summary differs from immutable receipt")
        expected = {item["slug"]: item for item in receipt["diagrams"]}
        actual_origins = {diagram["slug"]: diagram["origin"] for diagram in manifest["diagrams"] if "origin" in diagram}
        if set(actual_origins) != set(expected):
            errors.append("manifest: original diagram dispositions are incomplete")
        for slug, origin in actual_origins.items():
            frozen = expected.get(slug)
            if frozen is None:
                continue
            identities = [{key: relation.get(key) for key in ("id", "statement_sha256")} for relation in origin["relationships"]]
            if any(origin.get(key) != frozen[key] for key in ("document", "ordinal", "source_sha256")) or identities != frozen["relationships"]:
                errors.append(f"{slug}: original source/relationship identity or digest changed")
    actual = {path.relative_to(root).as_posix() for suffix in ("*.html", "*.svg", "*.png") for path in directory.glob(suffix)}
    errors.extend(f"{path}: orphan diagram asset" for path in sorted(actual - assets))
    vendor = root / "scripts/vendor/diagram_design"
    upstream = json.loads((vendor / "UPSTREAM.json").read_text())
    for item in upstream["files"]:
        path = vendor / item["path"]
        if not path.is_file() or digest(path) != item["sha256"]:
            errors.append(f"{item['path']}: pinned upstream helper bytes changed")
    return {"diagrams": len(slugs), "relationships": relationship_count, "errors": sorted(errors)}
