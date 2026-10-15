"""Run pinned diagram-design checks with a narrowly audited embedded-font adapter.

Upstream's resource rule rejects every non-fragment CSS URL, including offline data fonts.
Verify the exact local font hashes and declarations, remove only those declarations from a
scratch copy, then run the unchanged upstream skin and accessibility checks on that copy.
All other resource, script, geometry, skin and accessibility rules remain active.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess  # nosec B404 -- fixed vendored tools, explicit argv, no shell
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.render_documentation_diagrams import ASSETS, VENDOR, embedded_fonts  # noqa: E402


def main() -> int:
    """Validate exact font bytes before applying unchanged upstream non-font checks."""
    provenance = json.loads((ASSETS / "fonts/provenance.json").read_text())
    for font in provenance["fonts"]:
        if hashlib.sha256((ASSETS / "fonts" / font["file"]).read_bytes()).hexdigest() != font["sha256"]:
            raise ValueError(f"Changed licensed font bytes: {font['file']}")
    upstream = json.loads((VENDOR / "UPSTREAM.json").read_text())
    for helper in upstream["files"]:
        if hashlib.sha256((VENDOR / helper["path"]).read_bytes()).hexdigest() != helper["sha256"]:
            raise ValueError(f"Changed upstream helper bytes: {helper['path']}")
    with tempfile.TemporaryDirectory(prefix="phaze-diagram-check-") as temporary:
        paths = []
        for source in sorted(ASSETS.glob("*.html")):
            text = source.read_text()
            faces = embedded_fonts()
            if text.count(faces) != 1:
                raise ValueError(f"{source.name}: font declarations differ from the audited local adapter")
            target = Path(temporary) / source.name
            target.write_text(text.replace(faces, "", 1))
            paths.append(str(target))
        commands = [
            [str(VENDOR / "scripts/verify-geometry.py"), *map(str, sorted(ASSETS.glob("*.html")))],
            [str(VENDOR / "scripts/lint-skin.py"), *paths],
            [str(VENDOR / "skills/diagram-design/scripts/self_check.py"), "--offline", *paths],
            [str(VENDOR / "scripts/verify-contrast.py"), "--style-guide", str(VENDOR / "skills/diagram-design/references/style-guide.md")],
        ]
        results = [subprocess.run([sys.executable, *command], check=False).returncode for command in commands]  # noqa: S603
        return int(any(result != 0 for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
