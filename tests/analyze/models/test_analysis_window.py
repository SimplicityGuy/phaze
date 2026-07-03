"""Tests for the AnalysisWindow ORM model.

AnalysisWindow is the queryable child table of ``files`` (1:many) introduced in
phase 31. Unlike the 1:1 ``AnalysisResult`` aggregate row, a single file can own
many window rows, so ``file_id`` is indexed but NOT unique and carries
``ON DELETE CASCADE`` (deleting a file removes its windows -- no orphans). The
existing ``AnalysisResult`` must stay structurally unchanged: its ``file_id``
keeps ``unique=True`` and must NOT gain an ``ondelete`` (migration 018 is
additive-only, so an ORM CASCADE there would claim a constraint Postgres never
enforces).
"""

import uuid

from phaze.models.analysis import AnalysisResult, AnalysisWindow


class TestAnalysisWindowModel:
    """Tests for the AnalysisWindow ORM model."""

    def test_tablename(self) -> None:
        assert AnalysisWindow.__tablename__ == "analysis_window"

    def test_instantiates_with_fine_tier_fields(self) -> None:
        fid = uuid.uuid4()
        w = AnalysisWindow(
            file_id=fid,
            tier="fine",
            window_index=0,
            start_sec=0.0,
            end_sec=30.0,
            bpm=128.0,
            musical_key="Am",
        )
        assert w.file_id == fid
        assert w.tier == "fine"
        assert w.window_index == 0
        assert w.start_sec == 0.0
        assert w.end_sec == 30.0
        assert w.bpm == 128.0
        assert w.musical_key == "Am"

    def test_instantiates_with_coarse_tier_fields(self) -> None:
        w = AnalysisWindow(
            file_id=uuid.uuid4(),
            tier="coarse",
            window_index=1,
            start_sec=0.0,
            end_sec=180.0,
            mood="happy",
            style="house",
            danceability=0.8,
            features={"energy": 0.9},
        )
        assert w.tier == "coarse"
        assert w.mood == "happy"
        assert w.style == "house"
        assert w.danceability == 0.8
        assert w.features == {"energy": 0.9}

    def test_has_id(self) -> None:
        w = AnalysisWindow(file_id=uuid.uuid4(), tier="fine", window_index=0, start_sec=0.0, end_sec=30.0)
        assert hasattr(w, "id")

    def test_has_timestamp_columns(self) -> None:
        """created_at/updated_at are provided by TimestampMixin (not redeclared)."""
        columns = {c.name for c in AnalysisWindow.__table__.columns}
        assert "created_at" in columns
        assert "updated_at" in columns

    def test_file_id_fk_to_files(self) -> None:
        col = AnalysisWindow.__table__.columns["file_id"]
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "files.id" in fk_targets

    def test_file_id_is_indexed(self) -> None:
        col = AnalysisWindow.__table__.columns["file_id"]
        assert col.index is True

    def test_file_id_is_not_unique(self) -> None:
        """Two windows can share a file_id (1:many), so file_id must not be unique."""
        col = AnalysisWindow.__table__.columns["file_id"]
        assert not col.unique

    def test_file_id_fk_has_cascade_ondelete(self) -> None:
        col = AnalysisWindow.__table__.columns["file_id"]
        ondeletes = {fk.ondelete for fk in col.foreign_keys}
        assert "CASCADE" in ondeletes

    def test_file_id_not_nullable(self) -> None:
        col = AnalysisWindow.__table__.columns["file_id"]
        assert col.nullable is False

    def test_fine_only_columns_nullable(self) -> None:
        for name in ("bpm", "musical_key", "mood", "style", "danceability", "features"):
            assert AnalysisWindow.__table__.columns[name].nullable is True


class TestAnalysisResultUnchanged:
    """AnalysisResult must remain structurally unchanged (additive-only migration)."""

    def test_analysis_result_file_id_still_unique(self) -> None:
        col = AnalysisResult.__table__.columns["file_id"]
        assert col.unique is True

    def test_analysis_result_file_id_has_no_cascade(self) -> None:
        """AnalysisResult must NOT claim an ORM CASCADE (no matching DB ALTER in 018)."""
        col = AnalysisResult.__table__.columns["file_id"]
        ondeletes = {fk.ondelete for fk in col.foreign_keys}
        assert ondeletes == {None}

    def test_analysis_window_is_the_only_cascade_fk(self) -> None:
        """Across both models in analysis.py, only AnalysisWindow.file_id is CASCADE."""
        cascade_cols = []
        for model in (AnalysisResult, AnalysisWindow):
            for col in model.__table__.columns:
                for fk in col.foreign_keys:
                    if fk.ondelete == "CASCADE":
                        cascade_cols.append(f"{model.__tablename__}.{col.name}")
        assert cascade_cols == ["analysis_window.file_id"]
