"""Tests for Tracklist, TracklistVersion, and TracklistTrack models."""

from datetime import date
import uuid

from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


class TestTracklistModel:
    """Tests for the Tracklist ORM model."""

    def test_tracklist_has_id_column(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert hasattr(t, "id")

    def test_tracklist_has_external_id(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert t.external_id == "tl-123"

    def test_tracklist_has_source_url(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert t.source_url == "https://example.com/tl/123"

    def test_tracklist_file_id_nullable(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert t.file_id is None

    def test_tracklist_file_id_accepts_uuid(self):
        fid = uuid.uuid4()
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123", file_id=fid)
        assert t.file_id == fid

    def test_tracklist_match_confidence_nullable(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert t.match_confidence is None

    def test_tracklist_auto_linked_default_false(self):
        """Verify auto_linked column has default=False."""
        col = Tracklist.__table__.columns["auto_linked"]
        assert col.default.arg is False

    def test_tracklist_artist_nullable(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert t.artist is None

    def test_tracklist_event_nullable(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert t.event is None

    def test_tracklist_date_nullable(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert t.date is None

    def test_tracklist_date_accepts_date(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123", date=date(2025, 4, 12))
        assert t.date == date(2025, 4, 12)

    def test_tracklist_latest_version_id_nullable(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert t.latest_version_id is None

    def test_tracklist_external_id_is_unique_among_canonical_rows_only(self):
        """phaze-fq9h.7 narrowed a GLOBAL unique to a PARTIAL one, and both halves matter.

        The drain looks a unique set up once and propagates the tracklist to the set's duplicate
        files, so one 1001TL page legitimately has several rows -- the propagated projections carry
        the page's own ``external_id``. What the old global UNIQUE really encoded, though, was "one
        row per page that we SCRAPED", and that survives verbatim as the partial index's predicate.

        Asserted structurally here; the behaviour (a second canonical row is refused, a projection
        is accepted) is pinned against real Postgres DDL in
        ``tests/integration/test_tracklist_propagation_real_db.py``.
        """
        assert Tracklist.__table__.columns["external_id"].unique is not True, "a GLOBAL unique would forbid every propagation"

        index = next(i for i in Tracklist.__table__.indexes if i.name == "ix_tracklists_external_id")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["external_id"]
        assert str(index.dialect_options["postgresql"]["where"]) == "propagated_from_set_key IS NULL"

    def test_propagation_columns_are_nullable_provenance(self):
        """Both are NULL on a scraped row -- which is what makes every pre-existing row canonical."""
        tracklist = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert tracklist.propagated_from_set_key is None
        assert tracklist.propagation_confidence is None

    def test_tracklist_file_id_fk_to_files(self):
        """Verify file_id FK references files.id."""
        col = Tracklist.__table__.columns["file_id"]
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "files.id" in fk_targets

    def test_tracklist_tablename(self):
        assert Tracklist.__tablename__ == "tracklists"

    def test_tracklist_has_timestamp_columns(self):
        t = Tracklist(external_id="tl-123", source_url="https://example.com/tl/123")
        assert hasattr(t, "created_at")
        assert hasattr(t, "updated_at")


class TestTracklistVersionModel:
    """Tests for the TracklistVersion ORM model."""

    def test_version_has_id(self):
        v = TracklistVersion(tracklist_id=uuid.uuid4(), version_number=1)
        assert hasattr(v, "id")

    def test_version_has_tracklist_id(self):
        tid = uuid.uuid4()
        v = TracklistVersion(tracklist_id=tid, version_number=1)
        assert v.tracklist_id == tid

    def test_version_has_version_number(self):
        v = TracklistVersion(tracklist_id=uuid.uuid4(), version_number=3)
        assert v.version_number == 3

    def test_version_has_scraped_at(self):
        v = TracklistVersion(tracklist_id=uuid.uuid4(), version_number=1)
        assert hasattr(v, "scraped_at")

    def test_version_tracklist_id_fk(self):
        col = TracklistVersion.__table__.columns["tracklist_id"]
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "tracklists.id" in fk_targets

    def test_version_tablename(self):
        assert TracklistVersion.__tablename__ == "tracklist_versions"

    def test_version_has_timestamp_columns(self):
        v = TracklistVersion(tracklist_id=uuid.uuid4(), version_number=1)
        assert hasattr(v, "created_at")
        assert hasattr(v, "updated_at")


class TestTracklistTrackModel:
    """Tests for the TracklistTrack ORM model."""

    def test_track_has_id(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=1)
        assert hasattr(t, "id")

    def test_track_has_position(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=5)
        assert t.position == 5

    def test_track_has_artist(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=1, artist="Skrillex")
        assert t.artist == "Skrillex"

    def test_track_artist_nullable(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=1)
        assert t.artist is None

    def test_track_has_title(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=1, title="Bangarang")
        assert t.title == "Bangarang"

    def test_track_has_label(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=1, label="OWSLA")
        assert t.label == "OWSLA"

    def test_track_has_timestamp(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=1, timestamp="01:23:45")
        assert t.timestamp == "01:23:45"

    def test_track_is_mashup_default_false(self):
        """Verify is_mashup column has default=False."""
        col = TracklistTrack.__table__.columns["is_mashup"]
        assert col.default.arg is False

    def test_track_has_remix_info(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=1, remix_info="VIP Mix")
        assert t.remix_info == "VIP Mix"

    def test_track_version_id_fk(self):
        col = TracklistTrack.__table__.columns["version_id"]
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "tracklist_versions.id" in fk_targets

    def test_track_tablename(self):
        assert TracklistTrack.__tablename__ == "tracklist_tracks"

    def test_track_has_timestamp_columns(self):
        t = TracklistTrack(version_id=uuid.uuid4(), position=1)
        assert hasattr(t, "created_at")
        assert hasattr(t, "updated_at")
