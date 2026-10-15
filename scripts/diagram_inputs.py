"""Bind editable diagram semantics and local rendering inputs to exported artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from pathlib import Path


MIGRATION_RECEIPT_SHA256 = "bd317f119cf6f6be185f39649dc06ec32529e4277230ab7e516a1bec1755fe34"


_OUTPUT_KEYS = frozenset(
    {"artifacts", "source_sha256", "svg_sha256", "png_sha256", "preview_source_sha256", "preview_svg_sha256", "render_input_sha256"}
)


def render_input_digest(root: Path, diagram: dict[str, Any]) -> str:
    """Hash semantic/layout source, profile, renderer, exporter and exact embedded fonts."""
    assets = root / "docs/diagrams"
    fonts = json.loads((assets / "fonts/provenance.json").read_text())
    resources = [
        "docs/diagrams/profile.json",
        "docs/diagrams/fonts/provenance.json",
        "scripts/render_documentation_diagrams.py",
        "scripts/diagram_inputs.py",
        "scripts/vendor/diagram_design/skills/diagram-design/scripts/export_svg.py",
        *[f"docs/diagrams/fonts/{font['file']}" for font in fonts["fonts"]],
    ]
    inputs = {
        "diagram": {key: value for key, value in diagram.items() if key not in _OUTPUT_KEYS},
        "resources": {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in resources},
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
