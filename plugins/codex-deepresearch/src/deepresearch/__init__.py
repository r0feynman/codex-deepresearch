"""Core helpers for the Codex DeepResearch plugin runner."""

from .evidence_schema import ValidationError, ValidationResult, validate_artifacts
from .execution_mode import ConfigResolutionError, RunConfig, resolve_config
from .fetch_claims import FetchClaimsError, fetch_claims
from .manual_sources import ManualSourcesError, ingest_manual_sources
from .modality_router import ModalityDecision, route_angle, route_angles
from .report_generation import ReportGenerationError, synthesize_report
from .search_handoff import SearchHandoffError, ingest_run, prepare_run, resolve_run_dir
from .verification_matrix import VerificationMatrixError, verify_claims
from .vision_adapter import VisionAdapterError, ingest_vision_observations

__all__ = [
    "ConfigResolutionError",
    "FetchClaimsError",
    "ManualSourcesError",
    "ModalityDecision",
    "ReportGenerationError",
    "RunConfig",
    "SearchHandoffError",
    "ValidationError",
    "ValidationResult",
    "VerificationMatrixError",
    "VisionAdapterError",
    "fetch_claims",
    "ingest_run",
    "ingest_manual_sources",
    "ingest_vision_observations",
    "prepare_run",
    "route_angle",
    "route_angles",
    "resolve_config",
    "resolve_run_dir",
    "synthesize_report",
    "validate_artifacts",
    "verify_claims",
]
