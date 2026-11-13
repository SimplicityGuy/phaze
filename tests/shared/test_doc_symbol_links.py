"""A doc link that names a source symbol must point at a file that still defines it (phaze-2al7q.3, finding 10).

``docs/design/tracklist-provider-orchestration.md`` kept linking ``build_cue_tracks_for_versions`` to
``services/cue_review.py`` after docs/design/0027-retire-legacy-timestamp-qualification.md removed it, and
``docs/design/tracklist-provider-contract.md`` did the same for ``_approved_applied_tracklist_base``. A file
link still resolves when the symbol it names is gone, so the link checker passes and the prose reads as current.

The check: in every tracked Markdown file, a link ``[text](path/to/src/phaze/....py)`` whose text names
snake_case identifiers must find each one in the linked file. Path components of the link target are not
symbols (``[stage_status](.../stage_status.py)`` names the module) and are skipped. Measured when written:
72 identifiers checked, 2 stale, both fixed in the same change.
"""

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
_LINK = re.compile(r"\[([^\[\]]+)\]\(([^)#\s]+\.py)(?:#[^)]*)?\)")
_SYMBOL = re.compile(r"^_?[a-z][a-z0-9_]*$")


def _markdown() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "*.md"],  # noqa: S607 -- git is the repository's required source-of-truth tool
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / name for name in tracked]


def stale_symbol_links(files: list[Path]) -> list[str]:
    stale: list[str] = []
    for doc in files:
        for match in _LINK.finditer(doc.read_text(errors="ignore")):
            target = (doc.parent / match.group(2)).resolve()
            if not target.is_file() or "/src/phaze/" not in target.as_posix():
                continue
            module_parts = {part.removesuffix(".py") for part in target.parts}
            source = target.read_text()
            for token in re.split(r"[\s/,`().:]+", match.group(1)):
                if _SYMBOL.match(token) and "_" in token and token not in module_parts and not re.search(rf"\b{re.escape(token)}\b", source):
                    stale.append(f"{doc.relative_to(ROOT)}: {token} is not in {target.relative_to(ROOT)}")
    return stale


def test_every_symbol_a_doc_link_names_exists_in_the_linked_file() -> None:
    assert stale_symbol_links(_markdown()) == []


def test_the_check_reports_a_removed_symbol(tmp_path: Path) -> None:
    doc = ROOT / "docs" / f"_probe_{tmp_path.name}.md"
    doc.write_text("[gone_symbol / build_cue_tracks_for_file](../src/phaze/services/cue_review.py)\n")
    try:
        assert stale_symbol_links([doc]) == [f"docs/{doc.name}: gone_symbol is not in src/phaze/services/cue_review.py"]
    finally:
        doc.unlink()
