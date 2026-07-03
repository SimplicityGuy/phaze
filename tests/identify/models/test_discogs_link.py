"""Tests for DiscogsLink model."""

import uuid

from phaze.models.discogs_link import DiscogsLink


class TestDiscogsLinkModel:
    """Tests for the DiscogsLink ORM model."""

    def test_discogs_link_instance_creation(self):
        """DiscogsLink can be created with all required fields."""
        link = DiscogsLink(
            track_id=uuid.uuid4(),
            discogs_release_id="r12345",
            confidence=85.5,
            status="candidate",
        )
        assert link.track_id is not None
        assert link.discogs_release_id == "r12345"
        assert link.confidence == 85.5
        assert link.status == "candidate"

    def test_status_defaults_to_candidate(self):
        """status column server_default is 'candidate'."""
        col = DiscogsLink.__table__.columns["status"]
        assert col.server_default is not None
        assert col.server_default.arg == "candidate"

    def test_tablename(self):
        """DiscogsLink has correct __tablename__."""
        assert DiscogsLink.__tablename__ == "discogs_links"

    def test_index_on_track_id(self):
        """DiscogsLink has index on track_id."""
        indexes = {idx.name for idx in DiscogsLink.__table__.indexes}
        assert "ix_discogs_links_track_id" in indexes

    def test_index_on_status(self):
        """DiscogsLink has index on status."""
        indexes = {idx.name for idx in DiscogsLink.__table__.indexes}
        assert "ix_discogs_links_status" in indexes

    def test_index_on_discogs_release_id(self):
        """DiscogsLink has index on discogs_release_id."""
        indexes = {idx.name for idx in DiscogsLink.__table__.indexes}
        assert "ix_discogs_links_discogs_release_id" in indexes

    def test_optional_fields_accept_none(self):
        """Optional fields (discogs_artist, discogs_title, discogs_label, discogs_year) accept None."""
        link = DiscogsLink(
            track_id=uuid.uuid4(),
            discogs_release_id="r12345",
            confidence=85.5,
        )
        assert link.discogs_artist is None
        assert link.discogs_title is None
        assert link.discogs_label is None
        assert link.discogs_year is None

    def test_optional_fields_accept_values(self):
        """Optional fields accept actual values."""
        link = DiscogsLink(
            track_id=uuid.uuid4(),
            discogs_release_id="r12345",
            confidence=85.5,
            discogs_artist="deadmau5",
            discogs_title="Strobe",
            discogs_label="mau5trap",
            discogs_year=2009,
        )
        assert link.discogs_artist == "deadmau5"
        assert link.discogs_title == "Strobe"
        assert link.discogs_label == "mau5trap"
        assert link.discogs_year == 2009

    def test_has_id_column(self):
        """DiscogsLink has uuid id column."""
        link = DiscogsLink(
            track_id=uuid.uuid4(),
            discogs_release_id="r12345",
            confidence=85.5,
        )
        assert hasattr(link, "id")

    def test_track_id_fk_to_tracklist_tracks(self):
        """track_id FK references tracklist_tracks.id."""
        col = DiscogsLink.__table__.columns["track_id"]
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "tracklist_tracks.id" in fk_targets

    def test_has_timestamp_columns(self):
        """DiscogsLink inherits TimestampMixin columns."""
        link = DiscogsLink(
            track_id=uuid.uuid4(),
            discogs_release_id="r12345",
            confidence=85.5,
        )
        assert hasattr(link, "created_at")
        assert hasattr(link, "updated_at")
