"""SQLAlchemy ORM models - import all models for Alembic autogenerate discovery."""

from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.models.backend_breaker import BackendBreaker
from phaze.models.cloud_budget import CloudBudget
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.companion_import import CompanionImportItem, CompanionImportRun, ProviderAcquisitionAttempt
from phaze.models.companion_junk_review import CompanionJunkReview
from phaze.models.dedup_resolution import DedupResolution
from phaze.models.dedup_review_plan import DedupReviewPlan
from phaze.models.deployment import Deployment
from phaze.models.discogs_link import DiscogsLink
from phaze.models.execution import ExecutionLog, ExecutionStatus
from phaze.models.file import FileRecord
from phaze.models.file_companion import FileCompanion
from phaze.models.filename_convention import FilenameConvention
from phaze.models.metadata import FileMetadata
from phaze.models.orphan_companion_diagnostic import OrphanCompanionDiagnostic
from phaze.models.pipeline_stage_control import PipelineStageControl
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.models.provider_source import (
    ProviderRecordingCandidate,
    ProviderRecordingSelection,
    ProviderSelectionEvent,
    ProviderSourceObject,
    ProviderSourceObservation,
)
from phaze.models.route_control import RouteControl
from phaze.models.runtime_config_override import RuntimeConfigOverride
from phaze.models.scan_batch import ScanBatch, ScanStatus
from phaze.models.scheduling_ledger import SchedulingLedger
from phaze.models.set_profile import SetProfile
from phaze.models.stage_skip import StageSkip
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.models.tracklist import Tracklist, TracklistTrack, TracklistVersion


__all__ = [
    "Agent",
    "AnalysisResult",
    "AnalysisWindow",
    "BackendBreaker",
    "CloudBudget",
    "CloudJob",
    "CloudJobStatus",
    "CompanionContentFeatures",
    "CompanionImportItem",
    "CompanionImportRun",
    "CompanionJunkReview",
    "DedupResolution",
    "DedupReviewPlan",
    "Deployment",
    "DiscogsLink",
    "ExecutionLog",
    "ExecutionStatus",
    "FileCompanion",
    "FileMetadata",
    "FileRecord",
    "FilenameConvention",
    "OrphanCompanionDiagnostic",
    "PipelineStageControl",
    "ProposalStatus",
    "ProviderAcquisitionAttempt",
    "ProviderRecordingCandidate",
    "ProviderRecordingSelection",
    "ProviderSelectionEvent",
    "ProviderSourceObject",
    "ProviderSourceObservation",
    "RenameProposal",
    "RouteControl",
    "RuntimeConfigOverride",
    "ScanBatch",
    "ScanStatus",
    "SchedulingLedger",
    "SetProfile",
    "StageSkip",
    "TagWriteLog",
    "TagWriteStatus",
    "Tracklist",
    "TracklistTrack",
    "TracklistVersion",
]
