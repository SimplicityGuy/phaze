"""SQLAlchemy ORM models - import all models for Alembic autogenerate discovery."""

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord, FileState
from phaze.models.file_companion import FileCompanion
from phaze.models.fingerprint import FingerprintResult
from phaze.models.metadata import FileMetadata
from phaze.models.pipeline_stage_control import PipelineStageControl
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.route_control import RouteControl
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.stage_skip import StageSkip
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


__all__ = [
    "Agent",
    "AnalysisResult",
    "AnalysisWindow",
    "CloudJob",
    "CloudJobStatus",
    "DedupResolution",
    "DiscogsLink",
    "ExecutionLog",
    "ExecutionStatus",
    "FileCompanion",
    "FileMetadata",
    "FileRecord",
    "FileState",
    "FingerprintResult",
    "PipelineStageControl",
    "ProposalStatus",
    "RenameProposal",
    "RouteControl",
    "ScanBatch",
    "ScanStatus",
    "SchedulingLedger",
    "StageSkip",
    "TagWriteLog",
    "TagWriteStatus",
    "Tracklist",
    "TracklistTrack",
    "TracklistVersion",
]
