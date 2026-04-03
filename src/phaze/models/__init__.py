"""SQLAlchemy ORM models - import all models for Alembic autogenerate discovery."""

from phaze.models.analysis import AnalysisResult
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.proposal import RenameProposal
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


__all__ = [
    "AnalysisResult",
    "DiscogsLink",
    "ExecutionLog",
    "FileCompanion",
    "FileMetadata",
    "FileRecord",
    "FingerprintResult",
    "RenameProposal",
    "ScanBatch",
    "ScanStatus",
    "TagWriteLog",
    "TagWriteStatus",
    "Tracklist",
    "TracklistTrack",
    "TracklistVersion",
]
