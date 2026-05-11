"""Tests for Agent model."""

from phaze.models.agent import Agent
from phaze.models.base import Base


class TestAgent:
    """Tests for Agent model."""

    def test_table_name(self) -> None:
        assert Agent.__tablename__ == "agents"

    def test_table_in_metadata(self) -> None:
        assert "agents" in Base.metadata.tables

    def test_agents_table_columns(self) -> None:
        columns = {c.name for c in Agent.__table__.columns}
        required = {
            "id",
            "name",
            "token_hash",
            "scan_roots",
            "last_seen_at",
            "revoked_at",
            "created_at",
            "updated_at",
        }
        assert required.issubset(columns)

    def test_id_is_primary_key(self) -> None:
        pk_cols = [c.name for c in Agent.__table__.primary_key.columns]
        assert pk_cols == ["id"]

    def test_token_hash_nullable(self) -> None:
        assert Agent.__table__.c.token_hash.nullable is True

    def test_scan_roots_is_jsonb(self) -> None:
        assert "JSONB" in str(Agent.__table__.c.scan_roots.type)

    def test_id_charset_constraint_declared(self) -> None:
        constraint_names = {c.name for c in Agent.__table__.constraints}
        assert "ck_agents_id_charset" in constraint_names

    def test_name_required(self) -> None:
        assert Agent.__table__.c.name.nullable is False

    def test_token_hash_max_length(self) -> None:
        type_str = str(Agent.__table__.c.token_hash.type)
        assert "VARCHAR(128)" in type_str or "String(128)" in type_str
