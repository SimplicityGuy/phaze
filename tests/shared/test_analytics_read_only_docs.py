"""Every ``analytics_*`` view and column is documented (phaze-p22p8). Filesystem-only: no database needed."""

from pathlib import Path

from tests.integration.test_migrations.analytics_contract import EXPECTED_COLUMNS


_DOC = Path(__file__).resolve().parents[2] / "docs" / "analytics-read-only-access.md"


def test_the_docs_describe_every_view_and_column() -> None:
    doc = _DOC.read_text(encoding="utf-8")
    for view, columns in EXPECTED_COLUMNS.items():
        assert f"### `{view}`" in doc, f"{view} has no section in {_DOC.name}"
        section = doc.split(f"### `{view}`", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
        for column in columns:
            assert f"| `{column}` |" in section, f"{view}.{column} is not documented"
    for phrase in ("pg_hba", "listen_addresses", "hostssl", "Stability rule"):
        assert phrase in doc
