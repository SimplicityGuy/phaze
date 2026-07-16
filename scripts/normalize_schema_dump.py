"""Normalize a ``pg_dump --schema-only`` file for schema-equivalence comparison.

Phase 102 (Alembic migration-chain flatten) fidelity tooling: the merge gate compares
the schema produced by the pre-flatten 001-039 chain against the schema produced by the
single ``039`` baseline migration. Raw ``pg_dump -s`` output carries run-specific noise
(session ``SET``s, ownership, comments, the psql ``\\restrict`` guard) and the
``alembic_version`` bookkeeping table, none of which is schema under test. This script
strips that noise so a plain ``diff`` of two normalized dumps is the equivalence proof.

Usage:
    uv run python scripts/normalize_schema_dump.py <raw-dump.sql> <normalized-out.sql>
"""

import argparse
from pathlib import Path


_SKIP_PREFIXES = (
    "\\restrict",
    "\\unrestrict",
    "SET ",
    "SELECT pg_catalog.set_config",
    "--",
    "COMMENT ON ",
)


def normalize_dump(raw: str) -> str:
    """Return ``raw`` pg_dump SQL with non-schema noise and ``alembic_version`` removed."""
    lines: list[str] = []
    skip_until_semicolon = False
    for line in raw.splitlines():
        stripped = line.strip()
        if skip_until_semicolon:
            if stripped.endswith(";"):
                skip_until_semicolon = False
            continue
        if not stripped:
            continue
        if any(stripped.startswith(prefix) for prefix in _SKIP_PREFIXES):
            continue
        # Ownership is environment noise, not schema.
        if stripped.startswith("ALTER TABLE") and " OWNER TO " in stripped:
            continue
        # alembic_version is Alembic bookkeeping, managed outside the migration bodies:
        # drop its CREATE TABLE block and the ALTER TABLE ONLY block adding its PK.
        if stripped.startswith(("CREATE TABLE public.alembic_version", "ALTER TABLE ONLY public.alembic_version")):
            skip_until_semicolon = not stripped.endswith(";")
            continue
        # The PK constraint line belongs to the two-line ALTER block above; when the
        # ALTER header ended without a semicolon the block-skip above consumes it.
        lines.append(line.rstrip())
    return "\n".join(lines) + "\n"


def main() -> None:
    """CLI entry point: normalize ``input_path`` into ``output_path``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_path", type=Path, help="raw pg_dump --schema-only file")
    parser.add_argument("output_path", type=Path, help="normalized output file")
    args = parser.parse_args()
    args.output_path.write_text(normalize_dump(args.input_path.read_text(encoding="utf-8")), encoding="utf-8")


if __name__ == "__main__":
    main()
