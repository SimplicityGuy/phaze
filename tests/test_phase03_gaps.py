"""Phase 03 gap-filling tests.

Covers behaviors not exercised by the existing 17 companion/dedup tests:
- FileCompanion model: tablename, column definitions, constraint declarations
- Pydantic schemas: required fields and types for all four companion/dedup schemas
- Alembic migration 003: down_revision chain, downgrade function present, index creation
"""

from __future__ import annotations

import uuid

from sqlalchemy import UniqueConstraint

from phaze.models.file_companion import FileCompanion
from phaze.schemas.companion import AssociateResponse, DuplicateFile, DuplicateGroup, DuplicateGroupsResponse


# ---------------------------------------------------------------------------
# FileCompanion model
# ---------------------------------------------------------------------------


def test_file_companion_tablename() -> None:
    """FileCompanion should use file_companions as its table name."""
    assert FileCompanion.__tablename__ == "file_companions"


def test_file_companion_has_id_column() -> None:
    """FileCompanion should have a UUID primary key column named id."""
    col = FileCompanion.__table__.columns["id"]
    assert col.primary_key is True


def test_file_companion_has_companion_id_column() -> None:
    """FileCompanion should have a non-nullable companion_id FK column."""
    col = FileCompanion.__table__.columns["companion_id"]
    assert col.nullable is False


def test_file_companion_has_media_id_column() -> None:
    """FileCompanion should have a non-nullable media_id FK column."""
    col = FileCompanion.__table__.columns["media_id"]
    assert col.nullable is False


def test_file_companion_companion_id_references_files() -> None:
    """companion_id FK should reference the files table."""
    col = FileCompanion.__table__.columns["companion_id"]
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "files"


def test_file_companion_media_id_references_files() -> None:
    """media_id FK should reference the files table."""
    col = FileCompanion.__table__.columns["media_id"]
    fk = next(iter(col.foreign_keys))
    assert fk.column.table.name == "files"


def test_file_companion_has_unique_constraint_on_pair() -> None:
    """FileCompanion should have a unique constraint on (companion_id, media_id)."""
    constraints = {c for c in FileCompanion.__table__.constraints if isinstance(c, UniqueConstraint)}
    unique_col_sets = [frozenset(col.name for col in c.columns) for c in constraints]
    assert frozenset({"companion_id", "media_id"}) in unique_col_sets


def test_file_companion_has_timestamp_columns() -> None:
    """FileCompanion should have created_at and updated_at columns from TimestampMixin."""
    column_names = {c.name for c in FileCompanion.__table__.columns}
    assert "created_at" in column_names
    assert "updated_at" in column_names


def test_file_companion_fk_cascade_delete_companion() -> None:
    """companion_id FK should CASCADE on delete."""
    col = FileCompanion.__table__.columns["companion_id"]
    fk = next(iter(col.foreign_keys))
    assert fk.ondelete.upper() == "CASCADE"


def test_file_companion_fk_cascade_delete_media() -> None:
    """media_id FK should CASCADE on delete."""
    col = FileCompanion.__table__.columns["media_id"]
    fk = next(iter(col.foreign_keys))
    assert fk.ondelete.upper() == "CASCADE"


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


def test_associate_response_required_fields() -> None:
    """AssociateResponse should require new_associations (int) and message (str)."""
    resp = AssociateResponse(new_associations=5, message="Done")
    assert resp.new_associations == 5
    assert resp.message == "Done"


def test_associate_response_new_associations_is_int() -> None:
    """AssociateResponse.new_associations should be an integer."""
    resp = AssociateResponse(new_associations=0, message="")
    assert isinstance(resp.new_associations, int)


def test_duplicate_file_required_fields() -> None:
    """DuplicateFile should require id, original_path, file_size, and file_type."""
    uid = uuid.uuid4()
    f = DuplicateFile(id=uid, original_path="/music/track.mp3", file_size=3000000, file_type="mp3")
    assert f.id == uid
    assert f.original_path == "/music/track.mp3"
    assert f.file_size == 3000000
    assert f.file_type == "mp3"


def test_duplicate_group_required_fields() -> None:
    """DuplicateGroup should require sha256_hash, count, and files list."""
    uid = uuid.uuid4()
    df = DuplicateFile(id=uid, original_path="/music/a.mp3", file_size=1000, file_type="mp3")
    group = DuplicateGroup(sha256_hash="a" * 64, count=1, files=[df])
    assert group.sha256_hash == "a" * 64
    assert group.count == 1
    assert len(group.files) == 1


def test_duplicate_groups_response_required_fields() -> None:
    """DuplicateGroupsResponse should require groups, total_groups, limit, and offset."""
    resp = DuplicateGroupsResponse(groups=[], total_groups=0, limit=100, offset=0)
    assert resp.groups == []
    assert resp.total_groups == 0
    assert resp.limit == 100
    assert resp.offset == 0


def test_duplicate_groups_response_groups_is_list_of_duplicate_group() -> None:
    """DuplicateGroupsResponse.groups should be a list of DuplicateGroup instances."""
    uid = uuid.uuid4()
    df = DuplicateFile(id=uid, original_path="/music/b.mp3", file_size=2000, file_type="mp3")
    group = DuplicateGroup(sha256_hash="b" * 64, count=1, files=[df])
    resp = DuplicateGroupsResponse(groups=[group], total_groups=1, limit=10, offset=0)
    assert isinstance(resp.groups[0], DuplicateGroup)


# ---------------------------------------------------------------------------
# Alembic migration 003
# ---------------------------------------------------------------------------


def test_migration_003_down_revision_is_002() -> None:
    """Migration 003 should chain from revision 002."""
    import importlib.util
    from pathlib import Path

    migration_path = Path(__file__).parent.parent / "alembic" / "versions" / "003_add_file_companions_table.py"
    spec = importlib.util.spec_from_file_location("migration_003", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert module.down_revision == "002"


def test_migration_003_revision_is_003() -> None:
    """Migration 003 should have revision identifier '003'."""
    import importlib.util
    from pathlib import Path

    migration_path = Path(__file__).parent.parent / "alembic" / "versions" / "003_add_file_companions_table.py"
    spec = importlib.util.spec_from_file_location("migration_003", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert module.revision == "003"


def test_migration_003_has_downgrade_function() -> None:
    """Migration 003 should define a downgrade() function."""
    import importlib.util
    from pathlib import Path

    migration_path = Path(__file__).parent.parent / "alembic" / "versions" / "003_add_file_companions_table.py"
    spec = importlib.util.spec_from_file_location("migration_003", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert callable(getattr(module, "downgrade", None))


def test_migration_003_has_upgrade_function() -> None:
    """Migration 003 should define an upgrade() function."""
    import importlib.util
    from pathlib import Path

    migration_path = Path(__file__).parent.parent / "alembic" / "versions" / "003_add_file_companions_table.py"
    spec = importlib.util.spec_from_file_location("migration_003", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert callable(getattr(module, "upgrade", None))
