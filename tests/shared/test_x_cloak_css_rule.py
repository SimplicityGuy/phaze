"""Guard: the app's source CSS must define the `[x-cloak]` display rule.

Alpine v3 does not inject `[x-cloak]{display:none}` itself — the app must define it. Without it every
`x-cloak` in the templates is inert and flashes fallback content before `x-show` initializes. The rule
lives in the source `assets/src/app.css`; the compiled `src/phaze/static/css/app.css` is gitignored and
rebuilt via `just tailwind`, so we assert the source (the durable artifact), not the build output.
"""

from __future__ import annotations

from pathlib import Path
import re


_APP_CSS = Path(__file__).resolve().parents[2] / "assets" / "src" / "app.css"


def test_source_css_defines_x_cloak_display_none() -> None:
    css = _APP_CSS.read_text(encoding="utf-8")
    # Match `[x-cloak] { display: none ... }` tolerating whitespace and an optional !important.
    pattern = re.compile(r"\[x-cloak\]\s*\{[^}]*display\s*:\s*none[^}]*\}", re.IGNORECASE)
    assert pattern.search(css), (
        "assets/src/app.css must define `[x-cloak] { display: none !important; }` — Alpine v3 does not "
        "inject it, so without this rule every x-cloak in the templates is inert and flashes on load."
    )
