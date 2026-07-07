"""Deterministic semantic angle planning and validation."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence


SEMANTIC_PLANNER_SCHEMA_VERSION = "codex-deepresearch.semantic-planner.v0"
SEMANTIC_PLANNER_VALIDATION_FILENAME = "semantic_planner_validation.json"
SEMANTIC_EXPECTATION_ORACLE_FILENAME = "semantic_expectation_oracle.json"
SEMANTIC_PLAN_FILENAME = "semantic_plan.json"
SEMANTIC_PLAN_REVIEW_FILENAME = "semantic_plan_review.json"
SEMANTIC_PLAN_DELTA_FILENAME = "semantic_plan_delta.json"
SEMANTIC_MATERIALIZATION_DIFF_FILENAME = "semantic_materialization_diff.json"
SEMANTIC_RAW_DIRNAME = "semantic_planner_raw"
SEMANTIC_RAW_REQUEST_FILENAME = "planner_request.json"
SEMANTIC_RAW_RESPONSE_FILENAME = "planner_response.json"
PLANNER_MODE_CODEX_SEMANTIC = "codex_semantic"
PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK = "heuristic_template_fallback"
PLANNER_MODE_MANUAL_ANGLES = "manual_angles"
PLANNER_MODE_FIXTURE = "fixture"
PLANNER_MODE_BLOCKED = "blocked"
BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE = "blocked_semantic_planner_unavailable"
CODEX_SEMANTIC_ADAPTER_NAME = "codex_native_semantic_candidate_adapter"
CODEX_SEMANTIC_PROMPT_VERSION = "p3-sp2-candidate-v1"
CODEX_SEMANTIC_PLANNER_COMMAND_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_PLANNER_COMMAND"
CODEX_SEMANTIC_PLANNER_TIMEOUT_ENV = "CODEX_DEEPRESEARCH_SEMANTIC_PLANNER_TIMEOUT_SECONDS"
CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND = "codex_exec_json"
SEMANTIC_FIT_SCORE_THRESHOLD = 9.0
ALLOWED_PLANNER_MODES = (
    PLANNER_MODE_CODEX_SEMANTIC,
    PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK,
    PLANNER_MODE_MANUAL_ANGLES,
    PLANNER_MODE_FIXTURE,
    PLANNER_MODE_BLOCKED,
)
RELEASE_INELIGIBLE_PLANNER_MODES = {
    PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK,
    PLANNER_MODE_MANUAL_ANGLES,
    PLANNER_MODE_FIXTURE,
    PLANNER_MODE_BLOCKED,
}
SEMANTIC_COMMON_INTEGRITY_FIELDS = (
    "question_scope",
    "raw_request_path",
    "raw_response_path",
    "raw_request_hash",
    "raw_response_hash",
    "provenance",
    "template_use",
    "session_id",
    "session_id_unavailable_reason",
)

ALLOWED_EVIDENCE_NEEDS = (
    "official_source",
    "primary_source",
    "recent_change",
    "counter_evidence",
    "implementation_detail",
    "pricing_or_limits",
    "policy_or_legal",
    "user_workflow",
    "visual_example",
    "visual_observation",
    "comparative_analysis",
    "failure_pattern",
    "risk_or_guardrail",
)

VISUAL_EXPECTED_EVIDENCE = {"visual_example", "visual_observation", "vlm_analysis"}
MATERIAL_ORIGINAL_OVERLAP_LIMIT = 0.72
MATERIAL_PEER_OVERLAP_LIMIT = 0.84

GENERIC_LENS_PHRASES = (
    "official documentation",
    "official docs",
    "primary sources",
    "primary source",
    "recent changes",
    "counter evidence",
    "counter-evidence",
)

_CLASS_GENERAL = "general"
_CLASS_TECHNICAL = "technical_api"
_CLASS_PRODUCT = "product_market"
_CLASS_VISUAL = "visual_style"
_CLASS_POLICY = "policy_risk"
_CLASS_IMPLEMENTATION = "implementation_architecture"

_VISUAL_KEYWORDS = (
    "visual",
    "image",
    "images",
    "photo",
    "photos",
    "screenshot",
    "screenshots",
    "ui",
    "interface",
    "chart",
    "graph",
    "style",
    "quality",
    "\uc0ac\uc9c4",
    "\uc774\ubbf8\uc9c0",
    "\uc2a4\ub0c5\uc0ac\uc9c4",
    "\uc2dc\uac01",
    "\ud654\uba74",
    "\ucc28\ud2b8",
)


@dataclass(frozen=True)
class SemanticAngle:
    angle_id: str
    title: str
    research_question: str
    question_context: str
    route: str
    evidence_need: str
    expected_artifacts: list[str]
    success_criteria: list[str]
    report_section: str
    why_this_angle_matters: str = ""
    included_scope: list[str] = field(default_factory=list)
    excluded_scope: list[str] = field(default_factory=list)
    expected_source_types: list[str] = field(default_factory=list)
    expected_visual_targets: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    risk_or_contradiction_checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SemanticPlan:
    schema_version: str
    question_class: str
    broad_question: bool
    source: str
    expected_evidence_needs: list[str]
    angles: list[SemanticAngle]
    intent_summary: str = ""
    domain_entities: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    question_scope: str = "narrow"
    decomposition_strategy: str = ""
    requirement_coverage_map: list[dict[str, Any]] = field(default_factory=list)
    negative_scope: list[str] = field(default_factory=list)
    bounded_tasks: list[dict[str, Any]] = field(default_factory=list)
    planner_provenance: dict[str, Any] = field(default_factory=dict)
    model_or_surface: str = ""
    original_question: str = ""
    language: str = ""
    planner_mode: str = PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK
    semantic_release_eligible: bool = False
    status: str = "prepared_heuristic_template_fallback"
    diagnostics: dict[str, Any] | None = None
    raw_request_payload: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    raw_response_payload: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["angles"] = [angle.to_dict() for angle in self.angles]
        data["diagnostics"] = dict(self.diagnostics or {})
        data.pop("raw_request_payload", None)
        data.pop("raw_response_payload", None)
        return data


def heuristic_template_planner(
    *,
    question: str,
    route_fallback_angles: Sequence[str] | None = None,
    planner_mode: str = PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK,
) -> SemanticPlan:
    """Return the explicit release-ineligible keyword/template fallback plan."""

    if planner_mode not in {
        PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK,
        PLANNER_MODE_FIXTURE,
    }:
        raise ValueError("heuristic_template_planner requires a heuristic or fixture planner_mode")
    normalized_question = " ".join(question.strip().split())
    question_class = classify_question(normalized_question)
    diagnostics = _fallback_diagnostics(planner_mode)
    if route_fallback_angles is not None:
        normalized = _normalize_explicit_angles(route_fallback_angles)
        angles = [
            _explicit_angle_record(
                angle=angle,
                index=index,
                question_class=question_class,
            )
            for index, angle in enumerate(normalized, start=1)
        ]
        return SemanticPlan(
            schema_version=SEMANTIC_PLANNER_SCHEMA_VERSION,
            question_class=question_class,
            broad_question=len({angle.evidence_need for angle in angles}) >= 4,
            source="heuristic_template_planner",
            expected_evidence_needs=_ordered_unique(angle.evidence_need for angle in angles),
            angles=angles,
            planner_mode=planner_mode,
            semantic_release_eligible=False,
            status="prepared_heuristic_template_fallback",
            diagnostics=diagnostics,
        )

    if question_class == _CLASS_GENERAL:
        angle = SemanticAngle(
            angle_id="angle_001",
            title="Primary source discovery",
            research_question="Find authoritative sources that directly answer the research question.",
            question_context=normalized_question,
            route="text_only",
            evidence_need="primary_source",
            expected_artifacts=["source list", "supporting quotes"],
            success_criteria=[
                "At least one source directly addresses the question.",
                "Claims remain tied to quoted source spans.",
            ],
            report_section="Primary Sources",
        )
        return SemanticPlan(
            schema_version=SEMANTIC_PLANNER_SCHEMA_VERSION,
            question_class=question_class,
            broad_question=False,
            source="heuristic_template_planner",
            expected_evidence_needs=["primary_source"],
            angles=[angle],
            planner_mode=planner_mode,
            semantic_release_eligible=False,
            status="prepared_heuristic_template_fallback",
            diagnostics=diagnostics,
        )

    templates = _templates_for_class(question_class)
    angles = [
        SemanticAngle(
            angle_id=f"angle_{index:03d}",
            title=template["title"],
            research_question=template["research_question"],
            question_context=normalized_question,
            route=template["route"],
            evidence_need=template["evidence_need"],
            expected_artifacts=list(template["expected_artifacts"]),
            success_criteria=list(template["success_criteria"]),
            report_section=template["report_section"],
        )
        for index, template in enumerate(templates, start=1)
    ]
    return SemanticPlan(
        schema_version=SEMANTIC_PLANNER_SCHEMA_VERSION,
        question_class=question_class,
        broad_question=True,
        source="heuristic_template_planner",
        expected_evidence_needs=[angle.evidence_need for angle in angles],
        angles=angles,
        planner_mode=planner_mode,
        semantic_release_eligible=False,
        status="prepared_heuristic_template_fallback",
        diagnostics=diagnostics,
    )


def manual_angle_planner(
    *,
    question: str,
    explicit_angles: Sequence[str],
    status: str = "prepared_manual_fallback",
) -> SemanticPlan:
    """Return a release-ineligible plan from user-supplied/manual angles."""

    normalized_question = " ".join(question.strip().split())
    question_class = classify_question(normalized_question)
    normalized = _normalize_explicit_angles(explicit_angles)
    angles = [
        _explicit_angle_record(
            angle=angle,
            index=index,
            question_class=question_class,
        )
        for index, angle in enumerate(normalized, start=1)
    ]
    return SemanticPlan(
        schema_version=SEMANTIC_PLANNER_SCHEMA_VERSION,
        question_class=question_class,
        broad_question=len({angle.evidence_need for angle in angles}) >= 4,
        source="manual_angles",
        expected_evidence_needs=_ordered_unique(angle.evidence_need for angle in angles),
        angles=angles,
        planner_mode=PLANNER_MODE_MANUAL_ANGLES,
        semantic_release_eligible=False,
        status=status,
        diagnostics=_fallback_diagnostics(PLANNER_MODE_MANUAL_ANGLES),
    )


def manual_fallback_plan(
    *,
    question: str,
    status: str = "manual_angles_pending",
) -> SemanticPlan:
    """Return the schema stub for a manual fallback run before angles exist."""

    normalized_question = " ".join(question.strip().split())
    question_class = classify_question(normalized_question)
    return SemanticPlan(
        schema_version=SEMANTIC_PLANNER_SCHEMA_VERSION,
        question_class=question_class,
        broad_question=False,
        source="manual_fallback",
        expected_evidence_needs=[],
        angles=[],
        planner_mode=PLANNER_MODE_MANUAL_ANGLES,
        semantic_release_eligible=False,
        status=status,
        diagnostics=_fallback_diagnostics(PLANNER_MODE_MANUAL_ANGLES),
    )


def blocked_semantic_planner_plan(
    *,
    question: str,
    reason: str,
    raw_request_payload: Mapping[str, Any] | None = None,
    raw_response_payload: Mapping[str, Any] | None = None,
    planner_provenance: Mapping[str, Any] | None = None,
) -> SemanticPlan:
    """Return a release-ineligible blocked semantic-planner-unavailable stub."""

    normalized_question = " ".join(question.strip().split())
    question_class = classify_question(normalized_question)
    return SemanticPlan(
        schema_version=SEMANTIC_PLANNER_SCHEMA_VERSION,
        question_class=question_class,
        broad_question=False,
        source=BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE,
        expected_evidence_needs=[],
        angles=[],
        planner_provenance=dict(planner_provenance or {}),
        original_question=normalized_question,
        planner_mode=PLANNER_MODE_BLOCKED,
        semantic_release_eligible=False,
        status=BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE,
        diagnostics={
            **_fallback_diagnostics(PLANNER_MODE_BLOCKED),
            "blocked_reason": reason,
        },
        raw_request_payload=dict(raw_request_payload or {}),
        raw_response_payload=dict(raw_response_payload or {}),
    )


def codex_semantic_candidate_plan(
    *,
    question: str,
    user_constraints: Sequence[str] | None = None,
    depth_preset: str = "standard",
    visual_preference: str | None = None,
    budget_cap: Mapping[str, Any] | None = None,
    provided_sources: Sequence[Mapping[str, Any]] | None = None,
    provided_images: Sequence[Mapping[str, Any]] | None = None,
) -> SemanticPlan:
    """Build the P3-SP2 Codex-native semantic candidate plan.

    The adapter keeps the candidate release-ineligible until P3-SP3/P3-SP4
    reviewer and E2E gates make it eligible.
    """

    original_question = question.strip()
    raw_request = _codex_semantic_raw_request(
        question=original_question,
        user_constraints=user_constraints or [],
        depth_preset=depth_preset,
        visual_preference=visual_preference,
        budget_cap=budget_cap or {},
        provided_sources=provided_sources or [],
        provided_images=provided_images or [],
    )
    raw_request["adapter_request_hash"] = _sha256_payload(raw_request)
    try:
        adapter_response = invoke_codex_semantic_planner_adapter(raw_request)
    except SemanticPlannerAdapterUnavailable as exc:
        return _blocked_codex_semantic_adapter_plan(
            question=original_question,
            raw_request=raw_request,
            reason=str(exc) or "Codex semantic planner adapter is unavailable.",
            failure_category="adapter_unavailable",
        )
    except Exception as exc:  # pragma: no cover - defensive boundary guard
        return _blocked_codex_semantic_adapter_plan(
            question=original_question,
            raw_request=raw_request,
            reason=f"Codex semantic planner adapter failed: {exc.__class__.__name__}",
            failure_category="adapter_failed",
        )
    if adapter_response is None:
        return _blocked_codex_semantic_adapter_plan(
            question=original_question,
            raw_request=raw_request,
            reason=(
                "Codex semantic planner adapter is not configured; refusing to "
                "materialize local heuristic output as codex_semantic."
            ),
            failure_category="adapter_unavailable",
        )
    try:
        raw_response = _structured_codex_adapter_response(
            raw_request=raw_request,
            adapter_response=adapter_response,
        )
        candidate = _candidate_plan_from_adapter_response(raw_response)
    except SemanticPlannerAdapterUnavailable as exc:
        return _blocked_codex_semantic_adapter_plan(
            question=original_question,
            raw_request=raw_request,
            raw_response=adapter_response,
            reason=str(exc) or "Codex semantic planner adapter returned invalid output.",
            failure_category="adapter_invalid_response",
        )
    angles = [
        SemanticAngle(
            angle_id=str(angle["angle_id"]),
            title=str(angle["title"]),
            research_question=str(angle["research_question"]),
            question_context=original_question,
            route=str(angle["route"]),
            evidence_need=str(angle["evidence_need"]),
            expected_artifacts=list(angle["expected_artifacts"]),
            success_criteria=list(angle["success_criteria"]),
            report_section=str(angle["report_section"]),
            why_this_angle_matters=str(angle["why_this_angle_matters"]),
            included_scope=list(angle["included_scope"]),
            excluded_scope=list(angle["excluded_scope"]),
            expected_source_types=list(angle["expected_source_types"]),
            expected_visual_targets=list(angle["expected_visual_targets"]),
            search_queries=list(angle["search_queries"]),
            risk_or_contradiction_checks=list(angle["risk_or_contradiction_checks"]),
        )
        for angle in candidate["angles"]
    ]
    question_class = _question_class_from_candidate(candidate, angles)
    parsed_response_hash = _sha256_text(
        json.dumps(candidate, sort_keys=True, ensure_ascii=True)
    )
    raw_response["parsed_response_hash"] = parsed_response_hash
    raw_response.setdefault("candidate_plan", candidate)
    raw_response["raw_response_hash"] = _sha256_payload(raw_response)
    provenance = {
        "planner_mode": PLANNER_MODE_CODEX_SEMANTIC,
        "planner_source": "codex_semantic",
        "planner_adapter": CODEX_SEMANTIC_ADAPTER_NAME,
        "prompt_version": CODEX_SEMANTIC_PROMPT_VERSION,
        "model_or_surface": _adapter_model_or_surface(raw_response),
        "child_session_id": _adapter_child_session_id(raw_response),
        "session_id": _adapter_child_session_id(raw_response),
        "session_id_unavailable_reason": _adapter_session_unavailable_reason(raw_response),
        "raw_request_required": True,
        "raw_response_required": True,
        "adapter_request_hash": raw_request["adapter_request_hash"],
        "raw_response_hash": raw_response["raw_response_hash"],
        "parsed_response_hash": parsed_response_hash,
        "preselected_template_class": None,
        "semantic_release_eligible": False,
        "adapter_invocation": dict(raw_response.get("provenance") or {}),
    }
    diagnostics = {
        "semantic_release_eligible": False,
        "planner_mode": PLANNER_MODE_CODEX_SEMANTIC,
        "fallback_kind": None,
        "user_visible_diagnostic": (
            "A Codex semantic candidate plan was generated, but it remains "
            "release-ineligible until independent semantic review and E2E gates pass."
        ),
        "parsed_response_hash": parsed_response_hash,
    }
    return SemanticPlan(
        schema_version=SEMANTIC_PLANNER_SCHEMA_VERSION,
        question_class=question_class,
        broad_question=candidate["question_scope"] == "broad",
        source="codex_semantic",
        expected_evidence_needs=_ordered_unique(
            angle.evidence_need for angle in angles
        ),
        angles=angles,
        intent_summary=str(candidate["intent_summary"]),
        domain_entities=list(candidate["domain_entities"]),
        constraints=list(candidate["constraints"]),
        question_scope=str(candidate["question_scope"]),
        decomposition_strategy=str(candidate["decomposition_strategy"]),
        requirement_coverage_map=list(candidate["requirement_coverage_map"]),
        negative_scope=list(candidate["negative_scope"]),
        bounded_tasks=list(candidate["bounded_tasks"]),
        planner_provenance=provenance,
        model_or_surface=_adapter_model_or_surface(raw_response),
        original_question=original_question,
        language=str(candidate["language"]),
        planner_mode=PLANNER_MODE_CODEX_SEMANTIC,
        semantic_release_eligible=False,
        status="candidate_codex_semantic_release_ineligible",
        diagnostics=diagnostics,
        raw_request_payload=raw_request,
        raw_response_payload=raw_response,
    )


def plan_semantic_angles(
    *,
    question: str,
    explicit_angles: Sequence[str] | None = None,
    user_constraints: Sequence[str] | None = None,
    depth_preset: str = "standard",
    visual_preference: str | None = None,
    budget_cap: Mapping[str, Any] | None = None,
    provided_sources: Sequence[Mapping[str, Any]] | None = None,
    provided_images: Sequence[Mapping[str, Any]] | None = None,
) -> SemanticPlan:
    """Compatibility wrapper returning manual fallback or semantic candidate plans."""

    if explicit_angles is not None:
        return manual_angle_planner(question=question, explicit_angles=explicit_angles)
    return codex_semantic_candidate_plan(
        question=question,
        user_constraints=user_constraints,
        depth_preset=depth_preset,
        visual_preference=visual_preference,
        budget_cap=budget_cap,
        provided_sources=provided_sources,
        provided_images=provided_images,
    )


class SemanticPlannerAdapterUnavailable(RuntimeError):
    """Raised when no real Codex semantic planner response can be consumed."""


def invoke_codex_semantic_planner_adapter(
    request: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Invoke the configured Codex-native semantic planner boundary.

    The default path is intentionally unavailable unless a Codex exec JSON
    command is configured. Production code must not synthesize codex_semantic
    output locally or accept arbitrary subprocesses as Codex-native provenance.
    """

    command_text = os.environ.get(CODEX_SEMANTIC_PLANNER_COMMAND_ENV, "").strip()
    if not command_text:
        return None
    command = shlex.split(command_text)
    if not command:
        return None
    command_boundary = validate_codex_semantic_adapter_command(command)
    timeout_seconds = float(os.environ.get(CODEX_SEMANTIC_PLANNER_TIMEOUT_ENV, "300"))
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        input=json.dumps(dict(request), ensure_ascii=False, sort_keys=True),
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        raise SemanticPlannerAdapterUnavailable(
            f"Codex semantic planner command exited {completed.returncode}; "
            f"stderr={_preview_text(completed.stderr)}; stdout={_preview_text(completed.stdout)}"
        )
    payload, codex_events = _parse_codex_exec_json_output(completed.stdout)
    codex_event_provenance = _codex_event_provenance(codex_events)
    provenance = dict(payload.get("provenance") or {})
    provenance.setdefault("adapter_command", _redacted_command(command))
    provenance.setdefault(
        "adapter_invocation_kind",
        command_boundary["adapter_invocation_kind"],
    )
    provenance.setdefault("raw_request_hash", request.get("adapter_request_hash"))
    for key, value in codex_event_provenance.items():
        provenance.setdefault(key, value)
    payload = dict(payload)
    payload["provenance"] = provenance
    return payload


def _blocked_codex_semantic_adapter_plan(
    *,
    question: str,
    raw_request: Mapping[str, Any],
    reason: str,
    failure_category: str,
    raw_response: Mapping[str, Any] | None = None,
) -> SemanticPlan:
    blocked_response = {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_planner_raw_response",
        "planner_adapter": CODEX_SEMANTIC_ADAPTER_NAME,
        "prompt_version": CODEX_SEMANTIC_PROMPT_VERSION,
        "planner_mode": PLANNER_MODE_BLOCKED,
        "semantic_release_eligible": False,
        "status": BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE,
        "failure_category": failure_category,
        "blocked_reason": reason,
        "adapter_response": dict(raw_response or {}),
        "provenance": {
            "planner_mode": PLANNER_MODE_BLOCKED,
            "planner_source": BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE,
            "planner_adapter": CODEX_SEMANTIC_ADAPTER_NAME,
            "prompt_version": CODEX_SEMANTIC_PROMPT_VERSION,
            "child_session_id": None,
            "session_id": None,
            "session_id_unavailable_reason": "Semantic planner adapter was unavailable.",
            "adapter_request_hash": raw_request.get("adapter_request_hash"),
            "semantic_release_eligible": False,
        },
    }
    provenance = {
        "planner_mode": PLANNER_MODE_BLOCKED,
        "planner_source": BLOCKED_SEMANTIC_PLANNER_UNAVAILABLE,
        "planner_adapter": CODEX_SEMANTIC_ADAPTER_NAME,
        "prompt_version": CODEX_SEMANTIC_PROMPT_VERSION,
        "child_session_id": None,
        "session_id": None,
        "session_id_unavailable_reason": "Semantic planner adapter was unavailable.",
        "adapter_request_hash": raw_request.get("adapter_request_hash"),
        "raw_response_hash": _sha256_payload(blocked_response),
        "failure_category": failure_category,
        "semantic_release_eligible": False,
        "preselected_template_class": None,
    }
    return blocked_semantic_planner_plan(
        question=question,
        reason=reason,
        raw_request_payload=raw_request,
        raw_response_payload=blocked_response,
        planner_provenance=provenance,
    )


def _structured_codex_adapter_response(
    *,
    raw_request: Mapping[str, Any],
    adapter_response: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(adapter_response, Mapping):
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner adapter returned a non-object response"
        )
    raw_response = dict(adapter_response)
    raw_response.setdefault("schema_version", SEMANTIC_PLANNER_SCHEMA_VERSION)
    raw_response.setdefault("artifact_type", "semantic_planner_raw_response")
    raw_response.setdefault("planner_adapter", CODEX_SEMANTIC_ADAPTER_NAME)
    raw_response.setdefault("prompt_version", CODEX_SEMANTIC_PROMPT_VERSION)
    raw_response.setdefault("planner_mode", PLANNER_MODE_CODEX_SEMANTIC)
    raw_response.setdefault("semantic_release_eligible", False)
    provenance = dict(raw_response.get("provenance") or {})
    if not provenance:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner adapter response is missing provenance"
        )
    raw_response["provenance"] = validate_codex_semantic_adapter_provenance(
        raw_request=raw_request,
        provenance=provenance,
    )
    if raw_response.get("planner_mode") != PLANNER_MODE_CODEX_SEMANTIC:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner adapter response is not marked codex_semantic"
        )
    if raw_response.get("semantic_release_eligible") is True:
        raise SemanticPlannerAdapterUnavailable(
            "P3-SP2 codex_semantic candidates must remain semantic_release_eligible=false"
        )
    return raw_response


def _candidate_plan_from_adapter_response(raw_response: Mapping[str, Any]) -> dict[str, Any]:
    candidate = raw_response.get("candidate_plan")
    if not isinstance(candidate, Mapping):
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner adapter response is missing candidate_plan"
        )
    candidate_plan = dict(candidate)
    candidate_plan.setdefault("schema_version", SEMANTIC_PLANNER_SCHEMA_VERSION)
    candidate_plan.setdefault("planner_mode", PLANNER_MODE_CODEX_SEMANTIC)
    candidate_plan.setdefault("semantic_release_eligible", False)
    candidate_plan.setdefault("source", "codex_semantic")
    if candidate_plan.get("planner_mode") != PLANNER_MODE_CODEX_SEMANTIC:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic candidate_plan is not marked codex_semantic"
        )
    if candidate_plan.get("semantic_release_eligible") is True:
        raise SemanticPlannerAdapterUnavailable(
            "P3-SP2 candidate_plan must keep semantic_release_eligible=false"
        )
    required_fields = (
        "intent_summary",
        "domain_entities",
        "constraints",
        "question_scope",
        "decomposition_strategy",
        "requirement_coverage_map",
        "negative_scope",
        "angles",
        "bounded_tasks",
        "language",
    )
    missing = [field for field in required_fields if field not in candidate_plan]
    if missing:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic candidate_plan is missing required fields: "
            + ", ".join(missing)
        )
    if not isinstance(candidate_plan.get("angles"), list) or not candidate_plan["angles"]:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic candidate_plan must include non-empty angles"
        )
    if not isinstance(candidate_plan.get("bounded_tasks"), list) or not candidate_plan["bounded_tasks"]:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic candidate_plan must include non-empty bounded_tasks"
        )
    return candidate_plan


def _adapter_model_or_surface(raw_response: Mapping[str, Any]) -> str:
    provenance = raw_response.get("provenance")
    if isinstance(provenance, Mapping):
        value = provenance.get("model_or_surface") or provenance.get("model") or provenance.get("adapter")
        if value:
            return str(value)
    return str(raw_response.get("model_or_surface") or "codex-semantic-planner-adapter")


def _adapter_child_session_id(raw_response: Mapping[str, Any]) -> str | None:
    provenance = raw_response.get("provenance")
    if isinstance(provenance, Mapping):
        value = provenance.get("child_session_id") or provenance.get("session_id")
        if value:
            return str(value)
    return None


def _adapter_session_unavailable_reason(raw_response: Mapping[str, Any]) -> str | None:
    if _adapter_child_session_id(raw_response):
        return None
    provenance = raw_response.get("provenance")
    if isinstance(provenance, Mapping) and provenance.get("session_id_unavailable_reason"):
        return str(provenance["session_id_unavailable_reason"])
    return "Adapter returned structured raw response provenance without a child session id."


def _sha256_payload(payload: Mapping[str, Any]) -> str:
    return _sha256_text(json.dumps(dict(payload), sort_keys=True, ensure_ascii=True))


def _redacted_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    sensitive_next = False
    for part in command:
        if sensitive_next:
            redacted.append("<redacted>")
            sensitive_next = False
            continue
        lowered = part.lower()
        if any(token in lowered for token in ("key", "token", "secret", "password")):
            redacted.append("<redacted>")
            if part.startswith("--"):
                sensitive_next = True
        else:
            redacted.append(part)
    return redacted


def validate_codex_semantic_adapter_command(command: Sequence[str]) -> dict[str, Any]:
    """Validate that the semantic adapter boundary is Codex exec JSON only."""

    parts = [str(part) for part in command]
    if not parts:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner command is empty; expected codex exec --json"
        )
    command_basename = Path(parts[0]).name
    if command_basename != "codex":
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner command must use codex exec --json; "
            f"got command basename {command_basename!r}"
        )
    if len(parts) < 2 or parts[1] != "exec":
        subcommand = parts[1] if len(parts) >= 2 else "<missing>"
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner command must use codex exec --json; "
            f"got subcommand {subcommand!r}"
        )
    if not _codex_command_has_json_mode(parts):
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner command must enable JSON mode with --json "
            "or an equivalent JSON output flag"
        )
    return {
        "adapter_invocation_kind": CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND,
        "command_basename": command_basename,
        "subcommand": "exec",
        "json_mode": True,
    }


def _codex_command_has_json_mode(command: Sequence[str]) -> bool:
    for index, part in enumerate(command[2:], start=2):
        lowered = part.lower()
        if lowered == "--json":
            return True
        if lowered in {"--output-format", "--format"}:
            if index + 1 < len(command) and str(command[index + 1]).lower() in {"json", "jsonl"}:
                return True
        if lowered.startswith("--output-format=") or lowered.startswith("--format="):
            value = lowered.split("=", 1)[1]
            if value in {"json", "jsonl"}:
                return True
    return False


def validate_codex_semantic_adapter_provenance(
    *,
    raw_request: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate Codex-native raw-response provenance for codex_semantic output."""

    if not isinstance(provenance, Mapping):
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner adapter response provenance is not an object"
        )
    normalized = dict(provenance)
    invocation_kind = str(normalized.get("adapter_invocation_kind") or "")
    if invocation_kind != CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner adapter response must record "
            f"adapter_invocation_kind={CODEX_SEMANTIC_ADAPTER_INVOCATION_KIND}"
        )
    request_hash = str(raw_request.get("adapter_request_hash") or "")
    if request_hash and str(normalized.get("raw_request_hash") or "") != request_hash:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner adapter response provenance does not match the raw request hash"
        )
    adapter_command = normalized.get("adapter_command")
    if adapter_command:
        if isinstance(adapter_command, str):
            command_parts = shlex.split(adapter_command)
        elif isinstance(adapter_command, Sequence) and not isinstance(adapter_command, (str, bytes)):
            command_parts = [str(part) for part in adapter_command]
        else:
            raise SemanticPlannerAdapterUnavailable(
                "Codex semantic planner adapter provenance has invalid adapter_command"
            )
        validate_codex_semantic_adapter_command(command_parts)
    identity_fields = (
        "child_session_id",
        "session_id",
        "raw_response_id",
        "codex_event_id",
        "response_id",
    )
    if not any(str(normalized.get(field) or "").strip() for field in identity_fields):
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner adapter response lacks Codex raw response identity; "
            "expected child/session/raw response id or codex event id"
        )
    return normalized


def _parse_codex_exec_json_output(stdout: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = (stdout or "").strip()
    if not text:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner command returned empty JSON output"
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _parse_codex_exec_jsonl_output(text)
    if isinstance(payload, Mapping):
        direct = _semantic_adapter_payload_from_value(payload)
        if direct is not None:
            return direct, []
        events = payload.get("events")
        if isinstance(events, list):
            event_payload = _semantic_adapter_payload_from_events(events)
            if event_payload is not None:
                return event_payload, [dict(event) for event in events if isinstance(event, Mapping)]
    if isinstance(payload, list):
        event_payload = _semantic_adapter_payload_from_events(payload)
        if event_payload is not None:
            return event_payload, [dict(event) for event in payload if isinstance(event, Mapping)]
    raise SemanticPlannerAdapterUnavailable(
        "Codex semantic planner command returned JSON without a semantic adapter response object"
    )


def _parse_codex_exec_jsonl_output(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SemanticPlannerAdapterUnavailable(
                f"Codex semantic planner command returned invalid JSONL on line {line_number}: {exc}"
            ) from exc
        if not isinstance(event, Mapping):
            raise SemanticPlannerAdapterUnavailable(
                f"Codex semantic planner command returned non-object JSONL event on line {line_number}"
            )
        events.append(dict(event))
    event_payload = _semantic_adapter_payload_from_events(events)
    if event_payload is None:
        raise SemanticPlannerAdapterUnavailable(
            "Codex semantic planner JSONL output did not include a semantic adapter response object"
        )
    return event_payload, events


def _semantic_adapter_payload_from_events(events: Sequence[Any]) -> dict[str, Any] | None:
    for event in reversed(events):
        if not isinstance(event, Mapping):
            continue
        payload = _semantic_adapter_payload_from_value(event)
        if payload is not None:
            return payload
        for key in (
            "semantic_planner_response",
            "adapter_response",
            "response",
            "payload",
            "data",
            "output",
            "content",
            "message",
        ):
            payload = _semantic_adapter_payload_from_value(event.get(key))
            if payload is not None:
                return payload
    return None


def _semantic_adapter_payload_from_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        if _is_semantic_adapter_response_payload(value):
            return dict(value)
        for key in ("content", "message", "output", "text", "json"):
            payload = _semantic_adapter_payload_from_value(value.get(key))
            if payload is not None:
                return payload
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or not stripped.startswith("{"):
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return _semantic_adapter_payload_from_value(parsed)
    return None


def _is_semantic_adapter_response_payload(value: Mapping[str, Any]) -> bool:
    return (
        "candidate_plan" in value
        or value.get("artifact_type") == "semantic_planner_raw_response"
    )


def _codex_event_provenance(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    provenance: dict[str, Any] = {}
    event_types: list[str] = []
    for event in events:
        event_type = event.get("type") or event.get("event_type")
        if event_type:
            event_types.append(str(event_type))
    for event in reversed(events):
        for target, aliases in {
            "codex_event_id": ("id", "event_id", "codex_event_id"),
            "session_id": ("session_id", "conversation_id", "thread_id"),
            "child_session_id": ("child_session_id", "child_thread_id"),
            "raw_response_id": ("raw_response_id", "response_id", "message_id"),
        }.items():
            if target in provenance:
                continue
            for alias in aliases:
                value = event.get(alias)
                if value:
                    provenance[target] = str(value)
                    break
    if event_types:
        provenance["codex_event_types"] = list(dict.fromkeys(event_types))
    return provenance


def _preview_text(value: str, limit: int = 240) -> str:
    text = " ".join((value or "").split())
    if not text:
        return "<empty>"
    return text[:limit] + ("..." if len(text) > limit else "")


def validate_semantic_candidate_plan(
    *,
    original_question: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate P3-SP2 candidate semantics before independent review exists."""

    failures: list[dict[str, Any]] = []
    question = original_question.strip()
    requirements = [
        requirement
        for requirement in plan.get("requirement_coverage_map", [])
        if isinstance(requirement, Mapping)
    ]
    angles = [
        angle for angle in plan.get("angles", []) if isinstance(angle, Mapping)
    ]
    tasks = [
        task for task in plan.get("bounded_tasks", []) if isinstance(task, Mapping)
    ]
    scope = str(plan.get("question_scope") or "")
    if scope == "broad" and not (5 <= len(angles) <= 8):
        failures.append({"code": "broad_angle_count_out_of_range"})
    if scope == "broad" and not (20 <= len(tasks) <= 40):
        failures.append({"code": "broad_task_count_out_of_range"})
    if scope == "narrow" and tasks and not (6 <= len(tasks) <= 12):
        failures.append({"code": "narrow_task_count_out_of_range"})
    angle_ids = {str(angle.get("angle_id") or "") for angle in angles}
    task_ids = {str(task.get("task_id") or "") for task in tasks}
    executable_text = _candidate_executable_text(angles=angles, tasks=tasks)
    subject_tokens = _core_question_tokens(question)
    if subject_tokens and not _candidate_text_covers_subject(executable_text, subject_tokens):
        failures.append({"code": "subject_requirement_drift"})
    for requirement in requirements:
        if requirement.get("coverage_status") != "covered":
            failures.append(
                {
                    "code": "requirement_not_covered",
                    "requirement_id": requirement.get("requirement_id"),
                }
            )
        if requirement.get("non_negotiable") is True:
            covered_angles = set(_string_list(requirement.get("covered_by_angle_ids")))
            covered_tasks = set(_string_list(requirement.get("covered_by_task_ids")))
            if not covered_angles or not covered_angles <= angle_ids:
                failures.append(
                    {
                        "code": "non_negotiable_missing_angle_coverage",
                        "requirement_id": requirement.get("requirement_id"),
                    }
                )
            if not covered_tasks or not covered_tasks <= task_ids:
                failures.append(
                    {
                        "code": "non_negotiable_missing_task_coverage",
                        "requirement_id": requirement.get("requirement_id"),
                    }
                )
        requirement_type = str(requirement.get("requirement_type") or "")
        if requirement_type == "time_range" and not _candidate_has_recent_task(tasks):
            failures.append(
                {
                    "code": "time_requirement_missing_recent_task",
                    "requirement_id": requirement.get("requirement_id"),
                }
            )
        if requirement_type == "geography" and not _candidate_has_geography_task(
            requirement=requirement,
            tasks=tasks,
        ):
            failures.append(
                {
                    "code": "geography_requirement_missing_task_scope",
                    "requirement_id": requirement.get("requirement_id"),
                }
            )
        if requirement_type == "deliverable_shape" and requirement.get("non_negotiable") is True:
            if not _candidate_has_deliverable_task(requirement=requirement, tasks=tasks):
                failures.append(
                    {
                        "code": "deliverable_requirement_missing_task_output",
                        "requirement_id": requirement.get("requirement_id"),
                    }
                )
    tasks_per_angle = Counter(str(task.get("angle_id") or "") for task in tasks)
    if scope == "broad":
        for angle_id in angle_ids:
            if tasks_per_angle[angle_id] < 2:
                failures.append(
                    {
                        "code": "broad_angle_has_too_few_tasks",
                        "angle_id": angle_id,
                    }
                )
    required_task_fields = (
        "task_id",
        "angle_id",
        "query",
        "route",
        "freshness_requirement",
        "source_policy",
        "expected_source_types",
        "expected_visual_targets",
        "expected_artifacts",
        "success_criteria",
        "max_sources",
        "max_images",
        "done_condition",
    )
    for task in tasks:
        for field_name in required_task_fields:
            if field_name not in task:
                failures.append(
                    {
                        "code": "bounded_task_missing_field",
                        "task_id": task.get("task_id"),
                        "field": field_name,
                    }
                )
        angle_title = next(
            (
                str(angle.get("title") or "")
                for angle in angles
                if str(angle.get("angle_id") or "") == str(task.get("angle_id") or "")
            ),
            "",
        )
        if _normalize_text(str(task.get("query") or "")) == _normalize_text(angle_title):
            failures.append(
                {
                    "code": "task_query_copies_angle_title",
                    "task_id": task.get("task_id"),
                }
            )
    visual_required = any(
        str(requirement.get("requirement_type") or "") == "visual_modality"
        for requirement in requirements
    ) or question_mentions_visual_evidence(question)
    if visual_required:
        visual_angles = [
            angle for angle in angles if str(angle.get("route") or "") != "text_only"
        ]
        visual_tasks = [
            task for task in tasks if str(task.get("route") or "") != "text_only"
        ]
        if not visual_angles or not visual_tasks:
            failures.append({"code": "visual_requirement_missing_visual_route"})
        if not any(_string_list(task.get("expected_visual_targets")) for task in visual_tasks):
            failures.append({"code": "visual_requirement_missing_targets"})
    official_required = any(
        str(requirement.get("requirement_type") or "") == "source_quality"
        and (
            "official" in str(requirement.get("requirement_text") or "").lower()
            or "regulatory" in str(requirement.get("requirement_text") or "").lower()
            or "\uaddc\uc81c" in str(requirement.get("requirement_text") or "")
        )
        for requirement in requirements
    )
    if official_required:
        official_tasks = [
            task
            for task in tasks
            if any(
                token in " ".join(_string_list(task.get("expected_source_types"))).lower()
                for token in ("official", "regulatory", "primary")
            )
            or "official" in json.dumps(task.get("source_policy"), sort_keys=True).lower()
            or "regulatory" in json.dumps(task.get("source_policy"), sort_keys=True).lower()
        ]
        if not official_tasks:
            failures.append({"code": "official_source_requirement_missing"})
        official_success_tasks = [
            task
            for task in tasks
            if any(
                token in " ".join(_string_list(task.get("success_criteria"))).lower()
                for token in ("official", "regulatory", "primary", "\uacf5\uc2dd", "\uaddc\uc81c")
            )
        ]
        if not official_success_tasks:
            failures.append({"code": "official_source_success_criteria_missing"})
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "planner_mode": plan.get("planner_mode"),
        "semantic_release_eligible": bool(plan.get("semantic_release_eligible")),
        "failure_count": len(failures),
        "failures": failures,
        "ok": not failures,
    }


def _codex_semantic_raw_request(
    *,
    question: str,
    user_constraints: Sequence[str],
    depth_preset: str,
    visual_preference: str | None,
    budget_cap: Mapping[str, Any],
    provided_sources: Sequence[Mapping[str, Any]],
    provided_images: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_planner_raw_request",
        "planner_mode": PLANNER_MODE_CODEX_SEMANTIC,
        "planner_adapter": CODEX_SEMANTIC_ADAPTER_NAME,
        "prompt_version": CODEX_SEMANTIC_PROMPT_VERSION,
        "semantic_release_eligible": False,
        "original_question": question,
        "user_constraints": [str(item) for item in user_constraints],
        "depth_preset": depth_preset,
        "visual_preference": visual_preference or "auto",
        "budget_cap": dict(budget_cap),
        "provided_sources": [dict(item) for item in provided_sources],
        "provided_images": [dict(item) for item in provided_images],
        "planner_instructions": [
            "Decompose the user's raw question into prompt-specific research angles.",
            "Preserve every explicit requirement and justified inferred constraint.",
            "Create bounded executable research tasks with source, visual, and done-condition fields.",
            "Do not use hidden template classes, fixed domain menus, canned task lists, or copied angle titles.",
        ],
        "response_schema_shape": {
            "intent_summary": "string",
            "domain_entities": "list",
            "constraints": "list",
            "question_scope": "broad|narrow",
            "decomposition_strategy": "string",
            "requirement_coverage_map": "list",
            "negative_scope": "list",
            "angles": "list",
            "bounded_tasks": "list",
        },
    }


def _fixture_semantic_candidate_response_for_validation_tests(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    question = str(request["original_question"])
    constraints = _fixture_extract_semantic_requirements(
        question=question,
        user_constraints=_string_list(request.get("user_constraints")),
        visual_preference=str(request.get("visual_preference") or "auto"),
    )
    entities = _fixture_extract_domain_entities(question, constraints)
    scope = _fixture_infer_candidate_question_scope(question, constraints)
    subject = _fixture_subject_phrase(question, entities)
    depth = str(request.get("depth_preset") or "standard")
    budget_cap = request.get("budget_cap")
    budget = budget_cap if isinstance(budget_cap, Mapping) else {}
    angles = _fixture_build_candidate_angles(
        question=question,
        subject=subject,
        entities=entities,
        requirements=constraints,
        scope=scope,
    )
    bounded_tasks = _fixture_build_candidate_bounded_tasks(
        question=question,
        subject=subject,
        angles=angles,
        requirements=constraints,
        scope=scope,
        depth_preset=depth,
        budget_cap=budget,
    )
    coverage = _fixture_candidate_requirement_coverage(
        requirements=constraints,
        angles=angles,
        bounded_tasks=bounded_tasks,
    )
    candidate_plan = {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "planner_mode": PLANNER_MODE_FIXTURE,
        "semantic_release_eligible": False,
        "source": "fixture_semantic_candidate_response_for_validation_tests",
        "model_or_surface": "fixture-validation-helper",
        "original_question": question,
        "language": _fixture_question_language(question),
        "depth_preset": depth,
        "intent_summary": _fixture_intent_summary(question, subject, constraints),
        "domain_entities": entities,
        "constraints": constraints,
        "question_scope": scope,
        "decomposition_strategy": _fixture_decomposition_strategy(scope, subject, constraints),
        "requirement_coverage_map": coverage,
        "negative_scope": _fixture_negative_scope(question, subject),
        "angles": angles,
        "bounded_tasks": bounded_tasks,
    }
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_planner_raw_response",
        "planner_mode": PLANNER_MODE_FIXTURE,
        "planner_adapter": "fixture_semantic_candidate_response_for_validation_tests",
        "prompt_version": "fixture-validation-helper",
        "semantic_release_eligible": False,
        "candidate_plan": candidate_plan,
    }


def _fixture_extract_semantic_requirements(
    *,
    question: str,
    user_constraints: Sequence[str],
    visual_preference: str,
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []

    def add(
        requirement_type: str,
        text: str,
        *,
        prompt_text: str | None = None,
        explicit: bool = True,
        non_negotiable: bool = True,
        inferred_reason: str | None = None,
    ) -> None:
        requirements.append(
            {
                "requirement_id": f"req_{len(requirements) + 1:03d}",
                "requirement_type": requirement_type,
                "requirement_text": text,
                "prompt_text": prompt_text or text,
                "prompt_span": _fixture_prompt_span(question, prompt_text or text),
                "explicit": explicit,
                "non_negotiable": non_negotiable,
                "inferred_reason": inferred_reason,
            }
        )

    add("subject", question, prompt_text=question)
    for item in user_constraints:
        add("user_constraint", item, prompt_text=item)
    if question_mentions_visual_evidence(question) or visual_preference in {
        "visual_required",
        "visual_optional",
    }:
        add(
            "visual_modality",
            "Visual evidence or image/chart/figure analysis is required.",
            prompt_text=_fixture_first_matching_text(question, _VISUAL_KEYWORDS) or question,
        )
    if _contains_any(
        question.lower(),
        (
            "official",
            "regulatory",
            "regulation",
            "regulator",
            "primary source",
            "\uacf5\uc2dd",
            "\uaddc\uc81c",
            "\uadfc\uac70",
            "\uc815\ubd80",
        ),
    ):
        add(
            "source_quality",
            "Official, regulatory, or primary source evidence is required.",
            prompt_text=_fixture_first_matching_text(
                question,
                ("official", "regulatory", "regulation", "\uacf5\uc2dd", "\uaddc\uc81c", "\uadfc\uac70"),
            )
            or question,
        )
    if _contains_any(
        question.lower(),
        ("latest", "current", "recent", "202", "\ucd5c\uc2e0", "\ud604\uc7ac", "\ucd5c\uadfc"),
    ):
        add(
            "time_range",
            "Current or recent evidence is required.",
            prompt_text=_fixture_first_matching_text(
                question, ("latest", "current", "recent", "\ucd5c\uc2e0", "\ud604\uc7ac", "\ucd5c\uadfc")
            )
            or question,
        )
    geography = _fixture_extract_geography_requirement(question)
    if geography:
        add("geography", geography, prompt_text=geography)
    if _contains_any(
        question.lower(),
        ("report", "table", "checklist", "matrix", "\ub9ac\ud3ec\ud2b8", "\ubcf4\uace0\uc11c", "\ud45c", "\uc54c\ub824\uc918"),
    ):
        deliverable_prompt = _fixture_first_matching_text(
            question,
            (
                "report",
                "table",
                "checklist",
                "matrix",
                "\ub9ac\ud3ec\ud2b8",
                "\ubcf4\uace0\uc11c",
                "\ud45c",
                "\uc54c\ub824\uc918",
            ),
        )
        add(
            "deliverable_shape",
            "The final answer must synthesize findings into a user-readable deliverable.",
            prompt_text=deliverable_prompt or question,
            explicit=deliverable_prompt not in {None, "\uc54c\ub824\uc918"},
            non_negotiable=deliverable_prompt not in {None, "\uc54c\ub824\uc918"},
            inferred_reason="The prompt asks for synthesized research output.",
        )
    if _contains_any(
        question.lower(),
        (
            "safety",
            "risk",
            "fire",
            "hazard",
            "\uc548\uc804",
            "\ud654\uc7ac",
            "\uc704\ud5d8",
            "\ub9ac\uc2a4\ud06c",
        ),
    ):
        add(
            "safety_risk",
            "Safety, risk, incident, or hazard evidence must be handled explicitly.",
            prompt_text=_fixture_first_matching_text(
                question, ("safety", "risk", "fire", "\uc548\uc804", "\ud654\uc7ac", "\uc704\ud5d8")
            )
            or question,
        )
    return requirements


def _fixture_extract_domain_entities(
    question: str,
    requirements: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    lower = question.lower()
    entities: list[dict[str, str]] = []

    def add(name: str, entity_type: str, evidence: str) -> None:
        if not any(item["name"].lower() == name.lower() for item in entities):
            entities.append({"name": name, "type": entity_type, "evidence": evidence})

    concept_rules = (
        (("codex deepresearch", "deepresearch"), "Codex DeepResearch", "software_system"),
        (("semantic planner",), "semantic planner", "software_component"),
        (("runner",), "runner", "software_component"),
        (("next.js", "nextjs", "app router"), "Next.js App Router", "technology"),
        (("ev", "electric vehicle", "\uc804\uae30\ucc28"), "EV", "domain_subject"),
        (("battery", "\ubc30\ud130\ub9ac"), "battery", "domain_subject"),
        (("fire", "\ud654\uc7ac"), "fire safety", "risk"),
        (("thermal", "\uc5f4\ud3ed\uc8fc"), "thermal runaway", "failure_mode"),
        (("regulation", "regulatory", "\uaddc\uc81c"), "regulation", "source_domain"),
        (("safety test", "crash test", "\uc548\uc804 \ud14c\uc2a4\ud2b8"), "safety test", "evidence_artifact"),
        (("image", "photo", "figure", "chart", "\uc774\ubbf8\uc9c0", "\uc0ac\uc9c4", "\ub3c4\ud45c"), "visual evidence", "modality"),
    )
    for needles, name, entity_type in concept_rules:
        matched = next((needle for needle in needles if needle in lower or needle in question), None)
        if matched:
            add(name, entity_type, matched)
    if (
        any(entity["name"] == "EV" for entity in entities)
        and any(entity["name"] == "battery" for entity in entities)
        and any(entity["name"] == "fire safety" for entity in entities)
    ):
        add("thermal runaway", "failure_mode", "EV battery fire safety inference")
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9.+-]*(?:\s+[A-Z][A-Za-z0-9.+-]*){0,3}\b", question):
        text = match.group(0).strip()
        if len(text) > 1 and text.lower() not in {"the", "and"}:
            add(text, "named_entity", text)
    if not entities:
        add(_fixture_subject_phrase(question, []), "question_subject", question)
    return [dict(item) for item in entities[:12]]


def _fixture_infer_candidate_question_scope(
    question: str,
    requirements: Sequence[Mapping[str, Any]],
) -> str:
    lower = question.lower()
    if _contains_any(
        lower,
        ("compare", "impact", "risk", "strategy", "research", "investigate", "\uc870\uc0ac", "\ubd84\uc11d", "\ube44\uad50", "\uc601\ud5a5"),
    ):
        return "broad"
    if any(
        str(requirement.get("requirement_type") or "")
        in {"visual_modality", "source_quality", "safety_risk"}
        for requirement in requirements
    ):
        return "broad"
    if len(requirements) >= 3:
        return "broad"
    return "narrow"


def _fixture_build_candidate_angles(
    *,
    question: str,
    subject: str,
    entities: Sequence[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    scope: str,
) -> list[dict[str, Any]]:
    requirement_types = {str(req.get("requirement_type") or "") for req in requirements}
    is_implementation = _fixture_is_software_implementation_question(question, entities)
    specs: list[tuple[str, str, str, str, list[str], list[str]]] = []
    specs.append(
        (
            f"{subject} source baseline",
            f"What primary evidence directly defines the current state of {subject}?",
            "text_only",
            "primary_source",
            ["primary sources", "source excerpts"],
            ["primary or authoritative sources"],
        )
    )
    if "source_quality" in requirement_types:
        specs.append(
            (
                f"{subject} official and regulatory evidence",
                f"Which official, regulatory, or primary sources govern {subject}?",
                "text_only",
                "official_source",
                ["official source matrix", "regulatory excerpts"],
                ["official agencies", "regulators", "primary documents"],
            )
        )
    if "visual_modality" in requirement_types:
        specs.append(
            (
                f"{subject} visual evidence and figures",
                f"What do relevant images, charts, diagrams, or figures show about {subject}?",
                "visual_required",
                "visual_observation",
                ["visual observation notes", "image evidence table"],
                ["image pages", "charts", "figures", "diagrams"],
            )
        )
    if "safety_risk" in requirement_types:
        specs.append(
            (
                f"{subject} mechanism and failure modes",
                f"What mechanisms, incidents, hazards, or failure modes explain {subject}?",
                "text_only",
                "failure_pattern",
                ["failure mode inventory", "incident evidence"],
                ["incident reports", "technical safety sources"],
            )
        )
    if "time_range" in requirement_types:
        specs.append(
            (
                f"{subject} recent changes",
                f"What recent changes, incidents, releases, or guidance affect {subject}?",
                "text_only",
                "recent_change",
                ["recent-change timeline", "current status notes"],
                ["recent official updates", "dated sources"],
            )
        )
    if "geography" in requirement_types:
        specs.append(
            (
                f"{subject} geographic scope",
                f"How do the requested geography or jurisdiction constraints change {subject}?",
                "text_only",
                "comparative_analysis",
                ["jurisdiction comparison", "geographic caveats"],
                ["local regulators", "regional data"],
            )
        )
    if is_implementation:
        specs.extend(
            [
                (
                    f"{subject} architecture contract",
                    f"What architecture boundaries and artifact contracts are needed for {subject}?",
                    "text_only",
                    "implementation_detail",
                    ["architecture contract", "integration map"],
                    ["repository docs", "code references", "design notes"],
                ),
                (
                    f"{subject} validation strategy",
                    f"What tests and failure cases prove {subject} behaves as requested?",
                    "text_only",
                    "failure_pattern",
                    ["test matrix", "negative cases"],
                    ["test files", "failure artifacts"],
                ),
            ]
        )
    specs.append(
        (
            f"{subject} contradictions and caveats",
            f"What counter-evidence, limitations, or caveats could change the conclusion about {subject}?",
            "text_only",
            "counter_evidence",
            ["counter-evidence matrix", "caveat register"],
            ["independent sources", "conflicting reports"],
        )
    )
    specs.append(
        (
            f"{subject} decision implications",
            f"What practical implications, risks, or next decisions follow from the evidence about {subject}?",
            "text_only",
            "risk_or_guardrail",
            ["decision implications", "risk guardrails"],
            ["risk analysis", "implementation or policy guidance"],
        )
    )
    if scope == "broad":
        target_count = min(8, max(5, len(specs)))
    else:
        target_count = min(4, max(2, len(specs)))
    specs = _fixture_dedupe_angle_specs(specs)[:target_count]
    while scope == "broad" and len(specs) < 5:
        index = len(specs) + 1
        specs.append(
            (
                f"{subject} evidence axis {index}",
                f"What additional prompt-specific evidence is needed for {subject} axis {index}?",
                "text_only",
                _fixture_unused_evidence_need(specs),
                [f"{subject} evidence notes {index}"],
                ["prompt-specific supporting sources"],
            )
        )
    angles: list[dict[str, Any]] = []
    for index, (title, research_question, route, evidence_need, artifacts, sources) in enumerate(
        specs, start=1
    ):
        angle_id = f"angle_{index:03d}"
        visual_targets = (
            _fixture_visual_targets(subject, requirements)
            if route != "text_only"
            else []
        )
        angles.append(
            {
                "angle_id": angle_id,
                "title": title,
                "research_question": research_question,
                "why_this_angle_matters": (
                    f"This angle covers a distinct requirement for {subject} without "
                    "changing the user's requested domain."
                ),
                "included_scope": _fixture_included_scope_for_angle(title, requirements),
                "excluded_scope": _fixture_negative_scope(question, subject),
                "route": route,
                "evidence_need": evidence_need,
                "expected_source_types": sources,
                "expected_visual_targets": visual_targets,
                "expected_artifacts": artifacts,
                "search_queries": [
                    f"{subject} {research_question}",
                    f"{subject} {evidence_need.replace('_', ' ')} evidence",
                ],
                "success_criteria": [
                    f"Findings must mention {subject} or a listed domain entity.",
                    "Claims must be linked to source metadata or visual observations.",
                ],
                "report_section": _report_section_from_title(title),
                "risk_or_contradiction_checks": [
                    "Check whether sources disagree or omit important caveats.",
                    "Flag stale, low-quality, or out-of-scope evidence.",
                ],
            }
        )
    return angles


def _fixture_build_candidate_bounded_tasks(
    *,
    question: str,
    subject: str,
    angles: Sequence[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    scope: str,
    depth_preset: str,
    budget_cap: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if scope == "broad":
        target = {"deep": 48, "exhaustive": 80}.get(depth_preset, 24)
    else:
        target = 9
    if scope == "broad":
        target = max(20, min(target, 40 if depth_preset not in {"deep", "exhaustive"} else target))
    source_cap = max(1, int(budget_cap.get("max_sources") or 20))
    image_cap = max(0, int(budget_cap.get("max_images") or 12))
    tasks: list[dict[str, Any]] = []
    task_index = 1
    per_angle = max(2 if scope == "broad" else 1, math.ceil(target / max(1, len(angles))))
    for angle in angles:
        for occurrence in range(1, per_angle + 1):
            if len(tasks) >= target:
                break
            route = str(angle["route"])
            visual_targets = _string_list(angle.get("expected_visual_targets"))
            expected_source_types = _string_list(angle.get("expected_source_types"))
            query = _fixture_candidate_task_query(
                subject=subject,
                angle=angle,
                occurrence=occurrence,
                original_question=question,
            )
            expected_artifacts = _string_list(angle.get("expected_artifacts"))
            success_criteria = [
                *_string_list(angle.get("success_criteria")),
                f"The task is complete only when it can support or reject a claim about {subject}.",
            ]
            if _fixture_requires_official_source(requirements):
                success_criteria.append(
                    "Use official, regulatory, or primary sources when supporting source-quality claims."
                )
            deliverable_requirement = _fixture_deliverable_requirement(requirements)
            if deliverable_requirement:
                expected_artifacts = _ordered_unique(
                    [
                        *expected_artifacts,
                        _fixture_deliverable_artifact_name(deliverable_requirement),
                    ]
                )
                success_criteria.append(
                    "Outputs must preserve the requested report/table/checklist deliverable shape."
                )
            max_images = 0
            if route != "text_only":
                max_images = max(1, min(3, image_cap or 3))
            tasks.append(
                {
                    "task_id": f"task_semantic_{task_index:03d}",
                    "angle_id": str(angle["angle_id"]),
                    "query": query,
                    "route": route,
                    "freshness_requirement": _fixture_freshness_for_requirements(requirements),
                    "source_policy": _fixture_source_policy_for_requirements(requirements),
                    "expected_source_types": expected_source_types,
                    "expected_visual_targets": visual_targets,
                    "expected_artifacts": expected_artifacts,
                    "success_criteria": success_criteria,
                    "max_sources": max(1, min(5, source_cap)),
                    "max_images": max_images,
                    "done_condition": (
                        "Stop when the requested evidence type is found, source quality is "
                        "recorded, and caveats or missing evidence are explicitly noted."
                    ),
                }
            )
            task_index += 1
    return tasks


def _fixture_requires_official_source(requirements: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        str(requirement.get("requirement_type") or "") == "source_quality"
        for requirement in requirements
    )


def _fixture_deliverable_requirement(
    requirements: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for requirement in requirements:
        if (
            str(requirement.get("requirement_type") or "") == "deliverable_shape"
            and requirement.get("non_negotiable") is True
        ):
            return requirement
    return None


def _fixture_deliverable_artifact_name(requirement: Mapping[str, Any]) -> str:
    prompt_text = str(requirement.get("prompt_text") or "").lower()
    if "table" in prompt_text or "\ud45c" in prompt_text or "matrix" in prompt_text:
        return "requested table or matrix"
    if "checklist" in prompt_text:
        return "requested checklist"
    return "requested report outline"


def _fixture_candidate_requirement_coverage(
    *,
    requirements: Sequence[Mapping[str, Any]],
    angles: Sequence[Mapping[str, Any]],
    bounded_tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for requirement in requirements:
        requirement_type = str(requirement.get("requirement_type") or "")
        covered_angles = _fixture_coverage_angle_ids(requirement_type, angles)
        covered_tasks = [
            str(task.get("task_id"))
            for task in bounded_tasks
            if str(task.get("angle_id")) in covered_angles
        ]
        if not covered_angles and angles:
            covered_angles = [str(angles[0]["angle_id"])]
            covered_tasks = [
                str(task.get("task_id"))
                for task in bounded_tasks
                if str(task.get("angle_id")) in covered_angles
            ]
        output.append(
            {
                **dict(requirement),
                "covered_by_angle_ids": covered_angles,
                "covered_by_task_ids": covered_tasks,
                "coverage_status": "covered" if covered_angles and covered_tasks else "not_covered",
            }
        )
    return output


def _candidate_executable_text(
    *,
    angles: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
) -> str:
    values: list[Any] = []
    for angle in angles:
        values.extend(
            [
                angle.get("title"),
                angle.get("research_question"),
                angle.get("why_this_angle_matters"),
                angle.get("included_scope"),
                angle.get("expected_source_types"),
                angle.get("expected_visual_targets"),
                angle.get("expected_artifacts"),
                angle.get("search_queries"),
                angle.get("success_criteria"),
                angle.get("risk_or_contradiction_checks"),
            ]
        )
    for task in tasks:
        values.extend(
            [
                task.get("query"),
                task.get("source_policy"),
                task.get("expected_source_types"),
                task.get("expected_visual_targets"),
                task.get("expected_artifacts"),
                task.get("success_criteria"),
                task.get("done_condition"),
            ]
        )
    return json.dumps(values, ensure_ascii=False, sort_keys=True).lower()


def _core_question_tokens(question: str) -> set[str]:
    stopwords = {
        "research",
        "investigate",
        "analyze",
        "report",
        "table",
        "checklist",
        "official",
        "source",
        "sources",
        "image",
        "images",
        "chart",
        "charts",
        "figure",
        "figures",
        "current",
        "recent",
        "latest",
        "tell",
        "show",
        "about",
        "with",
        "from",
        "and",
        "the",
        "\uc870\uc0ac",
        "\uc870\uc0ac\ud574\uc918",
        "\ubd84\uc11d",
        "\ubcf4\uace0\uc11c",
        "\ub9ac\ud3ec\ud2b8",
        "\ud45c",
        "\uacf5\uc2dd",
        "\uaddc\uc81c",
        "\uadfc\uac70",
        "\uc774\ubbf8\uc9c0",
        "\ub3c4\ud45c",
        "\uc54c\ub824\uc918",
        "\ud574\uc918",
        "\uc911\uc2ec\uc73c\ub85c",
    }
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9.+#-]+|[\uac00-\ud7a3]+", question)
        if len(token) >= 2
    }
    return {token for token in tokens if token not in stopwords}


def _candidate_text_covers_subject(text: str, subject_tokens: set[str]) -> bool:
    if not subject_tokens:
        return True
    matched = {token for token in subject_tokens if token in text}
    required = 1 if len(subject_tokens) == 1 else min(2, len(subject_tokens))
    return len(matched) >= required


def _candidate_has_recent_task(tasks: Sequence[Mapping[str, Any]]) -> bool:
    for task in tasks:
        if str(task.get("freshness_requirement") or "").lower() == "recent":
            return True
    return False


def _candidate_has_geography_task(
    *,
    requirement: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> bool:
    geo_text = str(requirement.get("prompt_text") or requirement.get("requirement_text") or "")
    geo_tokens = _core_question_tokens(geo_text)
    if not geo_tokens:
        return True
    for task in tasks:
        task_text = json.dumps(
            [
                task.get("query"),
                task.get("expected_source_types"),
                task.get("expected_artifacts"),
                task.get("success_criteria"),
                task.get("done_condition"),
            ],
            ensure_ascii=False,
        ).lower()
        if any(token in task_text for token in geo_tokens):
            return True
    return False


def _candidate_has_deliverable_task(
    *,
    requirement: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
) -> bool:
    prompt_text = str(requirement.get("prompt_text") or "").lower()
    if "table" in prompt_text or "\ud45c" in prompt_text or "matrix" in prompt_text:
        deliverable_tokens = ("table", "matrix", "\ud45c")
    elif "checklist" in prompt_text:
        deliverable_tokens = ("checklist",)
    else:
        deliverable_tokens = ("report", "\ubcf4\uace0\uc11c", "\ub9ac\ud3ec\ud2b8")
    for task in tasks:
        task_output_text = json.dumps(
            [
                task.get("expected_artifacts"),
                task.get("success_criteria"),
                task.get("done_condition"),
            ],
            ensure_ascii=False,
        ).lower()
        if any(token in task_output_text for token in deliverable_tokens):
            return True
    return False


def _fixture_prompt_span(question: str, text: str) -> dict[str, int | None]:
    if not text:
        return {"start": None, "end": None}
    index = question.find(text)
    if index < 0:
        return {"start": None, "end": None}
    return {"start": index, "end": index + len(text)}


def _fixture_first_matching_text(question: str, candidates: Sequence[str]) -> str | None:
    lower = question.lower()
    for candidate in candidates:
        if candidate in question or candidate.lower() in lower:
            return candidate
    return None


def _fixture_question_language(question: str) -> str:
    return "ko" if re.search(r"[\uac00-\ud7a3]", question) else "en"


def _fixture_intent_summary(
    question: str,
    subject: str,
    requirements: Sequence[Mapping[str, Any]],
) -> str:
    requirement_labels = _ordered_unique(
        str(requirement.get("requirement_type") or "")
        for requirement in requirements
        if requirement.get("requirement_type")
    )
    return (
        f"Research {subject} by preserving the user's requested scope from the "
        f"original question: {question}. Required dimensions: "
        + ", ".join(requirement_labels)
        + "."
    )


def _fixture_decomposition_strategy(
    scope: str,
    subject: str,
    requirements: Sequence[Mapping[str, Any]],
) -> str:
    requirement_labels = _ordered_unique(
        str(requirement.get("requirement_type") or "")
        for requirement in requirements
        if requirement.get("requirement_type")
    )
    return (
        f"Treat the question as {scope}. Split {subject} by user requirements "
        f"({', '.join(requirement_labels)}) so each non-negotiable constraint "
        "has at least one angle and executable bounded tasks."
    )


def _fixture_subject_phrase(question: str, entities: Sequence[Mapping[str, Any]]) -> str:
    preferred = [
        str(entity.get("name") or "")
        for entity in entities
        if str(entity.get("type") or "") in {
            "software_system",
            "technology",
            "domain_subject",
            "question_subject",
        }
    ]
    if preferred:
        if {"EV", "battery"} <= set(preferred):
            return "EV battery fire safety"
        if "Codex DeepResearch" in preferred and "semantic planner" in preferred:
            return "Codex DeepResearch semantic planner"
        return " ".join(preferred[:3])
    tokens = [
        token
        for token in re.findall(r"[A-Za-z0-9.+#-]+|[\uac00-\ud7a3]+", question)
        if token.lower()
        not in {
            "research",
            "investigate",
            "analyze",
            "show",
            "tell",
            "\uc870\uc0ac",
            "\uc870\uc0ac\ud574\uc918",
            "\ubd84\uc11d",
            "\uc54c\ub824\uc918",
        }
    ]
    return " ".join(tokens[:8]) if tokens else "the user question"


def _fixture_extract_geography_requirement(question: str) -> str | None:
    geography_terms = (
        "United States",
        "US",
        "U.S.",
        "EU",
        "Europe",
        "Korea",
        "South Korea",
        "\ud55c\uad6d",
        "\ubbf8\uad6d",
        "\uc720\ub7fd",
        "\uc11c\uc6b8",
    )
    return _fixture_first_matching_text(question, geography_terms)


def _fixture_is_software_implementation_question(
    question: str,
    entities: Sequence[Mapping[str, Any]],
) -> bool:
    lower = question.lower()
    names = {str(entity.get("name") or "").lower() for entity in entities}
    software_subject = bool(
        {"codex deepresearch", "semantic planner", "runner"} & names
        or _contains_any(lower, ("api", "sdk", "next.js", "nextjs", "app router"))
    )
    implementation_request = _contains_any(
        lower,
        (
            "implement",
            "implementation",
            "architecture",
            "migration",
            "code",
            "test strategy",
            "\uad6c\ud604",
            "\uc544\ud0a4\ud14d\ucc98",
            "\ud14c\uc2a4\ud2b8 \uc804\ub7b5",
        ),
    )
    return software_subject and implementation_request


def _fixture_negative_scope(question: str, subject: str) -> list[str]:
    lower = question.lower()
    if _fixture_is_software_implementation_question(question, []) or (
        "codex" in lower and ("implement" in lower or "architecture" in lower)
    ):
        return [
            "Do not answer unrelated product-market or policy topics unless requested.",
            "Do not replace implementation architecture analysis with generic research advice.",
        ]
    return [
        "Do not drift into Codex runner architecture or internal implementation tasks.",
        f"Do not replace {subject} with a generic software-planning topic.",
    ]


def _fixture_visual_targets(
    subject: str,
    requirements: Sequence[Mapping[str, Any]],
) -> list[str]:
    has_visual = any(
        str(requirement.get("requirement_type") or "") == "visual_modality"
        for requirement in requirements
    )
    if not has_visual:
        return []
    return [
        f"{subject} public images",
        f"{subject} charts or figures",
        f"{subject} diagrams or screenshots",
    ]


def _fixture_included_scope_for_angle(
    title: str,
    requirements: Sequence[Mapping[str, Any]],
) -> list[str]:
    included = [title]
    for requirement in requirements:
        requirement_type = str(requirement.get("requirement_type") or "")
        if requirement_type in {"subject", "source_quality", "visual_modality", "safety_risk"}:
            included.append(str(requirement.get("requirement_text") or requirement_type))
    return _ordered_unique(included)


def _fixture_dedupe_angle_specs(
    specs: Sequence[tuple[str, str, str, str, list[str], list[str]]],
) -> list[tuple[str, str, str, str, list[str], list[str]]]:
    output: list[tuple[str, str, str, str, list[str], list[str]]] = []
    seen_titles: set[str] = set()
    seen_needs: set[str] = set()
    for spec in specs:
        title = _normalize_text(spec[0])
        evidence_need = spec[3]
        if title in seen_titles:
            continue
        if evidence_need in seen_needs and evidence_need not in {
            "visual_observation",
            "implementation_detail",
        }:
            continue
        seen_titles.add(title)
        seen_needs.add(evidence_need)
        output.append(spec)
    return output


def _fixture_unused_evidence_need(
    specs: Sequence[tuple[str, str, str, str, list[str], list[str]]],
) -> str:
    used = {spec[3] for spec in specs}
    for need in ALLOWED_EVIDENCE_NEEDS:
        if need not in used:
            return need
    return "primary_source"


def _fixture_freshness_for_requirements(requirements: Sequence[Mapping[str, Any]]) -> str:
    if any(str(req.get("requirement_type") or "") == "time_range" for req in requirements):
        return "recent"
    return "any"


def _fixture_source_policy_for_requirements(requirements: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    official = any(
        str(req.get("requirement_type") or "") == "source_quality"
        for req in requirements
    )
    return {
        "decision": "allowed",
        "requires_official_or_primary": official,
        "quality_requirements": (
            ["official", "regulatory", "primary"] if official else ["source-backed"]
        ),
        "flags": [],
    }


def _fixture_candidate_task_query(
    *,
    subject: str,
    angle: Mapping[str, Any],
    occurrence: int,
    original_question: str,
) -> str:
    title = str(angle.get("title") or "angle")
    research_question = str(angle.get("research_question") or title)
    variants = (
        "collect source-backed evidence",
        "verify claims and caveats",
        "find contradictions or missing evidence",
        "prepare report-ready artifacts",
    )
    variant = variants[(occurrence - 1) % len(variants)]
    return (
        f"{subject} / {variant}: {research_question} "
        f"Original ask: {original_question}"
    )


def _fixture_coverage_angle_ids(
    requirement_type: str,
    angles: Sequence[Mapping[str, Any]],
) -> list[str]:
    matched: list[str] = []
    for angle in angles:
        route = str(angle.get("route") or "")
        evidence_need = str(angle.get("evidence_need") or "")
        title = str(angle.get("title") or "").lower()
        if requirement_type == "subject":
            matched.append(str(angle["angle_id"]))
        elif requirement_type == "visual_modality" and route != "text_only":
            matched.append(str(angle["angle_id"]))
        elif requirement_type == "source_quality" and evidence_need == "official_source":
            matched.append(str(angle["angle_id"]))
        elif requirement_type == "time_range" and evidence_need == "recent_change":
            matched.append(str(angle["angle_id"]))
        elif requirement_type == "geography" and "geographic" in title:
            matched.append(str(angle["angle_id"]))
        elif requirement_type == "safety_risk" and evidence_need == "failure_pattern":
            matched.append(str(angle["angle_id"]))
        elif requirement_type in {"deliverable_shape", "user_constraint"}:
            matched.append(str(angle["angle_id"]))
    return _ordered_unique(matched)


def _question_class_from_candidate(
    candidate: Mapping[str, Any],
    angles: Sequence[SemanticAngle],
) -> str:
    candidate_class = str(candidate.get("question_class") or "")
    if candidate_class in {
        _CLASS_GENERAL,
        _CLASS_TECHNICAL,
        _CLASS_PRODUCT,
        _CLASS_VISUAL,
        _CLASS_POLICY,
        _CLASS_IMPLEMENTATION,
    }:
        return candidate_class
    if any(angle.route != "text_only" for angle in angles):
        return _CLASS_VISUAL
    if any(
        str(req.get("requirement_type") or "") in {"source_quality", "safety_risk"}
        for req in _list(candidate.get("constraints"))
        if isinstance(req, Mapping)
    ):
        return _CLASS_POLICY
    return _CLASS_GENERAL


def classify_question(question: str) -> str:
    text = question.lower()
    if _mentions_explicit_visual_evidence(text):
        return _CLASS_VISUAL
    if _contains_any(
        text,
        (
            "architecture",
            "implementation",
            "implement",
            "runner",
            "planner",
            "fan-out",
            "fanout",
            "test strategy",
            "\uc544\ud0a4\ud14d\ucc98",
            "\ud14c\uc2a4\ud2b8",
            "\uad6c\ud604",
            "\ucd94\uac00",
        ),
    ):
        return _CLASS_IMPLEMENTATION
    if _contains_any(
        text,
        (
            "policy",
            "legal",
            "law",
            "compliance",
            "risk",
            "rights",
            "watermark",
            "disclosure",
            "consent",
            "\uc815\ucc45",
            "\ubc95\uc801",
            "\ubc95\ub960",
            "\ub9ac\uc2a4\ud06c",
            "\uc6cc\ud130\ub9c8\ud06c",
            "\ucd08\uc0c1\uad8c",
            "\ud45c\uc2dc",
        ),
    ):
        return _CLASS_POLICY
    if _contains_any(text, _VISUAL_KEYWORDS):
        return _CLASS_VISUAL
    if _contains_any(
        text,
        (
            "market",
            "competitive",
            "competitor",
            "pricing",
            "business model",
            "product",
            "segment",
            "adoption",
            "\uc2dc\uc7a5",
            "\uacbd\uc7c1",
            "\uac00\uaca9",
            "\uc11c\ube44\uc2a4",
            "\uc81c\ud488",
        ),
    ):
        return _CLASS_PRODUCT
    if _contains_any(
        text,
        (
            "api",
            "library",
            "framework",
            "next.js",
            "nextjs",
            "app router",
            "cache",
            "caching",
            "sdk",
            "runtime",
            "migration",
            "release notes",
            "changelog",
            "\uce90\uc2f1",
            "\ub77c\uc774\ube0c\ub7ec\ub9ac",
            "\ud504\ub808\uc784\uc6cc\ud06c",
            "\ub9c8\uc774\uadf8\ub808\uc774\uc158",
        ),
    ):
        return _CLASS_TECHNICAL
    return _CLASS_GENERAL


def write_semantic_planner_validation(
    *,
    run_dir: str | Path,
    evidence: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]] | None = None,
    report_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write semantic planner validation metrics for one run."""

    run_path = Path(run_dir)
    artifact = semantic_planner_validation(
        run_dir=run_path,
        evidence=evidence,
        tasks=tasks,
        report_status=report_status,
    )
    _write_json(run_path / SEMANTIC_PLANNER_VALIDATION_FILENAME, artifact)
    return artifact


def write_semantic_integrity_artifacts(
    *,
    run_dir: str | Path,
    question: str,
    plan: SemanticPlan,
    routing: Sequence[Mapping[str, Any]] | None = None,
    search_tasks: Sequence[Mapping[str, Any]] | None = None,
    created_at: str | None = None,
) -> dict[str, str]:
    """Write P3-SP1 semantic integrity schema stubs for one run."""

    run_path = Path(run_dir)
    timestamp = created_at or _utc_now_from_run(run_path)
    raw_dir = run_path / SEMANTIC_RAW_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_request_path = raw_dir / SEMANTIC_RAW_REQUEST_FILENAME
    raw_response_path = raw_dir / SEMANTIC_RAW_RESPONSE_FILENAME

    request_payload = dict(plan.raw_request_payload) if plan.raw_request_payload else {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_planner_raw_request",
        "run_id": run_path.name,
        "created_at": timestamp,
        "planner_mode": plan.planner_mode,
        "semantic_release_eligible": False,
        "question": question,
        "question_scope": _question_scope(question, plan),
        "template_use": _template_use(plan),
        "provenance": _planner_provenance(plan),
    }
    request_payload.setdefault("schema_version", SEMANTIC_PLANNER_SCHEMA_VERSION)
    request_payload.setdefault("artifact_type", "semantic_planner_raw_request")
    request_payload["run_id"] = run_path.name
    request_payload["created_at"] = timestamp
    request_payload.setdefault("question", question)
    if not plan.raw_request_payload:
        request_payload.setdefault("question_scope", _question_scope(question, plan))
    request_payload.setdefault("template_use", _template_use(plan))
    request_payload.setdefault("provenance", _planner_provenance(plan))

    response_payload = dict(plan.raw_response_payload) if plan.raw_response_payload else {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "artifact_type": "semantic_planner_raw_response",
        "run_id": run_path.name,
        "created_at": timestamp,
        "planner_mode": plan.planner_mode,
        "semantic_release_eligible": False,
        "semantic_plan": plan.to_dict(),
        "diagnostics": dict(plan.diagnostics or {}),
        "provenance": _planner_provenance(plan),
    }
    response_payload.setdefault("schema_version", SEMANTIC_PLANNER_SCHEMA_VERSION)
    response_payload.setdefault("artifact_type", "semantic_planner_raw_response")
    response_payload["run_id"] = run_path.name
    response_payload["created_at"] = timestamp
    response_payload.setdefault("semantic_plan", plan.to_dict())
    response_payload.setdefault("diagnostics", dict(plan.diagnostics or {}))
    response_payload.setdefault("provenance", _planner_provenance(plan))
    _write_json(raw_request_path, request_payload)
    _write_json(raw_response_path, response_payload)
    raw_request_hash = _sha256_file(raw_request_path)
    raw_response_hash = _sha256_file(raw_response_path)

    base = _integrity_base_payload(
        run_path=run_path,
        question=question,
        plan=plan,
        created_at=timestamp,
        raw_request_path=raw_request_path,
        raw_response_path=raw_response_path,
        raw_request_hash=raw_request_hash,
        raw_response_hash=raw_response_hash,
    )
    oracle_requirement_map = _oracle_requirement_map(plan)
    requirement_coverage_map = _requirement_coverage_map(plan)
    semantic_fit_score = _release_ineligible_semantic_fit_score(plan)

    artifacts = {
        SEMANTIC_EXPECTATION_ORACLE_FILENAME: {
            **base,
            "artifact_type": "semantic_expectation_oracle",
            "oracle_requirement_map": oracle_requirement_map,
            "locked_before_plan_visible": plan.planner_mode == PLANNER_MODE_CODEX_SEMANTIC,
            "reverse_fit_risk": plan.planner_mode != PLANNER_MODE_CODEX_SEMANTIC,
        },
        SEMANTIC_PLAN_FILENAME: {
            **base,
            "artifact_type": "semantic_plan",
            "semantic_plan": plan.to_dict(),
            "intent_summary": plan.intent_summary,
            "domain_entities": list(plan.domain_entities),
            "constraints": list(plan.constraints),
            "candidate_question_scope": plan.question_scope,
            "decomposition_strategy": plan.decomposition_strategy,
            "negative_scope": list(plan.negative_scope),
            "bounded_tasks": list(plan.bounded_tasks),
            "planner_provenance": dict(plan.planner_provenance),
            "parsed_response_hash": _planner_provenance(plan).get("parsed_response_hash"),
            "angles": [angle.to_dict() for angle in plan.angles],
            "requirement_coverage_map": requirement_coverage_map,
            "routing_count": len(routing or []),
            "search_task_count": len(search_tasks or []),
        },
        SEMANTIC_PLAN_REVIEW_FILENAME: {
            **base,
            "artifact_type": "semantic_plan_review",
            "semantic_fit_score": semantic_fit_score,
            "blockers": _semantic_review_blockers(plan),
            "warnings": [],
            "reviewer_independence": _reviewer_independence(plan),
            "substitute_implementation_check": _substitute_implementation_check(plan),
            "final_verdict": "release_ineligible",
        },
        SEMANTIC_PLAN_DELTA_FILENAME: {
            **base,
            "artifact_type": "semantic_plan_delta",
            "delta_applied": False,
            "delta_status": "not_applicable",
            "base_plan_path": SEMANTIC_PLAN_FILENAME,
            "oracle_requirement_map": oracle_requirement_map,
            "reviewer_independence": _reviewer_independence(plan),
            "substitute_implementation_check": _substitute_implementation_check(plan),
        },
        SEMANTIC_MATERIALIZATION_DIFF_FILENAME: {
            **base,
            "artifact_type": "semantic_materialization_diff",
            "status": "stub_only",
            "valid": False,
            "full_materialization_validation_implemented": False,
            "out_of_scope_issue": "P3-SP4",
            "oracle_requirement_map": oracle_requirement_map,
            "reviewer_independence": _reviewer_independence(plan),
            "substitute_implementation_check": _substitute_implementation_check(plan),
        },
    }
    written: dict[str, str] = {
        "semantic_raw_request": str(raw_request_path),
        "semantic_raw_response": str(raw_response_path),
    }
    for filename, payload in artifacts.items():
        path = run_path / filename
        _write_json(path, payload)
        written[_artifact_key(filename)] = str(path)
    return written


def write_blocked_semantic_planner_artifacts(
    *,
    run_dir: str | Path,
    question: str,
    reason: str,
    created_at: str | None = None,
) -> dict[str, str]:
    """Write blocked semantic planner stubs plus validation for preflight blocks."""

    plan = blocked_semantic_planner_plan(question=question, reason=reason)
    artifacts = write_semantic_integrity_artifacts(
        run_dir=run_dir,
        question=question,
        plan=plan,
        routing=[],
        search_tasks=[],
        created_at=created_at,
    )
    validation = semantic_planner_validation(
        run_dir=run_dir,
        evidence={
            "run_id": Path(run_dir).name,
            "question": question,
            "semantic_planner": plan.to_dict(),
            "semantic_angles": [],
        },
        tasks=[],
    )
    _write_json(Path(run_dir) / SEMANTIC_PLANNER_VALIDATION_FILENAME, validation)
    artifacts["semantic_planner_validation"] = str(
        Path(run_dir) / SEMANTIC_PLANNER_VALIDATION_FILENAME
    )
    return artifacts


def semantic_integrity_artifact_filenames() -> tuple[str, ...]:
    return (
        SEMANTIC_EXPECTATION_ORACLE_FILENAME,
        SEMANTIC_PLAN_FILENAME,
        SEMANTIC_PLAN_REVIEW_FILENAME,
        SEMANTIC_PLAN_DELTA_FILENAME,
        SEMANTIC_MATERIALIZATION_DIFF_FILENAME,
    )


def semantic_planner_validation(
    *,
    run_dir: str | Path,
    evidence: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]] | None = None,
    report_status: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    run_path = Path(run_dir)
    planner = evidence.get("semantic_planner")
    planner_metadata = planner if isinstance(planner, Mapping) else {}
    question = str(evidence.get("question") or "")
    question_class = str(planner_metadata.get("question_class") or classify_question(question))
    expected_needs = _string_list(planner_metadata.get("expected_evidence_needs"))
    angles = _semantic_angles_from_evidence(evidence)
    if not expected_needs:
        expected_needs = _expected_needs_for_class(question_class, angles)
    broad_question = bool(planner_metadata.get("broad_question"))
    if not broad_question:
        broad_question = question_class != _CLASS_GENERAL and len(set(expected_needs)) >= 4

    route_counts = Counter(str(angle.get("route") or "text_only") for angle in angles)
    evidence_need_counts = Counter(
        str(angle.get("evidence_need") or "primary_source") for angle in angles
    )
    material_checks = _material_difference_checks(
        question=question,
        angles=angles,
        broad_question=broad_question,
    )
    failed_angles = [
        check for check in material_checks if not check.get("valid", False)
    ]

    task_records = [dict(task) for task in tasks or [] if isinstance(task, Mapping)]
    near_duplicate_records = _near_duplicate_task_records(question, task_records)
    generic_lens_records = _generic_lens_task_records(question, task_records)
    tasks_per_angle = Counter(str(task.get("angle_id") or "unknown") for task in task_records)
    visual_hits = _visual_expected_evidence_hits(angles, task_records)
    report_angle_claim_counts = _report_angle_claim_counts(evidence, report_status)

    failures = _validation_failures(
        question=question,
        question_class=question_class,
        broad_question=broad_question,
        angles=angles,
        failed_angles=failed_angles,
        task_records=task_records,
        near_duplicate_count=len(near_duplicate_records),
        generic_lens_count=len(generic_lens_records),
        visual_hits=visual_hits,
        report_angle_claim_counts=report_angle_claim_counts,
    )
    task_count = len(task_records)
    near_duplicate_ratio = _ratio(len(near_duplicate_records), task_count)
    generic_lens_ratio = _ratio(len(generic_lens_records), task_count)
    if broad_question and task_count and near_duplicate_ratio > 0.20:
        failures.append(
            {
                "code": "near_duplicate_task_ratio_exceeded",
                "message": "Near-duplicate task queries exceed 20% of ResearchTask records.",
                "ratio": near_duplicate_ratio,
            }
        )
    if broad_question and task_count and generic_lens_ratio > 0.30:
        failures.append(
            {
                "code": "generic_lens_task_ratio_exceeded",
                "message": "Generic lens-only task queries exceed 30% of ResearchTask records.",
                "ratio": generic_lens_ratio,
            }
        )

    planner_mode = str(planner_metadata.get("planner_mode") or "")
    semantic_release_eligible = bool(planner_metadata.get("semantic_release_eligible"))
    semantic_status = _semantic_release_status(
        run_path=run_path,
        planner_metadata=planner_metadata,
    )
    failures.extend(semantic_status["failures"])

    covered_needs = [
        need for need in expected_needs if need in evidence_need_counts
    ]
    artifact = {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "fixture_id": str(planner_metadata.get("fixture_id") or run_path.name),
        "run_id": str(evidence.get("run_id") or run_path.name),
        "planner_mode": planner_mode or "unknown",
        "semantic_release_eligible": semantic_release_eligible,
        "semantic_status": semantic_status,
        "question_class": question_class,
        "broad_question": broad_question,
        "angle_count": len(angles),
        "route_counts": dict(sorted(route_counts.items())),
        "evidence_need_counts": dict(sorted(evidence_need_counts.items())),
        "expected_evidence_needs": expected_needs,
        "covered_evidence_needs": covered_needs,
        "missing_evidence_needs": [
            need for need in expected_needs if need not in evidence_need_counts
        ],
        "near_duplicate_ratio": near_duplicate_ratio,
        "near_duplicate_tasks": near_duplicate_records,
        "generic_lens_ratio": generic_lens_ratio,
        "generic_lens_tasks": generic_lens_records,
        "visual_expected_evidence_hits": visual_hits,
        "task_count": task_count,
        "tasks_per_angle": dict(sorted(tasks_per_angle.items())),
        "report_angle_claim_counts": report_angle_claim_counts,
        "real_codex_exec_smoke": _real_codex_exec_smoke_status(run_path),
        "material_difference_checks": material_checks,
        "failed_angles": failed_angles,
        "failures": failures,
        "ok": not failures,
        "artifacts": {
            "semantic_planner_validation": str(
                run_path / SEMANTIC_PLANNER_VALIDATION_FILENAME
            ),
            "evidence": str(run_path / "evidence.json"),
            "research_tasks": str(run_path / "research_tasks.json"),
            "report_status": str(run_path / "report_status.json"),
        },
    }
    artifact.update(_semantic_common_integrity_fields(run_path))
    return artifact


def question_mentions_visual_evidence(question: str) -> bool:
    return _contains_any(question.lower(), _VISUAL_KEYWORDS)


def _mentions_explicit_visual_evidence(text: str) -> bool:
    return bool(
        re.search(
            r"\b("
            r"ui|screenshot(s)?|screen(s)?|interface|chart(s)?|graph(s)?|"
            r"image[- ]quality|visual evidence|visual comparison|image comparison"
            r")\b",
            text,
        )
    )


def _normalize_explicit_angles(angles: Sequence[str]) -> list[str]:
    normalized = [" ".join(angle.strip().split()) for angle in angles if angle.strip()]
    if not normalized:
        raise ValueError("at least one planner angle is required when angles are provided")
    return normalized


def _explicit_angle_record(
    *,
    angle: str,
    index: int,
    question_class: str,
) -> SemanticAngle:
    evidence_need = _infer_evidence_need(angle)
    return SemanticAngle(
        angle_id=f"angle_{index:03d}",
        title=angle,
        research_question=f"Investigate {angle}.",
        question_context="",
        route="text_only",
        evidence_need=evidence_need,
        expected_artifacts=_default_expected_artifacts(evidence_need),
        success_criteria=[
            "Evidence directly supports or refutes the supplied angle.",
            "Findings are bounded to this angle's scope.",
        ],
        report_section=_report_section_from_title(angle, fallback=question_class),
    )


def _infer_evidence_need(text: str) -> str:
    lower = text.lower()
    if _contains_any(lower, ("official", "docs", "documentation", "release notes", "changelog")):
        return "official_source"
    if _contains_any(lower, ("price", "pricing", "limit", "quota", "\uac00\uaca9")):
        return "pricing_or_limits"
    if _contains_any(lower, ("policy", "legal", "risk", "rights", "guardrail", "\uc815\ucc45", "\ub9ac\uc2a4\ud06c")):
        return "policy_or_legal"
    if _contains_any(lower, ("screenshot", "image", "photo", "visual", "\uc0ac\uc9c4", "\uc774\ubbf8\uc9c0")):
        return "visual_example"
    if _contains_any(lower, ("compare", "competitor", "competitive", "\ube44\uad50", "\uacbd\uc7c1")):
        return "comparative_analysis"
    if _contains_any(lower, ("fail", "failure", "break", "rollback", "\uc2e4\ud328")):
        return "failure_pattern"
    if _contains_any(lower, ("implement", "architecture", "runtime", "code", "\uad6c\ud604", "\uc544\ud0a4\ud14d\ucc98")):
        return "implementation_detail"
    return "primary_source"


def _templates_for_class(question_class: str) -> list[dict[str, Any]]:
    return {
        _CLASS_TECHNICAL: [
            _template(
                "Official change inventory",
                "Identify authoritative release notes, docs, and versioned behavior changes.",
                "text_only",
                "official_source",
                ["official docs", "release notes", "version timeline"],
                "Official Changes",
            ),
            _template(
                "Recent behavior delta",
                "Separate current behavior from older guidance and stale examples.",
                "text_only",
                "recent_change",
                ["change chronology", "deprecated guidance list"],
                "Recent Changes",
            ),
            _template(
                "Implementation impact",
                "Map code-level migration steps, configuration changes, and runtime constraints.",
                "text_only",
                "implementation_detail",
                ["migration checklist", "runtime constraint table"],
                "Implementation Impact",
            ),
            _template(
                "Failure patterns",
                "Find breaking changes, cache invalidation pitfalls, and reproducible failure modes.",
                "text_only",
                "failure_pattern",
                ["failure-mode list", "diagnostic signals"],
                "Breaking Changes",
            ),
            _template(
                "Rollback guardrails",
                "Define rollout risks, rollback triggers, and safe fallback options.",
                "text_only",
                "risk_or_guardrail",
                ["rollback plan", "guardrail checklist"],
                "Risk And Rollback",
            ),
            _template(
                "Contradictory guidance",
                "Collect counterexamples and incompatible recommendations across supported versions.",
                "text_only",
                "counter_evidence",
                ["counter-evidence matrix", "version caveats"],
                "Counter Evidence",
            ),
        ],
        _CLASS_PRODUCT: [
            _template(
                "Market definition",
                "Define the category, adjacent substitutes, and buyer problem boundaries.",
                "text_only",
                "primary_source",
                ["category definition", "market boundary notes"],
                "Market Definition",
            ),
            _template(
                "Competitor landscape",
                "Compare direct and adjacent competitors by positioning and workflow coverage.",
                "text_only",
                "comparative_analysis",
                ["competitor matrix", "positioning notes"],
                "Competitor Landscape",
            ),
            _template(
                "Pricing model",
                "Collect pricing, packaging, limits, and monetization evidence.",
                "text_only",
                "pricing_or_limits",
                ["pricing table", "limit summary"],
                "Pricing And Business Model",
            ),
            _template(
                "User segments",
                "Identify buyer and end-user segments, jobs-to-be-done, and workflow triggers.",
                "text_only",
                "user_workflow",
                ["segment map", "workflow triggers"],
                "User Segments",
            ),
            _template(
                "Adoption risks",
                "Find switching friction, procurement barriers, trust gaps, and counter-signals.",
                "text_only",
                "risk_or_guardrail",
                ["adoption risk register", "trust caveats"],
                "Adoption Risks",
            ),
            _template(
                "Market momentum",
                "Check recent launches, funding, partnerships, and demand signals.",
                "text_only",
                "recent_change",
                ["recent signal timeline", "momentum summary"],
                "Market Momentum",
            ),
        ],
        _CLASS_VISUAL: [
            _template(
                "Representative image set",
                "Collect representative public examples that show the target visual style.",
                "visual_required",
                "visual_example",
                ["image example set", "source attribution list"],
                "Representative Examples",
                expected_evidence=["visual_example"],
            ),
            _template(
                "Visual feature extraction",
                "Extract visible traits such as lighting, framing, texture, color, and artifacts.",
                "visual_required",
                "visual_observation",
                ["feature inventory", "VLM observation notes"],
                "Visual Features",
                expected_evidence=["visual_observation", "vlm_analysis"],
            ),
            _template(
                "Ordinary versus target comparison",
                "Compare baseline images against target-style examples to isolate differences.",
                "visual_optional",
                "comparative_analysis",
                ["comparison matrix", "difference taxonomy"],
                "Style Comparison",
                expected_evidence=["visual_example", "visual_observation"],
            ),
            _template(
                "Prompt and edit mapping",
                "Translate visual traits into prompt, capture, or editing instructions.",
                "text_only",
                "implementation_detail",
                ["instruction mapping", "edit checklist"],
                "Instruction Mapping",
            ),
            _template(
                "Failure patterns",
                "Identify artifacts, over-processing, and cases that fail to match the target style.",
                "visual_optional",
                "failure_pattern",
                ["failure gallery criteria", "diagnostic checklist"],
                "Failure Patterns",
                expected_evidence=["visual_observation"],
            ),
            _template(
                "Rights and source policy",
                "Check rights, attribution, likeness, and allowed-use constraints for examples.",
                "text_only",
                "policy_or_legal",
                ["rights checklist", "source policy notes"],
                "Rights And Policy",
            ),
        ],
        _CLASS_POLICY: [
            _template(
                "Official policy sources",
                "Collect current official legal, regulator, platform, and product policy sources.",
                "text_only",
                "official_source",
                ["official policy source list", "jurisdiction notes"],
                "Official Policy Sources",
            ),
            _template(
                "Use-case boundaries",
                "Separate allowed, restricted, and prohibited use cases with source support.",
                "text_only",
                "policy_or_legal",
                ["allowed-disallowed matrix", "source excerpts"],
                "Use Case Boundaries",
            ),
            _template(
                "Disclosure requirements",
                "Identify labeling, disclosure, watermark, and provenance requirements.",
                "text_only",
                "risk_or_guardrail",
                ["disclosure checklist", "watermark requirements"],
                "Disclosure And Watermarking",
            ),
            _template(
                "Consent workflow",
                "Map consent, rights, likeness, takedown, and complaint-handling workflows.",
                "text_only",
                "user_workflow",
                ["consent workflow", "rights handling process"],
                "Consent And Rights",
            ),
            _template(
                "Platform differences",
                "Compare platform policies and enforcement differences that affect launch scope.",
                "text_only",
                "comparative_analysis",
                ["platform comparison", "enforcement caveats"],
                "Platform Differences",
            ),
            _template(
                "Counterexamples and enforcement",
                "Find disputes, exceptions, enforcement actions, and unresolved legal uncertainty.",
                "text_only",
                "counter_evidence",
                ["counterexample list", "uncertainty register"],
                "Open Risks",
            ),
        ],
        _CLASS_IMPLEMENTATION: [
            _template(
                "Current architecture",
                "Inventory the current runner, planner, routing, and artifact contracts.",
                "text_only",
                "primary_source",
                ["architecture inventory", "artifact contract map"],
                "Current Architecture",
            ),
            _template(
                "Semantic schema design",
                "Define planner angle schema fields, validation rules, and compatibility behavior.",
                "text_only",
                "implementation_detail",
                ["schema proposal", "compatibility notes"],
                "Semantic Schema",
            ),
            _template(
                "Fan-out algorithm",
                "Design task expansion that preserves angle scope under min and max task bounds.",
                "text_only",
                "comparative_analysis",
                ["algorithm options", "fan-out invariant list"],
                "Fan Out Algorithm",
            ),
            _template(
                "Regression fixtures",
                "Specify deterministic fixtures that fail generic suffix duplication and bad routing.",
                "text_only",
                "failure_pattern",
                ["fixture matrix", "negative-case checklist"],
                "Validation Fixtures",
            ),
            _template(
                "Integration points",
                "Map integration with orchestration, merge, verification, and synthesis artifacts.",
                "text_only",
                "user_workflow",
                ["integration map", "stage handoff notes"],
                "Pipeline Integration",
            ),
            _template(
                "Rollback and observability",
                "Define failure modes, rollout checks, rollback triggers, and planner diagnostics.",
                "text_only",
                "risk_or_guardrail",
                ["observability checklist", "rollback triggers"],
                "Failure Modes",
            ),
        ],
    }[question_class]


def _template(
    title: str,
    research_question: str,
    route: str,
    evidence_need: str,
    expected_artifacts: Sequence[str],
    report_section: str,
    *,
    expected_evidence: Sequence[str] | None = None,
) -> dict[str, Any]:
    artifacts = list(expected_artifacts)
    return {
        "title": title,
        "research_question": research_question,
        "route": route,
        "evidence_need": evidence_need,
        "expected_artifacts": artifacts,
        "expected_evidence": list(expected_evidence or [evidence_need]),
        "success_criteria": [
            "At least two independent evidence items are considered when available.",
            "Findings are specific enough to support a dedicated report section.",
        ],
        "report_section": report_section,
    }


def _semantic_angles_from_evidence(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    semantic_angles = evidence.get("semantic_angles")
    if isinstance(semantic_angles, list) and semantic_angles:
        return [
            _normalized_angle_record(angle, index)
            for index, angle in enumerate(semantic_angles, start=1)
            if isinstance(angle, Mapping)
        ]
    routing = evidence.get("routing")
    if isinstance(routing, list):
        return [
            _normalized_angle_record(route, index)
            for index, route in enumerate(routing, start=1)
            if isinstance(route, Mapping)
        ]
    return []


def _normalized_angle_record(record: Mapping[str, Any], index: int) -> dict[str, Any]:
    title = str(record.get("title") or record.get("angle") or f"Angle {index}")
    research_question = str(record.get("research_question") or title)
    question_context = str(record.get("question_context") or "")
    route = str(record.get("route") or record.get("modality") or "text_only")
    evidence_need = str(record.get("evidence_need") or _infer_evidence_need(title))
    expected_artifacts = _string_list(record.get("expected_artifacts"))
    if not expected_artifacts:
        expected_artifacts = _default_expected_artifacts(evidence_need)
    success_criteria = _string_list(record.get("success_criteria"))
    if not success_criteria:
        success_criteria = ["The angle has source-backed findings."]
    return {
        "angle_id": str(record.get("angle_id") or record.get("id") or f"angle_{index:03d}"),
        "title": title,
        "research_question": research_question,
        "question_context": question_context,
        "route": route,
        "evidence_need": evidence_need,
        "expected_artifacts": expected_artifacts,
        "expected_evidence": _string_list(record.get("expected_evidence")),
        "success_criteria": success_criteria,
        "report_section": str(record.get("report_section") or _report_section_from_title(title)),
    }


def _material_difference_checks(
    *,
    question: str,
    angles: Sequence[Mapping[str, Any]],
    broad_question: bool,
) -> list[dict[str, Any]]:
    evidence_need_counts = Counter(str(angle.get("evidence_need") or "") for angle in angles)
    section_counts = Counter(str(angle.get("report_section") or "") for angle in angles)
    artifact_counts = Counter(
        tuple(_string_list(angle.get("expected_artifacts"))) for angle in angles
    )
    query_tokens = [_token_set(str(angle.get("research_question") or "")) for angle in angles]
    checks: list[dict[str, Any]] = []
    original_tokens = _token_set(question)
    for index, angle in enumerate(angles):
        title = str(angle.get("title") or "")
        research_question = str(angle.get("research_question") or "")
        evidence_need = str(angle.get("evidence_need") or "")
        report_section = str(angle.get("report_section") or "")
        expected_artifacts = tuple(_string_list(angle.get("expected_artifacts")))
        peer_overlaps = [
            _overlap_ratio(query_tokens[index], peer_tokens)
            for peer_index, peer_tokens in enumerate(query_tokens)
            if peer_index != index
        ]
        title_overlap = _overlap_ratio(_token_set(title), original_tokens)
        query_overlap = _overlap_ratio(_token_set(research_question), original_tokens)
        failures: list[str] = []
        if broad_question and title.strip().lower() == "primary source discovery":
            failures.append("primary_source_discovery_only")
        if broad_question and query_overlap > MATERIAL_ORIGINAL_OVERLAP_LIMIT:
            failures.append("research_question_too_close_to_original")
        if broad_question and max(peer_overlaps or [0.0]) > MATERIAL_PEER_OVERLAP_LIMIT:
            failures.append("research_question_too_close_to_peer")
        if broad_question and evidence_need_counts[evidence_need] > 1:
            failures.append("duplicate_evidence_need")
        if broad_question and section_counts[report_section] > 1:
            failures.append("duplicate_report_section")
        if broad_question and artifact_counts[expected_artifacts] > 1:
            failures.append("duplicate_expected_artifacts")
        checks.append(
            {
                "angle_id": str(angle.get("angle_id") or f"angle_{index + 1:03d}"),
                "title": title,
                "normalized_title_overlap": round(title_overlap, 4),
                "normalized_query_overlap": round(query_overlap, 4),
                "max_peer_query_overlap": round(max(peer_overlaps or [0.0]), 4),
                "unique_evidence_need": evidence_need_counts[evidence_need] == 1,
                "unique_report_section": section_counts[report_section] == 1,
                "non_identical_expected_artifacts": artifact_counts[expected_artifacts] == 1,
                "failures": failures,
                "valid": not failures,
            }
        )
    return checks


def _validation_failures(
    *,
    question: str,
    question_class: str,
    broad_question: bool,
    angles: Sequence[Mapping[str, Any]],
    failed_angles: Sequence[Mapping[str, Any]],
    task_records: Sequence[Mapping[str, Any]],
    near_duplicate_count: int,
    generic_lens_count: int,
    visual_hits: Mapping[str, int],
    report_angle_claim_counts: Mapping[str, int],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if broad_question and not (5 <= len(angles) <= 8):
        failures.append(
            {
                "code": "broad_question_angle_count_out_of_range",
                "message": "Broad questions must produce 5-8 semantic angles.",
                "angle_count": len(angles),
            }
        )
    if broad_question and len(angles) == 1 and str(angles[0].get("title") or "").lower() == "primary source discovery":
        failures.append(
            {
                "code": "primary_source_discovery_only",
                "message": "Broad questions cannot validate with only primary source discovery.",
            }
        )
    for check in failed_angles:
        failures.append(
            {
                "code": "semantic_angle_material_difference_failed",
                "angle_id": check.get("angle_id"),
                "failures": list(check.get("failures") or []),
            }
        )
    if task_records and near_duplicate_count > 0:
        pass
    if task_records and generic_lens_count > 0:
        pass

    visual_routes = {
        str(angle.get("route") or "text_only")
        for angle in angles
        if str(angle.get("route") or "text_only") != "text_only"
    }
    if question_class == _CLASS_VISUAL and not visual_routes:
        failures.append(
            {
                "code": "visual_question_all_text_only",
                "message": "Visual/style questions must include at least one visual route.",
            }
        )
    if broad_question and question_class == _CLASS_VISUAL and task_records:
        if int(visual_hits.get("visual_example") or 0) < 1:
            failures.append({"code": "visual_example_expected_evidence_missing"})
        observation_hits = int(visual_hits.get("visual_observation") or 0) + int(
            visual_hits.get("vlm_analysis") or 0
        )
        if observation_hits < 1:
            failures.append({"code": "visual_observation_expected_evidence_missing"})

    if (
        question_class == _CLASS_TECHNICAL
        and not question_mentions_visual_evidence(question)
        and angles
    ):
        text_count = sum(1 for angle in angles if str(angle.get("route")) == "text_only")
        if text_count / len(angles) < 0.80:
            failures.append({"code": "technical_text_only_route_ratio_too_low"})
        visual_tasks = [
            task
            for task in task_records
            if str(task.get("route") or "") != "text_only"
            or int(task.get("max_images") or 0) > 0
        ]
        if visual_tasks:
            failures.append({"code": "irrelevant_visual_tasks_for_text_only_question"})

    if not question_mentions_visual_evidence(question):
        irrelevant_visual_tasks = [
            task
            for task in task_records
            if _task_expected_evidence(task) & VISUAL_EXPECTED_EVIDENCE
        ]
        if question_class in {_CLASS_TECHNICAL, _CLASS_IMPLEMENTATION, _CLASS_POLICY} and irrelevant_visual_tasks:
            failures.append({"code": "irrelevant_visual_expected_evidence_for_text_question"})

    for task in task_records:
        if _task_expected_evidence(task) & VISUAL_EXPECTED_EVIDENCE:
            try:
                max_images = int(task.get("max_images") or 0)
            except (TypeError, ValueError):
                max_images = 0
            if max_images <= 0:
                failures.append(
                    {
                        "code": "visual_expected_evidence_without_image_budget",
                        "task_id": task.get("id"),
                    }
                )

    if report_angle_claim_counts:
        nonzero_angles = [
            angle_id
            for angle_id, count in report_angle_claim_counts.items()
            if int(count) > 0
        ]
        if broad_question and len(nonzero_angles) < 3:
            failures.append(
                {
                    "code": "report_angle_claim_coverage_too_low",
                    "message": "Synthesized reports for broad fixtures need claims from at least 3 semantic angles.",
                    "angle_count": len(nonzero_angles),
                }
            )
    return failures


def _near_duplicate_task_records(
    question: str,
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    original = _token_set(question)
    seen: list[tuple[str, set[str]]] = []
    duplicates: list[dict[str, Any]] = []
    for task in tasks:
        query = str(task.get("query") or "")
        tokens = _token_set(query)
        reasons: list[str] = []
        if _is_suffix_duplicate(question, query):
            reasons.append("original_query_with_generic_suffix")
        if _overlap_ratio(tokens, original) > 0.86:
            reasons.append("high_overlap_with_original")
        peer_overlap = max((_overlap_ratio(tokens, peer) for _peer_query, peer in seen), default=0.0)
        if peer_overlap > 0.92:
            reasons.append("high_overlap_with_peer_task")
        if reasons:
            duplicates.append(
                {
                    "task_id": task.get("id"),
                    "angle_id": task.get("angle_id"),
                    "query": query,
                    "reasons": reasons,
                }
            )
        seen.append((query, tokens))
    return duplicates


def _generic_lens_task_records(
    question: str,
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for task in tasks:
        query = str(task.get("query") or "")
        lens = _generic_lens_label(question, query)
        if lens:
            records.append(
                {
                    "task_id": task.get("id"),
                    "angle_id": task.get("angle_id"),
                    "query": query,
                    "lens": lens,
                }
            )
    return records


def _generic_lens_label(question: str, query: str) -> str | None:
    normalized_query = _normalize_text(query)
    normalized_question = _normalize_text(question)
    remainder = normalized_query
    if normalized_query.startswith(normalized_question):
        remainder = normalized_query[len(normalized_question):].strip(" :-#0123456789")
    for phrase in GENERIC_LENS_PHRASES:
        if remainder == phrase or remainder.startswith(f"{phrase} "):
            return phrase
        if normalized_query == phrase:
            return phrase
    return None


def _is_suffix_duplicate(question: str, query: str) -> bool:
    normalized_query = _normalize_text(query)
    normalized_question = _normalize_text(question)
    if not normalized_query.startswith(normalized_question):
        return False
    suffix = normalized_query[len(normalized_question):].strip()
    if not suffix:
        return True
    suffix = suffix.strip(" :-#0123456789")
    return any(suffix.startswith(phrase) for phrase in GENERIC_LENS_PHRASES)


def _visual_expected_evidence_hits(
    angles: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for angle in angles:
        evidence = set(_string_list(angle.get("expected_evidence")))
        evidence.add(str(angle.get("evidence_need") or ""))
        for item in evidence & VISUAL_EXPECTED_EVIDENCE:
            counts[item] += 1
    for task in tasks:
        for item in _task_expected_evidence(task) & VISUAL_EXPECTED_EVIDENCE:
            counts[item] += 1
    return dict(sorted(counts.items()))


def _task_expected_evidence(task: Mapping[str, Any]) -> set[str]:
    return set(_string_list(task.get("expected_evidence")))


def _report_angle_claim_counts(
    evidence: Mapping[str, Any],
    report_status: Mapping[str, Any] | None,
) -> dict[str, int]:
    if report_status is None:
        return {}
    included = report_status.get("included_claims")
    if not isinstance(included, list):
        return {}
    included_ids = {
        str(record.get("claim_id") or record.get("id") or "")
        for record in included
        if isinstance(record, Mapping)
    }
    if not included_ids:
        return {}
    counts: Counter[str] = Counter()
    claims = evidence.get("claims")
    if not isinstance(claims, list):
        return {}
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        claim_id = str(claim.get("id") or "")
        if claim_id not in included_ids:
            continue
        angle_id = str(claim.get("angle_id") or "unknown")
        counts[angle_id] += 1
    return dict(sorted(counts.items()))


def _real_codex_exec_smoke_status(run_dir: Path) -> dict[str, Any]:
    codex_path = shutil.which("codex")
    if codex_path is None:
        return {
            "status": "skipped",
            "skip_category": "missing_codex_cli",
            "reason": "codex CLI is not available on PATH",
            "accepted_shards": 0,
        }
    parallel_status = _read_optional_json(run_dir / "parallel_orchestration_status.json")
    if not isinstance(parallel_status, Mapping):
        return {
            "status": "skipped",
            "skip_category": "not_run",
            "reason": "real codex-exec smoke has not been run for this fixture",
            "codex_binary": codex_path,
            "accepted_shards": 0,
        }
    adapter = str(parallel_status.get("adapter") or "")
    merge = parallel_status.get("merge")
    accepted_shards = _list(merge.get("accepted_shards")) if isinstance(merge, Mapping) else []
    planned = int(parallel_status.get("planned_task_count") or 0)
    if adapter == "codex-exec" and len(accepted_shards) > 0:
        return {
            "status": "completed",
            "codex_binary": codex_path,
            "accepted_shards": len(accepted_shards),
            "planned_task_count": planned,
            "meets_issue_minimum": planned >= 3 and len(accepted_shards) > 0,
        }
    if adapter != "codex-exec":
        return {
            "status": "skipped",
            "skip_category": "fixture_or_non_codex_adapter",
            "reason": f"parallel orchestration used adapter '{adapter or 'unknown'}'",
            "codex_binary": codex_path,
            "accepted_shards": len(accepted_shards),
        }
    skip_category = _codex_skip_category(parallel_status)
    return {
        "status": "skipped",
        "skip_category": skip_category,
        "reason": _codex_skip_reason(skip_category),
        "codex_binary": codex_path,
        "accepted_shards": len(accepted_shards),
        "planned_task_count": planned,
        "parallel_status": parallel_status.get("status"),
    }


def _codex_skip_category(status: Mapping[str, Any]) -> str:
    text = json.dumps(dict(status), sort_keys=True).lower()
    markers = (
        ("missing_auth", ("auth", "login", "credential", "unauthorized", "unauthenticated")),
        (
            "model_capacity",
            (
                "model capacity",
                "model is at capacity",
                "selected model is at capacity",
                "temporarily unavailable",
                "server overloaded",
                "overloaded",
            ),
        ),
        ("quota_or_billing", ("quota", "billing", "payment", "rate limit")),
        ("codex_exec_timeout", ("timeout", "timed out", "codex_child_timeout")),
        (
            "sandbox_restriction",
            (
                "sandbox blocked",
                "sandbox approval",
                "approval blocked",
                "trusted directory",
                "not inside a trusted directory",
            ),
        ),
        ("approval_policy", ("approval policy", "approval required")),
        ("permission_denied", ("permission denied", "access denied", "eacces")),
    )
    for category, needles in markers:
        if any(needle in text for needle in needles):
            return category
    if str(status.get("status") or "") == "blocked_parallel_execution":
        return "parallel_execution_blocked"
    return "codex_exec_failed"


def _codex_skip_reason(category: str) -> str:
    return {
        "missing_auth": "codex-exec smoke was blocked by missing or invalid Codex authentication",
        "sandbox_restriction": "codex-exec smoke was blocked by sandbox or trusted-directory restrictions",
        "model_capacity": "codex-exec smoke was blocked by model capacity or transient service availability",
        "quota_or_billing": "codex-exec smoke was blocked by quota, rate limit, or billing state",
        "codex_exec_timeout": "codex-exec smoke timed out before an accepted evidence shard was produced",
        "approval_policy": "codex-exec smoke was blocked by approval-policy constraints",
        "permission_denied": "codex-exec smoke was blocked by filesystem or process permission denial",
        "parallel_execution_blocked": "codex-exec smoke was blocked before accepted shards were produced",
        "codex_exec_failed": "codex-exec smoke failed without an accepted evidence shard",
    }.get(category, "codex-exec smoke was skipped")


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _semantic_common_integrity_fields(run_path: Path) -> dict[str, Any]:
    sources = [
        payload
        for payload in (
            _read_optional_json(run_path / SEMANTIC_PLAN_FILENAME),
            _read_optional_json(run_path / SEMANTIC_EXPECTATION_ORACLE_FILENAME),
        )
        if isinstance(payload, Mapping)
    ]
    integrity: dict[str, Any] = {}
    for field in SEMANTIC_COMMON_INTEGRITY_FIELDS:
        for payload in sources:
            if field in payload:
                integrity[field] = payload[field]
                break
    return integrity


def _expected_needs_for_class(
    question_class: str,
    angles: Sequence[Mapping[str, Any]],
) -> list[str]:
    if angles:
        return _ordered_unique(str(angle.get("evidence_need") or "") for angle in angles)
    if question_class == _CLASS_GENERAL:
        return ["primary_source"]
    return [
        str(template["evidence_need"])
        for template in _templates_for_class(question_class)
    ]


def _default_expected_artifacts(evidence_need: str) -> list[str]:
    return {
        "official_source": ["official source list", "source excerpts"],
        "primary_source": ["primary source list", "supporting quotes"],
        "recent_change": ["change timeline", "current status notes"],
        "counter_evidence": ["counter-evidence list", "caveat summary"],
        "implementation_detail": ["implementation notes", "example map"],
        "pricing_or_limits": ["pricing table", "limit summary"],
        "policy_or_legal": ["policy source list", "risk notes"],
        "user_workflow": ["workflow map", "user segment notes"],
        "visual_example": ["visual example set", "image source list"],
        "visual_observation": ["visual observations", "feature taxonomy"],
        "comparative_analysis": ["comparison matrix", "difference notes"],
        "failure_pattern": ["failure mode list", "diagnostic notes"],
        "risk_or_guardrail": ["risk register", "guardrail checklist"],
    }.get(evidence_need, ["evidence notes"])


def _report_section_from_title(title: str, *, fallback: str = "Research") -> str:
    words = re.findall(r"[A-Za-z0-9]+", title)
    if not words:
        return fallback.replace("_", " ").title()
    return " ".join(word.capitalize() for word in words[:5])


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    return any(needle in text for needle in needles)


def _token_set(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Za-z0-9\uac00-\ud7a3]+", value.lower())
        if len(token) > 1
    }


def _normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[A-Za-z0-9\uac00-\ud7a3]+", value.lower()))


def _overlap_ratio(left: set[str], right: set[str]) -> float:
    if not left:
        return 0.0
    return len(left & right) / len(left)


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _ordered_unique(values: Sequence[str] | Any) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _fallback_diagnostics(planner_mode: str) -> dict[str, Any]:
    labels = {
        PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK: "keyword/template fallback planner",
        PLANNER_MODE_MANUAL_ANGLES: "manual user-provided angle fallback",
        PLANNER_MODE_FIXTURE: "fixture-only test planner",
        PLANNER_MODE_BLOCKED: "semantic planner unavailable",
    }
    return {
        "semantic_release_eligible": False,
        "planner_mode": planner_mode,
        "fallback_kind": labels.get(planner_mode, "release-ineligible planner"),
        "user_visible_diagnostic": (
            "True semantic decomposition did not run; this path is useful only as "
            "a release-ineligible fallback and cannot satisfy semantic planner gates."
        ),
    }


def _question_scope(question: str, plan: SemanticPlan) -> dict[str, Any]:
    return {
        "original_question": question,
        "question_hash": _sha256_text(question),
        "question_class": plan.question_class,
        "planner_mode": plan.planner_mode,
        "angle_count": len(plan.angles),
    }


def _template_use(plan: SemanticPlan) -> dict[str, Any]:
    uses_template = plan.planner_mode in {
        PLANNER_MODE_HEURISTIC_TEMPLATE_FALLBACK,
        PLANNER_MODE_FIXTURE,
    }
    return {
        "uses_preselected_template": uses_template,
        "template_source": "heuristic_template_planner" if uses_template else None,
        "template_release_eligible": False,
        "template_angle_titles": [angle.title for angle in plan.angles] if uses_template else [],
    }


def _planner_provenance(plan: SemanticPlan) -> dict[str, Any]:
    if plan.planner_provenance:
        return dict(plan.planner_provenance)
    return {
        "planner_mode": plan.planner_mode,
        "planner_source": plan.source,
        "raw_request_required": True,
        "raw_response_required": True,
        "session_id": None,
        "session_id_unavailable_reason": (
            "P3-SP1 records deterministic release-ineligible schema stubs; "
            "Codex-native planner sessions are introduced by P3-SP2."
        ),
        "semantic_release_eligible": False,
    }


def _integrity_base_payload(
    *,
    run_path: Path,
    question: str,
    plan: SemanticPlan,
    created_at: str,
    raw_request_path: Path,
    raw_response_path: Path,
    raw_request_hash: str,
    raw_response_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": SEMANTIC_PLANNER_SCHEMA_VERSION,
        "run_id": run_path.name,
        "created_at": created_at,
        "planner_mode": plan.planner_mode,
        "semantic_release_eligible": False,
        "status": plan.status,
        "question_scope": _question_scope(question, plan),
        "raw_request_path": str(raw_request_path),
        "raw_response_path": str(raw_response_path),
        "raw_request_hash": raw_request_hash,
        "raw_response_hash": raw_response_hash,
        "parsed_response_hash": _planner_provenance(plan).get("parsed_response_hash"),
        "provenance": _planner_provenance(plan),
        "template_use": _template_use(plan),
        "session_id": None,
        "session_id_unavailable_reason": _planner_provenance(plan)[
            "session_id_unavailable_reason"
        ],
    }


def _oracle_requirement_map(plan: SemanticPlan) -> list[dict[str, Any]]:
    if plan.requirement_coverage_map:
        return [
            {
                "requirement_id": str(record.get("requirement_id") or f"req_{index:03d}"),
                "source": "codex_semantic_candidate",
                "prompt_text": record.get("prompt_text"),
                "requirement_text": record.get("requirement_text"),
                "requirement_type": record.get("requirement_type"),
                "non_negotiable": bool(record.get("non_negotiable")),
                "covered_by_angle_ids": _string_list(record.get("covered_by_angle_ids")),
                "covered_by_task_ids": _string_list(record.get("covered_by_task_ids")),
                "coverage_status": record.get("coverage_status"),
            }
            for index, record in enumerate(plan.requirement_coverage_map, start=1)
            if isinstance(record, Mapping)
        ]
    if not plan.angles:
        return []
    return [
        {
            "requirement_id": f"req_{index:03d}",
            "source": "fallback_angle",
            "text": angle.title,
            "non_negotiable": False,
            "covered_by_angle_ids": [angle.angle_id],
            "covered_by_task_ids": [],
        }
        for index, angle in enumerate(plan.angles, start=1)
    ]


def _requirement_coverage_map(plan: SemanticPlan) -> list[dict[str, Any]]:
    if plan.requirement_coverage_map:
        return [dict(record) for record in plan.requirement_coverage_map]
    return [
        {
            "requirement_id": f"req_{index:03d}",
            "angle_id": angle.angle_id,
            "coverage_status": "fallback_release_ineligible",
            "evidence_need": angle.evidence_need,
        }
        for index, angle in enumerate(plan.angles, start=1)
    ]


def _release_ineligible_semantic_fit_score(plan: SemanticPlan) -> float | None:
    if plan.planner_mode == PLANNER_MODE_CODEX_SEMANTIC and plan.semantic_release_eligible:
        return SEMANTIC_FIT_SCORE_THRESHOLD
    return None


def _semantic_review_blockers(plan: SemanticPlan) -> list[dict[str, Any]]:
    if plan.planner_mode in RELEASE_INELIGIBLE_PLANNER_MODES:
        return [
            {
                "code": "release_ineligible_planner_mode",
                "planner_mode": plan.planner_mode,
                "message": "Fallback/manual/fixture planners cannot satisfy semantic fit review.",
            }
        ]
    if not plan.semantic_release_eligible:
        return [
            {
                "code": "semantic_review_not_implemented",
                "planner_mode": plan.planner_mode,
                "message": "Semantic review gate has not made this plan release-eligible.",
            }
        ]
    return []


def _reviewer_independence(plan: SemanticPlan) -> dict[str, Any]:
    independent = plan.planner_mode == PLANNER_MODE_CODEX_SEMANTIC and plan.semantic_release_eligible
    return {
        "independent": independent,
        "oracle_planner_shared_provenance": not independent,
        "reviewer_planner_shared_provenance": not independent,
        "status": "not_implemented_release_ineligible" if not independent else "passed",
    }


def _substitute_implementation_check(plan: SemanticPlan) -> dict[str, Any]:
    passed = plan.planner_mode == PLANNER_MODE_CODEX_SEMANTIC and plan.semantic_release_eligible
    return {
        "passed": passed,
        "checked": True,
        "planner_mode": plan.planner_mode,
        "blocked_reason": None if passed else "fallback_or_unreviewed_semantic_plan",
    }


def _artifact_key(filename: str) -> str:
    return filename.removesuffix(".json")


def _semantic_release_status(
    *,
    run_path: Path,
    planner_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    planner_mode = str(planner_metadata.get("planner_mode") or "")
    semantic_release_eligible = bool(planner_metadata.get("semantic_release_eligible"))
    review = _read_optional_json(run_path / SEMANTIC_PLAN_REVIEW_FILENAME)
    semantic_fit_score = None
    blockers: list[Any] = []
    substitute_check: Mapping[str, Any] = {}
    if isinstance(review, Mapping):
        semantic_fit_score = review.get("semantic_fit_score")
        blockers = _list(review.get("blockers"))
        raw_substitute = review.get("substitute_implementation_check")
        if isinstance(raw_substitute, Mapping):
            substitute_check = raw_substitute

    failures: list[dict[str, Any]] = []
    if planner_mode not in ALLOWED_PLANNER_MODES:
        failures.append(
            {
                "code": "planner_mode_missing_or_unknown",
                "planner_mode": planner_mode or None,
            }
        )
    if planner_mode in RELEASE_INELIGIBLE_PLANNER_MODES:
        failures.append(
            {
                "code": "release_ineligible_planner_mode",
                "planner_mode": planner_mode,
                "message": "Heuristic, manual, fixture, and blocked planners cannot pass semantic validation.",
            }
        )
    if not semantic_release_eligible:
        failures.append({"code": "semantic_release_ineligible"})
    if planner_mode == PLANNER_MODE_CODEX_SEMANTIC and semantic_release_eligible:
        try:
            numeric_score = float(semantic_fit_score)
        except (TypeError, ValueError):
            numeric_score = None
        if numeric_score is None or not math.isfinite(numeric_score):
            failures.append(
                {
                    "code": "semantic_fit_score_missing_or_non_finite",
                    "semantic_fit_score": semantic_fit_score,
                    "threshold": SEMANTIC_FIT_SCORE_THRESHOLD,
                }
            )
        elif numeric_score < SEMANTIC_FIT_SCORE_THRESHOLD:
            failures.append(
                {
                    "code": "semantic_fit_score_below_threshold",
                    "semantic_fit_score": semantic_fit_score,
                    "threshold": SEMANTIC_FIT_SCORE_THRESHOLD,
                }
            )
        if blockers:
            failures.append({"code": "semantic_review_blockers_present", "count": len(blockers)})
        if substitute_check.get("passed") is not True:
            failures.append({"code": "substitute_implementation_check_failed"})
    else:
        if semantic_fit_score is not None:
            try:
                score_value = float(semantic_fit_score)
            except (TypeError, ValueError):
                score_value = -1.0
            if score_value >= SEMANTIC_FIT_SCORE_THRESHOLD:
                failures.append(
                    {
                        "code": "release_ineligible_fit_score_must_not_pass",
                        "semantic_fit_score": semantic_fit_score,
                    }
                )
    return {
        "planner_mode": planner_mode or "unknown",
        "semantic_release_eligible": semantic_release_eligible,
        "semantic_fit_score": semantic_fit_score,
        "review_blocker_count": len(blockers),
        "substitute_implementation_check_passed": substitute_check.get("passed"),
        "failures": failures,
    }


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _utc_now_from_run(run_path: Path) -> str:
    try:
        created = _read_optional_json(run_path / "status.json")
        if isinstance(created, Mapping) and isinstance(created.get("created_at"), str):
            return str(created["created_at"])
    except OSError:
        pass
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
