"""Core helpers for the Codex DeepResearch plugin runner."""

from .evidence_schema import ValidationError, ValidationResult, validate_artifacts
from .execution_mode import ConfigResolutionError, RunConfig, resolve_config
from .fetch_claims import FetchClaimsError, fetch_claims
from .guardrails import GuardrailsError, enforce_guardrails
from .manual_sources import ManualSourcesError, ingest_manual_sources
from .modality_router import ModalityDecision, route_angle, route_angles
from .mvp_smoke import MvpSmokeError, run_mvp_smoke
from .report_generation import ReportGenerationError, synthesize_report
from .run_state import (
    RUN_STEPS_FILENAME,
    RUN_STEPS_SCHEMA_VERSION,
    RunStepStateError,
    begin_stage,
    inspect_run_state,
    run_steps_path,
    transition_stage,
)
from .search_handoff import SearchHandoffError, ingest_run, prepare_run, resolve_run_dir
from .trace import (
    TRACE_FILENAME,
    TRACE_SCHEMA_VERSION,
    TraceError,
    TraceValidationError,
    TraceValidationResult,
    read_trace_records,
    validate_trace_file,
    validate_trace_record,
)
from .verification_matrix import VerificationMatrixError, verify_claims
from .vision_adapter import VisionAdapterError, ingest_vision_observations

__all__ = [
    "ConfigResolutionError",
    "FetchClaimsError",
    "GuardrailsError",
    "ManualSourcesError",
    "ModalityDecision",
    "MvpSmokeError",
    "ReportGenerationError",
    "RunConfig",
    "RUN_STEPS_FILENAME",
    "RUN_STEPS_SCHEMA_VERSION",
    "RunStepStateError",
    "SearchHandoffError",
    "TRACE_FILENAME",
    "TRACE_SCHEMA_VERSION",
    "TraceError",
    "TraceValidationError",
    "TraceValidationResult",
    "ValidationError",
    "ValidationResult",
    "VerificationMatrixError",
    "VisionAdapterError",
    "begin_stage",
    "enforce_guardrails",
    "fetch_claims",
    "ingest_run",
    "ingest_manual_sources",
    "ingest_vision_observations",
    "inspect_run_state",
    "prepare_run",
    "route_angle",
    "route_angles",
    "resolve_config",
    "resolve_run_dir",
    "run_steps_path",
    "run_mvp_smoke",
    "synthesize_report",
    "transition_stage",
    "validate_artifacts",
    "validate_trace_file",
    "validate_trace_record",
    "read_trace_records",
    "verify_claims",
]
